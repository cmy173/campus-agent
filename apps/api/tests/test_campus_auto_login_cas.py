"""End-to-end automatic re-login against a local fake CAS. No school traffic.

Only two hops are substituted, both documented in the assertions:
- the CAS base URL points at a loopback HTTP server, and the URL policy is relaxed
  for that server only (a separate test proves production policy still rejects it);
- the ZDBK *service* handoff is stubbed, because that is the school's JSP app and
  not part of credential handling.

Everything else is production code: `CampusManager.prepare/login`,
`CampusAuthentication.authenticate_passport` (execution token, RSA encryption,
cookie confirmation), the DPAPI vault and the bounded-retry policy.
"""
from __future__ import annotations

import json
import re
import threading
from http.cookiejar import DefaultCookiePolicy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from app import campus_credentials, secrets_store
from app.campus_workspace import CampusWorkspace
from connectors.campus import campus_auth, campus_network
from connectors.campus.campus import CampusClient
from connectors.campus.campus_common import CampusError
from connectors.campus.campus_network import check_url

USER = "3230100000"
SECRET = "synthetic-cas-secret"
# A syntactically valid public key; the client only encrypts with it.
MODULUS = "00c1f4b7d29e6a3f5b8c0d1e2f304152637485960718293a4b5c6d7e8f90112233445566778899aabbccddeeff00112233445566778899aabbccddeeff01"
EXPONENT = "010001"
EXECUTION = "e1s1-synthetic-execution"

_login_page = f'<html><body><form><input name="execution" value="{EXECUTION}"/></form></body></html>'
_mfa_page = "<html><body>请完成二次认证（动态口令）</body></html>"
_captcha_error_page = "<html><body><div id='msg'>验证码错误</div></body></html>"
_bad_password_page = "<html><body><div id='msg'>用户名或密码错误</div></body></html>"
# A syntactically valid JPEG payload; the client only checks the MIME type and size.
_CAPTCHA_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 96


class FakeCas(BaseHTTPRequestHandler):
    """Minimal official-shaped CAS: login page, pubkey, and the credential POST."""

    attempts: list[dict] = []
    mode = "ok"
    # The real zjuam login page always serves a captcha image, even when the school
    # does not enforce it. The fixture can reproduce that so it cannot mask the bug.
    serves_captcha = False

    def log_message(self, *args):  # keep test output clean
        pass

    def _send(self, status, body: bytes, content_type="text/html; charset=utf-8", cookie=None, location=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        if location:
            self.send_header("Location", location)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/cas/v2/getPubKey"):
            payload = json.dumps({"modulus": MODULUS, "exponent": EXPONENT}).encode()
            return self._send(200, payload, "application/json")
        if self.path.startswith("/cas/kaptcha"):
            if FakeCas.serves_captcha:
                return self._send(200, _CAPTCHA_JPEG, "image/jpeg;charset=UTF-8")
            return self._send(404, b"no captcha in fixture")
        if self.path.startswith("/cas/login"):
            return self._send(200, _login_page.encode())
        return self._send(404, b"not found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        FakeCas.attempts.append(form)
        if FakeCas.mode == "mfa":
            return self._send(200, _mfa_page.encode())
        if FakeCas.mode == "captcha":
            return self._send(200, _captcha_error_page.encode())
        if FakeCas.mode == "bad_password":
            return self._send(200, _bad_password_page.encode())
        if FakeCas.mode == "reject":
            return self._send(200, b"<html><body>login</body></html>")
        # Success requires the real passport cookie to be present and unexpired.
        return self._send(302, b"", cookie="iPlanetDirectoryPro=synthetic-ticket; Path=/; HttpOnly",
                          location="/cas/login?ticket=ST-synthetic")


@pytest.fixture()
def fake_cas(monkeypatch):
    FakeCas.attempts, FakeCas.mode, FakeCas.serves_captcha = [], "ok", False
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCas)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/cas/"
    monkeypatch.setattr(campus_auth, "CAS", base)
    # The fake server is loopback HTTP; relax only the URL policy for this test, at
    # both binding sites (the transport layer and the auth module).
    monkeypatch.setattr(campus_network, "check_url", lambda url: url)
    monkeypatch.setattr(campus_auth, "check_url", lambda url: url)
    # Production binds cookies to the three real ZJU hosts. The fixture must therefore
    # accept the loopback host too; `test_production_cookie_policy_rejects_loopback`
    # proves the real policy still refuses it. Everything else is unchanged.
    trusted = {"zjuam.zju.edu.cn", "zdbk.zju.edu.cn", "eta.zju.edu.cn", "127.0.0.1", "localhost"}

    def set_ok(self, cookie, request):
        host = urlsplit(request.full_url).hostname
        if host not in trusted or not DefaultCookiePolicy.set_ok(self, cookie, request):
            return False
        cookie.domain = host
        cookie.domain_specified = False
        cookie.domain_initial_dot = False
        return True

    def return_ok(self, cookie, request):
        host = urlsplit(request.full_url).hostname
        if cookie.name.lower() == 'iplanetdirectorypro':
            if host not in ('zjuam.zju.edu.cn', '127.0.0.1', 'localhost'):
                return False
        elif cookie.domain.lstrip('.').lower() != host:
            return False
        return DefaultCookiePolicy.return_ok(self, cookie, request)

    monkeypatch.setattr(campus_network._CampusCookiePolicy, "set_ok", set_ok)
    monkeypatch.setattr(campus_network._CampusCookiePolicy, "return_ok", return_ok)
    # The ZDBK JSP handoff is the school's application, not credential handling.
    monkeypatch.setattr(CampusClient, "connect_zdbk", lambda self: setattr(self, "zdbk_ok", True))
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def isolated_secrets(tmp_path, monkeypatch):
    directory = tmp_path / "secrets"
    directory.mkdir()
    monkeypatch.setattr(secrets_store, "SECRETS_DIR", directory)
    yield directory


def workspace(tmp_path):
    site = CampusWorkspace(tmp_path / "campus.sqlite", CampusClient)
    campus_credentials.save("passport", username=USER, password=SECRET)
    return site


def test_production_url_policy_still_rejects_the_loopback_fixture():
    # Guards the substitution above: the relaxation is test-local, not a product change.
    with pytest.raises(CampusError) as error:
        check_url("http://127.0.0.1:8080/cas/")
    assert error.value.code == "UNSAFE_REDIRECT"


def test_production_cookie_policy_rejects_loopback_issuers():
    """The real policy (not the fixture's) must refuse a non-ZJU cookie issuer."""
    from http.cookiejar import Cookie
    from urllib.request import Request

    policy = campus_network._CampusCookiePolicy()
    cookie = Cookie(0, "iPlanetDirectoryPro", "synthetic", None, False, "127.0.0.1", False, False,
                    "/", True, False, None, True, None, None, {})
    assert policy.set_ok(cookie, Request("http://127.0.0.1:8080/cas/login")) is False


def test_automatic_relogin_submits_encrypted_credentials_and_continues(tmp_path, fake_cas):
    site = workspace(tmp_path)
    try:
        outcome = site.auto_relogin_locked()
        assert outcome == {"ok": True, "code": "AUTO_LOGIN_OK", "needs_user": False}
        assert len(FakeCas.attempts) == 1
        submitted = FakeCas.attempts[0]
        assert submitted["username"] == USER
        assert submitted["execution"] == EXECUTION, "must use the freshly fetched form token"
        assert submitted["_eventId"] == "submit"
        # The password leaves the vault encrypted, never in clear text.
        assert submitted["password"] != SECRET
        assert SECRET not in json.dumps(submitted)
        assert re.fullmatch(r"[0-9a-f]+", submitted["password"])
        assert len(submitted["password"]) >= 2 * len(SECRET)
        # Session state is now live and the audit record holds no credential.
        assert site.manager.connector_session("zdbk", "local") is not None
        assert site.last_login["ok"] is True and site.last_login["automatic"] is True
        assert SECRET not in json.dumps(site.status())
        assert SECRET not in json.dumps(site.credential_status())
    finally:
        site.close()


def test_rejected_credentials_are_reported_once_without_looping(tmp_path, fake_cas):
    FakeCas.mode = "reject"
    site = workspace(tmp_path)
    try:
        outcome = site.auto_relogin_locked()
        assert outcome["ok"] is False and outcome["code"] == "CAS_REJECTED"
        assert outcome["needs_user"] is True, "a human must confirm; this is not proof of a wrong password"
        assert len(FakeCas.attempts) == 1
        assert site.last_login["ok"] is False and SECRET not in json.dumps(site.last_login)
    finally:
        site.close()


def test_mfa_challenge_is_classified_for_one_manual_step(tmp_path, fake_cas):
    FakeCas.mode = "mfa"
    site = workspace(tmp_path)
    try:
        outcome = site.auto_relogin_locked()
        assert outcome["ok"] is False and outcome["code"] == "INTERACTION_REQUIRED"
        assert outcome["needs_user"] is True
        assert len(FakeCas.attempts) == 1
    finally:
        site.close()


def test_an_available_captcha_image_never_blocks_automatic_relogin(tmp_path, fake_cas):
    """Regression: the real zjuam page always serves /cas/kaptcha, so the mere
    presence of a captcha image must not abort the credential path. The school
    decides; the client attempts exactly one login and reports what it answered."""
    FakeCas.serves_captcha = True
    site = workspace(tmp_path)
    try:
        outcome = site.auto_relogin_locked()
        assert outcome == {"ok": True, "code": "AUTO_LOGIN_OK", "needs_user": False}
        assert len(FakeCas.attempts) == 1, "the stored credential must actually be submitted"
        assert FakeCas.attempts[0]["username"] == USER
    finally:
        site.close()


def test_captcha_challenge_is_reported_as_one_manual_step(tmp_path, fake_cas):
    FakeCas.serves_captcha = True
    FakeCas.mode = "captcha"
    site = workspace(tmp_path)
    try:
        outcome = site.auto_relogin_locked()
        assert outcome["ok"] is False and outcome["code"] == "CAPTCHA_REQUIRED"
        assert outcome["needs_user"] is True
        assert len(FakeCas.attempts) == 1, "never loop on a captcha challenge"
    finally:
        site.close()


def test_rejected_credential_is_reported_distinctly(tmp_path, fake_cas):
    """A school credential hint is actionable and must not be folded into the
    ambiguous 'not confirmed' outcome."""
    FakeCas.mode = "bad_password"
    site = workspace(tmp_path)
    try:
        outcome = site.auto_relogin_locked()
        assert outcome["ok"] is False and outcome["code"] == "CREDENTIAL_REJECTED"
        assert outcome["needs_user"] is True
        assert len(FakeCas.attempts) == 1
        assert SECRET not in json.dumps(site.status())
    finally:
        site.close()


def test_query_recovers_automatically_after_the_session_expires(tmp_path, fake_cas, monkeypatch):
    """Full chain: expired read -> stored credential -> live CAS session -> refresh."""
    site = workspace(tmp_path)
    try:
        monkeypatch.setattr(site, "_check_browser_grant", lambda: None)
        monkeypatch.setattr(site, "_browser_cookie_provider",
                            lambda: pytest.fail("stored credentials must not need the browser"))
        site.manager.disconnect("local")  # simulate an expired passport session
        with pytest.raises(CampusError):
            site.manager.connector_session("zdbk", "local")
        outcome = site.auto_relogin_locked()
        assert outcome["ok"] is True
        assert len(FakeCas.attempts) == 1
        assert site.manager.connector_session("zdbk", "local") is not None
        # The credential vault never leaks through the public status payloads.
        assert SECRET not in json.dumps(site.status())
    finally:
        site.close()
