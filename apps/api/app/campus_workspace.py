"""Single-person ZJU workspace, using migrated connectors, never the legacy server.

Cookie adoption is a private Python capability. No model tool accepts cookies,
passwords, a school identity or an arbitrary remote endpoint.
"""
from __future__ import annotations

from http.cookiejar import Cookie
import os
from pathlib import Path
import threading
import time

from connectors.campus.campus import CampusClient, CampusManager, merge_profile
from connectors.campus.campus_common import CampusError
from connectors.campus.campus_store import CAMPUS_SCHEMA, SQLiteStore
from connectors.campus.connector_contracts import UserContext, standard_error

from .config import REPO_ROOT
from .errors import ApiError

OWNER = "local"
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


class CacheStore(SQLiteStore):
    """Campus cache schema plus exact historical-import provenance; no passwords/local accounts."""

    def __init__(self, path: Path):
        super().__init__(path, schema=CAMPUS_SCHEMA + IMPORT_SCHEMA)


class CampusWorkspace:
    def __init__(self, path: Path, client_factory=CampusClient):
        self.store = CacheStore(path)
        self.manager = CampusManager(self.store, client_factory=client_factory)

    def status(self):
        return {"school": "浙江大学", "local_owner": OWNER,
                "login_url": "https://zjuam.zju.edu.cn/cas/login?service=https%3A%2F%2Fzdbk.zju.edu.cn%2Fjwglxt%2Fxtgl%2Flogin_ssologin.html",
                **self.manager.status(OWNER),
                **self.manager.service.connection_status(UserContext(OWNER)),
                "login_mode": "persistent_browser",
                "webvpn": {"login_url": "https://webvpn.zju.edu.cn/login", "mode": "persistent_browser",
                    "session_status": "check_in_browser", "supports": ["cas", "zdbk"],
                    "note": "WebVPN是独立内网通道；教务/通行证仍需学校登录，不把VPN登录当作教务登录"},
                "session_persistence": "browser_profile; connector cookies are memory-only"}

    def query(self, capability, query, platform=None, *, refresh=False):
        operation = self.manager.service.sync if refresh else self.manager.service.query
        return operation(capability, query, UserContext(OWNER), platform=platform).model_dump(mode="json")

    def webvpn_entry(self, service: str) -> dict:
        from connectors.campus.webvpn_adapter import HOST_TOKENS
        from connectors.campus.campus_endpoints import CAS, SERVICE
        from urllib.parse import urlencode, urlsplit
        if service not in {"cas", "zdbk"}:
            raise ApiError(400, "INVALID_INPUT", "未登记的WebVPN服务")
        original = CAS+"login?"+urlencode({"service": SERVICE}) if service == "cas" else SERVICE
        parts = urlsplit(original)
        token = HOST_TOKENS[parts.hostname]
        return {"service": service, "url": "https://webvpn.zju.edu.cn/https/"+token+parts.path+("?"+parts.query if parts.query else ""),
            "login_url": "https://webvpn.zju.edu.cn/login", "token_source": "observed portal mappings 2026-10-12; live revalidation required",
            "execution": "browser", "direct_connector_uses_webvpn": False}

    def adopt_browser_cookies(self, cookies: list[dict]) -> dict:
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
                domain = str(item.get("domain", "")).lstrip(".").lower()
                name = str(item.get("name", ""))
                if domain not in {"zjuam.zju.edu.cn", "zdbk.zju.edu.cn", "eta.zju.edu.cn", "zju.edu.cn"}:
                    continue
                # A parent cookie is scoped to CAS only (same policy as the wire connector).
                if domain == "zju.edu.cn":
                    domain = "zjuam.zju.edu.cn"
                if not name or len(name) > 512 or not isinstance(item.get("value"), str):
                    continue
                expires = item.get("expires", -1)
                if not isinstance(expires, (int, float)) or expires == 0 or 0 < expires <= time.time():
                    continue
                path = item.get("path", "/")
                if not isinstance(path, str) or not path.startswith("/"):
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
                if any(w.get("code") == "PROFILE_IDENTITY_MISMATCH" for w in warnings) or (previous_identity and identities != {previous_identity}):
                    raise CampusError("PROFILE_IDENTITY_MISMATCH", "学校身份发生变化，请先清除学校缓存再连接")
                snapshot = self.manager._snapshot(OWNER)
                for profile in profiles:
                    merge_profile(snapshot, profile)
                self.manager._save(OWNER, snapshot)
                self.manager.disconnect(OWNER)
                self.manager.states[OWNER] = {"client": client, "passport": client.passport_authenticated,
                    "connected": True, "flow": "", "expires": time.monotonic()+3600,
                    "identity": previous_identity or next(iter(identities), "")}
            return self.status()
        except CampusError as exc:
            client.close()
            error = standard_error(exc)
            raise ApiError(409, error.code.value, error.message) from None
        except Exception:
            client.close()
            raise ApiError(409, "PROTOCOL_CHANGED", "学校浏览器会话未能确认，保留已有连接") from None

    def close(self):
        self.manager.close()


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
