"""Owner/turn boundary and immutable browser downloads, without paid services."""
import json
import pytest
from fastapi.testclient import TestClient

from app.agent_bridge.browser import deliver_downloads, BrowserHost
from app.errors import ApiError
from app.main import app
from test_agent_gateway import gateway, conversation, turn, internal  # noqa: F401


def test_browser_owner_and_runtime_scope(gateway, monkeypatch):
    client, runtime, tokens, _ = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    calls = []
    def rpc(cid, method, **args):
        calls.append((cid, method, args))
        return {'running': False, 'headed': False, 'pages': [], 'reviews': [], 'downloads': []}
    monkeypatch.setattr(runtime.browser, 'rpc', rpc)
    base = f'/api/v1/agent/conversations/{cid}/browser'
    assert client.get(base).status_code == 200
    assert client.post(base + '/open', json={'url': 'file:///C:/secret'}).status_code == 400
    assert client.post(base + '/open', json={'url': 'https://example.com'}).status_code == 200
    assert calls[-1] == (cid, 'open', {'url': 'https://example.com'})
    stranger = TestClient(app, base_url='http://127.0.0.1')
    try:
        assert stranger.get(base).status_code == 404
    finally:
        stranger.close()
    token = tokens[tid]
    assert internal(client, token, 'browser', {'conversation_id': 'other', 'method': 'call', 'name': 'browser_snapshot'}).status_code == 403
    assert internal(client, token, 'browser', {'conversation_id': cid, 'method': 'confirm', 'action_id': 'x'}).status_code == 400
    assert internal(client, token, 'browser', {'conversation_id': cid, 'method': 'tools'}).status_code == 200
    assert calls[-1][2]['turn_id'] == tid
    runtime.store.finish(tid, 'cancelled')
    assert internal(client, token, 'browser', {'conversation_id': cid, 'method': 'tools'}).status_code == 403


def test_owned_browser_download_publishes_once_and_preserves_snapshot(gateway):
    client, runtime, _, _ = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    path = runtime.browser.home / 'downloads' / cid / 'download-one' / '附件.txt'
    path.parent.mkdir(parents=True)
    path.write_text('原始下载-937', encoding='utf-8')
    def status():
        return {'downloads': [{'id': 'download-one', 'path': str(path), 'name': path.name}]}
    assert deliver_downloads(runtime, cid, status(), tid) == {}
    file = runtime.files.list(cid)[0]
    path.write_text('工作副本后来修改', encoding='utf-8')
    deliver_downloads(runtime, cid, status(), tid)
    assert len(runtime.files.list(cid)) == 1
    assert runtime.files.get(cid, file['id'])[1].read_text(encoding='utf-8') == '原始下载-937'
    published = [e for e in runtime.store.events(tid)['items'] if e['kind'] == 'file.published']
    assert len(published) == 1 and published[0]['data']['file']['id'] == file['id']
    outside = runtime.browser.home / 'unrelated.txt'
    outside.write_text('outside', encoding='utf-8')
    deliver_downloads(runtime, cid, {'downloads': [{'id': 'outside', 'path': str(outside), 'name': 'outside.txt'}]}, tid)
    assert len(runtime.files.list(cid)) == 1


def test_browser_error_and_restart_receipt_do_not_start_or_repeat_actions(tmp_path, monkeypatch):
    host = BrowserHost(tmp_path / 'browser')
    host.home.mkdir()
    (host.home / 'actions.json').write_text(json.dumps({'reviews': [{'id': 'review-one', 'conversation_id': 'conv-one',
        'status': 'executing', 'call': {'name': 'private-action'}, 'fingerprint': 'private-fingerprint', 'private_state': 'private-state',
        'preview': {'page_url': 'https://example.com'}, 'created_at': ''}], 'downloads': []}), encoding='utf-8')
    restored = host.peek('conv-one')
    assert restored['reviews'][0]['status'] == 'uncertain' and host.process is None
    assert 'private-' not in json.dumps(restored)
    assert host.peek('other')['reviews'] == []
    monkeypatch.setattr(host, '_start', lambda: None)
    class FailedResponse:
        ok = False
        def json(self):
            return {'ok': False, 'code': 'BROWSER_ACTION_CHANGED', 'message': '目标已变化'}
    monkeypatch.setattr('app.agent_bridge.browser.requests.post', lambda *a, **k: FailedResponse())
    with pytest.raises(ApiError) as raised:
        host.rpc('conv-one', 'confirm', action_id='review-one')
    assert raised.value.code == 'BROWSER_ACTION_CHANGED' and raised.value.retryable is False
