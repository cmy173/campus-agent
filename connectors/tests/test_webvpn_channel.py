"""WebVPN background channel: channel choice, bounded re-auth, no leakage.

Everything here is loopback/synthetic: no school host is contacted and the portal
credential is a fixture string. The production code under test is
`CampusTransport` + `webvpn_tunnel.WebVPNTunnel` + `WebVPNAdapter`.
"""
from __future__ import annotations

import json
import re

import pytest

from connectors.campus import webvpn_tunnel
from connectors.campus.campus_common import CampusError
from connectors.campus.campus_network import CampusTransport
from connectors.campus.webvpn_adapter import HOST_TOKENS, WebVPNAdapter, WebVPNError

PORTAL_TOKEN = HOST_TOKENS["zdbk.zju.edu.cn"]
PORTAL_LOGIN = "https://webvpn.zju.edu.cn/https/" + PORTAL_TOKEN + "/jwglxt/notice"
USER, SECRET = "webvpn-synthetic-user", "webvpn-synthetic-password"

# The live login page (2026-10-15) posts its whole serialized form, including the
# per-page `_csrf`/`captcha_id` tokens. A fixture without them would hide the exact
# defect this file guards (HTTP 400 because `username`/`_csrf` were missing).
LOGIN_PAGE = ('<html><body><form id="form">'
              '<input type="hidden" name="_csrf" value="csrf-fixture">'
              '<input name="auth_type" type="hidden" value="local"/>'
              '<input id="user_name" type="text" name="username">'
              '<input name="sms_code" type="text" id="phone-code-input">'
              '<input type="password" name="password">'
              '<input type="text" name="captcha">'
              '<input type="hidden" name="needCaptcha" value="false">'
              '<input type="hidden" name="captcha_id" value="captcha-fixture">'
              '</form></body></html>')
CAPTCHA_PAGE = LOGIN_PAGE.replace('name="needCaptcha" value="false"', 'name="needCaptcha" value="true"')


class FakeResponse:
    def __init__(self, url, body=b"{}", status=200, headers=None):
        self.code, self._url, self._body = status, url, body
        self.headers = headers or {"Content-Type": "application/json"}

    def geturl(self):
        return self._url

    def read(self, limit=-1):
        return self._body[:limit] if limit and limit > 0 else self._body

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    """Scripted portal transport; records every request it was asked to make."""

    def __init__(self, plan=None):
        self.plan = list(plan or [])
        self.requests = []
        self.cookiejar = None

    def open(self, request, timeout=None):
        url = request.full_url
        self.requests.append({"url": url, "data": request.data, "timeout": timeout})
        step = self.plan.pop(0) if self.plan else ("body", b"{}")
        kind, payload = step
        if kind == "raise":
            raise payload
        return FakeResponse(url, payload)

    @property
    def posts(self):
        return [item for item in self.requests if item["data"]]


LOGIN_GET = ("body", LOGIN_PAGE.encode())
LOGIN_GET_CAPTCHA = ("body", CAPTCHA_PAGE.encode())
LOGIN_OK = ("body", json.dumps({"success": True}).encode())
LOGIN_BAD = ("body", json.dumps({"success": False, "error": "INVALID_ACCOUNT",
                                 "message": "用户名或密码错误"}).encode())
LOGIN_CAPTCHA = ("body", json.dumps({"success": False, "error": "CAPTCHA_FAILED",
                                     "message": "请输入验证码"}).encode())
LOGIN_TWO_STEP = ("body", json.dumps({"success": False, "error": "NEED_TWO_STEP",
                                      "message": "请输入短信验证码", "phone": "13800000000"}).encode())
LOGIN_NEED_CONFIRM = ("body", json.dumps({"success": False, "error": "NEED_CONFIRM",
                                          "message": "该账号已经登录"}).encode())
CONFIRM_OK = ("body", json.dumps({"success": True, "url": "/"}).encode())


@pytest.fixture(autouse=True)
def clean_channel():
    webvpn_tunnel.reset_tunnel()
    webvpn_tunnel.register_credential_provider(None)
    yield
    webvpn_tunnel.register_credential_provider(None)
    webvpn_tunnel.reset_tunnel()


def channel(plan, *, credentials=True, provider=None):
    opener = FakeOpener(plan)
    tunnel = webvpn_tunnel.WebVPNTunnel(adapter_factory=lambda: WebVPNAdapter(opener))
    webvpn_tunnel.reset_tunnel(tunnel)
    if provider is None and credentials:
        provider = lambda: {"username": USER, "password": SECRET, "auto_relogin": True}  # noqa: E731
    webvpn_tunnel.register_credential_provider(provider)
    return tunnel, opener


def transport(monkeypatch, *, reachable):
    monkeypatch.setattr("connectors.campus.campus_network.host_reachable",
                        lambda host, **kwargs: reachable(host))
    return CampusTransport(policy="direct")


def test_without_a_stored_credential_nothing_is_probed_or_rewritten(monkeypatch):
    client = transport(monkeypatch, reachable=lambda host: pytest.fail("must not probe without a credential"))
    try:
        assert client.channel_for("zdbk.zju.edu.cn") == "direct"
        assert client.channel_for("eta.zju.edu.cn") == "direct"
        assert client.cookies_for("zdbk.zju.edu.cn") is client.jar
    finally:
        client.close()


def test_off_campus_uses_the_portal_for_registered_hosts_only(monkeypatch):
    tunnel, opener = channel([LOGIN_GET, LOGIN_OK, ("body", b'{"ok":true}')])
    client = transport(monkeypatch, reachable=lambda host: host == "zjuam.zju.edu.cn")
    try:
        assert client.channel_for("zdbk.zju.edu.cn") == "webvpn"
        assert client.channel_for("eta.zju.edu.cn") == "direct"  # no registered token
        assert client.channel_for("zjuam.zju.edu.cn") == "direct"
        status, _, raw = client.request("https://zdbk.zju.edu.cn/jwglxt/notice")
        assert status == 200 and raw == b'{"ok":true}'
        assert opener.requests[-1]["url"].startswith("https://webvpn.zju.edu.cn/https/" + PORTAL_TOKEN)
        assert client.last_channel == "webvpn"
        assert len(opener.posts) == 1 and opener.posts[0]["url"].endswith("/do-login")
        assert SECRET.encode() not in opener.posts[0]["data"]
    finally:
        client.close()


def test_on_campus_never_touches_the_portal(monkeypatch):
    tunnel, opener = channel([("body", b"unused")])
    client = transport(monkeypatch, reachable=lambda host: True)
    try:
        assert client.channel_for("zdbk.zju.edu.cn") == "direct"
        assert opener.requests == []
        assert tunnel.status()["authenticated"] is False
    finally:
        client.close()


def test_the_portal_url_is_never_an_acceptable_target(monkeypatch):
    tunnel, opener = channel([("body", b"{}")])
    client = transport(monkeypatch, reachable=lambda host: False)
    try:
        with pytest.raises(CampusError) as error:
            client.request("https://webvpn.zju.edu.cn/user/info")
        assert error.value.code == "UNSAFE_REDIRECT"
        assert opener.requests == []
    finally:
        client.close()


def test_expired_portal_session_is_re_established_exactly_once():
    tunnel, opener = channel([LOGIN_GET, LOGIN_OK, ("raise", WebVPNError("WEBVPN_SESSION_EXPIRED", "expired")),
                              LOGIN_GET, LOGIN_OK, ("body", b'{"ok":1}')])
    response = tunnel.request("https://zdbk.zju.edu.cn/jwglxt/a")
    try:
        assert response.read() == b'{"ok":1}'
    finally:
        response.close()
    assert sum(item["url"].endswith("/do-login") for item in opener.requests) == 2
    assert tunnel.attempts == 2


def test_a_second_expiry_is_not_retried_again():
    tunnel, opener = channel([LOGIN_GET, LOGIN_OK, ("raise", WebVPNError("WEBVPN_SESSION_EXPIRED", "expired")),
                              LOGIN_GET, LOGIN_OK, ("raise", WebVPNError("WEBVPN_SESSION_EXPIRED", "expired"))])
    with pytest.raises(CampusError) as error:
        tunnel.request("https://zdbk.zju.edu.cn/jwglxt/a")
    assert error.value.code == "WEBVPN_SESSION_EXPIRED"
    assert sum(item["url"].endswith("/do-login") for item in opener.requests) == 2


def test_rejected_portal_credential_enters_cooldown_without_retrying():
    tunnel, opener = channel([LOGIN_GET, LOGIN_BAD])
    with pytest.raises(CampusError) as error:
        tunnel.ensure_session()
    assert error.value.code == "WEBVPN_CREDENTIAL_REJECTED"
    before = len(opener.requests)
    with pytest.raises(CampusError) as second:
        tunnel.ensure_session()
    assert second.value.code == "WEBVPN_COOLDOWN"
    assert len(opener.requests) == before, "a cooldown must not touch the school again"


def test_explicit_verification_bypasses_the_cooldown_once():
    tunnel, opener = channel([LOGIN_GET, LOGIN_BAD, LOGIN_GET, LOGIN_OK])
    assert webvpn_tunnel.probe_login()["code"] == "WEBVPN_CREDENTIAL_REJECTED"
    assert webvpn_tunnel.probe_login() == {"ok": True, "code": "WEBVPN_LOGIN_OK", "needs_user": False}


def test_captcha_challenge_needs_one_manual_login():
    tunnel, opener = channel([LOGIN_GET, LOGIN_CAPTCHA])
    with pytest.raises(CampusError) as error:
        tunnel.ensure_session()
    assert error.value.code == "WEBVPN_CAPTCHA_REQUIRED"
    assert tunnel.status()["last_error"]["code"] == "WEBVPN_CAPTCHA_REQUIRED"


def test_password_never_appears_in_status_messages_or_records():
    tunnel, opener = channel([LOGIN_GET, LOGIN_BAD])
    with pytest.raises(CampusError) as error:
        tunnel.ensure_session()
    assert SECRET not in str(error.value)
    assert SECRET not in json.dumps(tunnel.status(), ensure_ascii=False)
    assert USER not in json.dumps(tunnel.status(), ensure_ascii=False)


def test_a_broken_credential_provider_disables_the_channel_instead_of_crashing():
    def broken():
        raise RuntimeError("synthetic-sensitive-provider-error")
    tunnel, opener = channel([], provider=broken)
    assert tunnel.configured() is False
    assert tunnel.uses_channel("zdbk.zju.edu.cn") is False
    with pytest.raises(CampusError) as error:
        tunnel.ensure_session()
    assert error.value.code == "WEBVPN_NOT_CONFIGURED"
    assert "synthetic-sensitive-provider-error" not in json.dumps(tunnel.status())


def test_portal_session_is_memory_only():
    tunnel, opener = channel([LOGIN_GET, LOGIN_OK])
    tunnel.ensure_session()
    assert tunnel.status()["authenticated"] is True
    tunnel.invalidate()
    assert tunnel.status()["authenticated"] is False
    assert not list(tunnel.session_jar() or [])


def test_the_login_post_carries_the_live_form_contract():
    """Regression for the HTTP 400: the portal needs the whole serialized form."""
    tunnel, opener = channel([LOGIN_GET, LOGIN_OK])
    assert tunnel.ensure_session() is not None
    posted = opener.posts[-1]
    assert posted["url"] == "https://webvpn.zju.edu.cn/do-login"
    body = posted["data"].decode()
    fields = dict(part.split("=", 1) for part in body.split("&"))
    assert fields["username"] == USER
    assert fields["_csrf"] == "csrf-fixture", "per-page CSRF token must be echoed back"
    assert fields["auth_type"] == "local"
    assert fields["needCaptcha"] == "false"
    assert fields["captcha_id"] == "captcha-fixture"
    assert fields["captcha"] == "" and fields["sms_code"] == ""
    # The password leaves the vault encrypted by the page's own AES-CFB helper.
    assert fields["password"] != SECRET and SECRET not in body
    assert re.fullmatch(r"[0-9a-f]+", fields["password"])
    assert fields["password"].startswith("77726476706e6973617765736f6d6521")


def test_a_rendered_captcha_stops_before_posting():
    tunnel, opener = channel([LOGIN_GET_CAPTCHA])
    with pytest.raises(CampusError) as error:
        tunnel.ensure_session()
    assert error.value.code == "WEBVPN_CAPTCHA_REQUIRED"
    assert not opener.posts, "a page that demands a captcha must not receive a login POST"


def test_an_account_already_signed_in_is_taken_over_once():
    """NEED_CONFIRM is the school's own one-click takeover; the page does the same."""
    tunnel, opener = channel([LOGIN_GET, LOGIN_NEED_CONFIRM, CONFIRM_OK])
    adapter = tunnel.ensure_session()
    assert adapter.replaced_other_session is True
    assert tunnel.status()["replaced_other_session"] is True
    # The takeover POST carries no body, so it is listed by URL rather than by payload.
    assert [item["url"].rsplit("/", 1)[-1] for item in opener.requests
            if item["url"].startswith("https://webvpn.zju.edu.cn/do-")] == ["do-login", "do-confirm-login"]


def test_second_factor_is_reported_as_mfa_not_as_a_wrong_password():
    tunnel, opener = channel([LOGIN_GET, LOGIN_TWO_STEP])
    with pytest.raises(CampusError) as error:
        tunnel.ensure_session()
    assert error.value.code == "WEBVPN_MFA_REQUIRED"
    assert tunnel.status()["last_error"]["code"] == "WEBVPN_MFA_REQUIRED"


def test_the_sso_host_never_uses_the_portal():
    """zjuam is public and authenticates us; proxying it would strand its cookie."""
    tunnel, opener = channel([LOGIN_GET, LOGIN_OK])
    tunnel.ensure_session()
    assert tunnel.uses_channel("zdbk.zju.edu.cn") is True
    assert tunnel.uses_channel("zjuam.zju.edu.cn") is False
    assert tunnel.status()["direct_only_hosts"] == ["zjuam.zju.edu.cn"]


def test_a_redirect_to_the_portal_login_page_ends_the_session():
    import urllib.error
    expired = urllib.error.HTTPError(
        "https://webvpn.zju.edu.cn/https/" + PORTAL_TOKEN + "/jwglxt/a", 302, "redirect",
        {"Location": "https://webvpn.zju.edu.cn/login?msg=timeout"}, None)
    tunnel, opener = channel([LOGIN_GET, LOGIN_OK, ("raise", expired), LOGIN_GET, LOGIN_OK,
                              ("raise", expired)])
    tunnel.ensure_session()
    with pytest.raises(CampusError) as error:
        tunnel.request("https://zdbk.zju.edu.cn/jwglxt/a")
    assert error.value.code == "WEBVPN_SESSION_EXPIRED"
    assert sum(item["url"].endswith("/do-login") for item in opener.requests) == 2


def test_a_campus_redirect_is_translated_back_for_the_sso_handoff():
    import urllib.error
    handoff = urllib.error.HTTPError(
        "https://webvpn.zju.edu.cn/https/" + PORTAL_TOKEN + "/jwglxt/a", 302, "redirect",
        {"Location": "https://webvpn.zju.edu.cn/https/" + PORTAL_TOKEN + "/jwglxt/b?x=1"}, None)
    tunnel, opener = channel([LOGIN_GET, LOGIN_OK, ("raise", handoff)])
    response = tunnel.request("https://zdbk.zju.edu.cn/jwglxt/a")
    try:
        assert response.code == 302
        assert response.headers["Location"] == "https://zdbk.zju.edu.cn/jwglxt/b?x=1"
    finally:
        response.close()


def test_a_rejected_login_payload_is_reported_as_a_protocol_problem():
    """Real observation: the portal answers HTTP 400 to our credential POST.

    That is neither a credential verdict nor a network fault, so it must not be
    reported as one — the user needs to know the login protocol changed.
    """
    import urllib.error
    tunnel, opener = channel([LOGIN_GET, ("raise", urllib.error.HTTPError(
        'https://webvpn.zju.edu.cn/do-login', 400, 'Bad Request', {}, None))])
    with pytest.raises(CampusError) as error:
        tunnel.ensure_session()
    assert error.value.code == "WEBVPN_PROTOCOL_CHANGED"
    assert tunnel.status()["last_error"]["code"] == "WEBVPN_PROTOCOL_CHANGED"


def test_channel_mode_env_override(monkeypatch):
    monkeypatch.setenv("CAMPUS_WEBVPN_MODE", "webvpn")
    assert webvpn_tunnel.channel_mode() == "webvpn"
    monkeypatch.setenv("CAMPUS_WEBVPN_MODE", "direct")
    assert webvpn_tunnel.channel_mode() == "direct"
    monkeypatch.setenv("CAMPUS_WEBVPN_MODE", "nonsense")
    assert webvpn_tunnel.channel_mode() == "auto"
