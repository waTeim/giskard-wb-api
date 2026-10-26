"""Run lifecycle: CRUD, authorization, SkyPilot submission/cancellation, status
reconciliation and MinIO result collection.

Long operations (job submission, cancellation, result listing) run as
background tasks so the HTTP calls return promptly; their outcome is recorded
on the run and visible through `status`. All run state lives in the store, so a
restarted server resumes tracking via `reconcile()`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ..auth import Principal
from ..config import Config, StorageLocation
from ..errors import ApiError, not_found
from ..skypilot.client import SkyJob, SkyPilotClient
from ..skypilot.task import build_task, job_name, run_paths
from ..storage import ObjectStorage
from ..timeutil import from_epoch, iso, now, parse
from .model import ACTIVE_STATES, TERMINAL_STATES, ObjectRef, Run, RunState, is_active
from .store import RunStore

MAX_RESULT_OBJECTS = 10_000
# A submission with no recorded SkyPilot request after this long is considered lost.
LOST_SUBMISSION_SECONDS = 2 * 60
# On-demand status refreshes are skipped if the last check is newer than this.
STATUS_FRESH_SECONDS = 5

log = logging.getLogger(__name__)


def _within(loc: StorageLocation, bucket: str, key_or_prefix: str) -> bool:
    return loc.bucket == bucket and key_or_prefix.startswith(loc.prefix)


def _message(err: BaseException) -> str:
    return str(err) or type(err).__name__


def _age_seconds(timestamp: str) -> float:
    return time.time() - parse(timestamp).timestamp()


def map_sky_status(run: Run, status: str | None) -> RunState | None:
    """Maps a SkyPilot ManagedJobStatus to a run state, taking requested stop/abort into account."""
    ex = run['execution']
    state: RunState
    match status:
        case 'PENDING' | 'DEPRECATED_SUBMITTED' | 'SUBMITTED' | 'STARTING':
            state = 'queued'
        case 'RUNNING' | 'RECOVERING' | 'WINDING_DOWN':
            state = 'running'
        case 'CANCELLING':
            return 'aborting'
        case 'CANCELLED':
            return 'aborted'
        case 'SUCCEEDED':
            return 'stopped' if ex.get('stopRequestedAt') else 'succeeded'
        case _:
            return 'failed' if status and status.startswith('FAILED') else None
    if ex.get('abortRequestedAt'):
        return 'aborting'
    if ex.get('stopRequestedAt'):
        return 'stopping'
    return state


class RunService:
    def __init__(self, config: Config, store: RunStore, storage: ObjectStorage, sky: SkyPilotClient):
        self._config = config
        self._store = store
        self._storage = storage
        self._sky = sky
        # Background work in flight, keyed by `<kind>:<run_id>`, so it is never duplicated.
        self._in_flight: dict[str, asyncio.Task[None]] = {}

    async def idle(self) -> None:
        """Returns when all background work started so far has settled (used by tests and shutdown)."""
        while self._in_flight:
            await asyncio.gather(*self._in_flight.values(), return_exceptions=True)

    # ---------------------------------------------------------------- access

    @staticmethod
    def _can_access(principal: Principal, run: Run) -> bool:
        owner = run['owner']
        return principal.is_admin or (owner['issuer'] == principal.issuer and owner['subject'] == principal.subject)

    def get(self, principal: Principal, id: str) -> Run:
        """Returns the run if the caller may access it; otherwise 404 (existence is not disclosed)."""
        run = self._store.get(id)
        if not run or not self._can_access(principal, run):
            raise not_found()
        return run

    def list(self, principal: Principal, limit: int, offset: int, mine: bool = False) -> tuple[list[Run], int]:
        owner = (principal.issuer, principal.subject) if not principal.is_admin or mine else None
        return self._store.list(owner, limit, offset)

    # ------------------------------------------------------------ definitions

    async def _validate_inputs(self, inputs: dict[str, Any]) -> None:
        problems: list[str] = []
        trainer = inputs.get('trainer')
        dataset = inputs.get('dataset')
        config = inputs.get('config')
        compute = inputs.get('compute')
        cfg = self._config

        if compute:
            gpu = next((g for g in cfg.gpus if g.type == compute['gpuType']), None)
            if not gpu:
                problems.append(f'compute.gpuType must be one of: {", ".join(g.type for g in cfg.gpus)}')
            elif compute['gpuCount'] not in gpu.counts:
                problems.append(f'compute.gpuCount for {gpu.type} must be one of: {", ".join(map(str, gpu.counts))}')
            if compute.get('maxRuntimeMinutes', 0) > cfg.max_runtime_minutes:
                problems.append(f'compute.maxRuntimeMinutes must be at most {cfg.max_runtime_minutes}')
        if trainer and not any(_within(loc, trainer['bucket'], trainer['key']) for loc in cfg.allowed_trainer_locations):
            problems.append('trainer is not in an approved MinIO location')
        dataset_path = (dataset.get('key') if 'key' in dataset else dataset['prefix']) if dataset else ''
        if dataset and not any(_within(loc, dataset['bucket'], dataset_path) for loc in cfg.allowed_dataset_locations):
            problems.append('dataset is not in an approved MinIO location')
        config_object: ObjectRef | None = config.get('object') if config else None
        if config_object and not any(
            _within(loc, config_object['bucket'], config_object['key']) for loc in cfg.allowed_config_locations
        ):
            problems.append('config.object is not in an approved MinIO location')
        if problems:
            raise ApiError(422, 'invalid_run', 'Run definition rejected', problems)

        # Existence checks only after the locations are known to be approved.
        try:
            if trainer and not await self._storage.object_exists(trainer['bucket'], trainer['key']):
                problems.append(f'trainer object {trainer["bucket"]}/{trainer["key"]} does not exist')
            if dataset:
                exists = (
                    await self._storage.object_exists(dataset['bucket'], dataset['key'])
                    if 'key' in dataset
                    else await self._storage.prefix_has_objects(dataset['bucket'], dataset['prefix'])
                )
                if not exists:
                    problems.append(f'dataset {dataset["bucket"]}/{dataset_path} has no objects')
            if config_object and not await self._storage.object_exists(config_object['bucket'], config_object['key']):
                problems.append(f'config object {config_object["bucket"]}/{config_object["key"]} does not exist')
        except Exception:
            log.exception('MinIO check failed')
            raise ApiError(502, 'storage_unavailable', 'Could not verify inputs in MinIO') from None
        if problems:
            raise ApiError(422, 'invalid_run', 'Run definition rejected', problems)

    async def create(self, principal: Principal, inputs: dict[str, Any]) -> Run:
        await self._validate_inputs(inputs)
        id = str(uuid.uuid4())
        ts = now()
        owner: dict[str, str] = {'issuer': principal.issuer, 'subject': principal.subject}
        if principal.name:
            owner['name'] = principal.name
        run: Run = {
            'id': id,
            'name': inputs['name'],
            'owner': owner,  # type: ignore[typeddict-item]
            'createdAt': ts,
            'updatedAt': ts,
            'trainer': inputs['trainer'],
            'dataset': inputs['dataset'],
            'config': inputs['config'],
            'compute': inputs['compute'],
            'results': {'bucket': self._config.results.bucket, 'prefix': run_paths(self._config, id).outputs_prefix},
            'execution': {'state': 'draft'},
            'resultsListing': {'collecting': False, 'objects': [], 'truncated': False},
        }
        self._store.insert(run)
        return run

    async def update(self, principal: Principal, id: str, patch: dict[str, Any]) -> Run:
        before = self.get(principal, id)
        if before['execution']['state'] != 'draft':
            raise ApiError(409, 'run_not_editable', f'Run inputs cannot be changed in state {before["execution"]["state"]}')
        await self._validate_inputs(patch)
        conflict: list[str] = []

        def apply(run: Run) -> Run | None:
            if run['execution']['state'] != 'draft':
                conflict.append(run['execution']['state'])
                return None
            run.update(patch)  # type: ignore[typeddict-item]
            return run

        updated = self._store.update(id, apply)
        if not updated:
            raise not_found()
        if conflict:
            raise ApiError(409, 'run_not_editable', f'Run inputs cannot be changed in state {conflict[0]}')
        return updated

    def delete(self, principal: Principal, id: str) -> None:
        self.get(principal, id)
        outcome = self._store.delete_if(
            id, lambda run: run['execution']['state'] == 'draft' or run['execution']['state'] in TERMINAL_STATES
        )
        if outcome == 'not_found':
            raise not_found()
        if outcome == 'rejected':
            raise ApiError(409, 'run_active', 'Active runs cannot be deleted; abort the run first')

    # -------------------------------------------------------------- execution

    async def start(self, principal: Principal, id: str) -> Run:
        """Moves a draft run to `submitting` and submits it in the background.

        Retried calls are idempotent: a run that has already left `draft` is
        returned unchanged and never resubmitted.
        """
        run = self.get(principal, id)
        if run['execution']['state'] != 'draft':
            return run

        # Inputs may have been removed from MinIO since the run was defined.
        await self._validate_inputs(run)  # type: ignore[arg-type]

        transitioned = False

        def submit(r: Run) -> Run | None:
            nonlocal transitioned
            if r['execution']['state'] != 'draft':
                return None
            transitioned = True
            r['execution'] = {
                'state': 'submitting',
                'skyJobName': job_name(self._config, r['id']),
                'submittedAt': now(),
                'message': 'Submitting job to SkyPilot',
            }
            return r

        updated = self._store.update(id, submit)
        if transitioned:
            self._background(f'launch:{id}', lambda: self._submit(id))
        assert updated is not None
        return updated

    async def _submit(self, id: str) -> None:
        run = self._store.get(id)
        if not run or run['execution']['state'] != 'submitting':
            return
        paths = run_paths(self._config, id)

        config = run['config']
        try:
            if 'object' in config:
                config_object: ObjectRef = config['object']
            else:
                is_json = config['format'] == 'json'
                body = json.dumps(config['value'], indent=2, ensure_ascii=False) if is_json else config['value']
                config_object = {
                    'bucket': self._config.results.bucket,
                    'key': f'{paths.inputs_prefix}config.{"json" if is_json else "yaml"}',
                }
                await self._storage.put_object(
                    config_object['bucket'],
                    config_object['key'],
                    body,
                    'application/json' if is_json else 'application/yaml',
                )
        except Exception as err:
            self._fail(id, f'Could not stage training config in MinIO: {_message(err)}')
            return

        name, task = build_task(self._config, run, config_object)
        try:
            request_id = await self._sky.launch(name, task)
        except Exception as err:
            self._fail(id, f'SkyPilot launch failed: {_message(err)}')
            return

        def record(r: Run) -> Run:
            r['execution']['skyRequestId'] = request_id
            return r

        self._store.update(id, record)
        await self._await_launch(id, request_id)

    async def _await_launch(self, id: str, request_id: str) -> None:
        launch_error: str | None = None
        try:
            job_id: int | None = await self._sky.wait_for_launch(request_id)
        except Exception as err:
            launch_error = _message(err)
            # The request may have succeeded even though waiting on it failed.
            job_id = await self._find_job_by_name(id)
        if job_id is None:
            self._fail(id, f'SkyPilot launch failed: {launch_error or "no job ID returned"}')
            return
        self._record_job_id(id, job_id)

    def _record_job_id(self, id: str, job_id: int) -> None:
        def record(r: Run) -> Run | None:
            ex = r['execution']
            if 'skyJobId' in ex:
                return None
            ex['skyJobId'] = job_id
            if ex['state'] == 'submitting':
                ex['state'] = (
                    'aborting' if ex.get('abortRequestedAt') else 'stopping' if ex.get('stopRequestedAt') else 'queued'
                )
            ex['message'] = f'Submitted as SkyPilot managed job {job_id}'
            return r

        run = self._store.update(id, record)
        # An abort that arrived while submitting is carried out now.
        if run and run['execution'].get('abortRequestedAt') and 'cancellation' not in run['execution']:
            self._background(f'cancel:{id}', lambda: self._cancel(id))

    async def _find_job_by_name(self, id: str) -> int | None:
        try:
            jobs = await self._sky.find_jobs_by_name(job_name(self._config, id))
        except Exception:
            log.warning('SkyPilot job lookup failed for run %s', id, exc_info=True)
            return None
        return min((j.job_id for j in jobs), default=None)

    def _fail(self, id: str, message: str) -> None:
        log.warning('run %s failed: %s', id, message)

        def fail(r: Run) -> Run:
            ex = r['execution']
            ex['state'] = 'failed'
            ex['lastError'] = message
            ex['message'] = message
            ex['finishedAt'] = now()
            return r

        self._store.update(id, fail)

    async def stop(self, principal: Principal, id: str) -> Run:
        """Requests a graceful stop by writing the stop marker the job polls for."""
        run = self.get(principal, id)
        state = run['execution']['state']
        if state == 'stopping':
            return run
        if state not in ('submitting', 'queued', 'running'):
            raise ApiError(409, 'invalid_state', f'Cannot stop a run in state {state}')
        try:
            await self._storage.put_object(
                self._config.results.bucket, run_paths(self._config, id).stop_marker_key, now(), 'text/plain'
            )
        except Exception:
            log.exception('writing stop marker failed for run %s', id)
            raise ApiError(502, 'stop_failed', 'Could not deliver the stop request; the run is unchanged') from None

        def request_stop(r: Run) -> Run | None:
            ex = r['execution']
            if ex['state'] not in ('submitting', 'queued', 'running'):
                return None
            ex['stopRequestedAt'] = now()
            if ex['state'] != 'submitting':
                ex['state'] = 'stopping'
            ex['message'] = (
                'Stop requested; the trainer receives SIGTERM within ~15 s of starting or polling '
                'and outputs are uploaded before exit'
            )
            return r

        updated = self._store.update(id, request_stop)
        assert updated is not None
        return updated

    def abort(self, principal: Principal, id: str) -> Run:
        """Requests prompt cancellation of the SkyPilot job."""
        run = self.get(principal, id)
        state = run['execution']['state']
        cancellation = run['execution'].get('cancellation')
        if state not in ACTIVE_STATES:
            raise ApiError(409, 'invalid_state', f'Cannot abort a run in state {state}')
        # Repeat calls only re-issue the cancel if the previous attempt failed.
        if state == 'aborting' and (not cancellation or cancellation['accepted']):
            return run

        def request_abort(r: Run) -> Run:
            ex = r['execution']
            ex.setdefault('abortRequestedAt', now())
            if ex['state'] != 'submitting':
                ex['state'] = 'aborting'
            ex['message'] = (
                'Abort requested; the job will be cancelled as soon as SkyPilot assigns it an ID'
                if 'skyJobId' not in ex
                else 'Abort requested; cancelling SkyPilot job'
            )
            return r

        updated = self._store.update(id, request_abort)
        assert updated is not None
        if 'skyJobId' in updated['execution']:
            self._background(f'cancel:{id}', lambda: self._cancel(id))
        return updated

    async def _cancel(self, id: str) -> None:
        run = self._store.get(id)
        job_id = run['execution'].get('skyJobId') if run else None
        if job_id is None:
            return
        requested_at = now()
        try:
            await self._sky.cancel(job_id)
        except Exception as err:
            message = f'Cancellation of SkyPilot job {job_id} failed: {_message(err)}. The job may still be running; retry abort.'

            def rejected(r: Run) -> Run:
                r['execution']['cancellation'] = {'requestedAt': requested_at, 'accepted': False, 'error': message}
                r['execution']['lastError'] = message
                r['execution']['message'] = message
                return r

            self._store.update(id, rejected)
        else:

            def accepted(r: Run) -> Run:
                r['execution']['cancellation'] = {'requestedAt': requested_at, 'accepted': True}
                r['execution']['message'] = f'SkyPilot accepted cancellation of job {job_id}; waiting for confirmation'
                return r

            self._store.update(id, accepted)
        await self.refresh_runs([id])

    # ------------------------------------------------------------ status/logs

    async def status(self, principal: Principal, id: str) -> Run:
        run = self.get(principal, id)
        ex = run['execution']
        last_checked = ex.get('lastCheckedAt')
        stale = last_checked is None or _age_seconds(last_checked) > STATUS_FRESH_SECONDS
        if is_active(run) and 'skyJobId' in ex and stale:
            await self.refresh_runs([id])
            return self._store.get(id) or run
        return run

    async def logs(self, principal: Principal, id: str, tail: int) -> tuple[int, str]:
        run = self.get(principal, id)
        job_id = run['execution'].get('skyJobId')
        if job_id is None:
            raise ApiError(409, 'no_job', f'Run has no SkyPilot job yet (state {run["execution"]["state"]})')
        try:
            return job_id, await self._sky.logs(job_id, tail)
        except Exception as err:
            log.warning('fetching logs failed for run %s', id, exc_info=True)
            raise ApiError(502, 'logs_unavailable', f'Could not retrieve logs from SkyPilot: {_message(err)}') from None

    async def refresh_runs(self, ids: list[str]) -> None:
        """Pulls SkyPilot status for the given runs (those with job IDs) and applies it."""
        runs = [r for r in map(self._store.get, ids) if r and 'skyJobId' in r['execution']]
        if not runs:
            return
        try:
            jobs = await self._sky.get_jobs([r['execution']['skyJobId'] for r in runs])
        except Exception as err:
            message = f'Status check failed: {_message(err)}'

            def uncertain(run: Run) -> Run | None:
                if not is_active(run):
                    return None
                run['execution']['statusUncertain'] = True
                run['execution']['lastError'] = message
                return run

            for r in runs:
                self._store.update(r['id'], uncertain)
            return
        by_id = {j.job_id: j for j in jobs}
        for r in runs:
            self._apply_job(r['id'], by_id.get(r['execution']['skyJobId']))

    def _apply_job(self, id: str, job: SkyJob | None) -> None:
        def apply(run: Run) -> Run | None:
            ex = run['execution']
            if not is_active(run) and not ex.get('statusUncertain'):
                return None
            ex['lastCheckedAt'] = now()
            if not job:
                ex['statusUncertain'] = True
                ex['message'] = f'SkyPilot does not report job {ex.get("skyJobId")}; state is unconfirmed'
                return run
            ex['statusUncertain'] = False
            if job.status is not None:
                ex['skyStatus'] = job.status
            if job.start_at and 'startedAt' not in ex:
                ex['startedAt'] = from_epoch(job.start_at)
            next_state = map_sky_status(run, job.status)
            if not next_state:
                ex['statusUncertain'] = True
                ex['message'] = f'Unrecognised SkyPilot status {job.status}'
                return run
            if next_state == ex['state'] and next_state not in TERMINAL_STATES:
                return run
            ex['state'] = next_state
            if next_state in TERMINAL_STATES:
                ex['finishedAt'] = from_epoch(job.end_at) if job.end_at else now()
            match next_state:
                case 'running':
                    ex['message'] = 'Training job is running'
                case 'queued':
                    ex['message'] = f'SkyPilot job {ex.get("skyJobId")} is {(job.status or "").lower()}'
                case 'succeeded':
                    ex['message'] = 'Training job finished successfully'
                case 'stopped':
                    ex['message'] = 'Training job stopped on request and exited cleanly'
                case 'failed':
                    reason = f': {job.failure_reason}' if job.failure_reason else ''
                    ex['message'] = f'Training job failed ({job.status}){reason}'
                    ex['lastError'] = ex['message']
                case 'aborted':
                    ex['message'] = (
                        'SkyPilot confirmed the job was cancelled'
                        if ex.get('abortRequestedAt')
                        else 'Job was cancelled outside this API'
                    )
                case 'aborting':
                    ex['message'] = 'SkyPilot is cancelling the job'
            return run

        self._store.update(id, apply)

    async def reconcile(self) -> None:
        """Resumes tracking of every active run: refreshes job status and recovers
        submissions interrupted by a restart. Called periodically by the poller.
        """
        with_job: list[str] = []
        for run in self._store.list_active():
            id = run['id']
            ex = run['execution']
            if 'skyJobId' in ex:
                with_job.append(id)
                if ex.get('abortRequestedAt') and 'cancellation' not in ex:
                    self._background(f'cancel:{id}', lambda id=id: self._cancel(id))
                continue
            if f'launch:{id}' in self._in_flight:
                continue
            # Submission interrupted (e.g. server restart): never resubmit, only recover.
            if request_id := ex.get('skyRequestId'):
                self._background(f'launch:{id}', lambda id=id, rid=request_id: self._await_launch(id, rid))
            else:
                self._background(
                    f'launch:{id}', lambda id=id, since=ex.get('submittedAt', run['updatedAt']): self._recover(id, since)
                )
        await self.refresh_runs(with_job)

    async def _recover(self, id: str, submitted_at: str) -> None:
        job_id = await self._find_job_by_name(id)
        if job_id is not None:
            self._record_job_id(id, job_id)
        elif _age_seconds(submitted_at) > LOST_SUBMISSION_SECONDS:
            self._fail(
                id,
                'Submission was interrupted before SkyPilot acknowledged it and no matching job exists; '
                'create a new run to retry',
            )

    # ---------------------------------------------------------------- results

    def collect(self, principal: Principal, id: str) -> Run:
        self.get(principal, id)

        def mark(r: Run) -> Run | None:
            if r['resultsListing']['collecting']:
                return None
            r['resultsListing']['collecting'] = True
            return r

        run = self._store.update(id, mark)
        assert run is not None
        self._background(f'collect:{id}', lambda: self._collect_results(id))
        return run

    async def _collect_results(self, id: str) -> None:
        run = self._store.get(id)
        if not run:
            return
        bucket, prefix = run['results']['bucket'], run['results']['prefix']
        try:
            listing = await self._storage.list(bucket, prefix, MAX_RESULT_OBJECTS)
        except Exception as err:
            log.exception('collecting results failed for run %s', id)

            def failed(r: Run) -> Run:
                r['resultsListing']['collecting'] = False
                r['resultsListing']['error'] = f'Listing results failed: {_message(err)}'
                return r

            self._store.update(id, failed)
            return

        objects = []
        for o in listing.objects:
            if not o.key.startswith(prefix) or len(o.key) <= len(prefix):
                continue
            obj: dict[str, Any] = {'key': o.key, 'path': o.key[len(prefix) :], 'size': o.size}
            if o.last_modified:
                obj['lastModified'] = iso(o.last_modified)
            if o.etag:
                obj['etag'] = o.etag
            objects.append(obj)

        def done(r: Run) -> Run:
            r['resultsListing'] = {
                'collecting': False,
                'collectedAt': now(),
                'truncated': listing.truncated,
                'objects': objects,  # type: ignore[typeddict-item]
            }
            return r

        self._store.update(id, done)

    def results(self, principal: Principal, id: str) -> dict[str, Any]:
        run = self.get(principal, id)
        bucket, prefix = run['results']['bucket'], run['results']['prefix']
        ttl = self._config.presign_ttl_seconds
        expires_at = from_epoch(time.time() + ttl)
        listing = run['resultsListing']
        return {
            'runId': id,
            'bucket': bucket,
            'prefix': prefix,
            'collecting': listing['collecting'],
            'collectedAt': listing.get('collectedAt'),
            'truncated': listing['truncated'],
            'error': listing.get('error'),
            'objects': [
                {**o, 'url': self._storage.presign_get(bucket, o['key'], ttl), 'expiresAt': expires_at}
                for o in listing['objects']
                if o['key'].startswith(prefix)
            ],
        }

    def reset_interrupted_work(self) -> None:
        """Clears flags for background work that was lost in a restart."""

        def clear(r: Run) -> Run:
            r['resultsListing']['collecting'] = False
            return r

        for id in self._store.list_collecting():
            self._store.update(id, clear)

    def _background(self, key: str, fn: Callable[[], Awaitable[None]]) -> None:
        if key in self._in_flight:
            return

        async def run() -> None:
            try:
                await fn()
            except Exception:
                log.exception('background task %s failed', key)
            finally:
                self._in_flight.pop(key, None)

        self._in_flight[key] = asyncio.create_task(run())
