"""Synthetic credential vault and automatic re-login. No school request, no browser.

The CAS encryption itself is covered by the existing connector tests; here we verify
storage, masking, bounded retry policy and the query() recovery wiring with fakes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import campus_credentials, secrets_store
from app.campus_workspace import CREDENTIAL_COOLDOWN_SECONDS, CampusWorkspace
from app.errors import ApiError
from connectors.campus.campus_common import CampusError
from test_campus_browser_bridge import COOKIES, Result
from test_campus_migration import CookieClient
from test_agent_gateway import gateway  # noqa: F401  isolated HTTP fixture

SECRET = "synthetic-passport-secret"
USER = "3230100000"
# Captured before any fixture patches the shared secrets module attribute.
_REAL_GET_KEY = secrets_store.get_key


@pytest.fixture(autouse=True)
def isolated_secrets(tmp_path, monkeypatch):
    """Never touch or read the developer's real secret store."""
    directory = tmp_path / "secrets"
    directory.mkdir()
    monkeypatch.setattr(secrets_store, "SECRETS_DIR", directory)
    monkeypatch.setattr(secrets_store, "SECRETS_DIR", directory)
    monkeypatch.setattr("app.config.SECRETS_DIR", directory)
    yield directory


def workspace(tmp_path, **kwargs):
    return CampusWorkspace(tmp_path / "campus.sqlite", CookieClient)


def test_vault_encrypts_at_rest_masks_reads_and_clears(tmp_path, isolated_secrets):
    status = campus_credentials.save("passport", username=USER, password=SECRET, authcode="1234")
    assert status["configured"] is True
    assert status["auto_relogin"] is True
    assert status["username_hint"] == "32***00"
    assert USER not in json.dumps(status) and SECRET not in json.dumps(status)
    stored = campus_credentials.load("passport")
    assert stored["username"] == USER and stored["password"] == SECRET and stored["authcode"] == "1234"
    blob = (isolated_secrets / "provider-campus-passport.key").read_bytes()
    if os.name == "nt":
        # DPAPI ciphertext must not contain the credential in recoverable form.
        assert SECRET.encode() not in blob and USER.encode() not in blob
    assert SECRET not in json.dumps(campus_credentials.all_status())
    assert campus_credentials.clear("passport")["configured"] is False
    assert campus_credentials.load("passport") is None
    assert campus_credentials.clear("passport")["configured"] is False  # idempotent


def test_vault_rejects_bad_payloads_and_scopes(tmp_path, isolated_secrets):
    for kwargs, scope in [({"username": "", "password": SECRET}, "passport"),
                          ({"username": USER, "password": ""}, "passport"),
                          ({"username": "a" * 65, "password": SECRET}, "passport"),
                          ({"username": USER, "password": "x" * 257}, "passport"),
                          ({"username": USER, "password": "line\nbreak"}, "passport"),
                          ({"username": USER, "password": SECRET}, "shell")]:
        with pytest.raises(campus_credentials.CredentialInputError):
            campus_credentials.save(scope, **kwargs)
    assert not campus_credentials.configured("passport")


def test_toggle_and_clear_keep_or_drop_the_secret_correctly(tmp_path, isolated_secrets):
    campus_credentials.save("passport", username=USER, password=SECRET)
    assert campus_credentials.set_auto_relogin("passport", False)["auto_relogin"] is False
    assert campus_credentials.load("passport")["password"] == SECRET  # toggle keeps it
    assert campus_credentials.set_auto_relogin("passport", True)["auto_relogin"] is True
    with pytest.raises(campus_credentials.CredentialInputError):
        campus_credentials.set_auto_relogin("webvpn", True)


def prepared(monkeypatch, workspace, *, captcha=None, login_error=None, calls=None):
    calls = calls if calls is not None else []
    monkeypatch.setattr(workspace.manager, "prepare", lambda owner: calls.append(("prepare", owner)) or {"flow_id": "flow_x", "captcha": captcha})
    def login(owner, flow, username, password, authcode):
        calls.append(("login", owner, flow, username, password, authcode))
        if login_error is not None:
            raise login_error
        return {"connected": True}
    monkeypatch.setattr(workspace.manager, "login", login)
    return calls


def test_automatic_relogin_uses_stored_credentials_without_any_browser(tmp_path, isolated_secrets, monkeypatch):
    site = workspace(tmp_path)
    try:
        campus_credentials.save("passport", username=USER, password=SECRET, authcode="9999")
        calls = prepared(monkeypatch, site)
        monkeypatch.setattr(site, "_discard_login_flow", lambda: pytest.fail("no flow to discard on success"))
        outcome = site.auto_relogin_locked()
        assert outcome == {"ok": True, "code": "AUTO_LOGIN_OK", "needs_user": False}
        assert calls == [("prepare", "local"), ("login", "local", "flow_x", USER, SECRET, "9999")]
        assert site.last_login["automatic"] is True and site.last_login["ok"] is True
    finally:
        site.close()


def test_captcha_and_mfa_stop_immediately_without_retrying_the_school(tmp_path, isolated_secrets, monkeypatch):
    """Challenges reported by the school fall back to one manual login, exactly once.

    A captcha *image* on the login page is not a challenge: the school serves one to
    everyone, so the credential must still be submitted (see
    `test_available_captcha_does_not_prevent_the_submission`).
    """
    for code in ("CAPTCHA_REQUIRED", "INTERACTION_REQUIRED", "CAS_REJECTED", "CREDENTIAL_REJECTED", "RATE_LIMITED"):
        site = workspace(tmp_path / code)
        try:
            campus_credentials.save("passport", username=USER, password=SECRET)
            calls = prepared(monkeypatch, site, captcha="data:image/png;base64,AAAA",
                             login_error=CampusError(code, "synthetic"))
            discarded = []
            monkeypatch.setattr(site, "_discard_login_flow", lambda: discarded.append(1))
            outcome = site.auto_relogin_locked()
            assert outcome["ok"] is False and outcome["code"] == code
            assert outcome["needs_user"] is (code != "RATE_LIMITED")
            assert [call[0] for call in calls] == ["prepare", "login"]
            assert SECRET not in json.dumps(outcome)
        finally:
            site.close()


def test_available_captcha_does_not_prevent_the_submission(tmp_path, isolated_secrets, monkeypatch):
    """The real page always shows a captcha; only the school's answer may stop us."""
    site = workspace(tmp_path)
    try:
        campus_credentials.save("passport", username=USER, password=SECRET)
        calls = prepared(monkeypatch, site, captcha="data:image/png;base64,AAAA")
        outcome = site.auto_relogin_locked()
        assert outcome == {"ok": True, "code": "AUTO_LOGIN_OK", "needs_user": False}
        assert [call[0] for call in calls] == ["prepare", "login"]
    finally:
        site.close()


def test_absent_or_disabled_credentials_never_attempt_login(tmp_path, isolated_secrets, monkeypatch):
    site = workspace(tmp_path)
    try:
        calls = prepared(monkeypatch, site)
        assert site.auto_relogin_locked()["code"] == "NO_CREDENTIAL"
        campus_credentials.save("passport", username=USER, password=SECRET, auto_relogin=False)
        assert site.auto_relogin_locked()["code"] == "AUTO_RELOGIN_DISABLED"
        assert calls == []
    finally:
        site.close()


def test_expired_session_recovers_via_credentials_without_browser_adoption(tmp_path, isolated_secrets, monkeypatch):
    """An expired session recovers through the stored credential, never the browser.

    Semantics change this round: a plain cache read no longer re-authenticates on a
    cached expiry; the one bounded credential recovery moved to the explicit sync
    entry (`query(..., refresh=True)`, agent tool `campus.sync`).
    """
    site = workspace(tmp_path)
    try:
        campus_credentials.save("passport", username=USER, password=SECRET)
        prepared(monkeypatch, site)
        monkeypatch.setattr(site, "_check_browser_grant", lambda: None)
        monkeypatch.setattr(site, "_browser_cookie_provider",
                            lambda: pytest.fail("stored credentials must not need the browser"))
        reads, syncs = [], []
        def query(*args, **kwargs):
            reads.append("query")
            return Result("SESSION_EXPIRED")
        def sync(*args, **kwargs):
            reads.append("sync")
            syncs.append(1)
            # The single retry only becomes live after the stored credential re-authenticated.
            return Result("SESSION_EXPIRED" if len(syncs) == 1 else None)
        monkeypatch.setattr(site.manager.service, "query", query)
        monkeypatch.setattr(site.manager.service, "sync", sync)

        cached = site.query("courses", {})
        assert cached["error"]["code"] == "SESSION_EXPIRED"
        assert reads == ["query"], "a cache read is offline: no sync, no re-authentication"
        assert site.last_login is None, "a cache read never attempts a login"

        recovered = site.query("courses", {}, refresh=True)
        assert recovered.get("error") is None
        assert reads == ["query", "sync", "sync"], "one bounded re-authentication plus one retry"
        assert site.last_login["ok"] is True
    finally:
        site.close()


def test_failed_automatic_login_is_bounded_by_cooldown(tmp_path, isolated_secrets, monkeypatch):
    site = workspace(tmp_path)
    try:
        campus_credentials.save("passport", username=USER, password=SECRET)
        calls = prepared(monkeypatch, site, login_error=CampusError("CAS_REJECTED", "synthetic"))
        monkeypatch.setattr(site.manager.service, "query", lambda *a, **k: Result("AUTH_EXPIRED"))
        monkeypatch.setattr(site.manager.service, "sync", lambda *a, **k: Result("AUTH_EXPIRED"))
        monkeypatch.setattr(site, "_check_browser_grant", lambda: None)
        # Only the explicit sync entry may reach the credential, so drive the repeats there.
        for _ in range(4):
            site.query("grades", {}, refresh=True)
        assert [call[0] for call in calls] == ["prepare", "login"], "no login loop"
        assert site.credential_status()["cooldown_active"] is True
        site.query("grades", {})
        assert [call[0] for call in calls] == ["prepare", "login"], "a cache read never re-authenticates"
        assert CREDENTIAL_COOLDOWN_SECONDS > 0
    finally:
        site.close()


def test_http_credential_endpoints_never_echo_secrets(gateway, isolated_secrets, monkeypatch):
    client, runtime, _, _ = gateway
    from app import campus_workspace as module
    # The gateway fixture replaces the provider key reader globally; restore the real
    # store reader so this test exercises genuine encryption rather than the stub.
    monkeypatch.setattr(secrets_store, "get_key", _REAL_GET_KEY)
    site = workspace(Path(runtime.store.path.parent) / "campus-http")
    monkeypatch.setattr(module, "get_campus_workspace", lambda: site)
    monkeypatch.setattr("app.routers.campus.get_campus_workspace", lambda: site)
    base = "/api/v1/agent/campus"
    try:
        saved = client.put(base + "/credentials", json={"scope": "passport", "username": USER, "password": SECRET, "authcode": "1234"})
        assert saved.status_code == 200, saved.text
        assert SECRET not in saved.text and USER not in saved.text
        assert saved.json()["username_hint"] == "32***00"
        listed = client.get(base + "/credentials")
        assert listed.status_code == 200 and SECRET not in listed.text and USER not in listed.text
        toggled = client.patch(base + "/credentials", json={"scope": "passport", "enabled": False})
        assert toggled.status_code == 200 and toggled.json()["auto_relogin"] is False
        assert SECRET not in toggled.text
        assert client.put(base + "/credentials", json={"scope": "passport", "username": USER, "password": SECRET, "extra": 1}).status_code == 400
        cleared = client.delete(base + "/credentials?scope=passport")
        assert cleared.status_code == 200 and cleared.json()["configured"] is False
        # The project's error envelope maps validation failures to 400.
        assert client.delete(base + "/credentials?scope=shell").status_code == 400
    finally:
        site.close()
