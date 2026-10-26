import asyncio
import json

import pytest

from giskard_wb_api.skypilot.client import SkyJob
from giskard_wb_api.timeutil import now

from .conftest import auth, valid_run

pytestmark = pytest.mark.anyio

HEADERS = auth()


async def call(ctx, method, url, payload=None):
    return await ctx.client.request(method, url, headers=HEADERS, json=payload)


async def create_run(ctx, **overrides):
    res = await call(ctx, 'POST', '/api/runs', {**valid_run(), **overrides})
    assert res.status_code == 201, res.text
    return res.json()


async def start_run(ctx):
    run = await create_run(ctx)
    assert (await call(ctx, 'POST', f'/api/runs/{run["id"]}/start')).status_code == 202
    await ctx.service.idle()
    return ctx.store.get(run['id'])


def set_execution(ctx, id, execution):
    def apply(r):
        r['execution'] = execution
        return r

    ctx.store.update(id, apply)


# --- run definitions


async def test_creates_reads_lists_updates_and_deletes_a_run(ctx):
    run = await create_run(ctx)
    assert run['name'] == 'sentiment fine-tune'
    assert run['owner']['subject'] == 'alice'
    assert run['execution'] == {'state': 'draft'}
    assert run['results'] == {'bucket': 'giskard-results', 'prefix': f'runs/{run["id"]}/outputs/'}

    assert (await call(ctx, 'GET', f'/api/runs/{run["id"]}')).json()['id'] == run['id']
    assert (await call(ctx, 'GET', '/api/runs')).json()['total'] == 1

    patched = await call(
        ctx,
        'PATCH',
        f'/api/runs/{run["id"]}',
        {
            'config': {'object': {'bucket': 'giskard-artifacts', 'key': 'configs/base.yaml'}},
            'compute': {'gpuType': 'A10', 'gpuCount': 1},
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()['compute'] == {'gpuType': 'A10', 'gpuCount': 1}

    assert (await call(ctx, 'DELETE', f'/api/runs/{run["id"]}')).status_code == 204
    assert (await call(ctx, 'GET', f'/api/runs/{run["id"]}')).status_code == 404


async def test_defaults_the_entrypoint_and_accepts_yaml_config_and_single_object_dataset(ctx):
    run = await create_run(
        ctx,
        trainer={'bucket': 'giskard-artifacts', 'key': 'wheels/trainer-1.0.0-py3-none-any.whl'},
        dataset={'bucket': 'giskard-datasets', 'key': 'reviews/train.parquet'},
        config={'format': 'yaml', 'value': 'epochs: 2\nlr: 0.01\n'},
    )
    assert run['trainer']['entrypoint'] == 'giskard_trainer'


@pytest.mark.parametrize(
    'overrides, status',
    [
        pytest.param({'compute': {'gpuType': 'V100', 'gpuCount': 1}}, 422, id='GPU type not on the allowlist'),
        pytest.param({'compute': {'gpuType': 'H100', 'gpuCount': 4}}, 422, id='GPU count not on the allowlist'),
        pytest.param(
            {'compute': {'gpuType': 'H100', 'gpuCount': 1, 'maxRuntimeMinutes': 6000}}, 422, id='runtime above the limit'
        ),
        pytest.param(
            {'trainer': {'bucket': 'giskard-artifacts', 'key': 'other/t-1.0-py3-none-any.whl'}},
            422,
            id='trainer outside approved locations',
        ),
        pytest.param({'dataset': {'bucket': 'private-bucket', 'prefix': 'x/'}}, 422, id='dataset outside approved locations'),
        pytest.param(
            {'trainer': {'bucket': 'giskard-artifacts', 'key': 'wheels/missing-1.0-py3-none-any.whl'}},
            422,
            id='wheel that does not exist',
        ),
        pytest.param({'dataset': {'bucket': 'giskard-datasets', 'prefix': 'empty/'}}, 422, id='dataset prefix with no objects'),
        pytest.param({'trainer': {'bucket': 'giskard-artifacts', 'key': 'wheels/trainer.tar.gz'}}, 400, id='non-wheel trainer'),
        pytest.param(
            {'trainer': {'bucket': 'giskard-artifacts', 'key': 'wheels/../secret/t-1-py3-none-any.whl'}},
            400,
            id='path traversal in key',
        ),
        pytest.param(
            {
                'trainer': {
                    'bucket': 'giskard-artifacts',
                    'key': 'wheels/trainer-1.0.0-py3-none-any.whl',
                    'entrypoint': 'x; rm -rf /',
                }
            },
            400,
            id='shell metacharacters in entrypoint',
        ),
        pytest.param({'command': 'bash -c evil'}, 400, id='unknown fields'),
        pytest.param({'config': {'format': 'yaml', 'value': 'a: [1,'}}, 400, id='invalid YAML config'),
        pytest.param({'compute': {'gpuType': 'H100', 'gpuCount': '8'}}, 400, id='GPU count as a string'),
        pytest.param({'name': None}, 400, id='null name'),
    ],
)
async def test_rejects_invalid_definitions(ctx, overrides, status):
    res = await call(ctx, 'POST', '/api/runs', {**valid_run(), **overrides})
    assert res.status_code == status, res.text


async def test_rejects_an_empty_or_null_patch(ctx):
    run = await create_run(ctx)
    for patch in ({}, {'name': None}):
        res = await call(ctx, 'PATCH', f'/api/runs/{run["id"]}', patch)
        assert res.status_code == 400, res.text


async def test_reports_validation_errors_with_paths(ctx):
    res = await call(ctx, 'POST', '/api/runs', {**valid_run(), 'compute': {'gpuType': 'H100', 'gpuCount': 0}})
    assert res.status_code == 400
    assert res.json()['error'] == 'invalid_request'
    assert 'compute.gpuCount' in [d['path'] for d in res.json()['details']]


# --- execution


async def test_submits_a_skypilot_job_on_lambda_with_the_run_inputs(ctx):
    run = await start_run(ctx)
    assert run['execution']['state'] == 'queued'
    assert run['execution']['skyJobId'] == 100
    assert run['execution']['skyRequestId'] == 'req-1'
    assert len(ctx.sky.launches) == 1
    name, task = ctx.sky.launches[0]
    assert name == f'giskard-{run["id"]}'
    assert task['resources']['infra'] == 'lambda'
    assert task['resources']['accelerators'] == 'H100:8'
    assert task['envs'] | {
        'GISKARD_TRAINER_KEY': 'wheels/trainer-1.0.0-py3-none-any.whl',
        'GISKARD_DATASET_KIND': 'prefix',
        'GISKARD_DATASET_PATH': 'reviews/',
        'GISKARD_RESULTS_PREFIX': f'runs/{run["id"]}/outputs/',
        'GISKARD_CONFIG_KEY': f'runs/{run["id"]}/inputs/config.json',
        'GISKARD_MAX_RUNTIME_SECONDS': '7200',
    } == task['envs']
    # Credentials travel as SkyPilot secrets, never as plain envs.
    assert 'server-secret-key-value' not in json.dumps(task['envs'])
    assert task['secrets']['AWS_SECRET_ACCESS_KEY'] == 'server-secret-key-value'
    # Inline config was staged in MinIO for the job.
    staged = ctx.storage.objects[f'giskard-results/runs/{run["id"]}/inputs/config.json']
    assert json.loads(staged) == {'epochs': 3, 'lr': 0.0001}


async def test_start_is_idempotent_when_retried(ctx):
    run = await create_run(ctx)
    a, b = await asyncio.gather(
        call(ctx, 'POST', f'/api/runs/{run["id"]}/start'), call(ctx, 'POST', f'/api/runs/{run["id"]}/start')
    )
    assert [a.status_code, b.status_code] == [202, 202]
    await ctx.service.idle()
    assert (await call(ctx, 'POST', f'/api/runs/{run["id"]}/start')).json()['state'] == 'queued'
    await ctx.service.idle()
    assert len(ctx.sky.launches) == 1


async def test_prevents_input_changes_and_deletion_while_executing(ctx):
    run = await start_run(ctx)
    assert (await call(ctx, 'PATCH', f'/api/runs/{run["id"]}', {'name': 'renamed'})).status_code == 409
    assert (await call(ctx, 'DELETE', f'/api/runs/{run["id"]}')).status_code == 409


async def test_tracks_status_through_skypilot_to_success(ctx):
    run = await start_run(ctx)
    ctx.sky.set_status(100, 'RUNNING')
    status = (await call(ctx, 'GET', f'/api/runs/{run["id"]}/status')).json()
    assert status['state'] == 'running'
    assert status['skyStatus'] == 'RUNNING'
    assert status['skyJobId'] == 100
    assert status['statusUncertain'] is False
    assert status['startedAt'] == '2023-11-14T22:13:20.000Z'

    ctx.sky.set_status(100, 'SUCCEEDED')
    await ctx.service.reconcile()
    status = (await call(ctx, 'GET', f'/api/runs/{run["id"]}/status')).json()
    assert status['state'] == 'succeeded'
    assert status['finishedAt'] == '2023-11-14T22:23:20.000Z'


async def test_reports_skypilot_failures_with_the_failure_reason(ctx):
    run = await start_run(ctx)
    ctx.sky.set_status(100, 'FAILED_SETUP', 'pip install failed')
    await ctx.service.reconcile()
    status = (await call(ctx, 'GET', f'/api/runs/{run["id"]}/status')).json()
    assert status['state'] == 'failed'
    assert 'pip install failed' in status['lastError']


async def test_marks_status_uncertain_when_skypilot_is_unreachable_instead_of_guessing(ctx):
    run = await start_run(ctx)
    ctx.sky.fail_queue = True
    await ctx.service.reconcile()
    status = (await call(ctx, 'GET', f'/api/runs/{run["id"]}/status')).json()
    assert status['state'] == 'queued'
    assert status['statusUncertain'] is True
    assert 'API server unreachable' in status['lastError']


async def test_fails_the_run_and_records_the_error_when_launch_fails(ctx):
    async def no_capacity(job_name, task):
        raise RuntimeError('No Lambda capacity for H100:8')

    ctx.sky.launch = no_capacity
    run = await start_run(ctx)
    assert run['execution']['state'] == 'failed'
    assert 'No Lambda capacity' in run['execution']['lastError']


async def test_returns_logs_from_skypilot(ctx):
    run = await create_run(ctx)
    assert (await call(ctx, 'GET', f'/api/runs/{run["id"]}/logs')).status_code == 409
    await call(ctx, 'POST', f'/api/runs/{run["id"]}/start')
    await ctx.service.idle()
    res = await call(ctx, 'GET', f'/api/runs/{run["id"]}/logs?tail=50')
    assert res.status_code == 200
    assert res.json()['skyJobId'] == 100
    assert res.json()['tail'] == 50
    assert 'loss=0.5' in res.json()['logs']


# --- stop and abort


async def test_stop_writes_the_marker_and_ends_as_stopped_when_the_job_exits_cleanly(ctx):
    run = await start_run(ctx)
    ctx.sky.set_status(100, 'RUNNING')
    res = await call(ctx, 'POST', f'/api/runs/{run["id"]}/stop')
    assert res.status_code == 202
    assert res.json()['state'] == 'stopping'
    assert f'giskard-results/runs/{run["id"]}/control/STOP' in ctx.storage.objects
    assert ctx.sky.cancelled == []

    await ctx.service.reconcile()
    assert ctx.store.get(run['id'])['execution']['state'] == 'stopping'
    ctx.sky.set_status(100, 'SUCCEEDED')
    await ctx.service.reconcile()
    assert ctx.store.get(run['id'])['execution']['state'] == 'stopped'


async def test_stop_reports_failure_and_leaves_the_run_unchanged_if_the_marker_cannot_be_written(ctx):
    run = await start_run(ctx)
    ctx.storage.fail_puts = True
    res = await call(ctx, 'POST', f'/api/runs/{run["id"]}/stop')
    assert res.status_code == 502
    assert ctx.store.get(run['id'])['execution']['state'] == 'queued'


async def test_abort_cancels_the_job_and_only_reports_aborted_once_confirmed(ctx):
    run = await start_run(ctx)
    res = await call(ctx, 'POST', f'/api/runs/{run["id"]}/abort')
    assert res.status_code == 202
    assert res.json()['state'] == 'aborting'
    await ctx.service.idle()
    assert ctx.sky.cancelled == [100]
    status = (await call(ctx, 'GET', f'/api/runs/{run["id"]}/status')).json()
    assert status['state'] == 'aborting'
    assert status['cancellation']['accepted'] is True

    ctx.sky.set_status(100, 'CANCELLED')
    await ctx.service.reconcile()
    status = (await call(ctx, 'GET', f'/api/runs/{run["id"]}/status')).json()
    assert status['state'] == 'aborted'


async def test_abort_reports_a_failed_cancellation_and_can_be_retried(ctx):
    run = await start_run(ctx)
    ctx.sky.set_status(100, 'RUNNING')
    ctx.sky.fail_cancel = True
    await call(ctx, 'POST', f'/api/runs/{run["id"]}/abort')
    await ctx.service.idle()
    status = (await call(ctx, 'GET', f'/api/runs/{run["id"]}/status')).json()
    assert status['state'] == 'aborting'
    assert status['cancellation']['accepted'] is False
    assert 'may still be running' in status['lastError']

    ctx.sky.fail_cancel = False
    await call(ctx, 'POST', f'/api/runs/{run["id"]}/abort')
    await ctx.service.idle()
    assert ctx.sky.cancelled == [100]


async def test_abort_during_submission_cancels_the_job_once_skypilot_assigns_an_id(ctx):
    ctx.sky.launch_gate = asyncio.Event()
    run = await create_run(ctx)
    await call(ctx, 'POST', f'/api/runs/{run["id"]}/start')
    res = await call(ctx, 'POST', f'/api/runs/{run["id"]}/abort')
    assert res.json()['state'] == 'submitting'
    ctx.sky.launch_gate.set()
    await ctx.service.idle()
    assert ctx.sky.cancelled == [100]
    assert ctx.store.get(run['id'])['execution']['state'] == 'aborting'


async def test_rejects_stop_and_abort_for_runs_that_are_not_executing(ctx):
    run = await create_run(ctx)
    assert (await call(ctx, 'POST', f'/api/runs/{run["id"]}/stop')).status_code == 409
    assert (await call(ctx, 'POST', f'/api/runs/{run["id"]}/abort')).status_code == 409


# --- restart recovery


async def test_recovers_a_submission_interrupted_after_the_launch_request_was_recorded(ctx):
    run = await create_run(ctx)
    set_execution(
        ctx,
        run['id'],
        {'state': 'submitting', 'skyJobName': f'giskard-{run["id"]}', 'skyRequestId': 'req-old', 'submittedAt': now()},
    )
    await ctx.service.reconcile()
    await ctx.service.idle()
    execution = ctx.store.get(run['id'])['execution']
    assert execution['state'] == 'queued'
    assert execution['skyJobId'] == 100
    assert ctx.sky.launches == []


async def test_finds_the_job_by_name_when_the_request_id_was_never_recorded_without_resubmitting(ctx):
    run = await create_run(ctx)
    set_execution(ctx, run['id'], {'state': 'submitting', 'skyJobName': f'giskard-{run["id"]}', 'submittedAt': now()})
    ctx.sky.jobs[7] = SkyJob(7, job_name=f'giskard-{run["id"]}', status='RUNNING')
    await ctx.service.reconcile()
    await ctx.service.idle()
    await ctx.service.reconcile()
    execution = ctx.store.get(run['id'])['execution']
    assert execution['state'] == 'running'
    assert execution['skyJobId'] == 7
    assert ctx.sky.launches == []


async def test_fails_a_lost_submission_after_the_grace_period(ctx):
    run = await create_run(ctx)
    set_execution(ctx, run['id'], {'state': 'submitting', 'submittedAt': '2020-01-01T00:00:00.000Z'})
    await ctx.service.reconcile()
    await ctx.service.idle()
    execution = ctx.store.get(run['id'])['execution']
    assert execution['state'] == 'failed'
    assert 'interrupted' in execution['lastError']


# --- results


async def test_collects_and_lists_only_objects_under_the_run_results_prefix(ctx):
    run = await start_run(ctx)
    prefix = f'runs/{run["id"]}/outputs/'
    ctx.storage.add('giskard-results', f'{prefix}model/weights.safetensors', 'w' * 10)
    ctx.storage.add('giskard-results', f'{prefix}metrics.json', '{}')
    ctx.storage.add('giskard-results', f'runs/{run["id"]}/control/STOP')
    ctx.storage.add('giskard-results', 'runs/someone-else/outputs/secret.bin')

    results = (await call(ctx, 'GET', f'/api/runs/{run["id"]}/results')).json()
    assert results['objects'] == []

    assert (await call(ctx, 'POST', f'/api/runs/{run["id"]}/collect')).status_code == 202
    await ctx.service.idle()

    results = (await call(ctx, 'GET', f'/api/runs/{run["id"]}/results')).json()
    assert 'collectedAt' in results
    assert sorted(o['path'] for o in results['objects']) == ['metrics.json', 'model/weights.safetensors']
    assert results['objects'][0]['url'].startswith('https://minio.example.com/giskard-results/runs/')
    assert 'server-secret-key-value' not in json.dumps(results)


async def test_restart_clears_an_interrupted_collection(ctx):
    run = await create_run(ctx)

    def mark(r):
        r['resultsListing']['collecting'] = True
        return r

    ctx.store.update(run['id'], mark)
    ctx.service.reset_interrupted_work()
    assert ctx.store.get(run['id'])['resultsListing']['collecting'] is False
