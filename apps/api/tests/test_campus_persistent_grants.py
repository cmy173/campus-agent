"""Persistent consent with synthetic private providers; never school/browser I/O."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from app.campus_workspace import CampusWorkspace, BROWSER_GRANT_SCOPE
from app.errors import ApiError
from connectors.campus.campus_common import CampusError
from test_campus_browser_bridge import COOKIES, Result
from test_campus_migration import CookieClient
from test_agent_gateway import gateway  # isolated HTTP fixture


def connection_query(workspace, calls):
    def query(*args, **kwargs):
        calls.append(1)
        return Result(None if workspace.manager._state("local") else "AUTH_REQUIRED")
    return query


def test_restart_restores_grant_not_secrets_and_explicit_sync_reprobes(tmp_path, monkeypatch):
    path = tmp_path / "campus.sqlite"
    probes, identities, cookies = [], [], []
    class Client(CookieClient):
        def connect_zdbk(self):
            probes.append(1)
            super().connect_zdbk()
        def profiles(self, expected):
            identities.append(expected)
            return super().profiles(expected)
    first = CampusWorkspace(path, Client)
    first.authorize_browser_session(lambda: COOKIES)
    authorized_at = first.status()["browser_authorization"]["authorized_at"]
    with first.store.connect() as db:
        grant = dict(db.execute("SELECT * FROM campus_browser_grants").fetchone())
        persisted = json.dumps([tuple(row) for row in db.execute("SELECT * FROM campus_browser_grants")])
    assert set(grant) == {"owner", "grant_id", "scope", "identity_sha256", "authorized_at"}
    assert grant["scope"] == BROWSER_GRANT_SCOPE
    assert grant["identity_sha256"] == hashlib.sha256(b"controlled-student").hexdigest()
    assert all(secret not in persisted for secret in ("controlled-cas", "iPlanetDirectoryPro", "password"))
    assert b"controlled-cas" not in path.read_bytes()
    first.close()
    reopened = CampusWorkspace(path, Client)
    try:
        queries, syncs = [], []
        reopened.bind_browser_cookie_provider(lambda: cookies.append(1) or COOKIES)
        monkeypatch.setattr(reopened.manager.service, "query", connection_query(reopened, queries))
        # Only the explicit sync entry may reach the school or re-adopt a browser
        # session: recovery after an adopted session refreshes the authorized read
        # instead of replaying a stale cached AUTH error. Keep the call accounting.
        monkeypatch.setattr(reopened.manager.service, "sync", connection_query(reopened, syncs))
        status = reopened.status()
        assert status["browser_session_authorized"] and status["browser_authorization"]["persisted"]
        assert not status["connected"] and not status["passport_connected"]
        assert cookies == []  # neither initialization nor status reads secrets
        # A plain cache read is offline: one local cache lookup, no school sync, no
        # browser credential read, no probe and no re-adoption of the granted session.
        cached = reopened.query("grades", {})
        assert cached["error"]["code"] == "AUTH_REQUIRED" and not cached["records"]
        assert queries == [1] and syncs == [] and cookies == []
        assert probes == [1] and identities == [""]
        # Only the explicit sync entry re-adopts the already authorized session, once.
        assert reopened.query("grades", {}, refresh=True)["error"] is None
        assert cookies == [1] and syncs == [1, 1]
        assert probes == [1, 1] and identities == ["", "controlled-student"]
        assert reopened.status()["browser_authorization"]["authorized_at"] == authorized_at
        assert reopened.query("grades", {})["error"] is None
        assert queries == [1, 1] and syncs == [1, 1] and cookies == [1]
        assert len(cookies) == 1  # healthy connector does not re-copy browser credentials
    finally:
        reopened.close()


@pytest.mark.parametrize("clear_cache", [False, True])
def test_disconnect_revokes_across_instances_even_with_stale_provider(tmp_path, monkeypatch, clear_cache):
    path = tmp_path / "campus.sqlite"
    first = CampusWorkspace(path, CookieClient)
    second = CampusWorkspace(path, CookieClient)
    copies = []
    try:
        first.authorize_browser_session(lambda: copies.append(1) or COOKIES)
        second.disconnect(clear_cache=clear_cache)
        calls = []
        monkeypatch.setattr(first.manager.service, "query", connection_query(first, calls))
        assert first.query("grades", {})["error"]["code"] == "AUTH_REQUIRED"
        assert len(copies) == 1 and len(calls) == 1
        assert not first.status()["browser_session_authorized"]
        assert not first.status()["connected"]
        reopened = CampusWorkspace(path, CookieClient)
        try:
            reopened.bind_browser_cookie_provider(lambda: pytest.fail("revoked grant read browser"))
            assert reopened.query("grades", {})["error"]["code"] == "AUTH_REQUIRED"
            assert not reopened.status()["browser_authorization"]["persisted"]
            assert bool(reopened.status()["snapshot"]["profile"]) is not clear_cache
        finally:
            reopened.close()
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("failure", ["probe", "identity", "missing_identity", "provider"])
def test_restart_failed_recovery_preserves_grant_cache_and_honest_auth(tmp_path, monkeypatch, failure):
    path = tmp_path / "campus.sqlite"
    first = CampusWorkspace(path, CookieClient)
    first.authorize_browser_session(lambda: COOKIES)
    first.close()
    candidates, copies, probes = [], [], []
    class Candidate(CookieClient):
        def __init__(self):
            super().__init__()
            candidates.append(self)
        def connect_zdbk(self):
            probes.append(1)
            if failure == "probe":
                raise CampusError("AUTH_REQUIRED", "synthetic probe refused")
            super().connect_zdbk()
        def profiles(self, expected):
            if failure == "missing_identity":
                return [], []
            records, warnings = super().profiles(expected)
            if failure == "identity":
                records[0]["data"]["student_id"] = "different-student"
            return records, warnings
    reopened = CampusWorkspace(path, Candidate)
    def provider():
        copies.append(1)
        if failure == "provider":
            raise RuntimeError("synthetic daemon failure, private secret must not escape")
        return COOKIES
    reopened.bind_browser_cookie_provider(provider)
    monkeypatch.setattr(reopened.manager.service, "query", lambda *a, **k: Result("AUTH_REQUIRED"))
    # Only the explicit sync entry may attempt recovery, and even there the provider
    # is read at most once per cooldown window: never an authentication loop.
    monkeypatch.setattr(reopened.manager.service, "sync", lambda *a, **k: Result("AUTH_REQUIRED"))
    before = reopened.manager._snapshot("local")
    try:
        for _ in range(3):
            result = reopened.query("grades", {}, refresh=True)
            assert result["error"]["code"] == "AUTH_REQUIRED" and not result["records"]
            assert "private secret" not in json.dumps(result)
        assert len(copies) == 1  # bounded cooldown, never authentication loops
        assert reopened.manager._snapshot("local") == before
        status = reopened.status()
        assert status["browser_session_authorized"] and not status["connected"]
        assert all(client.closed for client in candidates)
        assert len(probes) == (0 if failure == "provider" else 1)
    finally:
        reopened.close()


def test_provider_binding_and_failed_connect_cannot_create_grant(tmp_path, monkeypatch):
    workspace = CampusWorkspace(tmp_path / "campus.sqlite", CookieClient)
    copies = []
    def unavailable():
        copies.append(1)
        raise ApiError(409, "BROWSER_AUTH_REQUIRED", "synthetic daemon is stopped")
    workspace.bind_browser_cookie_provider(unavailable)
    monkeypatch.setattr(workspace.manager.service, "query", lambda *a, **k: Result("AUTH_REQUIRED"))
    try:
        assert workspace.query("grades", {})["error"]["code"] == "AUTH_REQUIRED"
        assert copies == []
        with pytest.raises(ApiError):
            workspace.authorize_browser_session()
        assert copies == [1] and not workspace.status()["browser_session_authorized"]
        with workspace.store.connect() as db:
            assert not db.execute("SELECT * FROM campus_browser_grants").fetchall()
    finally:
        workspace.close()


def test_revocation_during_provider_read_never_resurrects_grant(tmp_path, monkeypatch):
    path = tmp_path / "campus.sqlite"
    first = CampusWorkspace(path, CookieClient)
    first.authorize_browser_session(lambda: COOKIES)
    first.close()
    reopened = CampusWorkspace(path, CookieClient)
    other = CampusWorkspace(path, CookieClient)
    before = reopened.manager._snapshot("local")
    def cookies():
        other.disconnect(clear_cache=True)
        return COOKIES
    reopened.bind_browser_cookie_provider(cookies)
    monkeypatch.setattr(reopened.manager.service, "query", lambda *a, **k: Result("AUTH_REQUIRED"))
    # A plain cache read never touches the provider, so the revocation-during-read
    # race is exercised through the explicit sync entry that may re-adopt a session.
    monkeypatch.setattr(reopened.manager.service, "sync", lambda *a, **k: Result("AUTH_REQUIRED"))
    try:
        assert before["profile"]
        assert reopened.query("grades", {}, refresh=True)["error"]["code"] == "AUTH_REQUIRED"
        assert not reopened.status()["browser_session_authorized"]
        assert not reopened.status()["connected"]
        assert reopened.manager._snapshot("local")["profile"] is None
    finally:
        reopened.close()
        other.close()


def test_grant_identity_binding_survives_snapshot_removal(tmp_path, monkeypatch):
    path = tmp_path / "campus.sqlite"
    first = CampusWorkspace(path, CookieClient)
    first.authorize_browser_session(lambda: COOKIES)
    first.close()
    class DifferentClient(CookieClient):
        def profiles(self, expected):
            return [{"data": {"student_id": "different-student"}, "source": "synthetic"}], []
    reopened = CampusWorkspace(path, DifferentClient)
    try:
        reopened.manager.repository.delete("local")
        reopened.bind_browser_cookie_provider(lambda: COOKIES)
        monkeypatch.setattr(reopened.manager.service, "query", lambda *a, **k: Result("AUTH_REQUIRED"))
        assert reopened.query("grades", {})["error"]["code"] == "AUTH_REQUIRED"
        assert not reopened.status()["connected"]
        assert reopened.manager._snapshot("local")["profile"] is None
        assert reopened.status()["browser_session_authorized"]  # failure never rewrites old consent
    finally:
        reopened.close()


@pytest.mark.parametrize("field,value", [("scope", "all_browser_write:v9"),
                                         ("identity_sha256", "invalid"),
                                         ("grant_id", "invalid"),
                                         ("authorized_at", "invalid")])
def test_invalid_persisted_metadata_never_authorizes_provider(tmp_path, monkeypatch, field, value):
    path = tmp_path / "campus.sqlite"
    workspace = CampusWorkspace(path, CookieClient)
    workspace.authorize_browser_session(lambda: COOKIES)
    workspace.close()
    reopened = CampusWorkspace(path, CookieClient)
    try:
        with reopened.store.connect() as db:
            db.execute(f"UPDATE campus_browser_grants SET {field}=?", (value,))
        reopened.bind_browser_cookie_provider(lambda: pytest.fail("invalid consent read browser"))
        assert not reopened.status()["browser_session_authorized"]
        assert reopened.query("grades", {})["error"]["code"] == "AUTH_REQUIRED"
    finally:
        reopened.close()


def test_public_query_never_reads_browser_and_private_cache_is_retained(tmp_path, monkeypatch):
    workspace = CampusWorkspace(tmp_path / "campus.sqlite", CookieClient)
    try:
        workspace.bind_browser_cookie_provider(lambda: pytest.fail("ungranted/public read browser"))
        public = workspace.query("notices", {}, platform="zdbk_public")
        assert public["error"] is None  # cached public reads need no browser grant
        from connectors.campus.connector_contracts import GradesQuery
        scope = json.dumps(GradesQuery().model_dump(), sort_keys=True, ensure_ascii=False)
        workspace.manager.capability_repository.write("local", "zdbk", "grades", scope, {
            "source": "grades", "platform": "zdbk", "records": [],
            "fetched_at": "2026-10-14T10:00:00+08:00", "freshness": "fresh"})
        result = workspace.query("grades", {})
        assert result["error"]["code"] == "AUTH_REQUIRED"
        assert result["fetched_at"] == "2026-10-14T10:00:00+08:00" and result["freshness"] == "stale"
    finally:
        workspace.close()


def test_public_auth_error_with_grant_cannot_read_private_browser(tmp_path, monkeypatch):
    workspace = CampusWorkspace(tmp_path / "campus.sqlite", CookieClient)
    try:
        workspace.authorize_browser_session(lambda: COOKIES)
        workspace.bind_browser_cookie_provider(lambda: pytest.fail("public error copied private credentials"))
        monkeypatch.setattr(workspace.manager.service, "query", lambda *a, **k: Result("AUTH_REQUIRED"))
        assert workspace.query("notices", {}, "zdbk_public")["error"]["code"] == "AUTH_REQUIRED"
    finally:
        workspace.close()


def test_failed_explicit_reauthorization_keeps_old_grant_and_live_client(tmp_path):
    workspace = CampusWorkspace(tmp_path / "campus.sqlite", CookieClient)
    try:
        workspace.authorize_browser_session(lambda: COOKIES)
        client = workspace.manager.states["local"]["client"]
        with workspace.store.connect() as db:
            before = tuple(db.execute("SELECT * FROM campus_browser_grants").fetchone())
        with pytest.raises(ApiError):
            workspace.authorize_browser_session(lambda: [])
        with workspace.store.connect() as db:
            assert tuple(db.execute("SELECT * FROM campus_browser_grants").fetchone()) == before
        assert workspace.manager.states["local"]["client"] is client and not client.closed
        assert workspace.status()["browser_session_authorized"] and workspace.status()["connected"]
    finally:
        workspace.close()


def test_runtime_provider_resolves_new_instance_after_http_connect(gateway, tmp_path, monkeypatch):
    client, runtime, _, _ = gateway
    path = tmp_path / "campus.sqlite"
    workspace = CampusWorkspace(path, CookieClient)
    current = [workspace]
    copies = []
    monkeypatch.setattr("app.routers.campus.get_campus_workspace", lambda: current[0])
    monkeypatch.setattr(runtime.browser, "campus_cookies_private", lambda: copies.append("first") or COOKIES)
    base = "/api/v1/agent/campus"
    try:
        assert client.post(base + "/connect-browser", json={}).status_code == 200
        workspace.close()
        current[0] = CampusWorkspace(path, CookieClient)
        new_runtime = SimpleNamespace(browser=SimpleNamespace(
            campus_cookies_private=lambda: copies.append("restarted") or COOKIES))
        monkeypatch.setattr("app.agent_bridge.runtime.get_runtime", lambda: new_runtime)
        calls = []
        monkeypatch.setattr(current[0].manager.service, "query", connection_query(current[0], calls))
        monkeypatch.setattr(current[0].manager.service, "sync", connection_query(current[0], calls))
        status = client.get(base + "/status").json()
        assert status["browser_session_authorized"] and not status["connected"]
        assert copies == ["first"]
        # A plain cache read reports the expired session and stays offline: no browser
        # credential read, no remote sync (semantics change this round).
        cached = client.post(base + "/query", json={"capability": "grades"})
        assert cached.status_code == 200 and cached.json()["error"]["code"] == "AUTH_REQUIRED"
        assert copies == ["first"] and len(calls) == 1
        # The explicit sync entry re-adopts the restarted browser session once, then
        # retries the read once — the previous bounded-recovery property, preserved.
        response = client.post(base + "/sync", json={"capability": "grades"})
        assert response.status_code == 200 and response.json()["error"] is None
        assert copies == ["first", "restarted"] and len(calls) == 3
        assert client.post(base + "/disconnect", json={}).status_code == 200
        current[0].close()
        current[0] = CampusWorkspace(path, CookieClient)
        assert not client.get(base + "/status").json()["browser_session_authorized"]
    finally:
        current[0].close()


def test_explicit_sync_after_restart_reuses_verified_browser_and_cache_read_stays_offline(tmp_path, monkeypatch):
    path = tmp_path / "campus.sqlite"
    first = CampusWorkspace(path, CookieClient)
    first.authorize_browser_session(lambda: COOKIES)
    first.close()
    reopened = CampusWorkspace(path, CookieClient)
    copies, queries, syncs = [], [], []
    reopened.bind_browser_cookie_provider(lambda: copies.append(1) or COOKIES)
    try:
        assert not reopened.status()["connected"]
        monkeypatch.setattr(reopened.manager.service, "query", connection_query(reopened, queries))
        monkeypatch.setattr(reopened.manager.service, "sync", connection_query(reopened, syncs))
        # The explicit sync entry reuses the already verified browser session exactly
        # once: one authorized sync attempt, one adoption, then exactly one retry.
        result = reopened.query("grades", {}, refresh=True)
        assert result["error"] is None and result["records"]
        assert reopened.status()["connected"]
        assert copies == [1] and syncs == [1, 1] and queries == []
        # Afterwards a plain cache read is offline: it serves the cached records
        # without reading the browser provider or submitting another school sync.
        cached = reopened.query("grades", {})
        assert cached["error"] is None and cached["records"]
        assert copies == [1] and syncs == [1, 1] and queries == [1]
    finally:
        reopened.close()


def test_persisted_grant_daemon_stopped_keeps_auth_required(tmp_path, monkeypatch):
    path = tmp_path / "campus.sqlite"
    first = CampusWorkspace(path, CookieClient)
    first.authorize_browser_session(lambda: COOKIES)
    first.close()
    def stopped():
        raise ApiError(409, "BROWSER_AUTH_REQUIRED", "synthetic browser daemon is stopped")
    monkeypatch.setattr("app.agent_bridge.runtime.get_runtime", lambda: SimpleNamespace(
        browser=SimpleNamespace(campus_cookies_private=stopped)))
    reopened = CampusWorkspace(path, CookieClient)
    try:
        result = reopened.query("grades", {})
        assert result["error"]["code"] == "AUTH_REQUIRED" and not result["records"]
        status = reopened.status()
        assert status["browser_authorization"]["persisted"]
        assert status["browser_session_authorized"] and not status["connected"]
        assert not status["passport_connected"]
    finally:
        reopened.close()
