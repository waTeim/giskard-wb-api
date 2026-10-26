"""Test harness: a local OIDC signing key, in-memory MinIO and a scriptable SkyPilot fake."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from giskard_wb_api.app import build_app
from giskard_wb_api.auth import Authenticator, JwksKeySource
from giskard_wb_api.config import Config, parse_config
from giskard_wb_api.runs.service import RunService
from giskard_wb_api.runs.store import RunStore
from giskard_wb_api.skypilot.client import SkyJob
from giskard_wb_api.storage import ListResult, StoredObject

ISSUER = 'https://idp.test/realms/giskard'
AUDIENCE = 'giskard-api'

TEST_CONFIG: dict[str, Any] = {
    'server': {'cors_allowed_origins': ['https://app.example.com'], 'database_path': ':memory:'},
    'oidc': {
        'issuer': ISSUER,
        'audience': [AUDIENCE],
        'admin_role': 'giskard-admin',
        'roles_claim': 'realm_access.roles',
    },
    'minio': {
        'endpoint': 'http://minio.internal:9000',
        'public_endpoint': 'https://minio.example.com',
        'access_key': 'server-access-key',
        'secret_key': 'server-secret-key-value',
    },
    'storage': {
        'allowed_trainer_locations': ['giskard-artifacts/wheels'],
        'allowed_dataset_locations': ['giskard-datasets'],
        'allowed_config_locations': ['giskard-artifacts/configs'],
        'results_location': 'giskard-results/runs',
    },
    'runs': {'gpus': {'H100': [1, 8], 'A10': [1]}, 'max_runtime_minutes': 600},
}


def merged(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """`base` with `overrides` merged in, section by section."""
    out = dict(base)
    for key, value in overrides.items():
        out[key] = merged(base[key], value) if isinstance(value, dict) and isinstance(base.get(key), dict) else value
    return out


def make_config(overrides: dict[str, Any] | None = None) -> Config:
    return parse_config(merged(TEST_CONFIG, overrides or {}))


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, str] = {}
        self.fail_puts = False

    def add(self, bucket: str, key: str, body: str = 'x') -> None:
        self.objects[f'{bucket}/{key}'] = body

    async def object_exists(self, bucket: str, key: str) -> bool:
        return f'{bucket}/{key}' in self.objects

    async def prefix_has_objects(self, bucket: str, prefix: str) -> bool:
        return any(k.startswith(f'{bucket}/{prefix}') for k in self.objects)

    async def list(self, bucket: str, prefix: str, max_objects: int) -> ListResult:
        objects = [
            StoredObject(k[len(bucket) + 1 :], len(v), datetime.fromtimestamp(0, UTC), 'e')
            for k, v in self.objects.items()
            if k.startswith(f'{bucket}/{prefix}')
        ]
        return ListResult(objects, False)

    async def put_object(self, bucket: str, key: str, body: str, content_type: str) -> None:
        if self.fail_puts:
            raise RuntimeError('minio down')
        self.objects[f'{bucket}/{key}'] = body

    def presign_get(self, bucket: str, key: str, ttl_seconds: int) -> str:
        return f'https://minio.example.com/{bucket}/{key}?X-Amz-Expires={ttl_seconds}&X-Amz-Signature=sig'


class FakeSky:
    def __init__(self) -> None:
        self.launches: list[tuple[str, dict[str, Any]]] = []
        self.jobs: dict[int, SkyJob] = {}
        self.cancelled: list[int] = []
        self.next_job_id = 100
        self.fail_cancel = False
        self.fail_queue = False
        # When set, wait_for_launch blocks until the event is set.
        self.launch_gate: asyncio.Event | None = None

    async def launch(self, job_name: str, task: dict[str, Any]) -> str:
        self.launches.append((job_name, task))
        return f'req-{len(self.launches)}'

    async def wait_for_launch(self, request_id: str) -> int:
        if self.launch_gate:
            await self.launch_gate.wait()
        job_id = self.next_job_id
        self.next_job_id += 1
        self.jobs[job_id] = SkyJob(job_id, status='PENDING')
        return job_id

    async def get_jobs(self, job_ids: list[int]) -> list[SkyJob]:
        if self.fail_queue:
            raise RuntimeError('API server unreachable')
        return [self.jobs[i] for i in job_ids if i in self.jobs]

    async def find_jobs_by_name(self, job_name: str) -> list[SkyJob]:
        return [j for j in self.jobs.values() if j.job_name == job_name]

    async def cancel(self, job_id: int) -> None:
        if self.fail_cancel:
            raise RuntimeError('controller not reachable')
        self.cancelled.append(job_id)
        if job_id in self.jobs:
            self.jobs[job_id] = replace(self.jobs[job_id], status='CANCELLING')

    async def logs(self, job_id: int, tail_lines: int) -> str:
        return f'logs for {job_id} (tail {tail_lines})\nstep 1 loss=0.5\n'

    def set_status(self, job_id: int, status: str, failure_reason: str | None = None) -> None:
        base = self.jobs.get(job_id, SkyJob(job_id))
        self.jobs[job_id] = replace(
            base, status=status, failure_reason=failure_reason, start_at=1_700_000_000, end_at=1_700_000_600
        )


_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWKS = {
    'keys': [
        {**jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key(), as_dict=True), 'kid': 'test-key', 'alg': 'RS256', 'use': 'sig'}
    ]
}


def token(
    sub: str = 'alice',
    roles: list[str] | None = None,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    expires_at: int | None = None,
    extra: dict[str, Any] | None = None,
    typ: str = 'at+jwt',
    wrong_key: bool = False,
) -> str:
    now = int(time.time())
    payload = {
        'realm_access': {'roles': roles or []},
        'scope': 'openid giskard',
        'iss': issuer,
        'aud': audience,
        'sub': sub,
        'iat': now,
        'exp': expires_at if expires_at is not None else now + 300,
        **(extra or {}),
    }
    return jwt.encode(
        payload, _OTHER_KEY if wrong_key else _KEY, algorithm='RS256', headers={'kid': 'test-key', 'typ': typ}
    )


def auth(**opts: Any) -> dict[str, str]:
    return {'authorization': f'Bearer {token(**opts)}'}


@dataclass
class Context:
    client: httpx.AsyncClient
    service: RunService
    store: RunStore
    storage: FakeStorage
    sky: FakeSky
    config: Config

    async def close(self) -> None:
        await self.service.idle()
        await self.client.aclose()


def make_context(overrides: dict[str, Any] | None = None) -> Context:
    config = make_config(overrides)
    store = RunStore(':memory:')
    storage = FakeStorage()
    sky = FakeSky()
    storage.add('giskard-artifacts', 'wheels/trainer-1.0.0-py3-none-any.whl')
    storage.add('giskard-datasets', 'reviews/train.parquet')
    storage.add('giskard-artifacts', 'configs/base.yaml', 'epochs: 1\n')
    service = RunService(config, store, storage, sky)
    app = build_app(config, Authenticator(config.oidc, JwksKeySource(lambda: JWKS)), service)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
    return Context(client, service, store, storage, sky, config)


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
async def ctx():
    context = make_context()
    yield context
    await context.close()


def valid_run() -> dict[str, Any]:
    return {
        'name': 'sentiment fine-tune',
        'trainer': {'bucket': 'giskard-artifacts', 'key': 'wheels/trainer-1.0.0-py3-none-any.whl', 'entrypoint': 'trainer.main'},
        'dataset': {'bucket': 'giskard-datasets', 'prefix': 'reviews/'},
        'config': {'format': 'json', 'value': {'epochs': 3, 'lr': 0.0001}},
        'compute': {'gpuType': 'H100', 'gpuCount': 8, 'maxRuntimeMinutes': 120},
    }
