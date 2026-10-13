"""Existing login/SSO reuse must work even when no login form appears."""
from pathlib import Path
from unittest.mock import Mock, patch
import threading

from connectors.browser.session_runner import BrowserSession, ConnectorService
from connectors.tests.test_session_runner import FakeAdapter, FakeSessions


def state(name):
    return {"platform": "xzzd", "state": name, "checked_at": "2026-10-12T00:00:00Z",
            "action": None, "detail": None}


def test_existing_authenticated_snapshot_never_requires_login_page():
    sessions = FakeSessions()
    sessions.refresh_for_check = Mock()
    service = ConnectorService(sessions, adapter_factory=lambda *_: FakeAdapter(state=state("ok")))
    assert service.handle("check", {})[1]["result"]["state"] == "ok"
    sessions.refresh_for_check.assert_not_called()


def test_expired_platform_cookie_can_reuse_sso_then_recheck():
    sessions = FakeSessions()
    sessions.refresh_for_check = Mock(return_value=True)
    results = iter(["auth_required", "ok"])
    service = ConnectorService(sessions, adapter_factory=lambda *_: FakeAdapter(state=state(next(results))))
    assert service.handle("check", {})[1]["result"]["state"] == "ok"
    sessions.refresh_for_check.assert_called_once()


def test_missing_snapshot_can_be_restored_without_user_login():
    sessions = FakeSessions(with_session=False)
    def refresh():
        sessions.with_session = True
        return True
    sessions.refresh_for_check = refresh
    service = ConnectorService(sessions, adapter_factory=lambda *_: FakeAdapter(state=state("ok")))
    assert service.handle("check", {})[1]["result"]["state"] == "ok"


def test_refresh_timeout_does_not_claim_login_is_expired():
    sessions = FakeSessions(with_session=False)
    sessions.refresh_for_check = Mock(return_value=False)
    service = ConnectorService(sessions)
    assert service.handle("check", {})[1]["result"]["state"] == "network_error"


def test_still_expired_after_refresh_is_bounded():
    sessions = FakeSessions()
    sessions.refresh_for_check = Mock(return_value=True)
    service = ConnectorService(sessions, adapter_factory=lambda *_: FakeAdapter(state=state("auth_required")))
    assert service.handle("check", {})[1]["result"]["state"] == "auth_required"
    sessions.refresh_for_check.assert_called_once()


def test_start_and_refresh_visit_platform_home_and_capture_redirected_session():
    browser = BrowserSession(profile_dir=Path("unused-test-profile"))
    context = Mock()
    context.cookies.return_value = [{"name": "session", "value": "test-only"}]
    page = Mock()
    page.goto.side_effect = RuntimeError("ERR_ABORTED: existing SSO session redirected")
    with patch("connectors.browser.session_runner._launch", return_value=(Mock(), context, page)):
        browser.start()
    assert page.goto.call_args.args[0] == "https://courses.zju.edu.cn/"
    event = threading.Event()
    browser._refresh_requests.put(event)
    browser.pump_once()
    assert event.is_set()
    assert browser.cookies_snapshot() == context.cookies.return_value
    assert page.goto.call_count == 2
