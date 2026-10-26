"""Server configuration, read once at startup from a YAML file (see config.example.yaml).

The file is passed with `giskard-wb-api --config PATH`; in Kubernetes it is a
mounted ConfigMap. Credentials can be given inline (convenient for local
development) or, preferably, as files in `secrets_dir` (a mounted Secret), one
file per credential named by its dotted config key, e.g. `minio.secret_key`.
Environment variables are not consulted.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt, ValidationError

DEFAULT_CONFIG_PATH = '/etc/giskard-wb-api/config.yaml'


@dataclass(frozen=True)
class StorageLocation:
    bucket: str
    # Key prefix, either '' (whole bucket) or ending in '/'.
    prefix: str


@dataclass(frozen=True)
class GpuOption:
    type: str
    counts: tuple[int, ...]


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    audience: tuple[str, ...]
    # Explicit JWKS URI; when unset it is discovered from the issuer metadata.
    jwks_uri: str | None
    algorithms: tuple[str, ...]
    clock_tolerance_seconds: int
    # Scope every caller must hold, if set.
    required_scope: str | None
    # Scope that grants administrator access to all runs, if set.
    admin_scope: str | None
    # Role (looked up at `roles_claim`) that grants administrator access, if set.
    admin_role: str | None
    # Dot path of the roles claim, e.g. "roles" or "realm_access.roles".
    roles_claim: str
    # Require the RFC 9068 `at+jwt` token type header.
    require_at_jwt_type: bool


@dataclass(frozen=True)
class MinioConfig:
    endpoint: str
    # Endpoint used in presigned URLs handed to browsers.
    public_endpoint: str
    # Endpoint the remote training job uses (must be reachable from Lambda.ai).
    job_endpoint: str
    region: str
    access_key: str
    secret_key: str
    # Credentials injected into the remote job as SkyPilot secrets.
    job_access_key: str
    job_secret_key: str
    force_path_style: bool


@dataclass(frozen=True)
class SkyConfig:
    # SkyPilot API server; when unset the SDK starts a local one (development only).
    api_server_endpoint: str | None
    # Needed only when the API server enforces authentication.
    service_account_token: str | None
    infra: str
    region: str | None
    disk_size_gb: int
    image_id: str | None
    # Limit for a single SkyPilot SDK call.
    request_timeout_seconds: int
    # Limit for a launch request to produce a managed job ID.
    launch_timeout_seconds: int
    # Size of the thread pool that runs the (blocking) SkyPilot SDK.
    threads: int
    job_name_prefix: str


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    log_level: str
    database_path: str
    cors_origins: tuple[str, ...]
    oidc: OidcConfig
    minio: MinioConfig
    sky: SkyConfig
    allowed_trainer_locations: tuple[StorageLocation, ...]
    allowed_dataset_locations: tuple[StorageLocation, ...]
    allowed_config_locations: tuple[StorageLocation, ...]
    results: StorageLocation
    presign_ttl_seconds: int
    gpus: tuple[GpuOption, ...]
    max_runtime_minutes: int
    stop_grace_seconds: int
    poll_interval_seconds: int
    max_inline_config_bytes: int


BUCKET_RE = re.compile(r'^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$')


def parse_location(raw: str) -> StorageLocation:
    trimmed = raw.strip().removeprefix('s3://')
    bucket, _, prefix = trimmed.partition('/')
    if not BUCKET_RE.match(bucket):
        raise ValueError(f'Invalid bucket in location "{raw}"')
    if prefix and not prefix.endswith('/'):
        prefix += '/'
    return StorageLocation(bucket, prefix)


# The file schema. Sections mirror the YAML layout; unknown keys are rejected so
# that typos fail at startup instead of being silently ignored.


class _Section(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


def _check_location(raw: str) -> str:
    parse_location(raw)
    return raw


def _check_gpus(gpus: dict[str, list[int]]) -> dict[str, list[int]]:
    for gpu, counts in gpus.items():
        if not re.fullmatch(r'[A-Za-z0-9-]+', gpu):
            raise ValueError(f'Invalid GPU type "{gpu}"')
        if not counts or any(c < 1 for c in counts):
            raise ValueError(f'Invalid GPU counts for {gpu}: {counts}')
    return gpus


Text = Annotated[str, Field(min_length=1)]
Location = Annotated[str, AfterValidator(_check_location)]
Locations = Annotated[list[Location], Field(min_length=1)]


class _Server(_Section):
    host: str = '0.0.0.0'
    port: Annotated[int, Field(ge=1, le=65535)] = 8080
    log_level: str = 'info'
    cors_allowed_origins: list[str] = []
    database_path: Text = './data/giskard-wb-api.db'


class _Oidc(_Section):
    issuer: Text
    audience: Annotated[list[Text], Field(min_length=1)]
    jwks_uri: str | None = None
    algorithms: Annotated[list[Text], Field(min_length=1)] = ['RS256', 'PS256', 'ES256']
    clock_tolerance_seconds: NonNegativeInt = 30
    required_scope: str | None = None
    admin_scope: str | None = None
    admin_role: str | None = None
    roles_claim: Text = 'roles'
    require_at_jwt_type: bool = False


class _Minio(_Section):
    endpoint: Text
    public_endpoint: str | None = None
    job_endpoint: str | None = None
    region: Text = 'us-east-1'
    force_path_style: bool = True
    access_key: Text
    secret_key: Text
    job_access_key: str | None = None
    job_secret_key: str | None = None


class _Storage(_Section):
    allowed_trainer_locations: Locations
    allowed_dataset_locations: Locations
    allowed_config_locations: list[Location] = []
    results_location: Location
    presign_ttl_seconds: NonNegativeInt = 900


class _Runs(_Section):
    # GPU type -> counts callers may request, e.g. {H100: [1, 8], A10: [1]}.
    gpus: Annotated[dict[str, list[int]], Field(min_length=1), AfterValidator(_check_gpus)]
    max_runtime_minutes: NonNegativeInt = 24 * 60
    stop_grace_seconds: NonNegativeInt = 600
    status_poll_interval_seconds: NonNegativeInt = 30
    max_inline_config_bytes: NonNegativeInt = 256 * 1024


class _Skypilot(_Section):
    api_server_endpoint: str | None = None
    service_account_token: str | None = None
    infra: Text = 'lambda'
    region: str | None = None
    disk_size_gb: NonNegativeInt = 256
    image_id: str | None = None
    job_name_prefix: Text = 'giskard'
    request_timeout_seconds: NonNegativeInt = 120
    launch_timeout_seconds: NonNegativeInt = 1800
    threads: PositiveInt = 8


class _ConfigFile(_Section):
    secrets_dir: str | None = None
    server: _Server = _Server()
    oidc: _Oidc
    minio: _Minio
    storage: _Storage
    runs: _Runs
    skypilot: _Skypilot = _Skypilot()


# Credentials that may be supplied as files in secrets_dir, named by config key.
SECRET_KEYS = (
    'minio.access_key',
    'minio.secret_key',
    'minio.job_access_key',
    'minio.job_secret_key',
    'skypilot.service_account_token',
)


def _apply_secrets_dir(doc: dict[str, Any], base: Path) -> None:
    raw = doc.get('secrets_dir')
    if not raw:
        return
    secrets_dir = base / str(raw)
    if not secrets_dir.is_dir():
        raise ValueError(f'secrets_dir {secrets_dir} is not a directory')
    for name in SECRET_KEYS:
        path = secrets_dir / name
        if not path.is_file():
            continue
        section, key = name.split('.')
        values = doc.setdefault(section, {})
        if not isinstance(values, dict):
            continue  # reported by validation
        if values.get(key) not in (None, ''):
            raise ValueError(f'{name} is set both in the config file and in {path}')
        values[key] = path.read_text(encoding='utf-8').strip()


def _empty_to_none(value: Any) -> Any:
    """Treats empty strings as unset, so `key: ""` and an omitted key mean the same."""
    if isinstance(value, dict):
        return {k: v for k, v in ((k, _empty_to_none(v)) for k, v in value.items()) if v is not None}
    return None if value == '' else value


def _format_errors(err: ValidationError) -> str:
    lines = []
    for e in err.errors():
        path = '.'.join(str(p) for p in e['loc'])
        message = e['msg'].removeprefix('Value error, ')
        lines.append(f'  {path}: {message}')
    return 'Invalid configuration:\n' + '\n'.join(lines)


def parse_config(doc: Mapping[str, Any], base_dir: str | Path = '.') -> Config:
    """Builds the configuration from a parsed config file. A relative
    `secrets_dir` is resolved against `base_dir`."""
    data = _empty_to_none(dict(doc))
    _apply_secrets_dir(data, Path(base_dir))
    try:
        f = _ConfigFile.model_validate(data)
    except ValidationError as err:
        raise ValueError(_format_errors(err)) from None

    public_endpoint = f.minio.public_endpoint or f.minio.endpoint
    return Config(
        host=f.server.host,
        port=f.server.port,
        log_level=f.server.log_level,
        database_path=f.server.database_path,
        cors_origins=tuple(f.server.cors_allowed_origins),
        oidc=OidcConfig(
            issuer=f.oidc.issuer,
            audience=tuple(f.oidc.audience),
            jwks_uri=f.oidc.jwks_uri,
            algorithms=tuple(f.oidc.algorithms),
            clock_tolerance_seconds=f.oidc.clock_tolerance_seconds,
            required_scope=f.oidc.required_scope,
            admin_scope=f.oidc.admin_scope,
            admin_role=f.oidc.admin_role,
            roles_claim=f.oidc.roles_claim,
            require_at_jwt_type=f.oidc.require_at_jwt_type,
        ),
        minio=MinioConfig(
            endpoint=f.minio.endpoint,
            public_endpoint=public_endpoint,
            job_endpoint=f.minio.job_endpoint or public_endpoint,
            region=f.minio.region,
            access_key=f.minio.access_key,
            secret_key=f.minio.secret_key,
            job_access_key=f.minio.job_access_key or f.minio.access_key,
            job_secret_key=f.minio.job_secret_key or f.minio.secret_key,
            force_path_style=f.minio.force_path_style,
        ),
        sky=SkyConfig(
            api_server_endpoint=f.skypilot.api_server_endpoint,
            service_account_token=f.skypilot.service_account_token,
            infra=f.skypilot.infra,
            region=f.skypilot.region,
            disk_size_gb=f.skypilot.disk_size_gb,
            image_id=f.skypilot.image_id,
            request_timeout_seconds=f.skypilot.request_timeout_seconds,
            launch_timeout_seconds=f.skypilot.launch_timeout_seconds,
            threads=f.skypilot.threads,
            job_name_prefix=f.skypilot.job_name_prefix,
        ),
        allowed_trainer_locations=tuple(parse_location(v) for v in f.storage.allowed_trainer_locations),
        allowed_dataset_locations=tuple(parse_location(v) for v in f.storage.allowed_dataset_locations),
        allowed_config_locations=tuple(parse_location(v) for v in f.storage.allowed_config_locations),
        results=parse_location(f.storage.results_location),
        presign_ttl_seconds=f.storage.presign_ttl_seconds,
        gpus=tuple(GpuOption(gpu, tuple(counts)) for gpu, counts in f.runs.gpus.items()),
        max_runtime_minutes=f.runs.max_runtime_minutes,
        stop_grace_seconds=f.runs.stop_grace_seconds,
        poll_interval_seconds=f.runs.status_poll_interval_seconds,
        max_inline_config_bytes=f.runs.max_inline_config_bytes,
    )


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    """Reads and validates the config file at `path`."""
    path = Path(path)
    try:
        doc = yaml.safe_load(path.read_text(encoding='utf-8'))
    except (OSError, yaml.YAMLError) as err:
        raise ValueError(f'Cannot read config file {path}: {err}') from err
    if not isinstance(doc, dict):
        raise ValueError(f'Config file {path} must contain a mapping')
    return parse_config(doc, path.parent)
