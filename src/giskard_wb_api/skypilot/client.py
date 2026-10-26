"""SkyPilot access through its Python SDK.

The SDK is synchronous: each call makes blocking HTTP requests to the SkyPilot
API server. Every call therefore runs on a dedicated thread pool and is awaited
with a timeout, so the event loop keeps serving requests. A call that times out
cannot be interrupted; its thread finishes in the background, which is why the
pool is sized with headroom (skypilot.threads).

Launch requests can take as long as provisioning a GPU node. Rather than
blocking a thread for that long, `wait_for_launch` polls the request's status
and only fetches its result once it has finished.
"""

from __future__ import annotations

import asyncio
import functools
import io
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from ..config import SkyConfig

T = TypeVar('T')

# How often a pending launch request is checked.
LAUNCH_POLL_SECONDS = 5
_FINISHED_REQUEST_STATES = {'SUCCEEDED', 'FAILED', 'CANCELLED'}
# queue_v2 returns only the fields asked for; its defaults omit failure_reason and start_at.
_QUEUE_FIELDS = ('job_id', 'job_name', 'status', 'failure_reason', 'submitted_at', 'start_at', 'end_at')


@dataclass(frozen=True)
class SkyJob:
    job_id: int
    job_name: str | None = None
    # SkyPilot ManagedJobStatus value, e.g. RUNNING, FAILED_SETUP.
    status: str | None = None
    failure_reason: str | None = None
    # Epoch seconds.
    start_at: float | None = None
    end_at: float | None = None


class SkyPilotClient(Protocol):
    async def launch(self, job_name: str, task: dict[str, Any]) -> str:
        """Submits a managed-job launch request; returns the SkyPilot API request ID."""
        ...

    async def wait_for_launch(self, request_id: str) -> int:
        """Waits for a launch request to finish and returns the managed job ID."""
        ...

    async def get_jobs(self, job_ids: list[int]) -> list[SkyJob]: ...

    async def find_jobs_by_name(self, job_name: str) -> list[SkyJob]: ...

    async def cancel(self, job_id: int) -> None: ...

    async def logs(self, job_id: int, tail_lines: int) -> str: ...


class SkyPilotError(Exception):
    pass


def _status(value: Any) -> str | None:
    if value is None:
        return None
    return getattr(value, 'value', None) or str(value)


def _job(record: Any) -> SkyJob:
    rec = record.model_dump() if hasattr(record, 'model_dump') else dict(record)
    return SkyJob(
        job_id=int(rec['job_id']),
        job_name=rec.get('job_name'),
        status=_status(rec.get('status')),
        failure_reason=rec.get('failure_reason'),
        start_at=rec.get('start_at'),
        end_at=rec.get('end_at'),
    )


class SdkSkyPilotClient:
    def __init__(self, config: SkyConfig, secrets: Iterable[str]):
        # Imported here: the SDK is large, and nothing else needs it. The API
        # server endpoint and token are handed to the SDK by server.py.
        import sky
        from sky import jobs

        self._sky = sky
        self._jobs = jobs
        self._config = config
        # Values that must never appear in error messages we persist or return.
        self._secrets = [s for s in secrets if len(s) >= 4]
        self._executor = ThreadPoolExecutor(max_workers=config.threads, thread_name_prefix='skypilot')

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, '[REDACTED]')
        return text

    async def _call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Runs a blocking SDK function on the pool, with the per-call timeout."""
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, functools.partial(fn, *args, **kwargs))
        timeout = self._config.request_timeout_seconds
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise SkyPilotError(f'SkyPilot call timed out after {timeout} s') from None
        except Exception as exc:
            raise SkyPilotError(self._redact(f'{type(exc).__name__}: {exc}')) from exc

    # Blocking helpers; each runs on the pool.

    def _launch(self, job_name: str, task: dict[str, Any]) -> str:
        return str(self._jobs.launch(self._sky.Task.from_yaml_config(task), name=job_name))

    def _request_status(self, request_id: str) -> str | None:
        for payload in self._sky.api_status([request_id]):
            if payload.request_id == request_id:
                return str(payload.status).upper()
        return None

    def _launch_result(self, request_id: str) -> int:
        job_ids, _handle = self._sky.get(request_id)
        if isinstance(job_ids, (list, tuple)):
            job_ids = job_ids[0] if job_ids else None
        if job_ids is None:
            raise RuntimeError('SkyPilot returned no managed job ID')
        return int(job_ids)

    def _queue(self, job_ids: list[int] | None = None, name: str | None = None) -> list[SkyJob]:
        request_id = self._jobs.queue_v2(
            refresh=False, skip_finished=False, job_ids=job_ids, name_match=name, fields=_QUEUE_FIELDS
        )
        records, *_counts = self._sky.get(request_id)
        return [_job(r) for r in records]

    def _cancel(self, job_id: int) -> None:
        self._sky.get(self._jobs.cancel(job_ids=[job_id]))

    def _logs(self, job_id: int, tail_lines: int) -> str:
        buf = io.StringIO()
        self._jobs.tail_logs(job_id=job_id, follow=False, tail=tail_lines or None, output_stream=buf)
        return buf.getvalue()

    # SkyPilotClient

    async def launch(self, job_name: str, task: dict[str, Any]) -> str:
        return await self._call(self._launch, job_name, task)

    async def wait_for_launch(self, request_id: str) -> int:
        deadline = time.monotonic() + self._config.launch_timeout_seconds
        while True:
            status = await self._call(self._request_status, request_id)
            if status is None:
                raise SkyPilotError(f'SkyPilot has no record of request {request_id}')
            if status in _FINISHED_REQUEST_STATES:
                # Returns the job ID, or raises the request's error.
                return await self._call(self._launch_result, request_id)
            if time.monotonic() > deadline:
                raise SkyPilotError(
                    f'Launch request {request_id} still {status.lower()} after '
                    f'{self._config.launch_timeout_seconds} s'
                )
            await asyncio.sleep(LAUNCH_POLL_SECONDS)

    async def get_jobs(self, job_ids: list[int]) -> list[SkyJob]:
        if not job_ids:
            return []
        return await self._call(self._queue, job_ids=job_ids)

    async def find_jobs_by_name(self, job_name: str) -> list[SkyJob]:
        # name_match is a pattern match on the server; keep only exact names.
        return [j for j in await self._call(self._queue, name=job_name) if j.job_name == job_name]

    async def cancel(self, job_id: int) -> None:
        await self._call(self._cancel, job_id)

    async def logs(self, job_id: int, tail_lines: int) -> str:
        return self._redact(await self._call(self._logs, job_id, tail_lines))
