"""Closing the controlled window must not take the connector (and the launcher) down."""
from __future__ import annotations

from pathlib import Path

from connectors.browser import session_runner as sr


class TargetClosedError(Exception):
    """Same class name and message Playwright raises."""


class _Context:
    def cookies(self):
        raise TargetClosedError("BrowserContext.cookies: Target page, context or browser has been closed")


class _OtherContext:
    def cookies(self):
        raise ValueError("something else broke")


def test_closed_controlled_window_is_reported_as_a_lost_session(tmp_path: Path):
    session = sr.BrowserSession(profile_dir=tmp_path / "profile")
    session._context = _Context()
    session._snapshot = [{"name": "route", "value": "fixture-value"}]
    session.pump_once()  # must not raise: the window can be closed at any time
    assert session.cookies_snapshot() == []
    assert session.closed is True


def test_other_errors_still_propagate(tmp_path: Path):
    session = sr.BrowserSession(profile_dir=tmp_path / "profile")
    session._context = _OtherContext()
    try:
        session.pump_once()
    except ValueError:
        pass
    else:  # pragma: no cover - the guard must not swallow real bugs
        raise AssertionError("unexpected browser errors must not be swallowed")
