"""Real HTTP bridge with isolated domain DB; no school account or paid provider."""
import json

import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import models as m
from app.agent_bridge import model
from app.agent_bridge.runtime import Runtime
from app.agent_bridge.store import Store
from app.agent_bridge.limits import RunLimits
from app.db import Base, get_db
from app.errors import ApiError
from app.main import app
from app.providers import ProviderCallError
from app import secrets_store
REAL_GET_KEY = secrets_store.get_key


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + (tmp_path / "domain.sqlite").as_posix(), connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([m.Course(id="course_a", title="测试课程", term="2026秋"),
                    m.ProviderConfig(id="provider_a", name="合成服务", base_url="http://127.0.0.1:19191/v1", model="fixture")])
        db.commit()

    def sessions():
        with Session(engine) as db:
            yield db

    runtime = Runtime(Store(tmp_path / "journal.sqlite", RunLimits(model_calls=12)), tmp_path / "harness", "http://127.0.0.1:19190")
    tokens = {}

    def fake_start(turn, token):
        # Hold a live journal turn without spawning; process behavior is separately
        # checked by the real Harness integration scenario.
        runtime.store.running(turn["id"])
        tokens[turn["id"]] = token

    monkeypatch.setattr(runtime, "start", fake_start)
    # Prewarming boots a real runtime subprocess; gateway tests exercise the HTTP and
    # journal contract with a fake runner, so keep the optimisation out of them.
    monkeypatch.setattr(runtime, "prewarm_async", lambda cid: None)
    monkeypatch.setattr(runtime, "check_available", lambda: None)
    monkeypatch.setattr("app.agent_bridge.runtime.get_runtime", lambda: runtime)
    monkeypatch.setattr("app.routers.agent.get_runtime", lambda: runtime)
    monkeypatch.setattr("app.agent_bridge.model.secrets_store.get_key", lambda _: "synthetic-provider-key")
    app.dependency_overrides[get_db] = sessions
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client, runtime, tokens, engine
    app.dependency_overrides.pop(get_db, None)
    engine.dispose()


def conversation(client):
    response = client.post("/api/v1/agent/conversations", json={"provider_id": "provider_a", "send_to_provider": True, "course_id": "course_a"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def turn(client, cid, key="send-1", text="作业什么时候截止"):
    response = client.post(f"/api/v1/agent/conversations/{cid}/turns", headers={"Idempotency-Key": key}, json={"text": text})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def internal(client, token, path, data):
    return client.post("/api/v1/agent/internal/" + path, headers={"Authorization": "Bearer " + token}, json=data)


def test_long_tool_history_is_limited_by_content_not_record_count(gateway, monkeypatch):
    client, runtime, tokens, _ = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    seen = []
    def respond(db, settings, messages, max_tokens, tools):
        seen.append(messages)
        return {'content': 'history preserved', 'tool_calls': []}
    monkeypatch.setattr(model, 'call_model', respond)
    messages = [{'role': 'user', 'content': [{'type': 'text', 'text': str(i)}]} for i in range(240)]
    response = internal(client, tokens[tid], 'model', {'conversation_id': cid, 'messages': messages})
    assert response.status_code == 200, response.text
    assert seen == [messages]
    oversized = [{'role': 'user', 'content': [{'type': 'text', 'text': 'x' * 250001}]}]
    rejected = internal(client, tokens[tid], 'model', {'conversation_id': cid, 'messages': oversized})
    assert rejected.status_code == 400
    assert rejected.json()['error']['code'] == 'BUDGET_EXCEEDED'
    assert len(seen) == 1


def test_native_and_domain_tools_share_budget_and_replay(gateway):
    client, runtime, tokens, _ = gateway
    runtime.store.limits = RunLimits(tool_calls=2)
    cid = conversation(client)
    tid = turn(client, cid)
    token = tokens[tid]
    lease = {'conversation_id': cid, 'call_id': 'child:one', 'name': 'read'}
    assert internal(client, token, 'tool-lease', lease).status_code == 200
    domain = {'conversation_id': cid, 'call_id': 'domain-two', 'name': 'courses.list', 'arguments': {}}
    domain_result = internal(client, token, 'tool', domain).json()
    assert domain_result['status'] == 'partial' and domain_result['error'] is None
    assert domain_result['data']['items'][0]['id'] == 'course_a'
    assert internal(client, token, 'tool-lease', lease).status_code == 200
    assert internal(client, token, 'tool', domain).json() == domain_result
    assert internal(client, token, 'tool-lease', lease | {'call_id': 'other', 'name': 'write'}).json()['error']['code'] == 'BUDGET_EXCEEDED'
    assert internal(client, token, 'tool-lease', lease | {'name': 'write'}).json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    assert internal(client, token, 'tool-lease', lease | {'conversation_id': 'other'}).status_code == 403
    assert internal(client, token, 'tool-lease', lease | {'name': 'campus_courses_list'}).status_code == 403


def test_child_controls_are_owned_and_do_not_claim_quiescence(gateway, monkeypatch):
    client, runtime, tokens, _ = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    token = tokens[tid]
    registered = internal(client, token, 'control-address', {'conversation_id': cid, 'url': 'http://127.0.0.1:19192'})
    assert registered.status_code == 200
    assert internal(client, token, 'control-address', {'conversation_id': 'other', 'url': 'http://127.0.0.1:19192'}).status_code == 403
    assert internal(client, token, 'control-address', {'conversation_id': cid, 'url': 'https://example.com'}).status_code == 400
    runtime.store.event(tid, 'subagent.started', {'parentSessionId': cid, 'childSessionId': 'child_a'})
    captured = []
    def respond(url, **kwargs):
        captured.append((url, kwargs))
        return type('Response', (), {'ok': True})()
    monkeypatch.setattr('app.agent_bridge.runtime.requests.post', respond)
    path = f'/api/v1/agent/turns/{tid}/subagents/child_a/interrupt'
    response = client.post(path, json={})
    assert response.status_code == 200 and response.json()['accepted']
    assert captured[0][1]['json'] == {'child_id': 'child_a', 'parent_id': cid}
    assert runtime.store.events(tid)['items'][-1]['kind'] == 'subagent.stop_requested'
    assert client.post(path.replace('child_a', 'unknown'), json={}).status_code == 404
    runtime.store.event(tid, 'subagent.finished', {'childSessionId': 'child_a', 'stopReason': 'aborted', 'status': 'error'})
    assert client.post(path, json={}).json()['already_finished'] is True
    assert len(captured) == 1
    client.cookies.clear()
    assert client.post(path, json={}).status_code == 404


def test_runtime_settings_preserve_credentials_and_resolve_for_owned_process(gateway, tmp_path, monkeypatch):
    client, runtime, _, _ = gateway
    monkeypatch.setattr(secrets_store, 'SECRETS_DIR', tmp_path / 'private')
    monkeypatch.setattr(secrets_store, 'get_key', REAL_GET_KEY)
    payload = {'workspace': str(tmp_path / 'workspace'), 'skill_dirs': [str(tmp_path / 'skills')],
        'search': {'key_mode': 'separate', 'api_key': 'fixture-search-credential'},
        'mcp_servers': [{'serverName': 'example', 'command': 'python', 'env': {'TOKEN': 'fixture-mcp-credential'}}]}
    saved = client.put('/api/v1/agent/settings', json=payload)
    assert saved.status_code == 200, saved.text
    assert 'fixture-search-credential' not in saved.text and 'fixture-mcp-credential' not in saved.text
    assert saved.json()['search']['has_api_key'] is True
    assert saved.json()['mcp_servers'][0]['env']['TOKEN'] == '__SAVED_CREDENTIAL__'
    assert 'fixture-' not in runtime.settings.path.read_text(encoding='utf-8')
    public = saved.json()
    public['search'].pop('has_api_key')
    assert client.put('/api/v1/agent/settings', json=public).status_code == 200
    assert runtime.settings.resolved()['mcp_servers'][0]['env']['TOKEN'] == 'fixture-mcp-credential'
    assert runtime.settings.resolved()['search']['key'] == 'fixture-search-credential'
    public['search']['clear_key'] = True
    assert client.put('/api/v1/agent/settings', json=public).json()['search']['has_api_key'] is False


def test_goal_pause_uses_current_owned_revision_and_never_claims_completion(gateway, monkeypatch):
    client, runtime, tokens, _ = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    token = tokens[tid]
    internal(client, token, 'control-address', {'conversation_id': cid, 'url': 'http://127.0.0.1:19192'})
    goal = {'id': 'goal_actual', 'revision': 3, 'phase': 'active', 'activation': 'armed',
            'objective': '整理资料', 'roundsStarted': 1, 'maxGoalRounds': 4}
    calls = []
    def respond(url, **kwargs):
        calls.append((url, kwargs))
        payload = kwargs['json']
        status = 409 if payload.get('operation') == 'pause' and payload.get('revision') != goal['revision'] else 200
        if status == 200 and payload.get('operation') == 'pause':
            goal.update(phase='paused', activation='disarmed', revision=4)
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps({'goal': goal, 'agent_status': 'running'}).encode()
        return response
    monkeypatch.setattr('app.agent_bridge.runtime.requests.post', respond)
    path = f'/api/v1/agent/turns/{tid}/goal/pause'
    read = internal(client, token, 'goal-state', {'conversation_id': cid})
    assert read.json()['goal']['phase'] == 'active'
    assert internal(client, token, 'goal-state', {'conversation_id': 'other'}).status_code == 403
    assert client.post(path, json={'id': goal['id'], 'revision': 2}).json()['error']['code'] == 'GOAL_CHANGED'
    paused = client.post(path, json={'id': goal['id'], 'revision': 3})
    assert paused.status_code == 200 and paused.json()['goal']['phase'] == 'paused'
    assert runtime.store.turn(tid)['status'] == 'running', 'Pause acknowledgement is not process quiescence'
    assert runtime.store.events(tid)['items'][-1]['data']['goal']['phase'] == 'paused'
    assert calls[-1][1]['headers']['Authorization'] == 'Bearer ' + token
    client.cookies.clear()
    assert client.post(path, json={'id': goal['id'], 'revision': 4}).status_code == 404
    runtime.store.finish(tid, 'succeeded', response='进度已保存')
    assert internal(client, token, 'goal-state', {'conversation_id': cid}).status_code == 403
    with pytest.raises(ApiError) as ended:
        runtime.goal_control(tid, {'operation': 'read'})
    assert ended.value.code == 'TURN_STOPPED'


def test_goal_resume_admission_is_durable_owned_and_idempotent(gateway):
    client, runtime, _, _ = gateway
    cid = conversation(client)
    path = f'/api/v1/agent/conversations/{cid}/goal/resume'
    payload = {'id': 'goal_saved', 'revision': 4, 'maxGoalRounds': 66}
    headers = {'Idempotency-Key': 'resume-saved'}
    resumed = client.post(path, json=payload, headers=headers)
    assert resumed.status_code == 202, resumed.text
    tid = resumed.json()['id']
    assert runtime.store.events(tid)['items'][0]['kind'] == 'accepted_goal_resume'
    assert runtime.store.events(tid)['items'][0]['data'] == payload
    assert client.post(path, json=payload, headers=headers).json()['id'] == tid
    changed = client.post(path, json=payload | {'revision': 5}, headers=headers)
    assert changed.status_code == 409 and changed.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    client.cookies.clear()
    assert client.post(path, json=payload, headers=headers).status_code == 404


def test_mcp_settings_actual_connection_diagnostic(gateway, tmp_path):
    import sys
    from app.config import REPO_ROOT
    client, _, _, _ = gateway
    payload = {'mcp_servers': [{'serverName': 'calculator', 'command': sys.executable,
        'args': ['-u', str(REPO_ROOT / 'tools/fixtures/mcp_calculator.py'), str(tmp_path / 'mcp-audit.jsonl')]}]}
    assert client.put('/api/v1/agent/settings', json=payload).status_code == 200
    response = client.post('/api/v1/agent/settings/mcp/calculator/test', json={})
    assert response.status_code == 200, response.text
    assert response.json()['ok'] is True, response.text
    assert response.json()['tool_names'] == ['multiply']
    assert 'tools/list' in (tmp_path / 'mcp-audit.jsonl').read_text(encoding='utf-8')
    assert client.post('/api/v1/agent/settings/mcp/missing/test', json={}).status_code == 404


def test_search_settings_actual_provider_protocol(gateway, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    captured = []
    class SearchFixture(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            captured.append({'path': self.path, 'key': self.headers.get('x-api-key'), 'body': body})
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'content': [{'type': 'web_search_tool_result', 'content': [
                {'type': 'web_search_result', 'url': 'https://example.com/source', 'title': 'Controlled search source'}]}]}).encode())
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), SearchFixture)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    client, _, _, _ = gateway
    try:
        assert client.put('/api/v1/agent/settings', json={'search': {'base_url': f'http://127.0.0.1:{server.server_port}', 'model': 'fixture-search'}}).status_code == 200
        checked = client.post('/api/v1/agent/settings/search/test', json={'provider_id': 'provider_a'})
        assert checked.status_code == 200 and checked.json()['ok'], checked.text
        assert checked.json()['source_count'] == 1
        assert captured[0]['path'] == '/messages'
        assert captured[0]['key'] == 'synthetic-provider-key'
        assert captured[0]['body']['tools'][0]['type'] == 'web_search_20250305'
        assert 'synthetic-provider-key' not in checked.text
        monkeypatch.setattr(secrets_store, 'get_key', lambda _: None)
        missing = client.post('/api/v1/agent/settings/search/test', json={'provider_id': 'provider_a'}).json()
        assert not missing['ok'] and missing['error']['code'] == 'SEARCH_KEY_MISSING'
    finally:
        server.shutdown(); server.server_close(); worker.join(timeout=5)


def test_settings_transport_validation_and_local_origin(gateway):
    client, _, _, _ = gateway
    assert client.put('/api/v1/agent/settings', json={'mcp_servers': [{'serverName': 'bad', 'command': ''}]}).status_code == 400
    assert client.put('/api/v1/agent/settings', json={'mcp_servers': [
        {'serverName': 'same', 'command': 'python'}, {'serverName': 'same', 'command': 'node'}]}).status_code == 400
    assert client.get('/api/v1/agent/settings', headers={'Origin': 'https://unrelated.example'}).status_code == 403


def test_consent_and_browser_ownership(gateway):
    client, _, _, _ = gateway
    denied = client.post("/api/v1/agent/conversations", json={"provider_id": "provider_a"})
    assert denied.status_code == 403
    cid = conversation(client)
    assert any(c.name == "campus_agent_owner" and c.has_nonstandard_attr("HttpOnly") for c in client.cookies.jar)
    client.cookies.clear()
    assert client.get(f"/api/v1/agent/conversations/{cid}").status_code == 404
    assert client.get("/api/v1/agent/conversations").json()["items"] == []


def test_browser_acceptance_and_runtime_apply_revoke(gateway):
    from datetime import datetime, timedelta, timezone
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    day = datetime.now(timezone(timedelta(hours=8))).date() + timedelta(days=1)
    with Session(engine) as db:
        db.add(m.LearningTask(id="task_a", course_id="course_a", title="合成任务", type="reading", estimated_minutes=30))
        db.add(m.Availability(id=1, timezone="Asia/Shanghai", weekly_windows=[{"weekday": day.isoweekday(), "start": "18:00", "end": "20:00"}], date_exceptions=[], daily_limit_minutes=120))
        db.commit()
    tid = turn(client, cid)
    token = tokens[tid]
    def call(name, arguments, call_id):
        return internal(client, token, "tool", {"conversation_id": cid, "call_id": call_id, "name": name, "arguments": arguments}).json()
    preview = call("plans.preview", {"period": "tomorrow"}, "preview")
    action_id = preview["data"]["action"]["id"]
    assert client.post(f"/api/v1/agent/conversations/{cid}/turns", json={"text": "按这个来", "action_id": action_id},
                       headers={"Idempotency-Key": "unaccepted"}).status_code == 403
    assert call("plans.apply", {"action_id": action_id}, "unauthorized")["error"]["code"] == "APPROVAL_REQUIRED"
    url = f"/api/v1/agent/conversations/{cid}/actions/{action_id}/accept"
    assert client.post(url, json={"operation": "apply"}, headers={"Idempotency-Key": "accept", "Origin": "https://foreign.example"}).status_code == 403
    accepted = client.post(url, json={"operation": "apply"}, headers={"Idempotency-Key": "accept"})
    assert accepted.status_code == 200 and accepted.json()["status"] == "approved"
    applied = call("plans.apply", {"action_id": action_id}, "apply")
    assert applied["data"]["receipt"]["plan"]["status"] == "active"
    revoke = client.post(url, json={"operation": "revoke"}, headers={"Idempotency-Key": "revoke"}).json()
    assert revoke["kind"] == "revoke" and revoke["status"] == "approved"
    assert call("plans.revoke", {"action_id": revoke["id"]}, "revoke")["data"]["receipt"]["plan"]["status"] == "revoked"
    assert len(client.get(f"/api/v1/agent/conversations/{cid}/actions").json()["items"]) == 2
    runtime.store.finish(tid, "succeeded", response="done")
    body = {"text": "读取刚才操作的回执", "action_id": revoke["id"]}
    continuation = client.post(f"/api/v1/agent/conversations/{cid}/turns", json=body, headers={"Idempotency-Key": "accepted-turn"})
    assert continuation.status_code == 202
    next_id = continuation.json()["id"]
    assert runtime.store.events(next_id, 0)["items"][0]["data"] == {"action_id": revoke["id"], "tool": "plans.revoke"}
    assert client.post(f"/api/v1/agent/conversations/{cid}/turns", json=body, headers={"Idempotency-Key": "accepted-turn"}).json()["id"] == next_id
    assert client.post(f"/api/v1/agent/conversations/{cid}/turns", json=body | {"action_id": action_id}, headers={"Idempotency-Key": "accepted-turn"}).status_code == 409
    client.cookies.clear()
    assert client.post(url, json={"operation": "apply"}, headers={"Idempotency-Key": "foreign"}).status_code == 404


def test_no_cross_origin_or_remote_host(gateway):
    client, _, _, _ = gateway
    for headers in ({"Origin": "https://attacker.example"}, {"Host": "attacker.example"}, {"Sec-Fetch-Site": "cross-site"}):
        assert client.get("/api/v1/agent/conversations", headers=headers).status_code == 403


def test_strict_inputs_and_duplicate_send(gateway):
    client, runtime, tokens, _ = gateway
    cid = conversation(client)
    bad = client.post(f"/api/v1/agent/conversations/{cid}/turns", headers={"Idempotency-Key": "bad"}, json={"text": "hello", "principal": "another"})
    assert bad.status_code == 400
    tid = turn(client, cid)
    assert turn(client, cid) == tid
    assert len(tokens) == 1
    changed = client.post(f"/api/v1/agent/conversations/{cid}/turns", headers={"Idempotency-Key": "send-1"}, json={"text": "other"})
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    busy = client.post(f"/api/v1/agent/conversations/{cid}/turns", headers={"Idempotency-Key": "send-2"}, json={"text": "other"})
    assert busy.status_code == 409
    assert runtime.store.conversation(cid)["settings"]["current_course_id"] == "course_a"


def test_explicit_context_clear_does_not_change_authorized_scope(gateway):
    client, runtime, _, _ = gateway
    cid = conversation(client)
    response = client.post(f"/api/v1/agent/conversations/{cid}/turns", headers={"Idempotency-Key": "clear"},
                           json={"text": "查询所有课程", "clear_course_context": True})
    assert response.status_code == 202
    settings = runtime.store.conversation(cid)["settings"]
    assert settings["current_course_id"] is None and settings["course_ids"] == ["course_a"]
    runtime.store.finish(response.json()["id"], "succeeded", response="synthetic")
    invalid = client.post(f"/api/v1/agent/conversations/{cid}/turns", headers={"Idempotency-Key": "bad-clear"},
                          json={"text": "hi", "course_id": "course_a", "clear_course_context": True})
    assert invalid.status_code == 400


def test_internal_auth_scope_contract_and_idempotent_call(gateway):
    client, runtime, tokens, _ = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    token = tokens[tid]
    assert client.post("/api/v1/agent/internal/schemas", json={}).status_code == 403
    assert len(internal(client, token, "schemas", {}).json()) == 24
    request = {"conversation_id": cid, "call_id": "one", "name": "courses.list", "arguments": {}}
    first = internal(client, token, "tool", request)
    assert first.status_code == 200
    assert first.json()["data"]["items"][0]["title"] == "测试课程"
    assert internal(client, token, "tool", request).json() == first.json()
    events = runtime.store.events(tid)["items"]
    assert len(events) == 1 and events[0]["kind"] == "tool_result"
    assert internal(client, token, "tool", request | {"arguments": {"limit": 1}}).status_code == 409
    assert internal(client, token, "tool", request | {"conversation_id": "other"}).status_code == 403
    failure = internal(client, token, "tool", request | {"call_id": "bad", "arguments": {"user_id": "other"}})
    assert failure.json()["status"] == "error" and failure.json()["error"]["code"] == "INVALID_INPUT"
    snapshot = client.get(f"/api/v1/agent/turns/{tid}").json()
    assert token not in json.dumps(snapshot) and "token_hash" not in json.dumps(snapshot)


def test_cancel_revokes_runtime_and_recovery_is_persistent(gateway):
    client, runtime, tokens, _ = gateway
    cid, tid = None, None
    cid = conversation(client)
    tid = turn(client, cid)
    assert client.post(f"/api/v1/agent/turns/{tid}/cancel").json()["status"] == "cancelled"
    assert internal(client, tokens[tid], "schemas", {}).status_code == 403
    runtime.store.finish(tid, "succeeded", response="late response")
    assert runtime.store.turn(tid)["status"] == "cancelled"
    tid2 = turn(client, cid, key="next")
    reopened = Store(runtime.store.path)
    assert reopened.turn(tid2)["status"] == "running"
    reopened.recover()
    assert reopened.turn(tid2)["status"] == "interrupted"
    with pytest.raises(ApiError):
        reopened.runtime(tokens[tid2])
    assert reopened.conversation(cid)["settings"]["current_course_id"] == "course_a"


def provider_reply(monkeypatch, message, status=200):
    captured = []

    def post(url, **kwargs):
        captured.append((url, kwargs))
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps({"choices": [{"message": message, "finish_reason": "stop"}]}).encode()
        return response

    monkeypatch.setattr(model.requests, "post", post)
    return captured


def test_real_provider_projection_and_key_boundary(gateway, monkeypatch):
    client, runtime, tokens, _ = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    sent = provider_reply(monkeypatch, {"content": None, "tool_calls": [{"id": "tc_1", "function": {"name": "campus_courses_list", "arguments": "{}"}}]})
    response = internal(client, tokens[tid], "model", {"conversation_id": cid, "messages": [
        {"role": "assistant", "content": [{"type": "tool-call", "id": "prior", "name": "campus_courses_list", "arguments": "{}"}]},
        {"role": "tool", "toolCallId": "prior", "content": [{"type": "text", "text": "real tool result"}]},
        {"role": "user", "content": [{"type": "text", "text": "继续"}]}],
        "tools": [next(s for s in model.registry_schemas(None) if s['name'] == 'campus_courses_list'),
                  {"name": "pwsh", "description": "Run configured PowerShell", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}],
        "attribution_headers": {"Authorization": "injected"}})
    assert response.status_code == 200, response.text
    assert response.json()["tool_calls"][0]["arguments"] == {}
    url, kwargs = sent[0]
    assert url == "http://127.0.0.1:19191/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer synthetic-provider-key"
    assert kwargs["allow_redirects"] is False
    assert kwargs["json"]["messages"][1]["tool_call_id"] == "prior"
    assert [s['function']['name'] for s in kwargs['json']['tools']] == ['campus_courses_list', 'pwsh']
    assert "synthetic-provider-key" not in json.dumps(client.get(f"/api/v1/agent/turns/{tid}").json())


def test_registry_schema_cannot_replace_domain_contract(gateway, monkeypatch):
    client, _, tokens, _ = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    sent = provider_reply(monkeypatch, {'content': 'not reached'})
    response = internal(client, tokens[tid], 'model', {'conversation_id': cid,
        'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]}],
        'tools': [{'name': 'campus_courses_list', 'parameters': {'type': 'object', 'properties': {'user_id': {'type': 'string'}}}}]})
    assert response.status_code == 400 and not sent


def test_native_provider_configuration_and_frozen_lease(gateway, monkeypatch):
    client, _, tokens, engine = gateway
    with Session(engine) as db:
        cfg = db.get(m.ProviderConfig, 'provider_a')
        cfg.base_url = 'https://api.deepseek.com/v1'
        cfg.model = 'deepseek-flash'
        db.commit()
        settings = {'provider': model.snapshot(cfg), 'send_to_provider': True}
        configuration = model.runtime_configuration(db, settings)
        assert configuration == {'provider': 'deepseek-official', 'model': 'deepseek-flash', 'key': 'synthetic-provider-key'}
    cid = conversation(client)
    tid = turn(client, cid)
    result = internal(client, tokens[tid], 'model-lease', {'conversation_id': cid})
    assert result.status_code == 200
    assert 'synthetic-provider-key' not in result.text
    with Session(engine) as db:
        db.get(m.ProviderConfig, 'provider_a').base_url = 'https://other.example/v1'
        db.commit()
    assert internal(client, tokens[tid], 'model-lease', {'conversation_id': cid}).status_code == 409


def test_configurable_budget_supports_longer_tasks(tmp_path):
    store = Store(tmp_path / 'long.sqlite', RunLimits(model_calls=16, tool_calls=40))
    cid = store.create('owner', {})['id']
    turn, _ = store.enqueue(cid, 'one', 'long task', None)
    assert store.running(turn['id'])
    for _ in range(16):
        store.reserve_model(turn['id'])
    with pytest.raises(ApiError):
        store.reserve_model(turn['id'])
    for index in range(40):
        request = {'index': index}
        assert store.cached_call(turn['id'], str(index), request) is None
        store.cache_call(turn['id'], str(index), request, {'ok': True})
    with pytest.raises(ApiError):
        store.cached_call(turn['id'], 'forty-first', {})
    assert store.cached_call(turn['id'], '0', {'index': 0}) == {'ok': True}


@pytest.mark.parametrize("reply", [{"content": ""}, {"content": "secret body", "tool_calls": [{"id": "tc", "function": {"name": "shell", "arguments": "{}"}}]}, {"content": None, "tool_calls": [{"id": "tc", "function": {"name": "campus_courses_list", "arguments": "[]"}}]}])
def test_invalid_provider_response_is_scrubbed(gateway, monkeypatch, reply):
    client, _, tokens, _ = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    provider_reply(monkeypatch, reply)
    response = internal(client, tokens[tid], "model", {"conversation_id": cid, "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "PROVIDER_INVALID_RESPONSE"
    assert "secret body" not in response.text


def test_provider_change_blocks_sending_and_model_budget(gateway, monkeypatch):
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    sent = provider_reply(monkeypatch, {"content": "ok"})
    with Session(engine) as db:
        db.get(m.ProviderConfig, "provider_a").base_url = "https://other.example/v1"
        db.commit()
    response = internal(client, tokens[tid], "model", {"conversation_id": cid, "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]})
    assert response.status_code == 409 and not sent
    for _ in range(11):
        runtime.store.reserve_model(tid)
    with pytest.raises(ApiError, match="上限"):
        runtime.store.reserve_model(tid)


def test_runner_stderr_is_discarded_by_default_and_opt_in_capture_is_closed(tmp_path, monkeypatch):
    """A plugin-level failure is otherwise invisible; the opt-in path must not leak."""
    from app.agent_bridge import runtime as runtime_module

    instance = runtime_module.Runtime(Store(tmp_path / "journal.sqlite", RunLimits(model_calls=12)),
                                      tmp_path / "harness", "http://127.0.0.1:19190")
    monkeypatch.delenv("CAMPUS_RUNNER_STDERR", raising=False)
    assert instance._runner_stderr("turn_1") is runtime_module.subprocess.DEVNULL

    target = tmp_path / "runner.err"
    monkeypatch.setenv("CAMPUS_RUNNER_STDERR", str(target))
    capture = instance._runner_stderr("turn_2")
    try:
        assert capture is not runtime_module.subprocess.DEVNULL
        assert capture.name == str(target)
        assert capture.mode.startswith("a"), "diagnostics append instead of truncating"
        instance.stderr_captures["turn_2"] = capture
        assert instance.stderr_captures["turn_2"] is capture
    finally:
        handle = instance.stderr_captures.pop("turn_2", None)
        if handle is not None:
            handle.close()
    assert capture.closed
