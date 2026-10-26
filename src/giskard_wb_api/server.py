"""Entry point: `giskard-wb-api` (or `python -m giskard_wb_api`)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os

import uvicorn

from .app import build_app
from .auth import Authenticator, remote_key_source
from .config import DEFAULT_CONFIG_PATH, Config, load_config
from .runs.service import RunService
from .runs.store import RunStore
from .skypilot.client import SdkSkyPilotClient
from .storage import MinioStorage

log = logging.getLogger('giskard_wb_api')


async def _poll(service: RunService, interval: int) -> None:
    """Reconciles active runs with SkyPilot now and then every `interval` seconds."""
    while True:
        try:
            await service.reconcile()
        except Exception:
            log.exception('status reconciliation failed')
        await asyncio.sleep(interval)


def create_app(config: Config):
    store = RunStore(config.database_path)
    authenticator = Authenticator(config.oidc, remote_key_source(config.oidc))
    # The SkyPilot SDK takes its API server and token only from SKYPILOT_*
    # environment variables or ~/.sky/config.yaml. Hand them over in-process
    # rather than writing the token to disk; they still come from our config file.
    if config.sky.api_server_endpoint:
        os.environ['SKYPILOT_API_SERVER_ENDPOINT'] = config.sky.api_server_endpoint
    if config.sky.service_account_token:
        os.environ['SKYPILOT_SERVICE_ACCOUNT_TOKEN'] = config.sky.service_account_token
    secrets = [config.minio.secret_key, config.minio.job_secret_key, config.minio.access_key, config.minio.job_access_key]
    if config.sky.service_account_token:
        secrets.append(config.sky.service_account_token)
    sky = SdkSkyPilotClient(config.sky, secrets)
    service = RunService(config, store, MinioStorage(config.minio), sky)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        # Resume tracking of runs that were active before a restart, then poll.
        service.reset_interrupted_work()
        poller = asyncio.create_task(_poll(service, config.poll_interval_seconds))
        try:
            yield
        finally:
            poller.cancel()
            sky.close()
            store.close()

    return build_app(config, authenticator, service, lifespan)


# server.log_level also accepts the pino names used by earlier versions of this server.
_LEVEL_ALIASES = {'warn': 'warning', 'fatal': 'critical', 'trace': 'debug'}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog='giskard-wb-api', description='Giskard training-run API server.')
    parser.add_argument(
        '-c', '--config', default=DEFAULT_CONFIG_PATH, help=f'YAML config file (default: {DEFAULT_CONFIG_PATH})'
    )
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ValueError as err:
        parser.exit(2, f'giskard-wb-api: {err}\n')
    level = config.log_level.lower()
    level = _LEVEL_ALIASES.get(level, level)
    logging.basicConfig(level=level.upper(), format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level=level)


if __name__ == '__main__':
    main()
