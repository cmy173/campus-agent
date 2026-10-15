"""Trusted browser -> campus session bridge, entirely synthetic cookies/HTTP."""
import json

import pytest

from app.campus_workspace import CampusWorkspace
from app.errors import ApiError
from test_campus_migration import CookieClient
from test_agent_gateway import gateway  # isolated app fixture


COOKIES = [{"name": "iPlanetDirectoryPro", "value": "controlled-cas", "domain": "zjuam.zju.edu.cn",
            "path": "/", "expires": -1, "secure": True, "httpOnly": True}]


class Result:
    def __init__(self, error=None):
        self.error = error
    def model_dump(self, **_):
        return {"error": {"code": self.error, "message": "合成认证失效"} if self.error else None,
                "records": [] if self.error else [{"id": "synthetic"}], "freshness": "unknown" if self.error else "fresh"}


@pytest.fixture
def workspace(tmp_path):
    value = CampusWorkspace(tmp_path / "campus.sqlite", CookieClient)
    yield value
    value.close()


def test_explicit_authorization_then_repeated_query_does_not_relogin_or_recopy(workspace, monkeypatch):
    copies, queries = [], []
    def cookies():
        copies.append(1)
        return COOKIES
    def query(*args, **kwargs):
        queries.append(1)
        return Result()
    monkeypatch.setattr(workspace.manager.service, "query", query)
    assert not workspace.status()["browser_session_authorized"]
    status = workspace.authorize_browser_session(cookies)
    assert status["browser_session_authorized"] and status["connected"]
    assert "controlled-cas" not in json.dumps(status)
    for _ in range(3):
        assert workspace.query("grades", {}, refresh=False)["error"] is None
    assert len(copies) == 1 and len(queries) == 3  # no repeated consent or login


def test_expired_connector_reuses_authorized_browser_and_retries_read_once(workspace, monkeypatch):
    copies, queries = [], []
    def cookies():
        copies.append(1)
        return COOKIES
    workspace.authorize_browser_session(cookies)
    def query(*args, **kwargs):
        queries.append(1)
        return Result("SESSION_EXPIRED" if len(queries) == 1 else None)
    monkeypatch.setattr(workspace.manager.service, "query", query)
    monkeypatch.setattr(workspace.manager.service, "sync", query)
    assert workspace.query("courses", {})["error"] is None
    assert len(copies) == 2 and len(queries) == 2


def test_no_grant_never_reads_browser_or_retries(workspace, monkeypatch):
    calls = []
    monkeypatch.setattr(workspace.manager.service, "query", lambda *a, **k: calls.append(1) or Result("AUTH_REQUIRED"))
    assert workspace.query("grades", {})["error"]["code"] == "AUTH_REQUIRED"
    assert len(calls) == 1


def test_expired_browser_preserves_existing_state_and_bounded_cooldown(workspace, monkeypatch):
    calls = []
    workspace.authorize_browser_session(lambda: COOKIES)
    prior = workspace.manager.states["local"]["client"]
    def expired():
        calls.append(1)
        return []
    workspace._browser_cookie_provider = expired
    monkeypatch.setattr(workspace.manager.service, "query", lambda *a, **k: Result("AUTH_EXPIRED"))
    for _ in range(3):
        assert workspace.query("grades", {})["error"]["code"] == "AUTH_EXPIRED"
    assert len(calls) == 1
    assert workspace.manager.states["local"]["client"] is prior and not prior.closed


def test_business_error_does_not_reauthenticate(workspace, monkeypatch):
    calls = []
    workspace.authorize_browser_session(lambda: COOKIES)
    workspace._browser_cookie_provider = lambda: calls.append(1) or COOKIES
    monkeypatch.setattr(workspace.manager.service, "query", lambda *a, **k: Result("PROTOCOL_CHANGED"))
    assert workspace.query("grades", {})["error"]["code"] == "PROTOCOL_CHANGED"
    assert calls == []


def test_disconnect_revokes_persistent_grant_but_close_does_not(workspace, monkeypatch):
    workspace.authorize_browser_session(lambda: COOKIES)
    reopened = CampusWorkspace(workspace.store.path, CookieClient)
    try:
        assert reopened.status()["browser_session_authorized"]
        assert reopened.status()["browser_authorization"]["persisted"]
        assert not reopened.status()["connected"]
    finally:
        reopened.close()
    status = workspace.disconnect()
    assert not status["browser_session_authorized"] and not status["connected"]
    assert status["snapshot"]["profile"]["data"]["student_id"] == "controlled-student"


def test_http_trusted_empty_grant_owner_boundary_and_no_cookie_endpoint(gateway, workspace, monkeypatch):
    client, runtime, _, _ = gateway
    monkeypatch.setattr("app.routers.campus.get_campus_workspace", lambda: workspace)
    monkeypatch.setattr(runtime.browser, "campus_cookies_private", lambda: COOKIES)
    base = "/api/v1/agent/campus"
    # Token-only/model routes cannot grant browser-cookie reuse.
    assert client.post("/api/v1/agent/internal/browser", json={"method": "campus-session"}).status_code in (400, 403)
    assert client.post(base + "/connect-browser", headers={"Origin": "https://untrusted.example"}, json={}).status_code == 403
    assert client.post(base + "/connect-browser", json={"cookies": COOKIES}).status_code == 400
    response = client.post(base + "/connect-browser", json={})
    assert response.status_code == 200, response.text
    assert response.json()["browser_session_authorized"] and "controlled-cas" not in response.text
    assert client.get(base + "/cookies").status_code == 404
    disconnected = client.post(base + "/disconnect", json={"clear_cache": False})
    assert disconnected.status_code == 200 and not disconnected.json()["browser_session_authorized"]

@pytest.mark.parametrize("expires", [True, float("nan"), float("inf"), float("-inf"), 0, 1])
def test_adoption_invalid_expiry_cannot_grant_authorization(workspace, expires):
    cookies = [COOKIES[0] | {"expires": expires}]
    with pytest.raises(ApiError):
        workspace.authorize_browser_session(lambda: cookies)
    assert not workspace.status()["browser_session_authorized"]
    assert not workspace.status()["connected"]


def test_adoption_without_verifiable_identity_refuses_grant(workspace, monkeypatch):
    monkeypatch.setattr(CookieClient, "profiles", lambda *a: ([], []))
    with pytest.raises(ApiError):
        workspace.authorize_browser_session(lambda: COOKIES)
    assert not workspace.status()["browser_session_authorized"]


def test_model_auth_recovery_uses_same_safe_workspace_without_new_confirmation(workspace, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.agent_bridge.tools import ToolContext, execute
    workspace.authorize_browser_session(lambda: COOKIES)
    monkeypatch.setattr("app.campus_workspace.get_campus_workspace", lambda: workspace)
    requests = []
    class FullResult:
        def model_dump(self, **_):
            requests.append(1)
            expired = len(requests) == 1
            return {"error": {"code": "AUTH_EXPIRED", "message": "synthetic"} if expired else None,
                    "records": [] if expired else [{"id": "synthetic", "name": "测试课程"}],
                    "platform": "zdbk", "completeness": "partial", "freshness": "unknown" if expired else "fresh",
                    "fetched_at": "2026-10-15T10:00:00+08:00", "warnings": []}
    monkeypatch.setattr(workspace.manager.service, "query", lambda *a, **k: FullResult())
    monkeypatch.setattr(workspace.manager.service, "sync", lambda *a, **k: FullResult())
    with Session(create_engine("sqlite:///:memory:")) as db:
        result = execute("campus.query", {"capability": "courses"}, db, ToolContext("local", "synthetic-conversation"))
    assert result["error"] is None and result["status"] == "partial"
    assert result["data"]["records"][0]["id"] == "synthetic"
    assert len(requests) == 2  # bounded read recovery, not a second model request
