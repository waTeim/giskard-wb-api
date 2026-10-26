"""OIDC bearer access-token validation.

Tokens must be JWT access tokens issued by the configured issuer for this API
(audience), signed with a key from the issuer's JWKS, and unexpired. ID tokens
are rejected: they carry the client ID as audience, and we additionally refuse
tokens that look like ID tokens (nonce / at_hash / typ "ID" / typ header
"id_token+jwt") in case an IdP issues them with the API audience.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from .config import OidcConfig

log = logging.getLogger(__name__)

# Refetch the JWKS after this long, and on an unknown key ID at most this often.
JWKS_MAX_AGE_SECONDS = 10 * 60
JWKS_COOLDOWN_SECONDS = 30

_BEARER_RE = re.compile(r'^Bearer\s+([A-Za-z0-9._~+/-]+=*)$', re.IGNORECASE)


@dataclass(frozen=True)
class Principal:
    issuer: str
    subject: str
    name: str | None
    scopes: tuple[str, ...]
    is_admin: bool


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class KeyNotFound(Exception):
    pass


class JwksKeySource:
    """Signing keys from a JWKS document, refetched when stale or on an unknown key ID."""

    def __init__(self, fetch: Callable[[], dict[str, Any]]):
        self._fetch = fetch
        self._lock = threading.Lock()
        self._keys: jwt.PyJWKSet | None = None
        self._fetched_at = float('-inf')

    def _lookup(self, kid: str | None) -> Any:
        keys = self._keys.keys if self._keys else []
        if kid is not None:
            matches = [k for k in keys if k.key_id == kid]
        else:
            matches = [k for k in keys if k.public_key_use in (None, 'sig')]
        if len(matches) != 1:
            raise KeyNotFound(kid)
        return matches[0].key

    def _get(self, kid: str | None) -> Any:
        with self._lock:
            age = time.monotonic() - self._fetched_at
            if age < JWKS_MAX_AGE_SECONDS:
                try:
                    return self._lookup(kid)
                except KeyNotFound:
                    if age < JWKS_COOLDOWN_SECONDS:
                        raise
            self._keys = jwt.PyJWKSet.from_dict(self._fetch())
            self._fetched_at = time.monotonic()
            return self._lookup(kid)

    async def get_key(self, kid: str | None) -> Any:
        # Fetching the JWKS blocks, so lookups run off the event loop.
        return await asyncio.to_thread(self._get, kid)


def _get_json(url: str) -> dict[str, Any]:
    res = httpx.get(url, timeout=10, follow_redirects=True)
    if res.status_code != 200:
        raise RuntimeError(f'{url} returned {res.status_code}')
    return res.json()


def remote_key_source(config: OidcConfig) -> JwksKeySource:
    """Resolves the JWKS from config or the issuer's discovery document."""
    jwks_uri = config.jwks_uri
    if not jwks_uri:
        url = f'{config.issuer.rstrip("/")}/.well-known/openid-configuration'
        try:
            meta = _get_json(url)
        except Exception as err:
            raise RuntimeError(f'OIDC discovery failed: {err}') from err
        if meta.get('issuer') != config.issuer:
            raise RuntimeError(f'OIDC discovery issuer mismatch: {meta.get("issuer")}')
        if not meta.get('jwks_uri'):
            raise RuntimeError('OIDC discovery document has no jwks_uri')
        jwks_uri = meta['jwks_uri']
    return JwksKeySource(lambda: _get_json(jwks_uri))


def _claim_at(payload: dict[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split('.'):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _scopes_of(payload: dict[str, Any]) -> tuple[str, ...]:
    scope = payload.get('scope', payload.get('scp'))
    if isinstance(scope, str):
        return tuple(s for s in scope.split(' ') if s)
    if isinstance(scope, list):
        return tuple(s for s in scope if isinstance(s, str))
    return ()


def _media_type(typ: Any) -> str | None:
    return typ.lower().removeprefix('application/') if isinstance(typ, str) else None


class Authenticator:
    def __init__(self, config: OidcConfig, keys: JwksKeySource):
        self._config = config
        self._keys = keys

    async def authenticate(self, authorization: str | None) -> Principal:
        match = _BEARER_RE.match(authorization or '')
        if not match:
            raise AuthError(401, 'Missing or malformed bearer token')
        token = match.group(1)
        config = self._config

        try:
            header = jwt.get_unverified_header(token)
            key = await self._keys.get_key(header.get('kid'))
            payload = jwt.decode(
                token,
                key,
                algorithms=list(config.algorithms),
                audience=list(config.audience),
                issuer=config.issuer,
                leeway=config.clock_tolerance_seconds,
                options={'require': ['sub', 'exp']},
            )
        except jwt.ExpiredSignatureError:
            raise AuthError(401, 'Token expired') from None
        except Exception as err:
            # Callers only learn that the token is invalid; the reason (e.g.
            # "Invalid audience", an unreachable JWKS) is for the operator.
            log.info('rejected access token: %s: %s', type(err).__name__, err)
            raise AuthError(401, 'Invalid access token') from None

        typ = _media_type(header.get('typ'))
        if config.require_at_jwt_type and typ != 'at+jwt':
            raise AuthError(401, 'Invalid access token')
        if (
            typ == 'id_token+jwt'
            or 'nonce' in payload
            or 'at_hash' in payload
            or (isinstance(payload.get('typ'), str) and payload['typ'].lower() == 'id')
        ):
            raise AuthError(401, 'ID tokens are not accepted; send an access token')

        scopes = _scopes_of(payload)
        if config.required_scope and config.required_scope not in scopes:
            raise AuthError(403, f'Missing required scope {config.required_scope}')

        roles = _claim_at(payload, config.roles_claim)
        is_admin = (config.admin_scope is not None and config.admin_scope in scopes) or (
            config.admin_role is not None and isinstance(roles, list) and config.admin_role in roles
        )

        name = next(
            (v for v in (payload.get('preferred_username'), payload.get('email'), payload.get('name')) if isinstance(v, str)),
            None,
        )
        return Principal(payload['iss'], payload['sub'], name, scopes, is_admin)
