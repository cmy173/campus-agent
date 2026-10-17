"""C01/C02: a campus cache read never becomes authentication, browser access or a school request.

Boundary counters are taken on the three entries this workspace owns and that the
contract names:

* ``CampusWorkspace.auto_relogin_locked`` — the only password-submitting path;
* the private browser cookie provider — the only browser-credential read;
* ``CampusService.sync`` — the single remote-sync entry.

They are honest call counters on the real code path, not a packet capture: the
school transport for a private capability is reachable only through them, so a
zero count here is a zero school request. The pre-fix behaviour of the first test
in this file was reproduced against the baseline revision and is recorded in
``var/harness-campus-readonly-<uuid>/baseline-pre-fix/``.
"""
from __future__ import annotations

import pytest

from app.campus_workspace import CampusWorkspace
from test_campus_migration import CookieClient


COOKIES = [{"name": "iPlanetDirectoryPro", "value": "controlled-cas", "domain": "zjuam.zju.edu.cn",
            "path": "/", "expires": -1, "secure": True, "httpOnly": True}]


class FakeResult:
    """QueryResult-shaped fake; only the fields the workspace/tool layer reads."""

    def __init__(self, error=None, records=None, fetched_at="2026-10-14T10:00:00+08:00",
                 freshness=None, completeness="complete"):
        self.error = error
        self.records = records if records is not None else ([{"id": "synthetic"}] if not error else [])
        self.fetched_at = fetched_at
        # A stored error row always carries stale/unknown freshness: that is what the
        # sync writer persists (CampusService._sync_locked), so mirror it here.
        self.freshness = freshness if freshness is not None else ("stale" if error else "fresh")
        self.completeness = completeness
        self.warnings: list[dict] = []
        self.next_cursor = None
        self.platform = "zdbk"
        self.source = "grades"
        self.last_attempt_at = None

    def model_dump(self, **_):
        return {"error": {"code": self.error, "message": "合成认证失效"} if self.error else None,
                "records": self.records, "freshness": self.freshness, "completeness": self.completeness,
                "fetched_at": self.fetched_at, "platform": self.platform, "source": self.source,
                "warnings": self.warnings, "next_cursor": self.next_cursor,
                "last_attempt_at": self.last_attempt_at}


class Boundary:
    """Counts auth, browser-credential and remote-sync entries; never performs them."""

    def __init__(self, workspace, *, service_result=None, sync_results=None):
        self.workspace = workspace
        self.auth: list[str] = []
        self.browser: list[int] = []
        self.cache: list[int] = []
        self.sync: list[int] = []
        self.service_result = service_result
        self.sync_results = list(sync_results or [])
        self.auth_outcome = {"ok": False, "code": "NO_CREDENTIAL", "needs_user": False}

    def install(self, monkeypatch):
        monkeypatch.setattr(self.workspace, "auto_relogin_locked", self._auto_relogin)
        monkeypatch.setattr(self.workspace, "_browser_cookie_provider", self._provider)
        monkeypatch.setattr(self.workspace.manager.service, "query", self._cache_read)
        monkeypatch.setattr(self.workspace.manager.service, "sync", self._sync_read)
        return self

    def _auto_relogin(self):
        self.auth.append(self.auth_outcome["code"])
        return dict(self.auth_outcome)

    def _provider(self):
        self.browser.append(1)
        return COOKIES

    def _cache_read(self, *args, **kwargs):
        self.cache.append(1)
        return self.service_result if self.service_result is not None else FakeResult()

    def _sync_read(self, *args, **kwargs):
        self.sync.append(1)
        if self.sync_results:
            return self.sync_results[min(len(self.sync), len(self.sync_results)) - 1]
        return FakeResult()

    def reset(self):
        self.auth.clear(), self.browser.clear(), self.cache.clear(), self.sync.clear()


@pytest.fixture
def workspace(tmp_path):
    value = CampusWorkspace(tmp_path / "campus.sqlite", CookieClient)
    yield value
    value.close()


def granted_workspace(workspace):
    """Authorize a real grant whose private provider is the counting one."""
    boundary = Boundary(workspace)
    workspace._browser_cookie_provider = boundary._provider
    workspace.authorize_browser_session(boundary._provider)
    assert workspace.status()["browser_session_authorized"]
    boundary.reset()
    return boundary


@pytest.mark.parametrize("code", ["AUTH_REQUIRED", "AUTH_EXPIRED", "SESSION_EXPIRED",
                                  "PROTOCOL_CHANGED", "RATE_LIMITED"])
def test_cache_read_with_a_cached_error_never_authenticates_or_syncs(workspace, monkeypatch, code):
    """The reproduced defect: query(refresh=False) reached auto_relogin and then sync.

    Baseline (recorded pre-fix): cache=1, auth=1, browser=1, sync=1.
    """
    boundary = granted_workspace(workspace)
    boundary.service_result = FakeResult(error=code)
    boundary.install(monkeypatch)

    result = workspace.query("grades", {})

    assert result["error"]["code"] == code
    assert result["fetched_at"] == "2026-10-14T10:00:00+08:00"   # cached timestamp, not a fresh one
    assert boundary.cache == [1]
    assert boundary.auth == [] and boundary.browser == [] and boundary.sync == []
    assert result["freshness"] == "stale"  # cached data kept, labelled honestly


def test_cache_read_without_a_live_session_reports_the_error_and_stays_offline(workspace, monkeypatch):
    """Cached records plus a structured connection error; still no auth/sync/browser."""
    boundary = granted_workspace(workspace)
    boundary.service_result = FakeResult()          # cache holds usable records
    workspace.manager.disconnect("local")            # no live connector session
    boundary.install(monkeypatch)

    result = workspace.query("grades", {})

    assert result["error"]["code"] == "AUTH_REQUIRED"   # reported, not repaired
    assert result["records"] == [{"id": "synthetic"}]   # cache retained
    assert result["freshness"] == "stale"               # unconfirmable live read is not "fresh"
    assert boundary.cache == [1] and boundary.auth == [] and boundary.browser == [] and boundary.sync == []


def test_cached_read_surfaces_are_all_offline(workspace, monkeypatch):
    """C01: every cache read surface stays offline, one cache read each, no auth/browser/sync."""
    boundary = granted_workspace(workspace)
    boundary.service_result = FakeResult(records=[{"id": "synthetic"}])
    boundary.install(monkeypatch)

    surfaces = [("courses", {}, None), ("grades", {}, None), ("profile", {}, None),
                ("notices", {}, "zdbk_public"), ("college_notices", {}, None)]
    for capability, query, platform in surfaces:
        boundary.reset()
        workspace.query(capability, query, platform)
        assert (boundary.auth, boundary.browser, boundary.sync) == ([], [], []), capability
        assert len(boundary.cache) == 1, capability

    boundary.reset()
    workspace.status()
    assert (boundary.cache, boundary.auth, boundary.browser, boundary.sync) == ([], [], [], [])

    boundary.reset()
    statistics = workspace.manager.academic_view("local")
    assert statistics is not None
    assert (boundary.auth, boundary.browser, boundary.sync) == ([], [], [])
    assert boundary.cache  # the statistics are composed from cache reads only


def test_restart_or_expired_cache_is_not_repaired_by_a_cache_read(workspace, monkeypatch):
    """A process restart keeps no live session: the read reports it and does not re-login."""
    boundary = granted_workspace(workspace)
    boundary.service_result = FakeResult(error="SESSION_EXPIRED")
    boundary.install(monkeypatch)
    workspace.manager.disconnect("local")  # simulate the memory-only session after a restart

    first = workspace.query("grades", {})
    second = workspace.query("grades", {})

    assert first["error"]["code"] == "SESSION_EXPIRED" and second["error"]["code"] == "SESSION_EXPIRED"
    assert boundary.auth == [] and boundary.browser == [] and boundary.sync == []
    assert len(boundary.cache) == 2


def test_explicit_sync_is_the_only_entry_that_recovers_once(workspace, monkeypatch):
    """C02: the explicit sync re-uses the authorized browser session exactly once."""
    boundary = granted_workspace(workspace)
    boundary.service_result = FakeResult(records=[{"id": "stale"}])
    boundary.sync_results = [FakeResult(error="SESSION_EXPIRED"), FakeResult(records=[{"id": "fresh"}])]
    boundary.install(monkeypatch)

    result = workspace.query("courses", {}, refresh=True)

    assert result["error"] is None and result["records"] == [{"id": "fresh"}]
    assert boundary.auth == ["NO_CREDENTIAL"]   # one bounded attempt, and it was refused
    assert boundary.browser == [1]              # one authorized re-adoption
    assert boundary.sync == [1, 1]              # one requested sync plus one retry


def test_captcha_or_rejection_cools_down_without_resubmitting_the_password(workspace, monkeypatch):
    """A captcha/MFA/rejection answer permits exactly one attempt per cooldown window."""
    workspace.manager.disconnect("local")   # no grant: isolate the credential path
    boundary = Boundary(workspace, sync_results=[FakeResult(error="AUTH_REQUIRED")])
    boundary.auth_outcome = {"ok": False, "code": "CAPTCHA_REQUIRED", "needs_user": True}
    boundary.install(monkeypatch)

    workspace.query("grades", {}, refresh=True)
    workspace.query("grades", {}, refresh=True)

    assert boundary.auth == ["CAPTCHA_REQUIRED"]   # the second call is inside the cooldown
    assert boundary.browser == []


def test_auto_relogin_disabled_never_reaches_the_password_submission(workspace, monkeypatch):
    """User setting off: neither the credential entry nor an explicit sync submits a password."""
    from app import campus_workspace as module

    prepared: list[str] = []
    monkeypatch.setattr(module.campus_credentials, "load",
                        lambda scope: {"username": "synthetic", "password": "synthetic",
                                       "authcode": "", "auto_relogin": False, "saved_at": ""})
    monkeypatch.setattr(workspace.manager, "prepare", lambda owner: prepared.append("prepare"))
    monkeypatch.setattr(workspace.manager, "login", lambda *a, **k: prepared.append("login"))
    monkeypatch.setattr(workspace, "_browser_cookie_provider",
                        lambda: pytest.fail("password path must not read browser cookies"))
    monkeypatch.setattr(workspace.manager.service, "sync", lambda *a, **k: FakeResult(error="AUTH_REQUIRED"))
    workspace.manager.disconnect("local")   # no live session and no grant

    outcome = workspace.auto_relogin_locked()
    result = workspace.query("grades", {}, refresh=True)

    assert outcome["code"] == "AUTO_RELOGIN_DISABLED"
    assert result["error"]["code"] == "AUTH_REQUIRED"
    assert prepared == []


def test_recovery_free_refresh_never_starts_authentication(workspace, monkeypatch):
    """R1: the trusted read-only refresh uses only an already validated session.

    `refresh=True, allow_recovery=False` is what the recovery probe issues: it must return
    the structured AUTH error without touching the password path or the browser provider.
    The same call with the default keeps the user's bounded authorized recovery.
    """
    boundary = granted_workspace(workspace)
    boundary.auth_outcome = {"ok": True, "code": "AUTO_LOGIN_OK", "needs_user": False}
    boundary.service_result = FakeResult(records=[{"id": "stale"}])
    boundary.sync_results = [FakeResult(error="AUTH_REQUIRED"), FakeResult(records=[{"id": "fresh"}])]
    boundary.install(monkeypatch)

    trusted = workspace.query("grades", {}, refresh=True, allow_recovery=False)
    assert trusted["error"]["code"] == "AUTH_REQUIRED"      # structured error, checkpoint kept
    assert boundary.auth == [] and boundary.browser == []   # no password login, no browser read
    assert boundary.sync == [1]                            # exactly one read-only refresh attempt

    boundary.reset()
    explicit = workspace.query("grades", {}, refresh=True)
    assert explicit["error"] is None and explicit["records"] == [{"id": "fresh"}]
    assert boundary.auth == ["AUTO_LOGIN_OK"]              # the user's explicit sync still recovers
    assert boundary.browser == [] and boundary.sync == [1, 1]


class _NullSession:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_recovery_probe_restores_a_pending_cache_read_by_one_explicit_sync(monkeypatch):
    """A pending cache read is refreshed once at the trusted recovery entry, never as a read.

    The cache can hold an AUTH error persisted by an earlier failed sync, and the
    pure cache read (by design) will not clear it. Restoring the original read-only
    target therefore belongs to this entry, with the checkpoint's own narrow scope.
    """
    from app.agent_bridge import runtime as runtime_module
    from app.agent_bridge import tools as bridge_tools
    from app.agent_bridge.runtime import Runtime
    from app.errors import ApiError

    executed: list[tuple] = []
    monkeypatch.setattr(runtime_module, "SessionLocal", _NullSession)
    monkeypatch.setattr(bridge_tools, "execute",
                        lambda name, args, db, ctx, **k: executed.append((name, args, ctx, k)) or {"status": "ok"})

    class Harness:
        RECOVERY_REFRESH_TOOL = Runtime.RECOVERY_REFRESH_TOOL
        probe_authenticated_read = Runtime.probe_authenticated_read

        def _auth_recovery_scope(self, db, checkpoint):
            return "ctx:" + checkpoint["conversation_id"]

    query_args = {"capability": "courses", "query": {}, "platform": None}
    Harness().probe_authenticated_read({"conversation_id": "c1",
                                        "payload": {"pending_tool": {"name": "campus.query", "arguments": query_args}}})
    assert executed == [("campus.sync", query_args, "ctx:c1", {"allow_auth_recovery": False})]

    executed.clear()
    sync_args = {"capability": "grades", "query": {}, "platform": None}
    Harness().probe_authenticated_read({"conversation_id": "c1",
                                        "payload": {"pending_tool": {"name": "campus.sync", "arguments": sync_args}}})
    assert executed == [("campus.sync", sync_args, "ctx:c1", {"allow_auth_recovery": False})]

    executed.clear()
    with pytest.raises(ApiError):
        Harness().probe_authenticated_read({"conversation_id": "c1", "payload": {"pending_tool": {
            "name": "campus.notice.import",
            "arguments": {"record_id": "ckc_public:1", "course_id": "c1"}}}})
    assert executed == []   # an automatic recovery never probes a write


def test_recovery_probe_refuses_when_the_refresh_tool_is_not_read_only(monkeypatch):
    """Defence in depth: if the explicit refresh tool were not read-only, refuse to probe.

    The recovery entry escalates a pending cache read to `campus.sync` only while that
    tool is still declared a read effect; a widening change must stop the probe instead
    of granting automatic write authority.
    """
    from app.agent_bridge import runtime as runtime_module
    from app.agent_bridge import tools as bridge_tools
    from app.agent_bridge.runtime import Runtime
    from app.errors import ApiError

    executed: list[tuple] = []
    original = bridge_tools.schemas
    monkeypatch.setattr(runtime_module, "SessionLocal", _NullSession)
    monkeypatch.setattr(bridge_tools, "execute",
                        lambda name, args, db, ctx, **k: executed.append((name, args, k)) or {"status": "ok"})
    monkeypatch.setattr(bridge_tools, "schemas",
                        lambda: [s | {"effects": "write"} if s["name"] == "campus.sync" else s
                                 for s in original()])

    class Harness:
        RECOVERY_REFRESH_TOOL = Runtime.RECOVERY_REFRESH_TOOL
        probe_authenticated_read = Runtime.probe_authenticated_read

        def _auth_recovery_scope(self, db, checkpoint):
            return "ctx"

    with pytest.raises(ApiError):
        Harness().probe_authenticated_read({"conversation_id": "c1", "payload": {"pending_tool": {
            "name": "campus.query", "arguments": {"capability": "courses", "query": {}, "platform": None}}}})
    assert executed == []


def _cached_course_row():
    return {"records": [{"id": "course_cached", "name": "线性代数", "weekday": 7, "start_period": 1}],
            "source": "courses", "platform": "zdbk", "fetched_at": "2026-10-16T18:06:48+08:00",
            "last_attempt_at": "2026-10-16T18:06:48+08:00", "freshness": "stale",
            "completeness": "partial", "next_cursor": None, "warnings": [], "error": None}


def test_sync_resolve_failure_preserves_known_cache_instead_of_erasing_it(workspace, monkeypatch):
    """A sync that cannot resolve its platform must not replace cached records with a blank row."""
    from connectors.campus.campus_common import CampusError

    repository = workspace.manager.service.repository
    repository.write("local", "zdbk", "courses", "{}", _cached_course_row())

    def unresolved(*args, **kwargs):
        raise CampusError("UNSUPPORTED", "平台或能力尚未支持")

    monkeypatch.setattr(workspace.manager.service, "_resolve", unresolved)

    result = workspace.query("courses", {}, refresh=True, allow_recovery=False)

    assert result["error"]["code"] == "UNSUPPORTED"
    assert result["records"] == _cached_course_row()["records"]      # returned for this call
    assert result["freshness"] == "stale"
    assert any(warning.get("code") == "CACHE_RETAINED" for warning in result["warnings"])
    stored = repository.read("local", "zdbk", "courses", "{}")
    assert stored["records"] == _cached_course_row()["records"]      # not erased on disk


def test_sync_fetch_failure_under_a_different_scope_preserves_known_cache(workspace, monkeypatch):
    """A failed fetch with a cache-scope miss must not erase the newest cached records either."""
    from types import SimpleNamespace
    from connectors.campus.campus_common import CampusError

    repository = workspace.manager.service.repository
    repository.write("local", "zdbk", "courses", '{"semester":"1|秋"}', _cached_course_row())

    def failing_fetch(*args, **kwargs):
        raise CampusError("UNSUPPORTED", "平台或能力尚未支持")

    adapter = SimpleNamespace(spec=SimpleNamespace(platform_id="zdbk", authentication="public"),
                              fetch=failing_fetch)
    monkeypatch.setattr(workspace.manager.service, "_resolve",
                        lambda *args, **kwargs: (adapter, {}, None, '{"semester":null}'))
    monkeypatch.setattr(workspace.manager.service, "_read",
                        lambda *args, **kwargs: workspace.manager.service._blank("courses", "zdbk"))

    result = workspace.query("courses", {}, refresh=True, allow_recovery=False)

    assert result["error"]["code"] == "UNSUPPORTED"
    assert result["records"] == _cached_course_row()["records"]
    assert result["freshness"] == "stale"
    assert any(warning.get("code") == "CACHE_RETAINED" for warning in result["warnings"])
    newest = repository.read_latest("local", "courses")
    assert newest["records"] == _cached_course_row()["records"]


def _timetable_snapshot():
    return {"profile": None, "grades": None,
            "courses": {"data": [{"name": "常微分方程", "weekday": "1", "start_period": "6", "weeks": "",
                                  "year": "2026", "semester": "1|秋", "stale": False}],
                        "fetched_at": "2026-10-17T13:44:51+08:00", "stale": False,
                        "complete": True, "terms": [], "warnings": []}}


def test_timetable_records_exposes_the_synced_snapshot_without_network(workspace):
    workspace.manager.repository.write("local", _timetable_snapshot())
    records = workspace.timetable_records()
    assert records["records"][0]["name"] == "常微分方程"
    assert records["fetched_at"] == "2026-10-17T13:44:51+08:00"
    assert records["freshness"] == "fresh"
    assert records["completeness"] == "complete"
    assert records["platform"] == "zdbk" and records["error"] is None


def test_sync_timetable_failure_keeps_known_snapshot_and_reports_error(workspace, monkeypatch):
    from connectors.campus.campus_common import CampusError

    workspace.manager.repository.write("local", _timetable_snapshot())
    monkeypatch.setattr(workspace.manager, "status", lambda owner: {"connected": True})

    def failing_sync(owner):
        raise CampusError("SESSION_EXPIRED", "教务会话已失效，保留缓存")

    monkeypatch.setattr(workspace.manager, "sync", failing_sync)
    result = workspace.sync_timetable(allow_recovery=False)
    assert result["error"]["code"] in {"AUTH_EXPIRED", "SESSION_EXPIRED"}
    assert result["records"][0]["name"] == "常微分方程"
    assert result["freshness"] == "stale"


def test_sync_timetable_unexpected_client_failure_is_a_structured_tool_error(workspace, monkeypatch):
    workspace.manager.repository.write("local", _timetable_snapshot())
    monkeypatch.setattr(workspace.manager, "status", lambda owner: {"connected": True})

    def broken_sync(owner):
        raise AttributeError("grades")

    monkeypatch.setattr(workspace.manager, "sync", broken_sync)
    result = workspace.sync_timetable(allow_recovery=False)
    assert result["error"]["code"] == "TIMETABLE_SYNC_FAILED"
    assert result["error"]["retryable"] is True
    assert result["records"][0]["name"] == "常微分方程"   # cached timetable retained
    assert result["freshness"] == "stale"
