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
@pytest.mark.parametrize('name,args', [
    ('browser_evaluate', {'function': 'untrusted fixture'}),
    ('browser_run_code_unsafe', {}), ('browser_file_upload', {'paths': ['private.txt']}),
    ('browser_drop', {}), ('browser_drag', {}), ('browser_handle_dialog', {'accept': True}),
])
def test_browser_blocked_tools_fail_before_process_or_network(tmp_path, monkeypatch, name, args):
    host = BrowserHost(tmp_path / 'browser')
    calls = []
    monkeypatch.setattr(host, '_start', lambda: calls.append('start'))
    with pytest.raises(ApiError) as error:
        host.rpc('fixture', 'call', name=name, arguments=args, turn_id='fixture-turn')
    assert error.value.code == 'BROWSER_APPROVAL_REQUIRED'
    assert error.value.status_code == 403
    assert calls == [] and host.process is None


def test_browser_readonly_and_dialog_rejection_validation():
    from app.agent_bridge.browser import validate_model_browser_call
    for name, args in [('browser_snapshot', {}), ('browser_handle_dialog', {'accept': False}),
                       ('browser_type', {'text': 'fixture', 'submit': False})]:
        validate_model_browser_call(name, args)


def test_private_campus_cookie_handoff_no_boot_or_generic_domains(tmp_path, monkeypatch):
    host = BrowserHost(tmp_path / 'browser')
    calls = []
    monkeypatch.setattr(host, '_start', lambda: calls.append('boot'))
    with pytest.raises(ApiError) as error:
        host.campus_cookies_private()
    assert error.value.code == 'BROWSER_AUTH_REQUIRED'
    assert calls == []
    host.process = type('FakeLive', (), {'poll': lambda self: None})()
    def rpc(cid, method, **kwargs):
        calls.append((cid, method, kwargs))
        return {'cookies': [
            {'domain': '.zjuam.zju.edu.cn', 'name': 'CASTGC', 'value': 'synthetic', 'httpOnly': True},
            {'domain': 'zdbk.zju.edu.cn', 'name': 'sid', 'value': 'synthetic2'},
            {'domain': 'zju.edu.cn', 'name': 'parent', 'value': 'synthetic3'},
            {'domain': 'evilzju.edu.cn', 'name': 'bad', 'value': 'never-import'},
            {'domain': 'other.zju.edu.cn', 'name': 'other', 'value': 'never-import'},
        ]}
    monkeypatch.setattr(host, 'rpc', rpc)
    assert [c['name'] for c in host.campus_cookies_private()] == ['CASTGC', 'sid', 'parent']
    assert calls == [('campus-session', 'campus-session', {})]


@pytest.mark.parametrize('part', [None, 'request-body', 'request-headers', 'response-headers'])
def test_browser_request_details_do_not_expose_human_authentication(part):
    from app.agent_bridge.browser import validate_model_browser_call
    with pytest.raises(ApiError) as error:
        validate_model_browser_call('browser_network_request', {'index': 1, **({'part': part} if part else {})})
    assert error.value.code == 'BROWSER_APPROVAL_REQUIRED'
    validate_model_browser_call('browser_network_request', {'index': 1, 'part': 'response-body'})


def test_private_campus_session_rpc_cannot_restart_dead_daemon(tmp_path, monkeypatch):
    host = BrowserHost(tmp_path / 'browser')
    calls = []
    monkeypatch.setattr(host, '_start', lambda: calls.append('boot'))
    with pytest.raises(ApiError) as error:
        host.rpc('campus-session', 'campus-session')
    assert error.value.code == 'BROWSER_AUTH_REQUIRED'
    assert calls == []
