"""Synthetic gateway control-plane checks, no real data or browser."""
from test_agent_gateway import gateway, conversation, turn, internal


def test_all_api_routes_reject_foreign_origin_and_host(gateway):
    client, _, _, _ = gateway
    for path in ['/api/v1/ai/providers', '/api/v1/preferences/availability', '/api/v1/connections', '/api/v1/agent/settings']:
        for headers in [{'Origin':'https://foreign.example'}, {'Host':'foreign.example'}, {'Sec-Fetch-Site':'cross-site'}]:
            assert client.get(path, headers=headers).status_code == 403


def test_runtime_bearer_cannot_call_user_settings_or_approval_routes(gateway):
    client, _, tokens, _ = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    headers = {'Authorization':'Bearer '+tokens[tid]}
    assert client.put('/api/v1/agent/settings', headers=headers, json={'workspace':'C:/'}).status_code == 403
    assert client.patch('/api/v1/ai/providers/provider_a', headers=headers, json={'enabled':False}).status_code == 403
    assert client.post('/api/v1/plans/forged/apply', headers=headers, json={}).status_code == 403
    assert internal(client,tokens[tid],'schemas',{}).status_code == 200


def test_restart_revokes_prewarm_authority(gateway):
    client, runtime, _, _ = gateway
    cid = conversation(client)
    token = runtime.store.grant(cid)
    assert runtime.store.granted(token)
    runtime.store.recover()
    assert runtime.store.granted(token) is None
    assert internal(client,token,'schemas',{}).status_code == 403
