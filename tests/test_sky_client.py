"""SdkSkyPilotClient against stand-in SDK functions that really block, to check
the thread-pool behavior: the event loop stays free, timeouts are enforced, and
launches are polled rather than waited on."""

import asyncio
import time
from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from giskard_wb_api.skypilot import client as client_module
from giskard_wb_api.skypilot.client import SdkSkyPilotClient, SkyPilotError

from .conftest import make_config

pytestmark = pytest.mark.anyio


@dataclass
class Record:
    job_id: int
    job_name: str
    status: SimpleNamespace
    failure_reason: str | None = None
    start_at: float | None = None
    end_at: float | None = None

    def model_dump(self):
        return self.__dict__


class FakeSdk:
    """Just enough of `sky` and `sky.jobs` for the client."""

    def __init__(self):
        self.request_states = ['PENDING', 'RUNNING', 'SUCCEEDED']
        self.status_calls = 0
        self.block_seconds = 0.0
        self.records = [Record(5, 'giskard-a', SimpleNamespace(value='RUNNING')), Record(6, 'giskard-b', SimpleNamespace(value='PENDING'))]
        self.Task = SimpleNamespace(from_yaml_config=lambda config: ('task', config))

    # sky
    def api_status(self, request_ids):
        state = self.request_states[min(self.status_calls, len(self.request_states) - 1)]
        self.status_calls += 1
        return [SimpleNamespace(request_id=request_ids[0], status=state)]

    def get(self, request_id):
        time.sleep(self.block_seconds)
        if request_id == 'launch-req':
            return [42], None
        if request_id == 'queue-req':
            return self.records, 2, {}, 0
        if request_id == 'cancel-req':
            raise RuntimeError('cancel rejected; key server-secret-key-value')
        raise AssertionError(request_id)

    # sky.jobs
    def launch(self, task, name):
        assert task == ('task', {'name': name})
        return 'launch-req'

    def queue_v2(self, refresh, skip_finished, job_ids, name_match, fields):
        assert 'failure_reason' in fields and 'start_at' in fields
        return 'queue-req'

    def cancel(self, job_ids):
        return 'cancel-req'

    def tail_logs(self, job_id, follow, tail, output_stream):
        output_stream.write(f'job {job_id} last {tail} lines; key server-secret-key-value\n')


@pytest.fixture
def sdk(monkeypatch):
    monkeypatch.setattr(client_module, 'LAUNCH_POLL_SECONDS', 0.01)
    return FakeSdk()


def make_client(sdk, **skypilot):
    config = make_config({'skypilot': skypilot})
    client = SdkSkyPilotClient(config.sky, [config.minio.secret_key])
    client._sky = client._jobs = sdk
    return client


async def test_launches_and_polls_the_request_until_it_finishes(sdk):
    client = make_client(sdk)
    request_id = await client.launch('giskard-a', {'name': 'giskard-a'})
    assert request_id == 'launch-req'
    assert await client.wait_for_launch(request_id) == 42
    assert sdk.status_calls == 3


async def test_gives_up_on_a_launch_that_never_finishes(sdk):
    sdk.request_states = ['PENDING']
    client = make_client(sdk, launch_timeout_seconds=0)
    with pytest.raises(SkyPilotError, match='still pending'):
        await client.wait_for_launch('launch-req')


async def test_reports_an_unknown_launch_request(sdk):
    sdk.api_status = lambda request_ids: []
    with pytest.raises(SkyPilotError, match='no record'):
        await make_client(sdk).wait_for_launch('launch-req')


async def test_reads_jobs_by_id_and_by_name(sdk):
    client = make_client(sdk)
    jobs = await client.get_jobs([5, 6])
    assert [(j.job_id, j.status) for j in jobs] == [(5, 'RUNNING'), (6, 'PENDING')]
    assert [j.job_id for j in await client.find_jobs_by_name('giskard-b')] == [6]
    assert await client.get_jobs([]) == []


async def test_redacts_secrets_from_errors_and_logs(sdk):
    client = make_client(sdk)
    with pytest.raises(SkyPilotError) as err:
        await client.cancel(5)
    assert str(err.value) == 'RuntimeError: cancel rejected; key [REDACTED]'
    assert await client.logs(5, 100) == 'job 5 last 100 lines; key [REDACTED]\n'


async def test_blocking_calls_leave_the_event_loop_free(sdk):
    sdk.block_seconds = 0.3
    client = make_client(sdk)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    started = time.monotonic()
    await asyncio.gather(*(client.get_jobs([5]) for _ in range(4)))
    elapsed = time.monotonic() - started
    task.cancel()
    # Four 0.3 s calls ran in parallel on the pool while the loop kept ticking.
    assert elapsed < 0.6
    assert ticks >= 10


async def test_times_out_a_hung_call(sdk):
    sdk.block_seconds = 1.5
    client = make_client(sdk, request_timeout_seconds=1)
    with pytest.raises(SkyPilotError, match='timed out after 1 s'):
        await client.get_jobs([5])
    client.close()


def test_status_values_are_read_from_enums_or_strings():
    record = Record(1, 'x', SimpleNamespace(value='FAILED_SETUP'))
    assert client_module._job(record).status == 'FAILED_SETUP'
    assert client_module._job(replace(record, status='RUNNING')).status == 'RUNNING'
