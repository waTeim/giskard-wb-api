import base64
import json
import time

import pytest

from .conftest import auth, make_context, token, valid_run

pytestmark = pytest.mark.anyio


async def list_runs(ctx, headers):
    return await ctx.client.get('/api/runs', headers=headers)


# --- OIDC access-token validation


async def test_rejects_requests_without_a_bearer_token(ctx):
    res = await ctx.client.get('/api/runs')
    assert res.status_code == 401
    assert 'Bearer' in res.headers['www-authenticate']


async def test_accepts_a_valid_access_token(ctx):
    assert (await list_runs(ctx, auth())).status_code == 200


@pytest.mark.parametrize(
    'opts',
    [
        pytest.param({'issuer': 'https://evil.test'}, id='wrong issuer'),
        pytest.param({'audience': 'spa-client'}, id='wrong audience (e.g. an ID token for the SPA client)'),
        pytest.param({'expires_at': int(time.time()) - 3600}, id='expired token'),
        pytest.param({'wrong_key': True}, id='signature from an unknown key'),
        pytest.param({'extra': {'nonce': 'n-123'}}, id='ID token with API audience (nonce)'),
        pytest.param({'extra': {'at_hash': 'abc'}}, id='ID token with API audience (at_hash)'),
        pytest.param({'extra': {'typ': 'ID'}}, id='Keycloak ID token typ claim'),
        pytest.param({'typ': 'id_token+jwt'}, id='id_token+jwt header'),
    ],
)
async def test_rejects_invalid_tokens(ctx, opts):
    assert (await list_runs(ctx, auth(**opts))).status_code == 401


async def test_logs_why_a_token_was_rejected_but_does_not_tell_the_caller(ctx, caplog):
    caplog.set_level('INFO', logger='giskard_wb_api.auth')
    res = await list_runs(ctx, auth(issuer='https://other.test/realms/giskard'))
    assert res.json()['message'] == 'Invalid access token'
    res = await list_runs(ctx, auth(audience='spa-client'))
    assert 'InvalidIssuerError: Invalid issuer' in caplog.text
    assert 'InvalidAudienceError: Audience doesn\'t match' in caplog.text
    assert token() not in caplog.text


async def test_rejects_an_unsigned_alg_none_token(ctx):
    parts = token().split('.')
    header = base64.urlsafe_b64encode(json.dumps({'alg': 'none', 'typ': 'at+jwt'}).encode()).rstrip(b'=').decode()
    res = await list_runs(ctx, {'authorization': f'Bearer {header}.{parts[1]}.'})
    assert res.status_code == 401


async def test_requires_the_configured_scope_when_set():
    ctx = make_context({'oidc': {'required_scope': 'giskard:runs'}})
    try:
        assert (await list_runs(ctx, auth())).status_code == 403
        assert (await list_runs(ctx, auth(extra={'scope': 'openid giskard:runs'}))).status_code == 200
    finally:
        await ctx.close()


async def test_enforces_rfc_9068_typ_when_configured():
    ctx = make_context({'oidc': {'require_at_jwt_type': True}})
    try:
        assert (await list_runs(ctx, auth(typ='JWT'))).status_code == 401
        assert (await list_runs(ctx, auth(typ='at+jwt'))).status_code == 200
    finally:
        await ctx.close()


async def test_does_not_require_auth_for_the_health_check(ctx):
    assert (await ctx.client.get('/healthz')).status_code == 200


# --- authorization


def run_paths(id):
    return [
        ('GET', f'/api/runs/{id}'),
        ('PATCH', f'/api/runs/{id}'),
        ('DELETE', f'/api/runs/{id}'),
        ('POST', f'/api/runs/{id}/start'),
        ('POST', f'/api/runs/{id}/stop'),
        ('POST', f'/api/runs/{id}/abort'),
        ('GET', f'/api/runs/{id}/status'),
        ('GET', f'/api/runs/{id}/logs'),
        ('POST', f'/api/runs/{id}/collect'),
        ('GET', f'/api/runs/{id}/results'),
    ]


async def test_hides_other_users_runs_on_every_endpoint(ctx):
    id = (await ctx.client.post('/api/runs', headers=auth(), json=valid_run())).json()['id']
    bob = auth(sub='bob')
    for method, url in run_paths(id):
        payload = {'name': 'x'} if method == 'PATCH' else None
        res = await ctx.client.request(method, url, headers=bob, json=payload)
        assert res.status_code == 404, f'{method} {url}'
    assert (await ctx.client.get('/api/runs', headers=bob)).json()['runs'] == []
    assert ctx.sky.launches == []


async def test_identifies_owners_by_issuer_and_subject_not_subject_alone(ctx):
    run = (await ctx.client.post('/api/runs', headers=auth(), json=valid_run())).json()
    assert run['owner'] == {'issuer': 'https://idp.test/realms/giskard', 'subject': 'alice'}


async def test_lets_an_administrator_see_and_control_all_runs(ctx):
    id = (await ctx.client.post('/api/runs', headers=auth(), json=valid_run())).json()['id']
    admin = auth(sub='root', roles=['giskard-admin'])
    assert (await ctx.client.get(f'/api/runs/{id}', headers=admin)).status_code == 200
    assert (await ctx.client.get('/api/runs', headers=admin)).json()['total'] == 1
    assert (await ctx.client.get('/api/runs?mine=true', headers=admin)).json()['total'] == 0
    assert (await ctx.client.delete(f'/api/runs/{id}', headers=admin)).status_code == 204


# --- CORS


async def test_cors_allows_only_configured_origins(ctx):
    preflight = {'access-control-request-method': 'POST'}
    ok = await ctx.client.options('/api/runs', headers={'origin': 'https://app.example.com', **preflight})
    assert ok.headers.get('access-control-allow-origin') == 'https://app.example.com'
    bad = await ctx.client.options('/api/runs', headers={'origin': 'https://evil.example.com', **preflight})
    assert 'access-control-allow-origin' not in bad.headers
