"""Single-person ZJU workspace, using migrated connectors, never the legacy server.

Cookie adoption is a private Python capability. No model tool accepts cookies,
passwords, a school identity or an arbitrary remote endpoint.
"""
from __future__ import annotations

from http.cookiejar import Cookie
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

from . import campus_credentials
from connectors.campus import webvpn_tunnel
from connectors.campus.campus import CampusClient, CampusManager, merge_profile
from connectors.campus.campus_common import CampusError
from connectors.campus.campus_store import CAMPUS_SCHEMA, SQLiteStore
from connectors.campus.connector_contracts import UserContext, standard_error

from .config import REPO_ROOT
from .errors import ApiError

OWNER = "local"


def stored_webvpn_credentials():
    """Injected into the connector channel: the vault is the only credential source.

    Returns None when nothing is saved or the user switched automatic re-login
    off, which disables the WebVPN channel entirely (no probe, no school request).
    """
    stored = campus_credentials.load("webvpn")
    if not stored or not stored.get("auto_relogin"):
        return None
    return stored


# Connectors never import app code; the app registers its own credential source.
webvpn_tunnel.register_credential_provider(stored_webvpn_credentials)
IMPORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS import_batches(id TEXT PRIMARY KEY,source_fingerprint TEXT NOT NULL,
 source_owner TEXT NOT NULL,target_owner TEXT NOT NULL,manifest TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS imported_records(batch_id TEXT NOT NULL REFERENCES import_batches(id),
 table_name TEXT NOT NULL,record_id TEXT NOT NULL,payload TEXT NOT NULL,content BLOB,
 PRIMARY KEY(batch_id,table_name,record_id));
CREATE TABLE IF NOT EXISTS import_changes(batch_id TEXT NOT NULL REFERENCES import_batches(id),
 table_name TEXT NOT NULL,key_json TEXT NOT NULL,before_json TEXT,after_json TEXT NOT NULL,
 PRIMARY KEY(batch_id,table_name,key_json));
"""

# App-owned consent metadata only: no browser profile paths, passwords or cookies.
BROWSER_GRANT_SCOPE = "official_campus_read:v1"
# One failed automatic re-authentication per window; a captcha or MFA challenge must
# fall back to a single manual login instead of retrying the school's endpoint.
CREDENTIAL_COOLDOWN_SECONDS = 300
BROWSER_GRANT_SCHEMA = """
CREATE TABLE IF NOT EXISTS campus_browser_grants(
 owner TEXT PRIMARY KEY, grant_id TEXT NOT NULL, scope TEXT NOT NULL,
 identity_sha256 TEXT NOT NULL, authorized_at REAL NOT NULL);
"""


def _runtime_browser_cookies() -> list[dict]:
    """Resolve the current private runtime capability on each authorized read.

    Never hold a bound method from an old runtime or boot a browser daemon here.
    The provider itself refuses access unless its owned browser is already live.
    """
    from .agent_bridge.runtime import get_runtime
    return get_runtime().browser.campus_cookies_private()


class CacheStore(SQLiteStore):
    """Campus cache schema plus exact historical-import provenance; no passwords/local accounts."""

    def __init__(self, path: Path):
        super().__init__(path, schema=CAMPUS_SCHEMA + IMPORT_SCHEMA + BROWSER_GRANT_SCHEMA)


class CampusWorkspace:
    def __init__(self, path: Path, client_factory=CampusClient):
        self.store = CacheStore(path)
        self.manager = CampusManager(self.store, client_factory=client_factory)
        # Consent survives API restarts; the private capability and cookies never do.
        # Rebinding a provider does not authorize it or read browser credentials.
        self._browser_cookie_provider = _runtime_browser_cookies
        self._browser_retry_after = 0.0
        self._browser_live_grant_id = None
        # Bounded auto-relogin: never hammer the school's authentication endpoint.
        self._credential_retry_after = 0.0
        self.last_login: dict | None = None

    def _browser_grant(self, db=None):
        if db is None:
            with self.store.connect() as connection:
                return self._browser_grant(connection)
        row = db.execute("SELECT * FROM campus_browser_grants WHERE owner=?", (OWNER,)).fetchone()
        if (row is None or row["scope"] != BROWSER_GRANT_SCOPE
                or not isinstance(row["identity_sha256"], str) or len(row["identity_sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in row["identity_sha256"])
                or not isinstance(row["grant_id"], str) or len(row["grant_id"]) != 32
                or any(char not in "0123456789abcdef" for char in row["grant_id"])
                or not isinstance(row["authorized_at"], (int, float))
                or not math.isfinite(row["authorized_at"]) or row["authorized_at"] <= 0):
            return None  # Unknown/invalid consent metadata never expands permissions.
        return dict(row)

    def bind_browser_cookie_provider(self, cookie_provider) -> None:
        """Rebind a trusted private capability; this is not a consent operation."""
        with self.manager.lock(OWNER):
            self._browser_cookie_provider = cookie_provider

    def _check_browser_grant(self):
        grant = self._browser_grant()
        if self._browser_live_grant_id and (not grant or grant["grant_id"] != self._browser_live_grant_id):
            # A second workspace can revoke consent. Do not keep its adopted client
            # alive or trust a stale in-memory grant after permanent disconnect.
            self.manager.disconnect(OWNER)
            self._browser_live_grant_id = None
        return grant

    def status(self):
        with self.manager.lock(OWNER):
            grant = self._check_browser_grant()
            return self._status(grant)

    def _status(self, grant):
        return {"school": "浙江大学", "local_owner": OWNER,
                "login_url": "https://zjuam.zju.edu.cn/cas/login?service=https%3A%2F%2Fzdbk.zju.edu.cn%2Fjwglxt%2Fxtgl%2Flogin_ssologin.html",
                **self.manager.status(OWNER),
                **self.manager.service.connection_status(UserContext(OWNER)),
                "login_mode": "persistent_browser",
                "webvpn": self._webvpn_summary(),
                "session_persistence": "browser_profile; authorization metadata persisted; connector cookies are memory-only",
                # Authorization is persisted consent, NOT current browser/connector liveness.
                # `connected`/connector statuses above remain verified memory-only state.
                "browser_session_authorized": grant is not None,
                # Credential summary is masked; values never leave the secret store.
                "credentials": {"passport": campus_credentials.status("passport"),
                                "webvpn": campus_credentials.status("webvpn")},
                "last_auto_login": self.last_login,
                "browser_authorization": {"persisted": grant is not None,
                    "scope": grant["scope"] if grant else None,
                    "authorized_at": grant["authorized_at"] if grant else None}}

    def authorize_browser_session(self, cookie_provider=None) -> dict:
        """Trusted settings/account UI consent; no credential parameters on HTTP.

        Official probe and identity verification precede durable consent. Failed
        attempts preserve the previous usable session and do not create a grant.
        """
        with self.manager.lock(OWNER):
            provider = cookie_provider or self._browser_cookie_provider
            self.adopt_browser_cookies(provider(), _authorize=True)
            self._browser_cookie_provider = provider
            self._browser_retry_after = 0.0
            return self.status()

    def disconnect(self, *, clear_cache=False) -> dict:
        with self.manager.lock(OWNER):
            # Delete durable consent before releasing any live state. close() must
            # never call this method: process shutdown is not user revocation.
            with self.store.connect() as db:
                db.execute("DELETE FROM campus_browser_grants WHERE owner=?", (OWNER,))
            self._browser_live_grant_id = None
            self._browser_retry_after = 0.0
            self.manager.disconnect(OWNER, delete=clear_cache)
            return self.status()

    def query(self, capability, query, platform=None, *, refresh=False, allow_recovery=True):
        """Read the cache, or explicitly synchronize the school site.

        ``refresh=False`` is a pure cache read: it never opens a school connection,
        never reads browser cookies and never submits a stored password. It returns
        the cached records with their real freshness/fetched_at plus a structured
        connection error when no live authorized session can confirm them.

        ``refresh=True`` is the explicit sync entry (agent ``campus.sync``,
        ``POST /campus/sync``, trusted recovery probe). Only it may make a remote
        request.

        ``allow_recovery`` separates the two callers of that entry:

        * the user's/model's explicit ``campus.sync`` keeps ``allow_recovery=True`` and
          therefore its bounded, authorized re-authentication (at most one stored-credential
          login or already-authorized browser-session re-adoption, then one retry);
        * the trusted read-only recovery probe passes ``allow_recovery=False``: it refreshes
          the original read **with the session that is already validated** and, when that
          session is not ready, returns the structured AUTH error so the caller keeps the
          checkpoint. It never starts a password login or browser-cookie adoption, so an
          unrelated platform's successful check cannot turn into authentication here.
        """
        operation = self.manager.service.sync if refresh else self.manager.service.query
        with self.manager.lock(OWNER):
            self._check_browser_grant()
            result = operation(capability, query, UserContext(OWNER), platform=platform).model_dump(mode="json")
            if not refresh:
                return self._cache_read_result(capability, query, platform, result)
            return self._resync_result(capability, query, platform, result, allow_recovery=allow_recovery)

    def _cache_read_result(self, capability, query, platform, result):
        """Cached records stay usable, but a cache read never becomes an auth attempt."""
        if not result.get("error"):
            # CampusService.query is cache-only and can return empty/no-error even
            # without a session. Keep cached records but do not represent that as a
            # live private connection; also allow restart recovery.
            adapter, _, _, _ = self.manager.service._resolve(capability, query, UserContext(OWNER), platform)
            if adapter.spec.authentication != "public":
                # Reading the in-memory session state is local: no connection is
                # opened and no browser credential is read. It only separates
                # "the cache holds this data" from "a live private session confirms it".
                try:
                    self.manager.connector_session(adapter.spec.platform_id, OWNER)
                except CampusError as exc:
                    error = standard_error(exc)
                    result["error"] = error.model_dump(mode="json")
                    result["freshness"] = "stale" if result.get("fetched_at") else "unknown"
        return result

    def _resync_result(self, capability, query, platform, result, *, allow_recovery=True):
        """Explicit sync: at most one authorized re-authentication, then one retry.

        With ``allow_recovery=False`` (the trusted read-only probe) no authentication is
        started at all: an auth error is returned as-is so the caller keeps its checkpoint.
        """
        with self.manager.lock(OWNER):
            error = result.get("error") or {}
            if error.get("code") not in {"AUTH_REQUIRED", "AUTH_EXPIRED", "SESSION_EXPIRED"}:
                return result
            if not allow_recovery:
                # Trusted read-only refresh: use the already validated session only.
                return result
            adapter, _, _, _ = self.manager.service._resolve(capability, query, UserContext(OWNER), platform)
            if adapter.spec.authentication == "public":
                return result  # Public-site failures cannot trigger private credentials/browser adoption.
            # 1) Stored credentials re-authenticate without any browser window or user
            #    action. This is the low-friction path when the session expires often.
            if time.monotonic() >= self._credential_retry_after:
                outcome = self.auto_relogin_locked()
                if outcome.get("ok"):
                    retried = self.manager.service.sync(capability, query, UserContext(OWNER), platform=platform).model_dump(mode="json")
                    if not retried.get("error"):
                        self._credential_retry_after = 0.0
                    return retried
                if outcome.get("needs_user") or outcome.get("code") in {
                        "CAPTCHA_REQUIRED", "INTERACTION_REQUIRED", "CAS_REJECTED", "CREDENTIAL_REJECTED",
                        "RATE_LIMITED"}:
                    # One bounded attempt only; captcha/MFA/wrong password must not loop.
                    self._credential_retry_after = time.monotonic() + CREDENTIAL_COOLDOWN_SECONDS
            # 2) Otherwise reuse an already logged-in browser session if consent exists.
            grant = self._check_browser_grant()
            if (grant is None or self._browser_cookie_provider is None
                    or time.monotonic() < self._browser_retry_after):
                return result
            current_grant = self._check_browser_grant()
            if not current_grant or current_grant["grant_id"] != grant["grant_id"]:
                return result
            # Only registered read-only campus capabilities can reach this path.
            # Re-adopt an already logged-in browser session once, never submit a
            # login form/replay a write or loop on an expired browser session.
            self._browser_retry_after = time.monotonic() + 30
            try:
                self.adopt_browser_cookies(self._browser_cookie_provider(), _grant=grant)
            except Exception:
                # Daemon down, private RPC failure, probe/identity mismatch: retain
                # the original honest result/cache, never invent online state or consent.
                return result
            current_grant = self._check_browser_grant()
            if not current_grant or current_grant["grant_id"] != grant["grant_id"]:
                return result
            # Session adoption alone cannot clear an AUTH error persisted in the
            # read cache. Refresh the original registered read capability once,
            # never replay a campus mutation or broaden its query scope.
            retried = self.manager.service.sync(capability, query, UserContext(OWNER), platform=platform).model_dump(mode="json")
            if not retried.get("error"):
                self._browser_retry_after = 0.0
            return retried

    def timetable_records(self):
        """The synced ZDBK timetable snapshot shaped like a connector cache result.

        This is cache-only (no network): the workbench-era campus manager owns the
        timetable, while the connector adapters never implemented ``courses``. Exposing
        the snapshot here is what lets the Agent answer timetable questions.
        """
        snapshot = self.manager.repository.read(OWNER) or {}
        courses = snapshot.get("courses") or {}
        rows = []
        for row in courses.get("data") or []:
            row = dict(row)
            if courses.get("stale"):
                row["stale"] = True
            rows.append(row)
        fetched_at = courses.get("fetched_at")
        stale = bool(courses.get("stale") or not fetched_at)
        return {"records": rows,
                "completeness": "complete" if courses.get("complete") else "partial",
                "freshness": "stale" if stale else "fresh",
                "fetched_at": fetched_at, "last_attempt_at": fetched_at,
                "platform": "zdbk", "source": "zdbk-timetable", "capability": "courses",
                "warnings": list(courses.get("warnings") or []),
                "next_cursor": None, "error": None}

    def sync_timetable(self, *, allow_recovery=True):
        """Refresh the real ZDBK timetable with at most one stored-credential login.

        The connector-adapter sync returns UNSUPPORTED for ``courses``; the timetable is
        owned by the workbench-era campus manager. Without this entry the Agent could not
        read a live timetable even when the app already has a valid stored session. No
        password prompt or browser interaction is started: only an already connected
        session or one bounded authorized re-login, then a real read-only sync.
        """
        with self.manager.lock(OWNER):
            state = self.manager.status(OWNER)
            if not state.get("connected") and allow_recovery and time.monotonic() >= self._credential_retry_after:
                outcome = self.auto_relogin_locked()
                if not outcome.get("ok"):
                    failure = self.timetable_records()
                    failure["error"] = {"code": str(outcome.get("code") or "AUTH_REQUIRED"),
                                        "message": "校园登录需要用户完成验证，已保留上次课表缓存",
                                        "retryable": False}
                    failure["freshness"] = "stale" if failure.get("fetched_at") else "unknown"
                    return failure
            try:
                self.manager.sync(OWNER)
            except CampusError as exc:
                failure = self.timetable_records()
                failure["error"] = standard_error(exc).model_dump(mode="json")
                failure["freshness"] = "stale" if failure.get("fetched_at") else "unknown"
                return failure
            return self.timetable_records()

    def credential_status(self) -> dict:
        return campus_credentials.all_status() | {
            "last_login": self.last_login,
            "cooldown_seconds": CREDENTIAL_COOLDOWN_SECONDS,
            "cooldown_active": time.monotonic() < self._credential_retry_after,
            "webvpn_channel": self.webvpn_status(),
        }

    def webvpn_status(self) -> dict:
        """Truthful channel state: whether the connector path uses the portal today."""
        status = webvpn_tunnel.tunnel().status()
        from connectors.campus.campus_endpoints import CAS, SERVICE
        from urllib.parse import urlsplit
        hosts = {urlsplit(url).hostname for url in (CAS, SERVICE)}
        status["mode"] = webvpn_tunnel.channel_mode()
        status["channel_active"] = {
            host: webvpn_tunnel.tunnel().uses_channel(host) for host in sorted(h for h in hosts if h)
        }
        return status

    def test_webvpn_login(self) -> dict:
        """Explicit user-triggered verification of the separately stored WebVPN credential."""
        outcome = webvpn_tunnel.probe_login()
        return outcome | {"status": self.webvpn_status()}

    def _webvpn_summary(self) -> dict:
        """One truthful line: who owns the off-campus channel right now.

        `connector` means a stored WebVPN credential lets the backend reach the
        mapped hosts itself; `browser` means only the user's browser session can.
        """
        channel = webvpn_tunnel.tunnel()
        configured = channel.configured()
        return {"login_url": "https://webvpn.zju.edu.cn/login",
            "mode": "connector" if configured else "persistent_browser",
            "session_status": "ready" if channel.uses_channel("zdbk.zju.edu.cn") else "check_in_browser",
            "supports": ["cas", "zdbk"], "mapped_hosts": sorted(channel.status()["mapped_hosts"]),
            "note": "WebVPN是独立内网通道，账号与校园通行证分开保存；教务/通行证仍需学校登录，不把VPN登录当作教务登录"}

    def save_credentials(self, scope, *, username, password, authcode="", auto_relogin=True) -> dict:
        """Trusted settings action only; the router never echoes credential values."""
        try:
            status = campus_credentials.save(scope, username=username, password=password,
                                            authcode=authcode, auto_relogin=auto_relogin)
        except campus_credentials.CredentialInputError as exc:
            raise ApiError(400, "INVALID_INPUT", str(exc)) from None
        self._credential_retry_after = 0.0
        return status

    def set_auto_relogin(self, scope, enabled: bool) -> dict:
        try:
            return campus_credentials.set_auto_relogin(scope, enabled)
        except campus_credentials.CredentialInputError as exc:
            raise ApiError(409, "NOT_CONFIGURED", str(exc)) from None

    def clear_credentials(self, scope) -> dict:
        self._credential_retry_after = 0.0
        self.last_login = None
        return campus_credentials.clear(scope)

    def auto_relogin_locked(self) -> dict:
        """Re-authenticate with the stored credential while holding the owner lock.

        Returns codes only: no exception text, no password, no browser interaction.
        """
        stored = campus_credentials.load("passport")
        if not stored:
            return {"ok": False, "code": "NO_CREDENTIAL"}
        if not stored.get("auto_relogin"):
            return {"ok": False, "code": "AUTO_RELOGIN_DISABLED"}
        try:
            flow = self.manager.prepare(OWNER)
        except CampusError as exc:
            return self._login_outcome(False, exc.code, needs_user=True)
        except Exception:
            return self._login_outcome(False, "AUTO_LOGIN_UNAVAILABLE", needs_user=True)
        if not isinstance(flow, dict) or not flow.get("flow_id"):
            self._discard_login_flow()
            return self._login_outcome(False, "AUTO_LOGIN_UNAVAILABLE", needs_user=True)
        # The official login page always renders a captcha image, so `flow["captcha"]`
        # being present is NOT evidence that this account must solve one: the school
        # only enforces it for some accounts/attempts. Attempt exactly one login and
        # let the CAS answer decide; a real captcha challenge comes back as
        # CAPTCHA_REQUIRED from authenticate_passport. Bailing out here made the whole
        # feature permanently "unavailable" against the real school.
        try:
            self.manager.login(OWNER, flow.get("flow_id"), stored["username"], stored["password"],
                               stored.get("authcode", ""))
        except CampusError as exc:
            needs_user = exc.code in {"CAPTCHA_REQUIRED", "INTERACTION_REQUIRED", "AUTH_CONTEXT_INVALID",
                                      "FLOW_EXPIRED", "ACCESS_REJECTED", "CAS_REJECTED", "CREDENTIAL_REJECTED"}
            return self._login_outcome(False, exc.code, needs_user=needs_user)
        except Exception:
            return self._login_outcome(False, "AUTO_LOGIN_FAILED", needs_user=True)
        return self._login_outcome(True, "AUTO_LOGIN_OK")

    def _discard_login_flow(self) -> None:
        try:
            self.manager.disconnect(OWNER)
        except Exception:
            pass

    def _login_outcome(self, ok: bool, code: str, *, needs_user: bool = False) -> dict:
        self.last_login = {"ok": ok, "code": code, "needs_user": needs_user,
                           "at": time.time(), "automatic": True}
        return {"ok": ok, "code": code, "needs_user": needs_user}

    def webvpn_entry(self, service: str) -> dict:
        from connectors.campus.webvpn_adapter import HOST_TOKENS
        from connectors.campus.campus_endpoints import CAS, SERVICE
        from urllib.parse import urlencode, urlsplit
        if service not in {"cas", "zdbk"}:
            raise ApiError(400, "INVALID_INPUT", "未登记的WebVPN服务")
        original = CAS+"login?"+urlencode({"service": SERVICE}) if service == "cas" else SERVICE
        parts = urlsplit(original)
        token = HOST_TOKENS[parts.hostname]
        channel = webvpn_tunnel.tunnel().uses_channel(parts.hostname)
        return {"service": service, "url": "https://webvpn.zju.edu.cn/https/"+token+parts.path+("?"+parts.query if parts.query else ""),
            "login_url": "https://webvpn.zju.edu.cn/login", "token_source": "observed portal mappings 2026-10-12; live revalidation required",
            # With a stored WebVPN credential the backend owns this channel itself;
            # without one the only available path is still the user's browser.
            "execution": "connector" if channel else "browser",
            "direct_connector_uses_webvpn": channel}

    def adopt_browser_cookies(self, cookies: list[dict], *, _authorize=False, _grant=None) -> dict:
        """Privately adopt trusted Playwright context cookies; return status, no cookies.

        Accept host-scoped ZJU cookies only, retain HttpOnly/secure/expiry/path.
        A browser cookie is not proof of successful login: probe the reviewed ZDBK
        menu via connect_zdbk before replacing the current usable client. A failed
        probe leaves the previous session/cache intact. New student ID detection
        in migrated profile protocols refuses cross-identity merges.
        """
        if not isinstance(cookies, list) or len(cookies) > 2048:
            raise ApiError(400, "INVALID_INPUT", "浏览器会话格式无效")
        client = self.manager.factory()
        try:
            for item in cookies:
                if not isinstance(item, dict):
                    continue
                domain = str(item.get("domain", "")).lstrip(".").lower()
                name = str(item.get("name", ""))
                if domain not in {"zjuam.zju.edu.cn", "zdbk.zju.edu.cn", "eta.zju.edu.cn", "zju.edu.cn"}:
                    continue
                # A parent cookie is scoped to CAS only (same policy as the wire connector).
                if domain == "zju.edu.cn":
                    domain = "zjuam.zju.edu.cn"
                if (not name or len(name) > 512 or not isinstance(item.get("value"), str)
                        or len(item["value"]) > 16384):
                    continue
                expires = item.get("expires", -1)
                if (isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires)
                        or expires == 0 or 0 < expires <= time.time()):
                    continue
                path = item.get("path", "/")
                if (not isinstance(path, str) or not path.startswith("/") or len(path) > 2048
                        or any(ord(char) < 32 or char == "\\" for char in path)):
                    continue
                client.jar.set_cookie(Cookie(0, name, item["value"], None, False,
                    domain, False, False, path, True, bool(item.get("secure", True)),
                    int(expires) if expires > 0 else None, expires <= 0,
                    None, None, {"HttpOnly": None} if item.get("httpOnly") else {}, False))
            client.passport_authenticated = any(c.name.lower() == "iplanetdirectorypro" for c in client.jar)
            client.start_operation()
            client.connect_zdbk()
            with self.manager.lock(OWNER):
                previous_identity = (self.manager._snapshot(OWNER).get("profile") or {}).get("data", {}).get("student_id", "")
                client.expected_identity = previous_identity
                # Validate identity before adoption when the school exposes it.
                profiles, warnings = client.profiles(previous_identity)
                identities = {r.get("data", {}).get("student_id") for r in profiles if r.get("data", {}).get("student_id")}
                if (len(identities) != 1 or any(w.get("code") == "PROFILE_IDENTITY_MISMATCH" for w in warnings)
                        or (previous_identity and identities != {previous_identity})):
                    raise CampusError("PROFILE_IDENTITY_MISMATCH", "学校身份发生变化，请先清除学校缓存再连接")
                identity = next(iter(identities))
                identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                if _grant and identity_hash != _grant["identity_sha256"]:
                    raise CampusError("PROFILE_IDENTITY_MISMATCH", "学校身份发生变化，请先清除学校缓存再连接")
                snapshot = self.manager._snapshot(OWNER)
                for profile in profiles:
                    merge_profile(snapshot, profile)
                adopted_grant_id = _grant["grant_id"] if _grant else None
                with self.store.connect() as db:
                    # Revocation during a slow provider/probe must not resurrect
                    # consent or install its candidate client/cache. Serialize the
                    # final check with the snapshot/grant write across instances.
                    db.execute("BEGIN IMMEDIATE")
                    current_grant = self._browser_grant(db)
                    if _grant and (not current_grant or current_grant["grant_id"] != _grant["grant_id"]):
                        raise ApiError(409, "AUTH_REQUIRED", "校园浏览器授权已解除")
                    if _authorize:
                        adopted_grant_id = uuid.uuid4().hex
                        db.execute("INSERT INTO campus_browser_grants VALUES(?,?,?,?,?) "
                            "ON CONFLICT(owner) DO UPDATE SET grant_id=excluded.grant_id,scope=excluded.scope,"
                            "identity_sha256=excluded.identity_sha256,authorized_at=excluded.authorized_at",
                            (OWNER, adopted_grant_id, BROWSER_GRANT_SCOPE, identity_hash, time.time()))
                    db.execute("INSERT INTO campus_snapshots(owner,snapshot) VALUES(?,?) "
                        "ON CONFLICT(owner) DO UPDATE SET snapshot=excluded.snapshot",
                        (OWNER, json.dumps(snapshot, ensure_ascii=False)))
                self.manager.disconnect(OWNER)
                self.manager.states[OWNER] = {"client": client, "passport": client.passport_authenticated,
                    "connected": True, "flow": "", "expires": time.monotonic()+3600,
                    "identity": previous_identity or identity}
                self._browser_live_grant_id = adopted_grant_id
            return self.status()
        except ApiError:
            client.close()
            raise
        except CampusError as exc:
            client.close()
            error = standard_error(exc)
            raise ApiError(409, error.code.value, error.message) from None
        except Exception:
            client.close()
            raise ApiError(409, "PROTOCOL_CHANGED", "学校浏览器会话未能确认，保留已有连接") from None

    def close(self):
        with self.manager.lock(OWNER):
            self._browser_cookie_provider = None
            self._browser_live_grant_id = None
            self.manager.close()  # Drop memory-only cookies, leave durable consent intact.


_workspace: CampusWorkspace | None = None
_guard = threading.Lock()


def get_campus_workspace() -> CampusWorkspace:
    global _workspace
    with _guard:
        if _workspace is None:
            home = Path(os.environ.get("CAMPUS_AGENT_HOME") or REPO_ROOT / "var" / "agent")
            _workspace = CampusWorkspace(home / "campus" / "workspace.sqlite3")
        return _workspace


def close_campus_workspace():
    global _workspace
    with _guard:
        if _workspace is not None:
            _workspace.close()
            _workspace = None
