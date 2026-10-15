"""Auth/VPN follow-up regressions: mock-only, no personal profiles or credentials."""
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests

from connectors.adapters.base import AdapterError
from connectors.adapters.transport import RequestsTransport, TransportResponse
from connectors.adapters.xzzd import XzzdAdapter
from connectors.adapters.zykt import ZyktAdapter
from connectors.browser import controlled_login as cl
from connectors.browser.session_runner import BrowserSession
from connectors.campus.webvpn_adapter import WebVPNAdapter, HOST_TOKENS
from connectors.tests.fakes import ScriptedTransport


def cookie(**fields):
    return dict(name='route', value='synthetic-cookie', domain='webvpn.zju.edu.cn',
                path='/', secure=True, expires=None) | fields


# The live login page (2026-10-15): the POST is the serialized form plus per-page
# `_csrf`/`captcha_id` tokens, so a JSON-only fixture would no longer represent it.
LOGIN_PAGE_HTML = ('<html><form id="form"><input type="hidden" name="_csrf" value="csrf-fixture">'
                   '<input name="auth_type" type="hidden" value="local"/>'
                   '<input id="user_name" type="text" name="username">'
                   '<input type="hidden" name="needCaptcha" value="false">'
                   '<input type="hidden" name="captcha_id" value="captcha-fixture">'
                   '</form></html>')


def opener(body=None, *, error=None, final=None, status=200, login_page=None):
    transport = Mock()
    def open_response(req, **kwargs):
        if error:
            raise error
        response = Mock(code=status)
        response.geturl.return_value = final or req.full_url
        if login_page is not None and req.full_url.endswith('/login'):
            response.read.return_value = login_page.encode('utf-8')
        else:
            response.read.return_value = json.dumps(body).encode('utf-8')
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response
    transport.open.side_effect = open_response
    return transport


@pytest.mark.parametrize('payload', [{}, [], {'success': False}, {'success': 'true'},
    {'success': True, 'url': '/login'}, {'success': True, 'url': '/LOGIN'}])
def test_vpn_probe_unknown_or_negative_json_invalidates(payload):
    vpn = WebVPNAdapter(opener(payload))
    vpn.authenticated = True
    vpn.username = 'synthetic-user'
    assert vpn.probe() is False
    assert vpn.authenticated is False
    assert vpn.username is None


def test_restore_requires_real_positive_probe_and_atomic_cookie_replacement():
    transport = opener({'success': True})
    vpn = WebVPNAdapter(transport)
    assert vpn.restore_cookies([cookie()]) is True
    assert vpn.is_session_valid() is True
    transport.open.assert_called_once()
    assert vpn.restore_cookies([cookie(name='one'), cookie(domain='evil.example')]) is False
    assert not vpn.authenticated and not list(vpn.cookies)


@pytest.mark.parametrize('rows', [[], [cookie(expires=1)], [cookie(expires=float('nan'))],
                                  [cookie(domain='.zju.edu.cn')], [cookie(path='invalid')]])
def test_restore_empty_expired_or_unsafe_never_authenticates_or_probes(rows):
    transport = opener({'success': True})
    vpn = WebVPNAdapter(transport)
    assert not vpn.restore_cookies(rows)
    assert not vpn.authenticated and not list(vpn.cookies)
    transport.open.assert_not_called()


def test_probe_redirect_and_network_failure_are_not_success():
    for transport in (opener({'success': True}, final='https://webvpn.zju.edu.cn/login'),
                      opener(error=urllib.error.URLError('synthetic-token-in-error'))):
        vpn = WebVPNAdapter(transport)
        vpn.authenticated = True
        assert not vpn.probe()
        assert not vpn.is_session_valid()


def test_failed_account_switch_clears_old_identity_without_leaking_error():
    vpn = WebVPNAdapter(opener(error=OSError('synthetic-sensitive-error')))
    vpn.authenticated = True
    vpn.username = 'old-test-user'
    with pytest.raises(ValueError) as exc:
        vpn.login('new-test-user', 'synthetic-test-password')
    assert 'synthetic-sensitive-error' not in str(exc.value)
    assert not vpn.authenticated and vpn.username is None


@pytest.mark.parametrize('url', [
    'https://user:pass@zdbk.zju.edu.cn/a', 'https://zdbk.zju.edu.cn:8443/a',
    'ftp://zdbk.zju.edu.cn/a', '//zdbk.zju.edu.cn/a',
    'https://zdbk.zju.edu.cn/a#fragment', 'https://zdbk.zju.edu.cn\\@evil.example/a',
    'https://zdbk.zju.edu.cn/a\r\nX-test: y', 'https://evil.example/a',
])
def test_vpn_invalid_target_fails_before_network(url):
    transport = opener({'success': True})
    vpn = WebVPNAdapter(transport)
    vpn.authenticated = True
    with pytest.raises(ValueError):
        vpn.open(url)
    transport.open.assert_not_called()


def test_vpn_conversion_preserves_path_query_and_refuses_direct_fallback():
    vpn = WebVPNAdapter(opener())
    with pytest.raises(ValueError):
        vpn.open('https://zdbk.zju.edu.cn/jwglxt/')
    vpn.authenticated = True
    assert vpn.proxy_url('https://zdbk.zju.edu.cn/a%20b?x=1&x=2') == (
        'https://webvpn.zju.edu.cn/https/' + HOST_TOKENS['zdbk.zju.edu.cn'] + '/a%20b?x=1&x=2')
    assert '/http/' in vpn.proxy_url('http://zdbk.zju.edu.cn:80/a')
    vpn.session_expiry = time.time() - 1
    with pytest.raises(ValueError):
        vpn.proxy_url('https://zdbk.zju.edu.cn/a')
    assert not vpn.authenticated


def test_vpn_default_transport_direct_no_redirect_and_cookie_isolation(monkeypatch):
    monkeypatch.setenv('CAMPUS_NETWORK_POLICY', 'direct')
    monkeypatch.setattr('urllib.request.getproxies', lambda: {'https': 'http://127.0.0.1:1'})
    vpn = WebVPNAdapter()
    proxies = [h for h in vpn.opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert not proxies or all(not h.proxies for h in proxies)
    redirects = [h for h in vpn.opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
    assert redirects[0].redirect_request(None, None, 302, '', {}, '/login') is None
    assert vpn.restore_cookies([cookie(domain='zjuam.zju.edu.cn')]) is False
    other = WebVPNAdapter(opener({'success': True}))
    assert other.restore_cookies([cookie()])
    vpn.close()
    assert other.authenticated and len(other.cookies) == 1


def test_vpn_redirect_to_the_portal_login_ends_the_session_without_looping():
    """A portal-login redirect means our session died; a school redirect is the SSO handoff."""
    import urllib.error
    transport = opener(error=urllib.error.HTTPError(
        'https://webvpn.zju.edu.cn/x', 302, 'redirect',
        {'Location': 'https://webvpn.zju.edu.cn/login'}, None))
    vpn = WebVPNAdapter(transport)
    vpn.authenticated = True
    with pytest.raises(ValueError):
        vpn.open('https://zdbk.zju.edu.cn/a')
    transport.open.assert_called_once()
    assert not vpn.authenticated


def test_vpn_redirect_to_a_school_host_is_returned_for_the_sso_handoff():
    import urllib.error
    handoff = urllib.error.HTTPError(
        'https://webvpn.zju.edu.cn/x', 302, 'redirect',
        {'Location': 'https://webvpn.zju.edu.cn/https/' + HOST_TOKENS['zdbk.zju.edu.cn'] + '/jwglxt/a'}, None)
    transport = opener(error=handoff)
    vpn = WebVPNAdapter(transport)
    vpn.authenticated = True
    response = vpn.open('https://zdbk.zju.edu.cn/jwglxt/a')
    assert response.code == 302
    # A portal Location is translated back so the campus URL policy stays authoritative.
    assert response.headers['Location'] == 'https://zdbk.zju.edu.cn/jwglxt/a'
    assert vpn.authenticated, 'an SSO handoff must not invalidate the portal session'


def test_vpn_unmapped_redirect_targets_are_left_alone():
    vpn = WebVPNAdapter(opener())
    vpn.authenticated = True
    assert vpn.unmap_url('https://zdbk.zju.edu.cn/jwglxt/a?x=1') == 'https://zdbk.zju.edu.cn/jwglxt/a?x=1'
    assert vpn.unmap_url('https://webvpn.zju.edu.cn/https/unknown-token/a') == 'https://webvpn.zju.edu.cn/https/unknown-token/a'
    assert vpn.unmap_url('https://webvpn.zju.edu.cn/user/info') == 'https://webvpn.zju.edu.cn/user/info'


def test_api_transport_does_not_follow_auth_redirect_or_leak_query():
    session = Mock()
    session.request.return_value = Mock(status_code=302, text='')
    transport = RequestsTransport('https://courses.zju.edu.cn', session)
    state = XzzdAdapter(connection_id='test', transport=transport).check_connection()
    assert state['state'] == 'auth_required'
    assert session.request.call_args.kwargs['allow_redirects'] is False


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_xzzd_redirect_maps_to_auth_expired_with_recovery(status):
    adapter = XzzdAdapter(connection_id='test', transport=ScriptedTransport([TransportResponse(status, '')]))
    assert adapter.check_connection()['state'] == 'auth_required'
    adapter = XzzdAdapter(connection_id='test', transport=ScriptedTransport([TransportResponse(status, '')]))
    with pytest.raises(AdapterError) as exc:
        adapter.list_courses()
    assert exc.value.error['code'] == 'AUTH_EXPIRED'
    assert exc.value.error['details']['action']['type'] == 'user_login'


@pytest.mark.parametrize('payload', [{}, [], {'message': 'login required'}, {'success': False}, 'unexpected'])
def test_xzzd_unknown_json_does_not_claim_session_valid(payload):
    adapter = XzzdAdapter(connection_id='test', transport=ScriptedTransport([
        TransportResponse(200, json.dumps(payload))]))
    assert adapter.check_connection()['state'] == 'blocked'


def test_classify_redirected_json_and_401_not_ok():
    assert cl.classify_read_result(200, 'application/json', True, True) == 'auth_required'
    assert cl.classify_read_result(401, 'application/json', True, False) == 'auth_required'


def test_cookie_snapshot_preserves_expiry_secure_and_platform_scope():
    browser = BrowserSession(profile_dir=Path('unused-mock-profile'))
    expiry = int(time.time()) + 1000
    browser._snapshot = [
        cookie(domain='.zju.edu.cn', name='platform', expires=expiry),
        cookie(domain='courses.zju.edu.cn', name='session', path='/api', expires=-1),
        cookie(domain='.zju.edu.cn', name='iPlanetDirectoryPro'),
        cookie(domain='zjuam.zju.edu.cn', name='cas'),
        cookie(domain='evil.example', name='evil'),
        cookie(domain='classroom.zju.edu.cn', name='other-platform'),
        cookie(domain='courses.zju.edu.cn', name='expired', expires=1),
    ]
    with browser.session() as session:
        assert {c.name for c in session.cookies} == {'platform', 'session'}
        assert {c.domain for c in session.cookies} == {'courses.zju.edu.cn'}
        assert all(c.secure for c in session.cookies)
        assert session.cookies.get('platform') == 'synthetic-cookie'
        assert next(c for c in session.cookies if c.name == 'platform').expires == expiry
        for url in ('https://zjuam.zju.edu.cn/api', 'https://classroom.zju.edu.cn/api',
                    'http://courses.zju.edu.cn/api'):
            assert 'Cookie' not in session.prepare_request(requests.Request('GET', url)).headers
        assert 'Cookie' in session.prepare_request(requests.Request('GET', 'https://courses.zju.edu.cn/api')).headers
    browser._snapshot = [cookie(domain='zjuam.zju.edu.cn')]
    assert browser.session() is None


def test_browser_stop_discards_memory_but_not_profile():
    browser = BrowserSession(profile_dir=Path('unused-mock-profile'))
    browser._context = Mock()
    browser._pw = Mock()
    browser._snapshot = [cookie(domain='courses.zju.edu.cn')]
    browser.stop()
    assert browser.cookies_snapshot() == []
    assert browser.session() is None
    browser._context.close.assert_called_once()


def test_requests_probe_uses_same_direct_cookie_filter_and_closes_on_error():
    with patch('connectors.browser.controlled_login._requests_session') as factory:
        session = Mock()
        session.cookies = []
        factory.return_value = session
        session.request.side_effect = RuntimeError('synthetic-sensitive-error')
        result = cl.read_check_with_requests([cookie(domain='courses.zju.edu.cn')])
        assert result['verdict'] == 'network_error'
        assert 'synthetic-sensitive-error' not in json.dumps(result)
        session.close.assert_called()

@pytest.mark.parametrize('initial,after_refresh,expected,checks,refreshes', [
    (False, 'ok', 'ok', 1, 1),  # first session becomes available via official browser
    (True, None, 'ok', 1, 0),  # existing session: zero login/refresh
    (True, 'ok', 'ok', 2, 1),  # stale platform session reused through browser SSO
    (True, 'auth_required', 'auth_required', 2, 1),  # still invalid, never loop
])
def test_browser_reuse_exact_login_and_api_request_counts(initial, after_refresh, expected, checks, refreshes):
    from connectors.browser.session_runner import ConnectorService
    from connectors.tests.test_session_runner import FakeSessions
    sessions = FakeSessions(with_session=initial)
    sessions.login = Mock()  # no connector code may invoke a credential login
    def refresh():
        sessions.with_session = True
        return True
    sessions.refresh_for_check = Mock(side_effect=refresh)
    bodies = ([TransportResponse(302, '')] if initial and after_refresh else [])
    bodies += [TransportResponse(200, '{"courses":[]}') if after_refresh != 'auth_required'
               else TransportResponse(302, '')]
    transport = ScriptedTransport(bodies)
    service = ConnectorService(sessions, adapter_factory=lambda *_:
        XzzdAdapter(connection_id='mock', transport=transport))
    result = service.handle('check', {})[1]['result']
    assert result['state'] == expected
    assert len(transport.calls) == checks
    assert sessions.refresh_for_check.call_count == refreshes
    assert sessions.login.call_count == 0


def test_explicit_vpn_login_once_then_probe_and_reuse_request_counts():
    transport = opener({'success': True}, login_page=LOGIN_PAGE_HTML)
    vpn = WebVPNAdapter(transport)
    with patch('connectors.campus.webvpn_adapter.webvpn_encrypt_password', return_value='synthetic-wire'):
        assert vpn.login('synthetic-user', 'synthetic-password')
    assert transport.open.call_count == 2  # GET login page + one POST
    assert sum(call.args[0].full_url.endswith('/do-login') for call in transport.open.call_args_list) == 1
    assert vpn.session_expiry is None  # actual duration not falsely assumed
    assert vpn.probe()
    response = vpn.open('https://zdbk.zju.edu.cn/jwglxt/')
    response.close()
    assert transport.open.call_count == 4  # + probe + business request
    assert sum(call.args[0].full_url.endswith('/do-login') for call in transport.open.call_args_list) == 1
    transport.open.side_effect = opener({'success': False}).open.side_effect
    assert not vpn.probe()
    assert transport.open.call_count == 5
    with pytest.raises(ValueError):
        vpn.open('https://zdbk.zju.edu.cn/jwglxt/')
    assert transport.open.call_count == 5  # invalid never falls back/direct/relogs


@pytest.mark.parametrize('marker', [True, False])
def test_verify_existing_marker_requires_probe_and_never_forces_login_form(marker):
    context = Mock()
    context.browser.version = 'mock-browser'
    context.cookies.return_value = ([cookie(name='JWTUser', domain='courses.zju.edu.cn')] if marker else [])
    page = Mock(url='https://courses.zju.edu.cn/')
    with patch.object(cl, '_launch', return_value=(Mock(), context, page)), \
         patch.object(Path, 'mkdir'), \
         patch.object(cl, '_write_result'), \
         patch.object(cl, '_wait_for_session', return_value={'detected': False}) as wait:
        result = cl.run_verify(Path('unused-mock-profile'), 'https://courses.zju.edu.cn/user/login',
                               'mock-channel', 1, Path('unused-mock-output'), 'mock', 'both')
    assert result['verdict'] == 'not_detected_within_timeout'
    wait.assert_called_once_with(context, 1)
    assert page.goto.call_count == 1
    assert page.goto.call_args.args[0] == 'https://courses.zju.edu.cn/'


def test_browser_snapshot_is_defensive_copy():
    browser = BrowserSession(profile_dir=Path('unused-mock-profile'))
    browser._snapshot = [cookie(domain='courses.zju.edu.cn')]
    copy = browser.cookies_snapshot()
    copy[0]['domain'] = 'evil.example'
    assert browser.cookies_snapshot()[0]['domain'] == 'courses.zju.edu.cn'


def test_probe_closes_background_tab_on_success():
    context = Mock()
    context.cookies.return_value = []
    with patch.object(cl, 'read_check_in_page', return_value={'verdict': 'ok'}):
        assert cl._wait_for_session(context, 1)['detected'] is True
    context.new_page.return_value.close.assert_called_once()


def test_zykt_auth_redirect_does_not_report_unknown_protocol():
    adapter = ZyktAdapter(connection_id='mock', transport=ScriptedTransport([TransportResponse(302, '')]))
    assert adapter.check_connection()['state'] == 'auth_required'

@pytest.mark.parametrize('path', ['https://evil.example/a', '.evil.example/a', '//evil.example/a',
                                   '/api\r\nHeader:x', '/api#fragment', '/api\\evil'])
def test_api_transport_rejects_unsafe_paths_before_token_callback(path):
    session = Mock()
    headers = Mock(return_value={'Authorization': 'synthetic-token'})
    transport = RequestsTransport('https://courses.zju.edu.cn', session, headers=headers)
    with pytest.raises(ValueError):
        transport.request('GET', path)
    headers.assert_not_called()
    session.request.assert_not_called()


def test_production_campus_transport_has_no_implicit_webvpn_channel():
    from connectors.campus.campus_network import CampusTransport
    from connectors.campus.campus_common import CampusError
    client = CampusTransport(policy='direct')
    assert not hasattr(client, 'uses_webvpn')
    with patch.object(client.opener, 'open') as network:
        with pytest.raises(CampusError) as exc:
            client.request('https://webvpn.zju.edu.cn/user/info')
        assert exc.value.code == 'UNSAFE_REDIRECT'
        network.assert_not_called()
    client.close()


def test_campus_close_clears_detached_client_auth_flags():
    from connectors.campus.campus import CampusClient
    client = CampusClient(policy='direct')
    client.authenticated = client.passport_authenticated = client.eta_authenticated = True
    client.jar.set_cookie(requests.cookies.create_cookie('session', 'synthetic', domain='zdbk.zju.edu.cn'))
    client.close()
    assert not list(client.jar)
    assert not client.authenticated and not client.passport_authenticated and not client.eta_authenticated


def test_connector_unexpected_exception_never_echoes_sensitive_text():
    from connectors.browser.session_runner import ConnectorService
    from connectors.tests.test_session_runner import FakeSessions
    def fail(*args):
        raise RuntimeError('synthetic-cookie-password-ticket-error')
    service = ConnectorService(FakeSessions(), adapter_factory=fail)
    status, body = service.handle('check', {})
    assert status == 500
    assert body['error']['code'] == 'INTERNAL_ERROR'
    assert 'synthetic-cookie-password-ticket-error' not in json.dumps(body)


def test_verify_unexpected_exception_never_persists_sensitive_text():
    with patch.object(cl, '_launch', side_effect=RuntimeError('synthetic-ticket-error')), \
         patch.object(Path, 'mkdir'), patch.object(cl, '_write_result') as write:
        result = cl.run_verify(Path('unused-mock-profile'), cl.LOGIN_ENTRY_URL, 'mock', 1,
                               Path('unused-mock-output'), 'mock', 'both')
    assert result['error'] == 'RuntimeError'
    assert 'synthetic-ticket-error' not in json.dumps(result)
    assert 'synthetic-ticket-error' not in json.dumps(write.call_args.args[2])


@pytest.mark.parametrize('code', [401, '401', 419])
def test_xzzd_json_auth_error_never_claims_valid_even_with_list(code):
    adapter = XzzdAdapter(connection_id='test', transport=ScriptedTransport([
        TransportResponse(200, json.dumps({'code': code, 'courses': []}))]))
    assert adapter.check_connection()['state'] == 'auth_required'


def test_xzzd_json_failure_never_claims_valid_even_with_list():
    adapter = XzzdAdapter(connection_id='test', transport=ScriptedTransport([
        TransportResponse(200, '{"success":false,"courses":[]}')]))
    assert adapter.check_connection()['state'] == 'blocked'
