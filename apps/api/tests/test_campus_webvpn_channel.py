"""WebVPN account storage and channel wiring at the app layer (synthetic only).

The WebVPN account is deliberately a *separate* vault slot from the campus
passport: the school issues them separately, and one must never configure,
verify or reset the other.
"""
from __future__ import annotations

import json

import pytest

from app import campus_credentials, secrets_store
from app.campus_workspace import CampusWorkspace
from connectors.campus import webvpn_tunnel
from connectors.campus.campus import CampusClient

PASSPORT = {"username": "3230100000", "password": "synthetic-passport-secret"}
WEBVPN = {"username": "wv-3230100000", "password": "synthetic-webvpn-secret"}


@pytest.fixture(autouse=True)
def isolated_secrets(tmp_path, monkeypatch):
    directory = tmp_path / "secrets"
    directory.mkdir()
    monkeypatch.setattr(secrets_store, "SECRETS_DIR", directory)
    # Restore the production wiring (the app registers the vault as the only
    # credential source at import time); only the vault location is swapped.
    from app.campus_workspace import stored_webvpn_credentials
    webvpn_tunnel.reset_tunnel()
    webvpn_tunnel.register_credential_provider(stored_webvpn_credentials)
    yield directory
    webvpn_tunnel.register_credential_provider(stored_webvpn_credentials)
    webvpn_tunnel.reset_tunnel()


def workspace(tmp_path):
    return CampusWorkspace(tmp_path / "campus.sqlite", CampusClient)


def test_the_webvpn_account_is_stored_in_its_own_slot(tmp_path):
    site = workspace(tmp_path)
    try:
        campus_credentials.save("passport", **PASSPORT)
        items = {item["scope"]: item for item in campus_credentials.all_status()["items"]}
        assert items["passport"]["configured"] is True
        assert items["webvpn"]["configured"] is False

        campus_credentials.save("webvpn", **WEBVPN)
        items = {item["scope"]: item for item in campus_credentials.all_status()["items"]}
        assert items["passport"]["configured"] and items["webvpn"]["configured"]
        assert items["passport"]["username_hint"] != items["webvpn"]["username_hint"]

        # Clearing one slot must not touch the other.
        campus_credentials.clear("webvpn")
        items = {item["scope"]: item for item in campus_credentials.all_status()["items"]}
        assert items["passport"]["configured"] is True and items["webvpn"]["configured"] is False
    finally:
        site.close()


def test_channel_is_disabled_until_a_webvpn_credential_is_saved_and_enabled(tmp_path):
    from app.campus_workspace import stored_webvpn_credentials
    assert stored_webvpn_credentials() is None
    campus_credentials.save("webvpn", **WEBVPN)
    assert stored_webvpn_credentials()["username"] == WEBVPN["username"]
    # The toggle alone is the switch: no credential reaches the connectors when off.
    campus_credentials.set_auto_relogin("webvpn", False)
    assert stored_webvpn_credentials() is None
    # The passport slot never feeds the WebVPN channel.
    campus_credentials.save("passport", **PASSPORT)
    assert stored_webvpn_credentials() is None


def test_status_exposes_a_truthful_channel_section(tmp_path):
    site = workspace(tmp_path)
    try:
        channel = site.credential_status()["webvpn_channel"]
        assert channel["configured"] is False
        assert channel["authenticated"] is False
        assert channel["mapped_hosts"] == ["zdbk.zju.edu.cn", "zjuam.zju.edu.cn"]
        assert channel["unmapped_hosts"] == ["eta.zju.edu.cn"]
        assert channel["direct_only_hosts"] == ["zjuam.zju.edu.cn"]
        assert channel["channel_active"] == {"zdbk.zju.edu.cn": False, "zjuam.zju.edu.cn": False}
        assert "password" not in json.dumps(channel, ensure_ascii=False).lower()

        campus_credentials.save("webvpn", **WEBVPN)
        channel = site.credential_status()["webvpn_channel"]
        assert channel["configured"] is True
        # The business host can use the portal; the campus SSO host never does.
        assert channel["channel_active"] == {"zdbk.zju.edu.cn": True, "zjuam.zju.edu.cn": False}
        assert WEBVPN["password"] not in json.dumps(site.credential_status(), ensure_ascii=False)
    finally:
        site.close()


def test_explicit_webvpn_verification_never_uses_the_passport_flow(tmp_path, monkeypatch):
    site = workspace(tmp_path)
    try:
        monkeypatch.setattr(site, "auto_relogin_locked", lambda: pytest.fail("must not use the passport path"))
        calls = []
        monkeypatch.setattr(webvpn_tunnel, "probe_login",
                            lambda force=True: calls.append(force) or {"ok": True, "code": "WEBVPN_LOGIN_OK", "needs_user": False})
        result = site.test_webvpn_login()
        assert calls == [True]
        assert result["ok"] is True and result["code"] == "WEBVPN_LOGIN_OK"
        assert result["status"]["configured"] is False
    finally:
        site.close()


def test_router_verifies_the_webvpn_scope_against_the_portal(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.errors import ApiError, error_response
    from app.routers import campus as campus_router

    site = workspace(tmp_path)
    monkeypatch.setattr(campus_router, "get_campus_workspace", lambda: site)
    monkeypatch.setattr(site, "auto_relogin_locked", lambda: pytest.fail("the passport flow must not run"))
    monkeypatch.setattr(webvpn_tunnel, "probe_login",
                        lambda force=True: {"ok": True, "code": "WEBVPN_LOGIN_OK", "needs_user": False})
    app = FastAPI()
    campus_router.install_campus_router(app, lambda: "trusted-local-browser")

    @app.exception_handler(ApiError)
    async def handler(request, exc):
        return error_response(request, exc.status_code, exc.code, exc.message, exc.retryable, exc.details)

    try:
        client = TestClient(app)
        verified = client.post("/api/v1/agent/campus/credentials/test-login", json={"scope": "webvpn"})
        assert verified.status_code == 200
        body = verified.json()
        assert body["code"] == "WEBVPN_LOGIN_OK"
        assert body["status"]["mapped_hosts"] == ["zdbk.zju.edu.cn", "zjuam.zju.edu.cn"]
    finally:
        site.close()
