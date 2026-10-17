#!/usr/bin/env python
"""Controlled-model end-to-end drill of T4 "登录失效后只读继续" (server-produced continuation).

What this tool proves
---------------------
The acceptance finding R2 says the implementation's T4 turn never completed the
automatic recovery: the first turn's failed target (教务成绩 / zdbk) never received an
authenticated synthetic session, so the server probe stayed AUTH_REQUIRED and the script
manually typed the next turn. A manual next turn is not evidence of automatic
continuation, so this tool builds the missing drill:

* a **real FastAPI app** in a **real uvicorn process** (``app.main:app``);
* the **real installed Harness** (``apps/agent-runtime/runner.mjs``) as an owned Node
  subprocess, driven by a **CONTROLLED local model endpoint** on loopback only - the paid
  provider is never contacted, and the configured provider host is asserted to be this
  tool's own server;
* the **real persisted SQLite checkpoint/journal** (``<home>/journal.sqlite``) and the real
  domain/campus SQLite stores;
* a synthetic school session injected ONLY at the test-startup / school-protocol boundary
  (``CampusClient.request`` - the wire boundary of the school transport), so the real
  ``CampusService`` -> real adapter -> real repository -> real cache pipeline runs.

Scenarios (one run, one entry point)
------------------------------------
1. Turn 1 with the target session absent: the model asks for a read-only grades summary
   and the server persists a ``waiting_auth`` checkpoint with the original goal and the
   narrow conversation scope.
2. NEGATIVE: a successful check of a DIFFERENT platform must not authenticate the target.
   The server's own trusted notification runs a probe; the probe still returns
   AUTH_REQUIRED, the checkpoint stays ``waiting_auth``, no continuation turn exists, and
   the probe started neither a password login nor browser-cookie adoption (counted at the
   real method boundaries, attributable to the probe).
3. NEGATIVE: revoking campus consent (``POST /api/v1/agent/campus/disconnect``) must not
   advance the checkpoint or create an automatic turn, and a revoked grant must not be
   resurrected by the probe.
4. POSITIVE: an already-authenticated synthetic session for the TARGET (zdbk) chain is
   installed, the isolated API process is restarted, and the server's own boot path
   (``AuthenticationRecovery.__init__`` -> ``request_auth_probes()`` + monitor) probes the
   pending checkpoint. The SERVER creates ``continuation_turn_id``; the driver never posts
   a new user message for it. The continuation turn reaches a terminal state and its final
   answer uses the preserved probe result (the synthetic grades).
5. Narrow scope / restart correctness: the resumed turn's server-built ``ToolContext``
   keeps only the checkpoint's conversation-scoped ids, even after the conversation
   settings and the domain DB are deliberately widened, and ``recovery_tool_allowed``
   refuses writes during the automatic continuation.

Honesty rules
-------------
* Synthetic data is injected only where the product itself talks to the school
  (``CampusClient.request``: wired to the fixed school endpoints) and where a synthetic
  platform connection check is registered (``sync_engine.register_check_adapter``). No
  production route is added, no permission is widened, no product file is modified.
* The isolated API process is booted through a generated ``sitecustomize.py`` inside this
  run's scratch directory (imported by the CPython interpreter before ``app.main``), which
  installs the boundary and writes observations to a sidecar JSONL. Nothing is written
  into product source, product tests, or the independent reviewer's directory.
* The probe counters are recorded at the real method boundaries
  (``CampusWorkspace.auto_relogin_locked``, ``CampusWorkspace.adopt_browser_cookies``,
  ``campus_workspace._runtime_browser_cookies``) and attributed to the probe by the
  ``allow_recovery=False`` keyword the trusted probe passes to ``tools.execute`` - which is
  also recorded, so "the probe ran" and "the counts are the probe's" are both measured.
* No cookie, ticket, password or signed URL is read, printed or stored.

CLI::

    apps/api/.venv/Scripts/python.exe -X utf8 tools/verify_campus_auth_resume.py
        [--out PATH] [--keep-scratch]

Exit code 0 only when every scenario check passed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "apps" / "api"
RUNTIME_RUNNER = ROOT / "apps" / "agent-runtime" / "runner.mjs"
PYTHON = str(API_DIR / ".venv" / "Scripts" / "python.exe") if os.name == "nt" else sys.executable
if not Path(PYTHON).is_file():
    PYTHON = sys.executable

RUN_ID = uuid.uuid4().hex
SCRATCH_ROOT = ROOT / "var" / "harness-campus-auth-resume"
DEFAULT_OUT = ROOT / "var" / "harness-campus-readonly-rework-2f9a6c41" / "m-ct4" / "controlled-t4.json"

# ─────────────────────── network audit (installed before app imports) ───────────
# Every socket connect this process makes is recorded, so "offline" is measured on the
# real call instead of asserted from a comment. Loopback is exempt (the controlled model
# server and SQLite are local).
_REAL_CONNECT = socket.socket.connect
_CONNECTIONS: list[dict] = []
_CONNECT_LOCK = threading.Lock()


def _is_loopback(host) -> bool:
    if not isinstance(host, str):
        return False
    if host in ("localhost", ""):
        return True
    try:
        import ipaddress
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def _audited_connect(self, address, *args, **kwargs):
    host, port = (address[0], address[1]) if isinstance(address, tuple) and len(address) >= 2 else (address, None)
    with _CONNECT_LOCK:
        _CONNECTIONS.append({"family": "unix" if not isinstance(host, str) else "tcp",
                             "host": host if _is_loopback(host) else "<non-loopback host redacted>",
                             "port": port, "loopback": _is_loopback(host)})
    return _REAL_CONNECT(self, address, *args, **kwargs)


socket.socket.connect = _audited_connect


def non_loopback_connections() -> list[dict]:
    with _CONNECT_LOCK:
        return [dict(item) for item in _CONNECTIONS if not item["loopback"]]


# ─────────────────────── isolation (same style as tools/audit_regression.py) ────
FORBIDDEN = tuple((ROOT / part).resolve() for part in
                  ("var/personal.db", "var/files", "var/secrets", "var/agent", "var/workspace"))
PERSONAL_ROOTS = {name: (ROOT / "var" / name) for name in
                  ("personal.db", "files", "secrets", "agent", "workspace")}

WORK = SCRATCH_ROOT / RUN_ID
ISOLATION = {
    "STUDY_DATABASE_URL": "sqlite:///" + (WORK / "domain.db").as_posix(),
    "STUDY_FILES_DIR": str(WORK / "files"),
    "STUDY_SECRETS_DIR": str(WORK / "secrets"),
    "CAMPUS_AGENT_HOME": str(WORK / "agent"),
    "CAMPUS_AGENT_WORKSPACE": str(WORK / "workspace"),
    "CAMPUS_WORKSPACE_DIR": str(WORK / "workspace"),
    "CAMPUS_DATA_DIR": str(WORK / "campus"),
    "STUDY_WORKER_ENABLED": "0",
    "STUDY_WORKER_INPROC": "0",
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "TMP": str(WORK / "temp"),
    "TEMP": str(WORK / "temp"),
}


def _guard(paths) -> None:
    for path in paths:
        resolved = Path(path).resolve()
        for forbidden in FORBIDDEN:
            if resolved == forbidden or forbidden in resolved.parents:
                raise SystemExit(f"REFUSING: isolation path {resolved} is inside {forbidden}")
        if resolved == ROOT.resolve():
            raise SystemExit(f"REFUSING: isolation path {resolved} is the repository root itself")


_guard([WORK, *(Path(value[8:]) if key == "STUDY_DATABASE_URL" else Path(value)
                for key, value in ISOLATION.items())])
for _folder in ("files", "secrets", "agent", "workspace", "campus", "temp", "logs", "shim"):
    (WORK / _folder).mkdir(parents=True, exist_ok=True)
os.environ.update(ISOLATION)

SHIM_DIR = WORK / "shim"
SIDECAR = WORK / "fixture-sidecar.jsonl"
WINDOW_FILE = WORK / "fixture-window.json"
SHIM_SOURCE = SHIM_DIR / "sitecustomize.py"
SERVER_MODULE_DIR = WORK / "server"
SERVER_MODULE = SERVER_MODULE_DIR / "campus_auth_resume_server.py"

# ─────────────────────── git / evidence plumbing ───────────────────────────────
def _git(*args: str) -> str:
    try:
        done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30)
        return done.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _clip(value, limit: int = 4000):
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"…(+{len(value) - limit} chars)"
    if isinstance(value, dict):
        return {str(k): _clip(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v, limit) for v in value]
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    print(message, flush=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fingerprint(root: Path) -> dict:
    """Size+mtime fingerprint of a real personal-data path; stats only, no content."""
    entries: dict[str, list] = {}
    if root.is_file():
        stat = root.stat()
        entries[root.name] = [stat.st_size, stat.st_mtime_ns]
    elif root.is_dir():
        for path in sorted(root.rglob("*")):
            try:
                stat = path.lstat()
            except OSError:
                continue
            entries[str(path.relative_to(root)).replace("\\", "/")] = [stat.st_size, stat.st_mtime_ns]
    return entries


def personal_fingerprints() -> dict:
    return {name: fingerprint(path) for name, path in PERSONAL_ROOTS.items()}


# ─────────────────────── synthetic school boundary (generated shim) ────────────
# The shim is the ONLY thing installed into the isolated API process. It is generated
# into this run's scratch tree and imported by CPython (sitecustomize) before app.main,
# so it replaces the school transport/session boundary without touching product code.
SHIM_TEMPLATE = r'''"""Generated school-protocol boundary for the controlled T4 drill (never product code).

Imported automatically by CPython (sitecustomize) before ``app.main``. It replaces
exactly two boundaries:

* ``connectors.campus.campus.CampusClient.request`` - the real school HTTP wire boundary,
  answered for the fixed reviewed endpoints only (grades/menu/terms/timetable index) from
  a fixed synthetic school session. Everything above it (real protocol parsing, real
  CampusService/adapter/repository/SQLite cache) is unchanged product code.
* ``app.sync_engine.register_check_adapter`` for the synthetic platform ``xzzd`` - the
  app's own connection-check boundary, so the trusted success notification can be driven
  through the real ``POST /api/v1/connections/{id}/check`` route.

It also records observations (probe counters at the real method boundaries) to a sidecar
JSONL. No cookie, ticket, password or signed URL is written anywhere.
"""
from __future__ import annotations

import builtins
import functools
import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from http.cookiejar import Cookie
from pathlib import Path

_SIDECAR = Path(os.environ["VERIFY_T4_SIDECAR"])
_WINDOW = Path(os.environ["VERIFY_T4_WINDOW"])
_LOCK = threading.Lock()

# Fixed synthetic school facts. The grade values are what the final answer must cite.
SYNTHETIC_GRADES = [
    {"kcmc": "高等数学（甲）", "kch": "MATH1001", "xkkh": "(2026-2027-1)-MATH1001-001",
     "cj": "88", "xf": "5.0", "jd": "4.2", "xnmmc": "2026-2027-1"},
    {"kcmc": "数据结构基础", "kch": "CS2002", "xkkh": "(2026-2027-1)-CS2002-002",
     "cj": "91", "xf": "4.0", "jd": "4.5", "xnmmc": "2026-2027-1"},
]
_EXPECTED_GRADE_NAMES = ["高等数学（甲）", "数据结构基础"]
_EXPECTED_GRADE_SCORES = {"高等数学（甲）": "88", "数据结构基础": "91"}


def _record(kind: str, **fields) -> None:
    try:
        with _LOCK:
            with _SIDECAR.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind": kind, "at": time.time(), **fields},
                                        ensure_ascii=False, default=str) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
    except Exception:
        pass


def _window() -> dict:
    try:
        return json.loads(_WINDOW.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _patch(target, name: str, build) -> None:
    original = getattr(target, name)
    if getattr(original, "__verify_t4__", False):
        return
    wrapper = build(original)
    wrapper.__verify_t4__ = True
    wrapper.__name__ = getattr(original, "__name__", name)
    wrapper.__doc__ = getattr(original, "__doc__", None)
    wrapper.__wrapped__ = original
    try:
        wrapper.__dict__.update(getattr(original, "__dict__", {}))
    except Exception:
        pass
    setattr(target, name, wrapper)


# ── school transport boundary ────────────────────────────────────────────────────
_JSON_CONTENT = "application/json;charset=UTF-8"
_PAGE_CONTENT = "text/html; charset=utf-8"
_EMPTY = (200, {}, b"{}")
_TIMETABLE_INDEX = ('<html><body><select id="xnm">'
                    '<option value="2026" selected>2026</option></select>'
                    '<select id="xqm"><option value="1" selected>秋</option></select>'
                    '</body></html>').encode("utf-8")


def _synthetic_response(url: str, form):
    leaf = url.split("://", 1)[-1].split("/", 1)
    path = ("/" + (leaf[1] if len(leaf) > 1 else "")).split("?", 1)[0].split("#", 1)[0]
    if path == "/jwglxt/cxdy/xscjcx_cxXscjIndex.html":
        payload = {"items": [dict(row) for row in SYNTHETIC_GRADES],
                   "totalCount": len(SYNTHETIC_GRADES), "records": len(SYNTHETIC_GRADES)}
        return 200, {"Content-Type": _JSON_CONTENT}, json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if path in ("/jwglxt/xtgl/index_cxLeafGnmkdm.html",
                "/jwglxt/kbcx/xskbcx_cxXsKb.html"):
        return _EMPTY[0], {"Content-Type": _JSON_CONTENT}, _EMPTY[2]
    if path == "/jwglxt/kbcx/xskbcx_cxXskbcxIndex.html":
        return 200, {"Content-Type": _PAGE_CONTENT}, _TIMETABLE_INDEX
    _record("unregistered_school_path", url=url.split("?", 1)[0][:300],
            host=url.split("://", 1)[-1].split("/", 1)[0])
    raise AssertionError("synthetic school boundary has no registered response for " + path)


def install_transport(client_cls) -> None:
    def request(self, url, form=None, *, read_body=True):
        status, headers, body = _synthetic_response(url, form)
        _record("transport_request", path=url.split("://", 1)[-1].split("/", 1)[0] +
                "/" + url.split("://", 1)[-1].split("/", 1)[1].split("?")[0].split("#")[0],
                read_body=bool(read_body), status=status)
        return status, dict(headers), body if read_body else b""

    client_cls.request = request


class SyntheticSchoolSession(object):
    """Placeholder attribute is replaced below with the real CampusClient subclass."""


def build_client_class(campus_client):
    class _SyntheticCampusClient(campus_client):
        """Real client (cookies/CAS/protocol intact) with the school wire boundary replaced."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.passport_authenticated = True
            self.eta_authenticated = False
            self.authenticated = True

    install_transport(_SyntheticCampusClient)
    return _SyntheticCampusClient


def install_session(workspace) -> None:
    """Install an already-authenticated synthetic session for the TARGET (zdbk) chain.

    This is the injection seam the acceptance document names: a session that is already
    authenticated, at the school-protocol boundary. No password is submitted and no
    browser cookie is read.
    """
    window = _window()
    if not window.get("session_ready"):
        _record("session_not_installed", reason="window file does not authorize a session")
        return
    client = workspace.manager.factory()
    client.passport_authenticated = True
    client.authenticated = True
    client.eta_authenticated = False
    for name, value in (("iPlanetDirectoryPro", "synthetic-t4-passport"),
                        ("JSESSIONID", "synthetic-t4-zdbk")):
        client.jar.set_cookie(Cookie(0, name, value, None, False, "zdbk.zju.edu.cn", False, False,
                                     "/", True, False, None, False, None, None, {}))
    with workspace.manager.lock("local"):
        previous = workspace.manager.states.get("local")
        if previous and previous.get("client") is not None:
            try:
                previous["client"].close()
            except Exception:
                pass
        workspace.manager.states["local"] = {
            "client": client, "passport": True, "connected": True, "flow": "",
            "expires": time.monotonic() + 6 * 3600, "identity": "synthetic-t4-student"}
    _record("session_installed", target="zdbk",
            grades=[{"name": row["kcmc"], "score": row["cj"]} for row in SYNTHETIC_GRADES])


# ── probe counters at the real method boundaries ────────────────────────────────
_PROBE = {"active": False, "allow_recovery_false_seen": 0}


def install_counters(workspace_module, runtime_module, tools_module) -> None:
    workspace_cls = workspace_module.CampusWorkspace
    runtime_cls = runtime_module.Runtime

    def query(original):
        @functools.wraps(original)
        def wrapped(self, capability, query_args, platform=None, *, refresh=False, allow_recovery=True):
            result = original(self, capability, query_args, platform, refresh=refresh,
                              allow_recovery=allow_recovery)
            if refresh:
                error = (result or {}).get("error") or {}
                _record("workspace_sync", capability=str(capability),
                        allow_recovery=bool(allow_recovery),
                        probe=bool(_PROBE["active"] and not allow_recovery),
                        error_code=error.get("code"),
                        record_count=len((result or {}).get("records") or []))
            return result
        return wrapped

    def relogin(original):
        @functools.wraps(original)
        def wrapped(self):
            _record("auto_relogin_locked", probe=bool(_PROBE["active"]))
            if _PROBE["active"]:
                _PROBE["auto_relogin_from_probe"] = _PROBE.get("auto_relogin_from_probe", 0) + 1
            return original(self)
        return wrapped

    def adopt(original):
        @functools.wraps(original)
        def wrapped(self, cookies, *args, **kwargs):
            _record("browser_cookie_adoption", probe=bool(_PROBE["active"]), count=len(cookies or []))
            if _PROBE["active"]:
                _PROBE["browser_adoption_from_probe"] = _PROBE.get("browser_adoption_from_probe", 0) + 1
            return original(self, cookies, *args, **kwargs)
        return wrapped

    def browser_provider(original):
        @functools.wraps(original)
        def wrapped():
            _record("browser_cookie_provider_read", probe=bool(_PROBE["active"]))
            if _PROBE["active"]:
                _PROBE["browser_provider_from_probe"] = _PROBE.get("browser_provider_from_probe", 0) + 1
            return original()
        return wrapped

    def execute(original):
        @functools.wraps(original)
        def wrapped(name, arguments, db, ctx, *, allow_auth_recovery=True):
            if not allow_auth_recovery:
                _PROBE["allow_recovery_false_seen"] = _PROBE.get("allow_recovery_false_seen", 0) + 1
                _record("probe_execute", tool=str(name), allow_auth_recovery=False,
                        capability=(arguments or {}).get("capability"),
                        course_ids=sorted(ctx.course_ids) if ctx.course_ids is not None else None,
                        request_key=ctx.request_key)
            return original(name, arguments, db, ctx, allow_auth_recovery=allow_auth_recovery)
        return wrapped

    def probe(original):
        @functools.wraps(original)
        def wrapped(self, checkpoint):
            before = dict(_PROBE)
            _PROBE["active"] = True
            _PROBE["probes"] = _PROBE.get("probes", 0) + 1
            try:
                result = original(self, checkpoint)
            except Exception as exc:
                _PROBE["active"] = False
                _record("probe_finished", checkpoint=checkpoint.get("id"), raised=type(exc).__name__)
                raise
            finally:
                _PROBE["active"] = False
            _record("probe_finished", checkpoint=checkpoint.get("id"),
                    status=(result or {}).get("status"),
                    error_code=((result or {}).get("error") or {}).get("code"),
                    record_count=len(((result or {}).get("data") or {}).get("records") or []),
                    counters_from_this_probe={
                        "auto_relogin_locked": _PROBE.get("auto_relogin_from_probe", 0) - before.get("auto_relogin_from_probe", 0),
                        "browser_cookie_adoption": _PROBE.get("browser_adoption_from_probe", 0) - before.get("browser_adoption_from_probe", 0),
                        "browser_cookie_provider_read": _PROBE.get("browser_provider_from_probe", 0) - before.get("browser_provider_from_probe", 0)})
            return result
        return wrapped

    def recovery_allowed(original):
        @functools.wraps(original)
        def wrapped(self, turn_or_id, name):
            allowed = original(self, turn_or_id, name)
            tid = turn_or_id["id"] if isinstance(turn_or_id, dict) else turn_or_id
            resume = self.auth_resume_context(turn_or_id)
            if resume is not None:
                _record("recovery_tool_guard", turn=tid, tool=str(name), allowed=bool(allowed),
                        probe=bool(_PROBE["active"]))
            return allowed
        return wrapped

    def recovery_context(original):
        @functools.wraps(original)
        def wrapped(self, turn_or_id, db):
            context = original(self, turn_or_id, db)
            if context is not None:
                tid = turn_or_id["id"] if isinstance(turn_or_id, dict) else turn_or_id
                _record("recovery_tool_context", turn=tid,
                        course_ids=sorted(context.course_ids) if context.course_ids is not None else None,
                        connection_ids=sorted(context.connection_ids) if context.connection_ids is not None else None,
                        request_key=context.request_key)
            return context
        return wrapped

    _patch(workspace_cls, "query", query)
    _patch(workspace_cls, "auto_relogin_locked", relogin)
    _patch(workspace_cls, "adopt_browser_cookies", adopt)
    _patch(workspace_module, "_runtime_browser_cookies", browser_provider)
    _patch(runtime_cls, "probe_authenticated_read", probe)
    _patch(runtime_cls, "recovery_tool_allowed", recovery_allowed)
    _patch(runtime_cls, "auth_recovery_tool_context", recovery_context)
    _patch(tools_module, "execute", execute)

def install_check_adapter(sync_engine) -> None:
    """Own the connection-check seam for the drill's synthetic platform connection.

    ``resolve_check_adapter`` is what ``POST /connections/{id}/check`` resolves its check
    surface with. The router imports it by name at import time, so the router's own
    reference is what must be replaced; only the drill's synthetic connection is
    intercepted, and every real connection still resolves through the untouched product
    registry exactly as before.
    """
    spot = os.environ.get("VERIFY_T4_CHECK_CONNECTION", "")
    if "::" not in spot:
        return
    platform, _connection_id = spot.split("::", 1)
    from app.routers import connections as connections_router
    original = connections_router.resolve_check_adapter

    def check(connection):
        return {"platform": connection["platform"], "state": "ok",
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "message": "synthetic platform check (boundary fixture)"}

    def resolve(requested_platform):
        if requested_platform == platform:
            return check
        return original(requested_platform)

    connections_router.resolve_check_adapter = resolve
    sync_engine.resolve_check_adapter = resolve


# ── installation (wrapped import + app lifespan seam) ──────────────────────────
_installed = {"session": False, "counters": False, "check": False, "app": False}


def _install_app_patches(app) -> None:
    if _installed["app"]:
        return
    _installed["app"] = True
    from app.campus_workspace import get_campus_workspace
    import app.campus_workspace as workspace_module
    from app import sync_engine
    from app.agent_bridge import runtime as runtime_module, tools as tools_module
    try:
        install_check_adapter(sync_engine)
        _record("check_adapter_installed", platform=os.environ.get("VERIFY_T4_CHECK_CONNECTION", ""))
    except Exception:
        _record("install_error", stage="check_adapter", traceback=traceback.format_exc()[-1200:])
    try:
        install_counters(workspace_module, runtime_module, tools_module)
    except Exception:
        _record("install_error", stage="counters", traceback=traceback.format_exc()[-1200:])
    try:
        install_session(get_campus_workspace())
    except Exception:
        _record("install_error", stage="session", traceback=traceback.format_exc()[-1200:])
    _record("fixture_ready", session_installed=bool(_window().get("session_ready")))


_real_import = builtins.__import__


def _wrapped_import(name, globals=None, locals=None, fromlist=(), level=0):
    module = _real_import(name, globals, locals, fromlist, level)
    if not _installed["session"] and (name.startswith("connectors.campus.campus")
                                      or name.startswith("app.campus_workspace")):
        # app.campus_workspace binds the default client_factory at import time, so the
        # patched class must be in place there too - whichever module is imported first.
        _installed["session"] = True
        try:
            import connectors.campus.campus as campus_module
            synthetic = build_client_class(campus_module.CampusClient)
            campus_module.CampusClient = synthetic
            try:
                import app.campus_workspace as workspace_module
                workspace_module.CampusClient = synthetic
            except Exception:
                pass
            _record("boundary_installed", target="connectors.campus.campus.CampusClient")
        except Exception:
            _record("install_error", stage="transport", traceback=traceback.format_exc()[-1200:])
    return module


builtins.__import__ = _wrapped_import

try:
    import app.main as _app_main

    if not _installed["app"]:
        _install_app_patches(_app_main.app)
except Exception:
    _record("install_error", stage="app_main", traceback=traceback.format_exc()[-1200:])

_record("sitecustomize_loaded", boundary="connectors.campus.campus.CampusClient.request")
'''


def write_shim() -> None:
    SHIM_SOURCE.write_text(SHIM_TEMPLATE, encoding="utf-8")
    SERVER_MODULE_DIR.mkdir(parents=True, exist_ok=True)
    SERVER_MODULE.write_text(
        '"""Generated uvicorn target for the isolated controlled T4 API process."""\n'
        "from app.main import app  # noqa: F401\n",
        encoding="utf-8")


# ─────────────────────── controlled model endpoint (loopback only) ─────────────
MATH_NAME = "高等数学（甲）"
CS_NAME = "数据结构基础"
GRADES_ANSWER = f"共 2 条成绩：{MATH_NAME} 88 分，{CS_NAME} 91 分；数据来自本次只读同步。"
GOAL_TEXT = "用只读的校园同步取回教务成绩，并给我一份成绩摘要（不要写操作）。"
CONTINUATION_INSTRUCTION_MARK = "校园认证已由用户完成"
RESUME_PAYLOAD_MARK = "accepted_auth_resume"


class ControlledModel:
    """Deterministic, local OpenAI-compatible endpoint; never the paid provider."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stats = {"requests": 0, "tool_call_responses": 0, "final_responses": 0,
                      "continuation_requests": 0}
        self.trajectory: list[dict] = []
        self._server: ThreadingHTTPServer | None = None
        self.port = 0

    def start(self) -> int:
        model = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _reply(self, status: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    payload = json.loads(raw or b"{}")
                    choice = model.choice(payload.get("messages") or [])
                except Exception:
                    model.trajectory.append({"kind": "model_error",
                                             "traceback": traceback.format_exc()[-800:]})
                    self._reply(500, {"error": {"message": "controlled model failure"}})
                    return
                self._reply(200, {"id": "chatcmpl-controlled-t4", "object": "chat.completion",
                                  "model": payload.get("model", "controlled"),
                                  "choices": [{"index": 0, "message": choice,
                                               "finish_reason": "tool_calls" if choice.get("tool_calls") else "stop"}],
                                  "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self._server = server
        self.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True, name="controlled-t4-model").start()
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # -- deterministic plan -----------------------------------------------------------------
    @staticmethod
    def _texts(messages: list) -> list[str]:
        out = []
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                out.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        out.append(block["text"])
        return out

    @classmethod
    def _is_continuation(cls, messages: list) -> bool:
        return any(CONTINUATION_INSTRUCTION_MARK in text for text in cls._texts(messages))

    @staticmethod
    def _history(messages: list) -> list[dict]:
        history = []
        for message in messages:
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    history.append({"name": function.get("name"), "id": call.get("id")})
        return history

    @staticmethod
    def _tool_results(messages: list) -> list[dict]:
        results = []
        for message in messages:
            if message.get("role") != "tool":
                continue
            value = message.get("content")
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    continue
            if isinstance(value, dict):
                results.append(value)
        return results

    def _answer_from_results(self, messages: list) -> str:
        """Answer ONLY from tool results the wire actually returned, so an invented
        summary is impossible and a missing read is visible as an incomplete answer."""
        grades = []
        for result in self._tool_results(messages):
            for row in ((result.get("data") or {}).get("records") or []):
                if isinstance(row, dict) and row.get("name"):
                    grades.append((str(row["name"]), str(row.get("score", ""))))
        if not grades:
            return "本轮没有取得可引用的成绩记录；请以校园缓存或重新同步的结果为准。"
        summary = "；".join(f"{name} {score} 分" for name, score in grades)
        return f"共 {len(grades)} 条成绩：{summary}；数据来自本次只读同步。"

    def choice(self, messages: list) -> dict:
        history = self._history(messages)
        names = [item["name"] for item in history]
        continuation = self._is_continuation(messages)
        # The drill's plan is derived STRUCTURALLY from the tool calls the wire shows, so
        # no step can be skipped by a message-shape change:
        #  * turn 1 issues one real explicit campus.sync (the absent target session makes
        #    the server answer AUTH_REQUIRED);
        #  * the server-produced continuation issues one read-only campus.query (so its
        #    server-built ToolContext is observable), then attempts one write-effect tool
        #    (plans.preview) that the continuation guard must refuse, then answers from the
        #    results it can actually see.
        if continuation:
            if "campus_campus_query" not in names:
                needed = "campus_campus_query"
            elif "campus_plans_preview" not in names:
                needed = "campus_plans_preview"
            else:
                needed = None
        else:
            needed = "campus_campus_sync" if "campus_campus_sync" not in names else None
        chosen = needed
        call_id = f"t4_call_{len(names) + 1}_{(chosen or 'final').replace('.', '_')}"
        with self.lock:
            self.stats["requests"] += 1
            if continuation:
                self.stats["continuation_requests"] += 1
        tool_call = None
        if chosen is not None:
            arguments = ({"capability": "grades", "query": {}} if chosen == "campus_campus_sync"
                         else {"capability": "grades", "query": {}, "platform": "zdbk"} if chosen == "campus_campus_query"
                         else {"period": "this_week"})
            tool_call = {"id": call_id, "type": "function",
                         "function": {"name": chosen, "arguments": json.dumps(arguments, ensure_ascii=False)}}
            with self.lock:
                self.stats["tool_call_responses"] += 1
        else:
            with self.lock:
                self.stats["final_responses"] += 1
        entry = {"turn": "continuation" if continuation else "turn1", "step": len(names),
                 "tool": chosen, "call_id": call_id if tool_call else None,
                 "continuation": continuation, "tool_history": names}
        self.trajectory.append(entry)
        return {"role": "assistant",
                "content": None if tool_call else self._answer_from_results(messages),
                "tool_calls": [tool_call] if tool_call else []}


# ─────────────────────── isolated API process ──────────────────────────────────
class IsolatedApi:
    def __init__(self, port: int, logs: Path):
        self.port = port
        self.logs = logs
        self.process: subprocess.Popen | None = None
        self.log = logs / f"api-{port}.log"
        # The owner is the browser cookie the real UI carries; one jar per process keeps
        # the same owner across create/send/poll requests (no synthetic bypass of it).
        self.jar = __import__("http.cookiejar", fromlist=["CookieJar"]).CookieJar()
        self.opener = __import__("urllib.request", fromlist=["build_opener"]).build_opener(
            __import__("urllib.request", fromlist=["HTTPCookieProcessor"]).HTTPCookieProcessor(self.jar))

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def environment(self) -> dict:
        env = os.environ.copy()
        env.update(ISOLATION)
        env.update(
            CAMPUS_BRIDGE_URL=self.base,
            CAMPUS_WEB_ORIGINS=f"http://127.0.0.1:{self.port},http://localhost:{self.port}",
            VERIFY_T4_SIDECAR=str(SIDECAR),
            VERIFY_T4_WINDOW=str(WINDOW_FILE),
            VERIFY_T4_CHECK_CONNECTION=f"xzzd::{CONNECTION_ID}",
            PYTHONPATH=os.pathsep.join([str(SHIM_DIR), str(SERVER_MODULE_DIR), str(ROOT), str(API_DIR)]),
            CAMPUS_RUNNER_STDERR=str(self.logs / f"runner-stderr-{self.port}.log"),
        )
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env.pop(key, None)
        env["NO_PROXY"] = "127.0.0.1,localhost"
        env["no_proxy"] = "127.0.0.1,localhost"
        return env

    def start(self) -> None:
        stream = open(self.log, "a", encoding="utf-8")
        try:
            self.process = subprocess.Popen(
                [PYTHON, "-m", "uvicorn", "campus_auth_resume_server:app",
                 "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning"],
                cwd=str(API_DIR), env=self.environment(), stdout=stream, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        finally:
            stream.close()
        deadline = time.time() + 120
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"isolated API exited early ({self.process.returncode}); see {self.log}")
            # Readiness must prove the AGENT routes are mounted, not merely that the port
            # answers: /health can respond before the app's routes are ready.
            try:
                status, _ = self.request("OPTIONS", "/api/v1/agent/conversations")
                if status in (200, 204, 405):
                    return
            except Exception:
                pass
            try:
                status, _ = self.request("GET", "/api/v1/agent/conversations")
                if status in (200, 405):
                    return
            except Exception:
                pass
            time.sleep(0.4)
        raise RuntimeError(f"isolated API did not become ready in 120s; see {self.log}")

    def stop(self) -> None:
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        else:
            process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    def request(self, method: str, path: str, payload: dict | None = None, headers: dict | None = None):
        import urllib.error
        import urllib.request

        url = self.base + path
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with self.opener.open(request, timeout=60) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw) if raw else None
            except ValueError:
                parsed = {"raw": raw[:400].decode("utf-8", "replace")}
            return exc.code, parsed

    def tail(self, limit: int = 25) -> list[str]:
        try:
            return self.log.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
        except OSError:
            return []


# ─────────────────────── journal / sidecar readers ────────────────────────────
def read_journal(path: Path, query: str, args: tuple = ()):
    if not Path(path).exists():
        return []
    connection = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=20)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query, args).fetchall()]
    finally:
        connection.close()


def write_journal(path: Path, statement: str, args: tuple = ()) -> int:
    connection = sqlite3.connect(str(path), timeout=20)
    try:
        with connection:
            cursor = connection.execute(statement, args)
            return cursor.rowcount
    finally:
        connection.close()


def sidecar_records(path: Path | None = None) -> list[dict]:
    path = path or SIDECAR
    records: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return records
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def probe_counters(records: list[dict]) -> dict:
    finished = [item for item in records if item.get("kind") == "probe_finished"]
    totals = {"probes": len(finished), "auto_relogin_locked": 0, "browser_cookie_adoption": 0,
              "browser_cookie_provider_read": 0}
    for item in finished:
        counters = item.get("counters_from_this_probe") or {}
        for key in ("auto_relogin_locked", "browser_cookie_adoption", "browser_cookie_provider_read"):
            totals[key] += int(counters.get(key) or 0)
    return totals


# ─────────────────────── per-scenario evidence plumbing ───────────────────────
class Scenario:
    def __init__(self, scenario_id: str, title: str):
        self.data = {"id": scenario_id, "title": title, "status": "failed", "checks": [],
                     "trajectory": [], "server_side": {}, "counters": {}}

    def check(self, name: str, ok: bool, detail) -> bool:
        self.data["checks"].append({"name": name, "ok": bool(ok), "detail": _clip(detail)})
        return bool(ok)

    def finalize(self) -> dict:
        failures = [item for item in self.data["checks"] if not item["ok"]]
        self.data["status"] = "passed" if self.data["checks"] and not failures else "failed"
        return self.data


def turn_trajectory(journal: Path, turn_id: str) -> list[dict]:
    rows = read_journal(journal, "SELECT seq,kind,data,created_at FROM events WHERE turn_id=? ORDER BY seq", (turn_id,))
    trajectory = []
    for row in rows:
        try:
            data = json.loads(row["data"])
        except (TypeError, ValueError):
            continue
        kind = row["kind"]
        if kind == "tool_result":
            result = data.get("result") or {}
            trajectory.append({
                "turn": turn_id, "kind": "tool_result", "name": data.get("name"),
                "arguments": _public_arguments(data.get("name"), data.get("arguments") or {}),
                "status": result.get("status"),
                "error": _clip(result.get("error")),
                "call_id": data.get("call_id")})
        elif kind == "harness_event":
            # The SDK's own session log. A refused tool call is surfaced here as a typed
            # error result, so the refusal is evidence on the same trajectory.
            event = data.get("event") or {}
            payload = event.get("data") or {}
            if event.get("type") == "tool/call":
                trajectory.append({"turn": turn_id, "kind": "harness_tool_call",
                                   "name": _tool_name(payload.get("name")),
                                   "arguments": _public_arguments(payload.get("name"), _tool_arguments(payload)),
                                   "status": "called", "error": None,
                                   "call_id": payload.get("callId")})
            elif event.get("type") == "tool/result":
                message = (payload.get("message") or {})
                text = ""
                for block in (message.get("content") or []):
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        text += block["text"]
                trajectory.append({"turn": turn_id, "kind": "harness_tool_result",
                                   "name": _tool_name(message.get("source", {}).get("name")),
                                   "arguments": None, "status": "error" if message.get("isError") else "ok",
                                   "error": {"message": _clip(text, 200)} if message.get("isError") else None,
                                   "call_id": message.get("toolCallId")})
        elif kind == "accepted_auth_resume":
            trajectory.append({"turn": turn_id, "kind": kind, "name": None, "arguments": None,
                               "status": "accepted", "error": None,
                               "checkpoint_id": data.get("checkpoint_id"),
                               "conversation_id": data.get("conversation_id"),
                               "read_only": data.get("read_only")})
        elif kind in ("runtime", "model_error", "stop_requested"):
            trajectory.append({"turn": turn_id, "kind": kind, "name": None, "arguments": None,
                               "status": kind, "error": _clip(data if kind == "model_error" else None)})
    return trajectory


def _tool_name(model_name) -> str:
    """Map the model-visible name back to the dotted domain name for the record.

    The bridge's own mapping is ``"campus_" + name.replace(".", "_")``; putting the first
    underscore back is unambiguous because every domain tool name has exactly one dot.
    """
    if isinstance(model_name, str) and model_name.startswith("campus_") and model_name.count("_") == 1:
        return "campus." + model_name[len("campus_"):]
    return model_name


def _tool_arguments(payload: dict) -> dict:
    raw = payload.get("arguments")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _public_arguments(name, arguments: dict) -> dict:
    allowed = {"capability", "platform", "record_id", "course_id", "connection_id", "limit", "day", "term"}
    summary = {key: value for key, value in (arguments or {}).items() if key in allowed}
    if isinstance((arguments or {}).get("query"), dict):
        summary["query"] = _clip(arguments["query"], 200)
    return summary


def wait_for_turn(journal: Path, predicate, timeout: float, describe: str) -> list[dict]:
    deadline = time.time() + timeout
    rows: list[dict] = []
    while time.time() < deadline:
        rows = predicate()
        if rows:
            return rows
        time.sleep(0.4)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {describe}")


def checkpoint_rows(journal: Path) -> list[dict]:
    rows = read_journal(journal, "SELECT * FROM auth_recovery ORDER BY created_at")
    for row in rows:
        for key in ("payload", "probe_result", "error"):
            if row.get(key):
                try:
                    row[key] = json.loads(row[key])
                except (TypeError, ValueError):
                    pass
    return rows


def turns(journal: Path) -> list[dict]:
    return read_journal(journal, "SELECT id,conversation_id,request_key,text,status,response,error,"
                                 "created_at,updated_at FROM turns ORDER BY created_at")


def terminal(turn_row: dict) -> bool:
    return turn_row["status"] in ("succeeded", "failed", "cancelled", "interrupted")


def wait_terminal(journal: Path, turn_id: str, timeout: float) -> dict:
    def predicate():
        rows = [row for row in turns(journal) if row["id"] == turn_id]
        return rows if rows and terminal(rows[0]) else []
    return wait_for_turn(journal, predicate, timeout, f"turn {turn_id} terminal")[0]


# ─────────────────────── domain seeding ───────────────────────────────────────
PROVIDER_ID = "provider_t4_controlled"
COURSE_IN_SCOPE = "course_t4_in_scope"
COURSE_WIDENED = "course_t4_widened"
CONNECTION_ID = "conn_t4_synthetic_xzzd"


def seed_domain(database_url: str, model_base_url: str, *, with_connection: bool,
                widened_course: bool = False) -> None:
    sys.path[:0] = [str(API_DIR), str(ROOT)]
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app import models as m
    from app.db import Base

    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.merge(m.ProviderConfig(id=PROVIDER_ID, name="受控本地合规模型（回环）",
                                  base_url=model_base_url, model="controlled-t4-fixture",
                                  protocol="openai_chat_completions", enabled=True))
        db.merge(m.Course(id=COURSE_IN_SCOPE, title="合成课程（会话范围内）", term="2026秋"))
        if widened_course:
            db.merge(m.Course(id=COURSE_WIDENED, title="合成课程（故意加宽，不属于检查点范围）", term="2026秋"))
        if with_connection:
            db.merge(m.Connection(id=CONNECTION_ID, platform="xzzd",
                                  display_name="合成平台连接（仅用于可信连接检查通知）", status="ok"))
        db.commit()
    engine.dispose()


def seed_browser_grant(campus_db: Path) -> dict:
    """Durable consent metadata as the real authorize path stores it (no cookies)."""
    import hashlib

    identity = "synthetic-t4-student"
    grant = {"grant_id": uuid.uuid4().hex, "scope": "official_campus_read:v1",
             "identity_sha256": hashlib.sha256(identity.encode("utf-8")).hexdigest()}
    connection = sqlite3.connect(str(campus_db), timeout=20)
    try:
        with connection:
            connection.execute("INSERT INTO campus_browser_grants VALUES(?,?,?,?,?) "
                               "ON CONFLICT(owner) DO UPDATE SET grant_id=excluded.grant_id,"
                               "scope=excluded.scope,identity_sha256=excluded.identity_sha256,"
                               "authorized_at=excluded.authorized_at",
                               ("local", grant["grant_id"], grant["scope"], grant["identity_sha256"], time.time()))
    finally:
        connection.close()
    return grant


def browser_grant(campus_db: Path) -> dict | None:
    if not Path(campus_db).exists():
        return None
    connection = sqlite3.connect(f"file:{Path(campus_db).as_posix()}?mode=ro", uri=True, timeout=20)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM campus_browser_grants WHERE owner='local'").fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


# ─────────────────────── main drill ───────────────────────────────────────────
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="evidence document path")
    parser.add_argument("--keep-scratch", action="store_true", help="keep the isolated workspace")
    args = parser.parse_args(argv)

    started = datetime.now(timezone.utc)
    begin = time.monotonic()
    log("ISOLATED_WORKSPACE=" + str(WORK))
    write_shim()

    model = ControlledModel()
    model_port = model.start()
    model_base_url = f"http://127.0.0.1:{model_port}/v1"
    domain_db = WORK / "domain.db"
    seed_domain(ISOLATION["STUDY_DATABASE_URL"], model_base_url, with_connection=False)

    journal = Path(ISOLATION["CAMPUS_AGENT_HOME"]) / "journal.sqlite"
    campus_db = Path(ISOLATION["CAMPUS_AGENT_HOME"]) / "campus" / "workspace.sqlite3"
    api_port = free_port()
    api = IsolatedApi(api_port, WORK / "logs")

    personal_before = personal_fingerprints()
    scenarios: list[dict] = []
    notes: list[str] = []
    conversation_id = None
    first_turn_id = None
    checkpoint_id = None
    probe_expectations: list[dict] = []

    def arm_probe_probe(reset_cooldown: bool = True) -> dict:
        """Every probe starts from an explicit, recorded journal state."""
        if checkpoint_id and reset_cooldown:
            write_journal(journal, "UPDATE auth_recovery SET next_probe_at=0,probe_requested=1 WHERE id=?",
                          (checkpoint_id,))
        return {"sidecar_records_before": len(sidecar_records())}

    def await_probe(marker: dict, timeout: float = 90) -> dict:
        before = marker["sidecar_records_before"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            records = sidecar_records()
            finished = [item for item in records[before:] if item.get("kind") == "probe_finished"]
            if finished:
                return {"records": records, "finished": finished[-1], "probes": probe_counters(records)}
            time.sleep(0.3)
        raise TimeoutError(f"no probe_finished recorded within {timeout:.0f}s")

    # ── scenario 1: turn 1 with the target session absent ──────────────────────
    scenario1 = Scenario("1", "Turn 1 target session absent -> terminal turn + waiting_auth checkpoint")
    try:
        WINDOW_FILE.write_text(json.dumps({"session_ready": False, "stage": "turn1"}), encoding="utf-8")
        api.start()
        status, created = api.request("POST", "/api/v1/agent/conversations",
                                      {"provider_id": PROVIDER_ID})
        scenario1.check("the real API created a conversation for the controlled provider",
                        status == 201 and isinstance(created, dict) and created.get("id", "").startswith("conv_"),
                        {"status": status, "conversation": _clip(created)})
        conversation_id = (created or {}).get("id")
        status, turn = api.request("POST", f"/api/v1/agent/conversations/{conversation_id}/turns",
                                   {"text": GOAL_TEXT}, {"Idempotency-Key": "t4-turn-1"})
        first_turn_id = (turn or {}).get("id")
        scenario1.check("the user's first turn was accepted over the real HTTP API",
                        status == 202 and bool(first_turn_id), {"status": status, "turn": _clip(turn)})
        final = wait_terminal(journal, first_turn_id, 300)
        scenario1.check("turn 1 ended terminal (no manual follow-up by this driver)",
                        terminal(final),
                        {"status": final["status"], "error": _clip(final.get("error")),
                         "response_head": _clip(final.get("response"), 200)})
        scenario1.data["trajectory"] = turn_trajectory(journal, first_turn_id)
        tool_results = [item for item in scenario1.data["trajectory"] if item["kind"] == "tool_result"]
        auth_results = [item for item in tool_results
                        if (item.get("error") or {}).get("code") in ("AUTH_REQUIRED", "AUTH_EXPIRED")]
        scenario1.check("the model's campus.sync produced a structured AUTH_REQUIRED tool result",
                        any(item["name"] == "campus.sync" for item in auth_results),
                        {"tool_results": tool_results})
        checkpoints = checkpoint_rows(journal)
        scenario1.check("the server persisted exactly one auth checkpoint",
                        len(checkpoints) == 1, {"count": len(checkpoints),
                                                "ids": [row["id"] for row in checkpoints]})
        checkpoint = checkpoints[0] if checkpoints else {}
        checkpoint_id = checkpoint.get("id")
        payload = checkpoint.get("payload") or {}
        scenario1.check("the checkpoint is waiting_auth",
                        checkpoint.get("status") == "waiting_auth",
                        {"status": checkpoint.get("status"), "error": _clip(checkpoint.get("error"))})
        scenario1.check("the checkpoint preserves the original goal text",
                        payload.get("original_goal") == GOAL_TEXT,
                        {"original_goal": _clip(payload.get("original_goal"))})
        scenario1.check("the checkpoint keeps the narrow conversation scope (course ids only)",
                        payload.get("scope", {}).get("course_ids") == [COURSE_IN_SCOPE]
                        and payload.get("scope", {}).get("connection_ids") == [],
                        {"scope": payload.get("scope")})
        scenario1.check("the pending read is the original read-only grades request",
                        (payload.get("pending_tool") or {}).get("name") == "campus.sync"
                        and (payload.get("pending_tool") or {}).get("arguments", {}).get("capability") == "grades",
                        {"pending_tool": _clip(payload.get("pending_tool"))})
        scenario1.data["server_side"] = {"checkpoint_id": checkpoint_id,
                                         "checkpoint_status": checkpoint.get("status"),
                                         "continuation_turn_id": checkpoint.get("continuation_turn_id"),
                                         "automatic": False, "turns": len(turns(journal))}
    except Exception:
        scenario1.check("scenario 1 ran without an internal error", False, traceback.format_exc()[-1600:])
    scenarios.append(scenario1.finalize())

    # ── scenario 2: another platform's success must not authenticate the target ─
    scenario2 = Scenario("2", "NEGATIVE: another platform's successful check is not target authentication")
    try:
        seed_domain(ISOLATION["STUDY_DATABASE_URL"], model_base_url, with_connection=True)
        before_checkpoints = checkpoint_rows(journal)
        before_turns = len(turns(journal))
        records_before = len(sidecar_records())
        arm_probe_probe()
        status, checked = api.request("POST", f"/api/v1/connections/{CONNECTION_ID}/check")
        scenario2.check("the app's own trusted connection check succeeded for the OTHER platform",
                        status == 200 and (checked or {}).get("status") == "ok",
                        {"status": status, "connection": _clip(checked)})
        deadline = time.time() + 90
        observed = None
        while time.time() < deadline:
            finished = [item for item in sidecar_records()[records_before:]
                        if item.get("kind") == "probe_finished"]
            if finished:
                observed = finished[-1]
                break
            time.sleep(0.3)
        scenario2.check("the trusted notification really ran a probe against the pending checkpoint",
                        observed is not None, {"probe": _clip(observed)})
        counters = probe_counters(sidecar_records()[records_before:])
        scenario2.check("the probe still returned AUTH_REQUIRED (the other platform proved nothing about zdbk)",
                        (observed or {}).get("error_code") == "AUTH_REQUIRED",
                        {"probe": _clip(observed)})
        after = checkpoint_rows(journal)
        scenario2.check("the checkpoint stayed waiting_auth with no continuation turn",
                        len(after) == 1 and after[0]["status"] == "waiting_auth"
                        and after[0].get("continuation_turn_id") is None,
                        {"status": after[0]["status"] if after else None,
                         "continuation_turn_id": after[0].get("continuation_turn_id") if after else None})
        scenario2.check("no automatic continuation turn was enqueued",
                        len(turns(journal)) == before_turns
                        and all("auth-resume" not in (row["request_key"] or "") for row in turns(journal)),
                        {"turns": [{"id": row["id"], "request_key": row["request_key"],
                                    "status": row["status"]} for row in turns(journal)]})
        scenario2.check("the probe started NO password login and NO browser-cookie path",
                        counters["auto_relogin_locked"] == 0 and counters["browser_cookie_adoption"] == 0
                        and counters["browser_cookie_provider_read"] == 0,
                        {"probe_counters": counters})
        sync_calls = [item for item in sidecar_records() if item.get("kind") == "probe_execute"]
        scenario2.check("the probe's refresh really ran with allow_auth_recovery=False",
                        bool(sync_calls) and all(item.get("allow_auth_recovery") is False for item in sync_calls),
                        {"probe_execute": _clip(sync_calls)})
        scenario2.data["trajectory"] = [{"turn": None, "kind": "server_probe", "name": "campus.sync",
                                         "arguments": {"capability": "grades"},
                                         "status": (observed or {}).get("status"),
                                         "error": {"code": (observed or {}).get("error_code")},
                                         "trigger": "trusted success notification of the OTHER platform"},
                                        {"turn": None, "kind": "continuation_turn_created",
                                         "name": None, "arguments": None, "status": "none",
                                         "error": None}]
        scenario2.data["server_side"] = {"checkpoint_id": checkpoint_id,
                                         "checkpoint_status": after[0]["status"] if after else None,
                                         "continuation_turn_id": None, "automatic": False,
                                         "trigger": f"POST /api/v1/connections/{CONNECTION_ID}/check",
                                         "turns": len(turns(journal))}
        scenario2.data["counters"] = {"auto_relogin_calls_from_probe": counters["auto_relogin_locked"],
                                      "browser_provider_calls_from_probe": counters["browser_cookie_provider_read"],
                                      "browser_cookie_adoption_from_probe": counters["browser_cookie_adoption"]}
        probe_expectations.append({"scenario": "2", **counters})
        _ = before_checkpoints
    except Exception:
        scenario2.check("scenario 2 ran without an internal error", False, traceback.format_exc()[-1600:])
    scenarios.append(scenario2.finalize())

    # ── scenario 3: revocation must not advance the checkpoint ─────────────────
    scenario3 = Scenario("3", "NEGATIVE: revoked campus consent cannot be resurrected by the probe")
    try:
        grant = seed_browser_grant(campus_db)
        scenario3.check("a durable browser consent row existed before revocation",
                        browser_grant(campus_db) is not None, {"grant_id": grant["grant_id"]})
        before_turns = len(turns(journal))
        status, revoked = api.request("POST", "/api/v1/agent/campus/disconnect", {"clear_cache": False})
        scenario3.check("the existing revocation entry returned the workspace status",
                        status == 200 and isinstance(revoked, dict) and "browser_session_authorized" in revoked,
                        {"status": status, "revoked": _clip(revoked)})
        scenario3.check("revocation really deleted the durable grant",
                        browser_grant(campus_db) is None, {"grant": browser_grant(campus_db)})
        records_before = len(sidecar_records())
        arm_probe_probe()
        status, _ = api.request("POST", f"/api/v1/connections/{CONNECTION_ID}/check")
        observed = None
        deadline = time.time() + 90
        while time.time() < deadline:
            finished = [item for item in sidecar_records()[records_before:]
                        if item.get("kind") == "probe_finished"]
            if finished:
                observed = finished[-1]
                break
            time.sleep(0.3)
        scenario3.check("the probe still ran after revocation and stayed AUTH_REQUIRED",
                        observed is not None and (observed or {}).get("error_code") == "AUTH_REQUIRED",
                        {"probe": _clip(observed)})
        after = checkpoint_rows(journal)
        scenario3.check("the checkpoint did not advance and no automatic turn was created",
                        len(after) == 1 and after[0]["status"] == "waiting_auth"
                        and after[0].get("continuation_turn_id") is None
                        and len(turns(journal)) == before_turns,
                        {"status": after[0]["status"] if after else None,
                         "continuation_turn_id": after[0].get("continuation_turn_id") if after else None,
                         "turn_count": len(turns(journal))})
        counters = probe_counters(sidecar_records()[records_before:])
        scenario3.check("revocation was not worked around: no password login / cookie adoption / grant recreation",
                        counters["auto_relogin_locked"] == 0 and counters["browser_cookie_adoption"] == 0
                        and counters["browser_cookie_provider_read"] == 0 and browser_grant(campus_db) is None,
                        {"probe_counters": counters, "grant_after": browser_grant(campus_db)})
        scenario3.data["trajectory"] = [{"turn": None, "kind": "revocation", "name": "campus.disconnect",
                                         "arguments": {"clear_cache": False}, "status": "revoked",
                                         "error": None},
                                        {"turn": None, "kind": "server_probe", "name": "campus.sync",
                                         "arguments": {"capability": "grades"},
                                         "status": (observed or {}).get("status"),
                                         "error": {"code": (observed or {}).get("error_code")},
                                         "trigger": "trusted success notification after revocation"},
                                        {"turn": None, "kind": "continuation_turn_created",
                                         "name": None, "arguments": None, "status": "none",
                                         "error": None}]
        scenario3.data["server_side"] = {"checkpoint_id": checkpoint_id,
                                         "checkpoint_status": after[0]["status"] if after else None,
                                         "continuation_turn_id": None, "automatic": False,
                                         "trigger": "POST /api/v1/agent/campus/disconnect {clear_cache:false}",
                                         "turns": len(turns(journal))}
        scenario3.data["counters"] = {"auto_relogin_calls_from_probe": counters["auto_relogin_locked"],
                                      "browser_provider_calls_from_probe": counters["browser_cookie_provider_read"]}
        probe_expectations.append({"scenario": "3", **counters})
    except Exception:
        scenario3.check("scenario 3 ran without an internal error", False, traceback.format_exc()[-1600:])
    scenarios.append(scenario3.finalize())

    # ── scenario 4 + 5: authenticated target session, restart, server continuation ─
    scenario4 = Scenario("4", "POSITIVE: target session authenticated -> server-produced continuation turn")
    scenario5 = Scenario("5", "Narrow scope and read-only guard during the automatic continuation")
    try:
        # Deliberate widening AFTER the checkpoint was written: the resumed turn must use
        # the checkpoint's own scope and the conversation settings must not expand it.
        seed_domain(ISOLATION["STUDY_DATABASE_URL"], model_base_url, with_connection=True,
                    widened_course=True)
        connection = sqlite3.connect(str(journal), timeout=20)
        try:
            with connection:
                row = connection.execute("SELECT settings FROM conversations WHERE id=?",
                                         (conversation_id,)).fetchone()
                settings = json.loads(row[0])
                settings["course_ids"] = sorted(set(settings["course_ids"]) | {COURSE_WIDENED})
                settings["connection_ids"] = sorted(set(settings.get("connection_ids") or []) | {CONNECTION_ID})
                connection.execute("UPDATE conversations SET settings=? WHERE id=?",
                                   (json.dumps(settings, ensure_ascii=False), conversation_id))
        finally:
            connection.close()
        scenario5.check("the domain DB and the conversation settings were deliberately widened",
                        True, {"course_ids": sorted([COURSE_IN_SCOPE, COURSE_WIDENED])})

        api.stop()
        WINDOW_FILE.write_text(json.dumps({"session_ready": True, "stage": "restart"}), encoding="utf-8")
        # A failed probe arms the product's own 30s per-checkpoint cooldown (next_probe_at).
        # The drill now simulates that window having elapsed instead of waiting 30 wall-clock
        # seconds: probe_requested stays set, only the clock moves. The app's boot path
        # (AuthenticationRecovery.__init__ -> restart_auth_recovery) re-arms the probe.
        reset = write_journal(journal, "UPDATE auth_recovery SET next_probe_at=0,probe_requested=1 WHERE id=?",
                              (checkpoint_id,))
        scenario4.check("only the probe cooldown clock was moved forward before the restart",
                        reset == 1, {"rows_reset": reset})
        records_before_restart = len(sidecar_records())
        checkpoint_before_restart = checkpoint_rows(journal)[0]
        turns_before_restart = len(turns(journal))
        api_port = free_port()
        api = IsolatedApi(api_port, WORK / "logs")
        api.start()
        after_restart = checkpoint_rows(journal)
        scenario4.check("the same pending checkpoint survived the API restart with its payload intact",
                        len(after_restart) == 1
                        and after_restart[0]["id"] == checkpoint_before_restart["id"]
                        and (after_restart[0].get("payload") or {}).get("original_goal") == GOAL_TEXT,
                        {"before_restart": checkpoint_before_restart["status"],
                         "after_restart": after_restart[0]["status"] if after_restart else None,
                         "checkpoint_id": checkpoint_before_restart["id"]})

        def continuation_created():
            rows = checkpoint_rows(journal)
            if rows and rows[0].get("continuation_turn_id"):
                return rows
            return []

        resumed = wait_for_turn(journal, continuation_created, 180,
                                "the server to create the continuation turn")
        continuation_turn_id = resumed[0]["continuation_turn_id"]
        scenario4.check("the SERVER produced continuation_turn_id (no driver message posted for it)",
                        bool(continuation_turn_id) and continuation_turn_id != first_turn_id,
                        {"continuation_turn_id": continuation_turn_id, "first_turn_id": first_turn_id})
        scenario4.check("no new user turn was posted by this driver",
                        len(turns(journal)) == turns_before_restart + 1,
                        {"turn_count": len(turns(journal)), "before_restart": turns_before_restart})
        continuation_row = [row for row in turns(journal) if row["id"] == continuation_turn_id][0]
        scenario4.check("the continuation turn was created by the checkpoint's own request key",
                        continuation_row["request_key"] == "auth-resume:" + str(checkpoint_id),
                        {"request_key": continuation_row["request_key"]})
        final = wait_terminal(journal, continuation_turn_id, 300)
        scenario4.check("the continuation turn reached a terminal state",
                        terminal(final), {"status": final["status"], "error": _clip(final.get("error"))})
        scenario4.data["trajectory"] = turn_trajectory(journal, continuation_turn_id)
        accepted = [item for item in scenario4.data["trajectory"] if item["kind"] == "accepted_auth_resume"]
        scenario4.check("the resume payload was accepted with read_only authority only",
                        bool(accepted) and all(item.get("read_only") is True for item in accepted),
                        {"accepted": _clip(accepted)})
        resume_rows = read_journal(
            journal, "SELECT data FROM events WHERE turn_id=? AND kind='accepted_auth_resume'",
            (continuation_turn_id,))
        resume_payload = json.loads(resume_rows[0]["data"]) if resume_rows else {}
        scenario4.check("the original goal and the narrow scope are preserved in the resume payload",
                        resume_payload.get("original_goal") == GOAL_TEXT
                        and resume_payload.get("scope", {}).get("course_ids") == [COURSE_IN_SCOPE],
                        {"original_goal": _clip(resume_payload.get("original_goal")),
                         "scope": resume_payload.get("scope")})
        probe_result = resume_payload.get("pending_read_result") or {}
        probe_records = (probe_result.get("data") or {}).get("records") or []
        expected_scores = {MATH_NAME: "88", CS_NAME: "91"}
        scenario4.check("the continuation reused the server's preserved probe result (the synthetic grades)",
                        probe_result.get("status") == "ok"
                        and sorted(str(row.get("name")) for row in probe_records) == sorted(expected_scores)
                        and all(str(row.get("score")) == expected_scores.get(str(row.get("name")))
                                for row in probe_records),
                        {"probe_result": _clip(probe_result)})
        answer = final.get("response") or ""
        scenario4.check("the final answer answers the ORIGINAL read-only goal with the synthetic grades",
                        MATH_NAME in answer and CS_NAME in answer and "88" in answer and "91" in answer,
                        {"answer": _clip(answer, 400)})
        tool_results = [item for item in scenario4.data["trajectory"] if item["kind"] == "tool_result"]
        refusals = [item for item in scenario4.data["trajectory"]
                    if item["kind"] == "harness_tool_result" and item.get("status") == "error"
                    and "恢复" in json.dumps(item.get("error") or {}, ensure_ascii=False)]
        scenario4.check("the continuation's write-effect attempt was refused by the server guard",
                        bool(refusals)
                        and all(item["name"] not in ("campus.notice.import", "plans.preview", "plans.apply",
                                                     "tasks.build", "connections.sync")
                                for item in tool_results if item.get("status") in ("ok", "partial")),
                        {"refusals": _clip(refusals), "harness_calls": _clip(
                            [item for item in scenario4.data["trajectory"]
                             if item["kind"] == "harness_tool_call"])})
        scenario4.check("the continuation executed no write-effect tool",
                        all(item["name"] not in ("campus.notice.import", "tasks.build", "plans.apply",
                                                 "connections.sync", "connections.courses.import",
                                                 "artifacts.export", "reviews.generate")
                            for item in tool_results if item.get("status") in ("ok", "partial")),
                        {"tool_results": tool_results})
        scenario4.check("no already-completed action was repeated (exactly one grades read in the continuation)",
                        len([item for item in tool_results if item["name"] == "campus.query"]) == 1,
                        {"tool_results": tool_results})

        records = sidecar_records()
        restarted_records = records[records_before_restart:]
        counters = probe_counters(restarted_records)
        finished = [item for item in restarted_records if item.get("kind") == "probe_finished"]
        scenario4.check("the restarted server's boot path probed the pending checkpoint exactly once",
                        len(finished) == 1 and (finished[0].get("error_code") is None),
                        {"probes": _clip(finished)})
        scenario4.check("the successful probe used the injected session and cleared the AUTH error",
                        (finished[0].get("record_count") or 0) == 2 if finished else False,
                        {"probe": _clip(finished[0] if finished else None)})
        scenario4.check("the probe started no password login and no browser adoption",
                        counters["auto_relogin_locked"] == 0 and counters["browser_cookie_adoption"] == 0
                        and counters["browser_cookie_provider_read"] == 0,
                        {"probe_counters": counters})
        scenario4.data["server_side"] = {
            "checkpoint_id": checkpoint_id,
            "checkpoint_status": checkpoint_rows(journal)[0]["status"],
            "continuation_turn_id": continuation_turn_id,
            "automatic": True,
            "trigger": "isolated API restart (AuthenticationRecovery.__init__ -> request_auth_probes + monitor)",
            "turns": len(turns(journal))}
        scenario4.data["counters"] = {"auto_relogin_calls_from_probe": counters["auto_relogin_locked"],
                                      "browser_provider_calls_from_probe": counters["browser_cookie_provider_read"],
                                      "browser_cookie_adoption_from_probe": counters["browser_cookie_adoption"]}
        probe_expectations.append({"scenario": "4", **counters})

        # scenario 5: narrow scope / read-only guard (recorded after the restart, so the
        # continuation turn's own observations are in this window)
        records = sidecar_records()
        context_records = [item for item in records if item.get("kind") == "recovery_tool_context"
                           and item.get("turn") == continuation_turn_id]
        query_events = [item for item in tool_results if item["name"] == "campus.query"]
        guarded = [item for item in records if item.get("kind") == "recovery_tool_guard"
                   and item.get("turn") == continuation_turn_id]
        scenario5.check("the resumed turn's server-built ToolContext kept only the checkpoint's course scope",
                        bool(context_records)
                        and context_records[-1]["course_ids"] == [COURSE_IN_SCOPE]
                        and COURSE_WIDENED not in (context_records[-1]["course_ids"] or []),
                        {"context": _clip(context_records)})
        scenario5.check("the narrowed context is the checkpoint's own request key, not the widened settings",
                        bool(context_records) and str(context_records[-1]["request_key"]).startswith("auth-probe:"),
                        {"context": _clip(context_records[-1] if context_records else None)})
        scenario5.check("the widened (deliberately out-of-scope) course stayed outside the automatic read",
                        all(item["arguments"].get("course_id") != COURSE_WIDENED for item in tool_results),
                        {"tool_results": tool_results})
        scenario5.check("the guarded read-only tool call really ran under that scope",
                        bool(query_events) and query_events[0]["status"] in ("ok", "partial")
                        and bool(context_records),
                        {"query": _clip(query_events), "context": _clip(context_records)})
        scenario5.check("recovery_tool_allowed refused write-effect tools on the automatic continuation",
                        any(item["tool"] == "plans.preview" and item["allowed"] is False for item in guarded),
                        {"guards": _clip(guarded)})
        scenario5.check("the same guard still allows the read-only campus tools",
                        any(item["allowed"] is True for item in guarded),
                        {"guards": _clip(guarded)})
        scenario5.data["server_side"] = {"checkpoint_id": checkpoint_id,
                                         "checkpoint_status": checkpoint_rows(journal)[0]["status"],
                                         "continuation_turn_id": continuation_turn_id, "automatic": True,
                                         "tool_context_scope": _clip(context_records[-1] if context_records else None),
                                         "write_attempts": [{"source": "model tool call via /internal/tool",
                                                             "tool": "plans.preview",
                                                             "error_code": "RECOVERY_READ_ONLY"}]
                                         + [dict(item, source="recovery_tool_allowed guard")
                                            for item in guarded if not item["allowed"]],
                                         "turns": len(turns(journal))}
        scenario5.data["trajectory"] = scenario4.data["trajectory"]
    except Exception:
        detail = traceback.format_exc()[-1600:]
        scenario4.check("scenario 4 ran without an internal error", False, detail)
        scenario5.check("scenario 5 ran without an internal error", False, detail)
        try:
            notes.append("api log tail: " + json.dumps(api.tail(15), ensure_ascii=False))
        except Exception:
            pass
    scenarios.append(scenario4.finalize())
    scenarios.append(scenario5.finalize())

    # ── final cross-checks and evidence ────────────────────────────────────────
    model.stop()
    api.stop()
    personal_after = personal_fingerprints()

    provider_host_ok = True
    try:
        rows = read_journal(domain_db, "SELECT base_url FROM provider_configs WHERE id=?", (PROVIDER_ID,))
        configured = (rows[0]["base_url"] if rows else "") or ""
        parsed = urlsplit(configured)
        provider_host_ok = (parsed.hostname == "127.0.0.1" and parsed.port == model_port)
    except Exception:
        provider_host_ok = False

    changed = {name: {"before": personal_before.get(name), "after": personal_after.get(name)}
               for name in PERSONAL_ROOTS if personal_before.get(name) != personal_after.get(name)}
    non_loopback = non_loopback_connections()

    global_checks = Scenario("0", "Global isolation / offline / provider checks")
    global_checks.check("no personal data path changed (size+mtime fingerprint of var/personal.db, "
                        "var/files, var/secrets, var/agent, var/workspace)",
                        not changed, {"changed": _clip(changed)})
    global_checks.check("the configured model host is this run's own loopback controlled server",
                        provider_host_ok, {"provider_base_url_host": f"127.0.0.1:{model_port}"})
    global_checks.check("zero non-loopback socket connections were made",
                        not non_loopback, {"non_loopback": non_loopback})
    global_checks.check("the controlled model served every model request (no paid provider call)",
                        model.stats["requests"] > 0, {"model": model.stats})

    document = {
        "run_id": RUN_ID,
        "script": "tools/verify_campus_auth_resume.py",
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": _now(),
        "duration_seconds": round(time.monotonic() - begin, 3),
        "python": sys.version.split()[0],
        "repository": str(ROOT),
        "git_head": _git("rev-parse", "HEAD"),
        "repo_head": _git("rev-parse", "HEAD"),
        "dirty_files": [line for line in _git("status", "--porcelain").splitlines() if line.strip()],
        "isolation": {
            "workspace": str(WORK),
            **ISOLATION,
            "protected_paths_never_touched": [str(path) for path in FORBIDDEN],
            "personal_data_fingerprint_unchanged": not changed,
        },
        "synthetic_boundary": {
            "school_protocol": "connectors.campus.campus.CampusClient.request (the school wire boundary) "
                               "answered only for the fixed registered endpoints of the target chain "
                               "(/jwglxt/cxdy/xscx* grades, /jwglxt/xtgl menu, timetable index); any other "
                               "school URL raises instead of guessing",
            "platform_check": "app.routers.connections.resolve_check_adapter - the reference the app's own "
                              "POST /api/v1/connections/{id}/check uses, intercepted only for this run's "
                              "synthetic connection platform; every real connection still resolves through "
                              "the untouched product registry",
            "session_injection": "an already-authenticated synthetic session installed into "
                                 "CampusManager.states['local'] at test startup (the school-protocol session "
                                 "boundary); no password submitted, no browser cookie read",
            "injection_channel": "a generated sitecustomize.py inside this run's scratch directory, imported "
                                 "by the isolated CPython before app.main; no production route, permission "
                                 "or product file was added or modified",
            "not_mocked": ["FastAPI app + uvicorn process", "app.agent_bridge Runtime and real turn journal",
                           "installed Harness apps/agent-runtime/runner.mjs", "CampusService / adapter / "
                           "repository / SQLite campus cache", "durable auth_recovery checkpoint and turns",
                           "the real final business success of the grades sync"],
        },
        "controlled_model": {"base_url": model_base_url, "host": "127.0.0.1",
                             "stats": model.stats, "trajectory": model.trajectory},
        "probe_attribution": probe_expectations,
        "non_loopback_connections": non_loopback,
        "scenarios": scenarios,
        "global": global_checks.finalize(),
        "summary": {item["id"]: {"passed": sum(1 for c in item["checks"] if c["ok"]),
                                 "failed": sum(1 for c in item["checks"] if not c["ok"]),
                                 "status": item["status"]} for item in scenarios} | {
                   "0": {"passed": sum(1 for c in global_checks.data["checks"] if c["ok"]),
                         "failed": sum(1 for c in global_checks.data["checks"] if not c["ok"]),
                         "status": global_checks.data["status"]}},
        "notes": notes,
    }

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

    failures = sum(item["failed"] for item in document["summary"].values())
    for item in scenarios + [global_checks.data]:
        print(f"scenario {item['id']}: {item['status']} "
              f"({sum(1 for c in item['checks'] if c['ok'])} passed / "
              f"{sum(1 for c in item['checks'] if not c['ok'])} failed)")
        for check in item["checks"]:
            print(f"  [{'ok' if check['ok'] else 'FAIL'}] {check['name']}")
    print(f"controlled_model_requests={model.stats['requests']} "
          f"non_loopback_connections={len(non_loopback)}")
    print("evidence: " + str(out_path))

    if not args.keep_scratch:
        cleaned = cleanup()
        print("isolated workspace removed: " + str(cleaned))
    if failures:
        print(f"RESULT: FAILED ({failures} failing checks)", flush=True)
        return 1
    print("RESULT: PASSED (all scenario checks ok)", flush=True)
    return 0


def cleanup() -> bool:
    """Remove only this run's isolated workspace; the evidence document is kept."""
    resolved = WORK.resolve()
    if resolved.parent != SCRATCH_ROOT.resolve() or resolved.name != RUN_ID:
        return False
    for attempt in range(3):
        shutil.rmtree(resolved, ignore_errors=True)
        if not resolved.exists():
            break
        time.sleep(0.5)
    return not resolved.exists()


if __name__ == "__main__":
    raise SystemExit(main())
