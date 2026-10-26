from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from ..auth import Principal
from ..config import Config
from ..errors import ApiError
from .model import Run
from .service import RunService
from .validation import CreateRun, ListQuery, LogsQuery, UpdateRun

MAX_BODY_BYTES = 1024 * 1024


async def principal(request: Request) -> Principal:
    return await request.app.state.authenticator.authenticate(request.headers.get('authorization'))


async def _json_body(request: Request) -> Any:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise ApiError(413, 'payload_too_large', f'Request body exceeds {MAX_BODY_BYTES} bytes')
    try:
        return json.loads(body)
    except ValueError:
        raise ApiError(400, 'invalid_request', 'Request body must be valid JSON') from None


def _without_none(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


def _status_view(run: Run) -> dict[str, Any]:
    ex = run['execution']
    return _without_none(
        {
            'id': run['id'],
            'state': ex['state'],
            'message': ex.get('message'),
            'lastError': ex.get('lastError'),
            'statusUncertain': ex.get('statusUncertain', False),
            'skyJobId': ex.get('skyJobId'),
            'skyJobName': ex.get('skyJobName'),
            'skyStatus': ex.get('skyStatus'),
            'submittedAt': ex.get('submittedAt'),
            'startedAt': ex.get('startedAt'),
            'finishedAt': ex.get('finishedAt'),
            'lastCheckedAt': ex.get('lastCheckedAt'),
            'stopRequestedAt': ex.get('stopRequestedAt'),
            'abortRequestedAt': ex.get('abortRequestedAt'),
            'cancellation': ex.get('cancellation'),
        }
    )


def run_router(service: RunService, config: Config) -> APIRouter:
    router = APIRouter(prefix='/api/runs')
    context = {'max_config_bytes': config.max_inline_config_bytes}
    # Every endpoint authenticates the caller.
    Caller = Depends(principal)

    @router.post('')
    async def create_run(request: Request, caller: Principal = Caller):
        inputs = CreateRun.model_validate(await _json_body(request), context=context)
        run = await service.create(caller, inputs.document())
        return JSONResponse(run, status_code=201, headers={'location': f'/api/runs/{run["id"]}'})

    @router.get('')
    async def list_runs(request: Request, caller: Principal = Caller):
        q = ListQuery.model_validate(dict(request.query_params))
        runs, total = service.list(caller, q.limit, q.offset, mine=q.mine == 'true')
        return {'runs': runs, 'total': total, 'limit': q.limit, 'offset': q.offset}

    @router.get('/{id}')
    async def get_run(id: str, caller: Principal = Caller):
        return service.get(caller, id)

    @router.patch('/{id}')
    async def update_run(id: str, request: Request, caller: Principal = Caller):
        patch = UpdateRun.model_validate(await _json_body(request), context=context)
        return await service.update(caller, id, patch.document())

    @router.delete('/{id}')
    async def delete_run(id: str, caller: Principal = Caller):
        service.delete(caller, id)
        return Response(status_code=204)

    @router.post('/{id}/start')
    async def start_run(id: str, caller: Principal = Caller):
        return JSONResponse(_status_view(await service.start(caller, id)), status_code=202)

    @router.post('/{id}/stop')
    async def stop_run(id: str, caller: Principal = Caller):
        return JSONResponse(_status_view(await service.stop(caller, id)), status_code=202)

    @router.post('/{id}/abort')
    async def abort_run(id: str, caller: Principal = Caller):
        return JSONResponse(_status_view(service.abort(caller, id)), status_code=202)

    @router.get('/{id}/status')
    async def run_status(id: str, caller: Principal = Caller):
        return _status_view(await service.status(caller, id))

    @router.get('/{id}/logs')
    async def run_logs(id: str, request: Request, caller: Principal = Caller):
        tail = LogsQuery.model_validate(dict(request.query_params)).tail
        job_id, logs = await service.logs(caller, id, tail)
        return {'id': id, 'skyJobId': job_id, 'tail': tail, 'logs': logs}

    @router.post('/{id}/collect')
    async def collect_results(id: str, caller: Principal = Caller):
        listing = service.collect(caller, id)['resultsListing']
        body = _without_none({'id': id, 'collecting': listing['collecting'], 'collectedAt': listing.get('collectedAt')})
        return JSONResponse(body, status_code=202)

    @router.get('/{id}/results')
    async def run_results(id: str, caller: Principal = Caller):
        return _without_none(service.results(caller, id))

    return router
