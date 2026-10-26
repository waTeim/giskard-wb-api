from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .auth import AuthError, Authenticator
from .config import Config
from .errors import ApiError
from .runs.routes import run_router
from .runs.service import RunService

log = logging.getLogger(__name__)


def _validation_details(err: ValidationError | RequestValidationError) -> list[dict[str, str]]:
    return [{'path': '.'.join(str(p) for p in e['loc']), 'message': e['msg']} for e in err.errors()]


def build_app(config: Config, authenticator: Authenticator, service: RunService, lifespan=None) -> FastAPI:
    # No generated docs: the API is described in README.md, and /docs would be unauthenticated.
    app = FastAPI(title='giskard-wb-api', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.authenticator = authenticator
    app.state.service = service

    app.add_middleware(
        CORSMiddleware,
        # Only explicitly configured browser origins are allowed.
        allow_origins=list(config.cors_origins),
        allow_methods=['GET', 'POST', 'PATCH', 'DELETE'],
        allow_headers=['Authorization', 'Content-Type'],
        max_age=600,
    )

    @app.get('/healthz')
    async def healthz():
        return {'ok': True}

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, err: AuthError):
        challenge = 'Bearer error="invalid_token"' if err.status == 401 else 'Bearer error="insufficient_scope"'
        return JSONResponse(
            {'error': 'unauthorized' if err.status == 401 else 'forbidden', 'message': err.message},
            status_code=err.status,
            headers={'www-authenticate': challenge},
        )

    @app.exception_handler(ApiError)
    async def api_error(request: Request, err: ApiError):
        body = {'error': err.code, 'message': err.message}
        if err.details is not None:
            body['details'] = err.details
        return JSONResponse(body, status_code=err.status)

    @app.exception_handler(ValidationError)
    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, err: ValidationError | RequestValidationError):
        return JSONResponse(
            {'error': 'invalid_request', 'message': 'Request validation failed', 'details': _validation_details(err)},
            status_code=400,
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, err: Exception):
        log.error('unhandled error on %s %s', request.method, request.url.path, exc_info=err)
        return JSONResponse({'error': 'internal_error', 'message': 'Internal server error'}, status_code=500)

    app.include_router(run_router(service, config))
    return app
