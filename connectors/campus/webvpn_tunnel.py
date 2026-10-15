"""Background WebVPN channel for the campus connectors.

Why this exists: the direct connectors (CAS -> ZDBK/ETA) only work on the campus
network. Off campus the same reads must go through the school's WebVPN portal.
This module owns that second channel so it can be used without opening a browser
window and without the model doing anything:

* one process-wide portal session, held in memory only (never persisted);
* the portal password comes from an app-injected provider (the local encrypted
  vault, scope ``webvpn``) and is never read from a file, env var, log or the
  model context by this module;
* exactly one bounded automatic re-authentication per cooldown window;
* ``zjuam``/``zdbk`` are the only mapped hosts today, and an unmapped host is
  never silently rewritten.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from .campus_common import CampusError
from .webvpn_adapter import HOST_TOKENS, WebVPNAdapter

# One failed automatic portal login per window. A captcha/lockout must fall back
# to a single manual login instead of hammering the school's login endpoint.
COOLDOWN_SECONDS = 300
# Off-campus detection is an explicit opt-out: on campus nothing changes.
MODE_AUTO = "auto"
MODE_DIRECT = "direct"
MODE_WEBVPN = "webvpn"
# The campus SSO host is publicly reachable and must stay on the direct channel: it is
# what authenticates *us*, and a proxied CAS login would put its session cookie inside
# the portal's jar, where the campus client cannot use it. Only the campus business
# hosts (zdbk, and any future registered host) are tunnelled.
DIRECT_ONLY_HOSTS = frozenset({"zjuam.zju.edu.cn"})

_provider_lock = threading.Lock()
_credential_provider: Optional[Callable[[], Optional[dict]]] = None


def register_credential_provider(provider: Optional[Callable[[], Optional[dict]]]) -> None:
    """App-owned hook. The provider returns ``{username, password, ...}`` or None.

    Returning None (nothing saved, or the user switched automatic re-login off)
    disables the channel entirely: no probe, no network, no credential read.
    """
    global _credential_provider
    with _provider_lock:
        _credential_provider = provider


def credential_provider():
    with _provider_lock:
        return _credential_provider


def _default_factory() -> WebVPNAdapter:
    return WebVPNAdapter()


class WebVPNTunnel:
    """Process-wide portal session with bounded automatic re-authentication.

    The session is deliberately *not* persisted: a restart re-authenticates from
    the vault, and a closed tunnel keeps no cookie, ticket or password.
    """

    def __init__(self, adapter_factory: Callable[[], WebVPNAdapter] = _default_factory, *,
                 clock: Callable[[], float] = time.monotonic, wall_clock: Callable[[], float] = time.time):
        self._factory = adapter_factory
        self._clock = clock
        self._wall = wall_clock
        self._lock = threading.RLock()
        self._adapter: Optional[WebVPNAdapter] = None
        self._retry_after = 0.0
        self.attempts = 0
        self.last_error: Optional[dict] = None
        self.authenticated_at: Optional[float] = None
        self.replaced_other_session = False
        self.last_used_at: Optional[float] = None

    # ---------------------------------------------------------------- state
    def stored_credentials(self) -> Optional[dict]:
        """Never raises and never logs: a broken provider disables the channel."""
        provider = credential_provider()
        if provider is None:
            return None
        try:
            stored = provider()
        except Exception:
            return None
        if not isinstance(stored, dict):
            return None
        username, password = stored.get("username"), stored.get("password")
        if not isinstance(username, str) or not username.strip():
            return None
        if not isinstance(password, str) or not password:
            return None
        return stored

    def configured(self) -> bool:
        return self.stored_credentials() is not None

    def cooldown_active(self) -> bool:
        return self._clock() < self._retry_after

    def status(self) -> dict:
        adapter = self._adapter
        return {
            "configured": self.configured(),
            "authenticated": bool(adapter is not None and adapter.is_session_valid()),
            "cooldown_active": self.cooldown_active(),
            "cooldown_seconds": COOLDOWN_SECONDS,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "authenticated_at": self.authenticated_at,
            "replaced_other_session": self.replaced_other_session,
            "mapped_hosts": sorted(HOST_TOKENS),
            "unmapped_hosts": ["eta.zju.edu.cn"],
            "direct_only_hosts": sorted(DIRECT_ONLY_HOSTS),
            "note": "WebVPN 凭据与本机加密保存在与通行证分开的槽位；会话只在内存中，不落盘、不进日志与对话。",
        }

    def invalidate(self) -> None:
        """Drop the in-memory portal session (used on an expired-session answer)."""
        with self._lock:
            if self._adapter is not None:
                self._adapter.close()
            self._adapter = None
            self.authenticated_at = None

    def session_jar(self):
        """The live portal cookie jar, or None when no portal session exists.

        Read-only accessor: it never performs a login, so a cookie check cannot
        turn into a school request.
        """
        adapter = self._adapter
        if adapter is None or not adapter.is_session_valid():
            return None
        return adapter.cookies

    def close(self) -> None:
        with self._lock:
            self.invalidate()
            self._retry_after = 0.0
            self.last_error = None

    # ------------------------------------------------------------ session
    def ensure_session(self, *, force: bool = False):
        """Return a live portal session, re-authenticating at most once per window."""
        with self._lock:
            if not force and self._adapter is not None and self._adapter.is_session_valid():
                return self._adapter
            stored = self.stored_credentials()
            if stored is None:
                raise CampusError("WEBVPN_NOT_CONFIGURED",
                                  "尚未保存 WebVPN 凭据（WebVPN 账号与校园通行证分开保存）")
            now = self._clock()
            if not force and now < self._retry_after:
                raise CampusError("WEBVPN_COOLDOWN",
                                  "WebVPN 自动登录失败后进入冷却，避免频繁请求校方；请稍后重试或手动登录")
            adapter = self._factory()
            self.attempts += 1
            try:
                adapter.login(stored["username"], stored["password"])
            except CampusError as exc:
                adapter.close()
                self._retry_after = now + COOLDOWN_SECONDS
                # Never copy a school response body or the password into the record.
                self.last_error = {"code": exc.code, "at": self._wall()}
                raise
            except ValueError as exc:
                adapter.close()
                self._retry_after = now + COOLDOWN_SECONDS
                self.last_error = {"code": "WEBVPN_LOGIN_FAILED", "at": self._wall()}
                raise CampusError("WEBVPN_LOGIN_FAILED", str(exc)) from None
            except Exception:
                adapter.close()
                self._retry_after = now + COOLDOWN_SECONDS
                self.last_error = {"code": "WEBVPN_UNAVAILABLE", "at": self._wall()}
                raise CampusError("WEBVPN_UNAVAILABLE", "WebVPN 登录未能完成，请稍后重试") from None
            self.invalidate()
            self._adapter = adapter
            self.authenticated_at = self._wall()
            self.last_error = None
            self._retry_after = 0.0
            # The school allows one portal session per account; report a takeover so the
            # user can see why their browser's WebVPN login stopped working.
            self.replaced_other_session = bool(getattr(adapter, "replaced_other_session", False))
            return adapter

    # -------------------------------------------------------------- channel
    def mapped(self, host: str) -> bool:
        return host in HOST_TOKENS

    def uses_channel(self, host: str) -> bool:
        return self.configured() and self.mapped(host) and host not in DIRECT_ONLY_HOSTS

    def open(self, url: str, *, data: Optional[bytes] = None, headers: Optional[dict] = None,
             timeout: float = 30):
        """Open one campus URL through the portal; never falls back to direct."""
        adapter = self.ensure_session()
        self.last_used_at = self._wall()
        return adapter.open(url, data=data, headers=dict(headers or {}), timeout=timeout)

    def request(self, url: str, *, form: Optional[str] = None, headers: Optional[dict] = None,
                timeout: float = 30):
        """Same contract as CampusTransport.open: one retry after re-auth, then give up."""
        attempt = 0
        while True:
            attempt += 1
            try:
                return self.open(url, data=form.encode("utf-8") if form is not None else None,
                                 headers=headers, timeout=timeout)
            except CampusError as exc:
                # An expired portal session is the one condition worth re-authing for.
                if exc.code == "WEBVPN_SESSION_EXPIRED" and attempt == 1:
                    self.invalidate()
                    continue
                raise


_tunnel: Optional[WebVPNTunnel] = None
_tunnel_lock = threading.Lock()


def tunnel() -> WebVPNTunnel:
    """The process-wide channel. Tests replace it via :func:`reset_tunnel`."""
    global _tunnel
    with _tunnel_lock:
        if _tunnel is None:
            _tunnel = WebVPNTunnel()
        return _tunnel


def reset_tunnel(replacement: Optional[WebVPNTunnel] = None) -> WebVPNTunnel:
    global _tunnel
    with _tunnel_lock:
        _tunnel = replacement if replacement is not None else WebVPNTunnel()
        return _tunnel


def channel_mode() -> str:
    import os
    value = (os.environ.get("CAMPUS_WEBVPN_MODE") or MODE_AUTO).strip().lower()
    return value if value in (MODE_AUTO, MODE_DIRECT, MODE_WEBVPN) else MODE_AUTO


def open_with_channel(url: str, *, form: Optional[str] = None, headers: Optional[dict] = None,
                      timeout: float = 30):
    """Module-level convenience used by tests and one-off scripts."""
    return tunnel().request(url, form=form, headers=headers, timeout=timeout)


def probe_login(force: bool = True) -> dict:
    """Explicit user-triggered verification of the stored WebVPN credential."""
    try:
        tunnel().ensure_session(force=force)
    except CampusError as exc:
        return {"ok": False, "code": exc.code, "needs_user": exc.code != "WEBVPN_NOT_CONFIGURED"}
    return {"ok": True, "code": "WEBVPN_LOGIN_OK", "needs_user": False}


__all__ = [
    "COOLDOWN_SECONDS", "DIRECT_ONLY_HOSTS", "MODE_AUTO", "MODE_DIRECT", "MODE_WEBVPN", "WebVPNTunnel",
    "channel_mode", "open_with_channel", "probe_login", "register_credential_provider",
    "reset_tunnel", "tunnel",
]
