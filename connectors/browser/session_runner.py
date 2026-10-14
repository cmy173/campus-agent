#!/usr/bin/env python3
"""A20：受控浏览器会话接入同步编排（会话 → transport → XzzdAdapter → B）。

把 A17 已验证的「受控浏览器可登录」接入真实同步链路。两种运行模式：

1. ``serve``（B 链路接入，关键路径）：本机常驻服务（127.0.0.1，stdlib HTTP）。
   受控浏览器窗口在用户桌面打开（独立 profile），用户自行完成登录；会话 cookie
   仅在内存（本进程），B 进程经 ``connectors/bridge.py`` 转发 check/sync/fetch 到
   本服务执行（B 侧配置 ``STUDY_SYNC_ADAPTERS``），不经手任何会话物料。
2. ``check`` / ``sync`` / ``fetch``（一次性直驱，留证）：进程内起浏览器执行单个
   操作后退出；不经 B，用于证据留存与排障。

红线：凭据（cookie/token）永不落盘、不进日志、不进任何响应与提交物；快照仅在内存；
profile 独立于用户主浏览器；日志记录前经 ``redact`` 剔除查询参数；只读/低频。

线程模型：Playwright sync API 绑定创建线程——浏览器主线程抓取 cookie 快照
（``pump_once``），HTTP 服务子线程只读快照（秒级新鲜度），不跨线程触碰 Playwright 对象。

无会话语义（不静默失败）：
  - check → ConnectionState(state=auth_required, action=登录入口)（不触网）；
  - sync/fetch → AUTH_EXPIRED + ``details.action``（登录入口），B 侧落
    ``auth_required`` + ``recovery_action``，UI 引导用户在连接器窗口登录后重试。

运行（仓库根目录）：
  python -m connectors.browser.session_runner serve --b-api http://127.0.0.1:8000
  python -m connectors.browser.session_runner check --wait 300
  python -m connectors.browser.session_runner sync --course-id <平台课号> --wait 300
  python -m connectors.browser.session_runner fetch --course-id <平台课号> --resource-id <资源external_id> --wait 300

fetch 语义（A22）：fetch 依赖「最近同步的可见性缓存」——CLI 直驱会先对该课执行
`sync_course(kinds=["resources"])` 再取；serve 模式经共享缓存由 B 的 sync → fetch 两阶段满足。
无会话语义见下；护栏 PD-1（64MB）与 deny 分支在适配器内实现。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import queue
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from connectors.adapters.transport import RequestsTransport  # noqa: E402
from connectors.adapters.xzzd import (  # noqa: E402
    LOGIN_ENTRY_URL,
    MAX_DOWNLOAD_BYTES,
    XzzdAdapter,
)
from connectors.adapters.zykt import ZyktAdapter, LOGIN_ENTRY_URL as ZYKT_ENTRY_URL
from connectors.browser.controlled_login import (  # noqa: E402
    _launch,
    _wait_for_session,
    default_profile_dir,
)
from connectors.redact import RedactingFilter  # noqa: E402
from connectors.sync import SyncOrchestrator  # noqa: E402

LOG = logging.getLogger("connectors.browser.session_runner")
if not LOG.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(_h)
    LOG.addFilter(RedactingFilter())
LOG.setLevel(logging.INFO)

PLATFORM = "xzzd"
XZZD_API_BASE = "https://courses.zju.edu.cn"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_RESULT_DIRNAME = "a20-results"
DEFAULT_DOWNLOAD_DIRNAME = "downloads"
COOKIE_SNAPSHOT_INTERVAL_S = 2.0
_BODY_LIMIT_BYTES = 64 * 1024
_FETCH_TIMEOUT_S = 30.0
_SYNC_TIMEOUT_S = 60.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def login_action() -> dict[str, Any]:
    return {
        "type": "user_login",
        "entry_url": LOGIN_ENTRY_URL,
        "message": "登录已失效，请在连接器窗口完成登录后重试",
    }


def auth_required_state(detail: str, *, action: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {
        "platform": PLATFORM,
        "state": "auth_required",
        "checked_at": utc_now(),
        "action": action if action is not None else login_action(),
        "detail": detail,
    }


class _InvalidRequest(Exception):
    pass


class AdapterFailure(Exception):
    """服务内适配器错误 → error dict（bridge/B 转译用；形状同 A3 AdapterError）。"""

    def __init__(self, code: str, message: str, *, retryable: bool,
                 details: Optional[dict] = None) -> None:
        super().__init__(message)
        self.error: dict[str, Any] = {"code": code, "message": message, "retryable": retryable}
        if details is not None:
            self.error["details"] = details


def _translate_adapter_error(exc: Exception) -> Optional[AdapterFailure]:
    """connectors.adapters.base.AdapterError（.error dict）→ AdapterFailure。"""
    error = getattr(exc, "error", None)
    if not isinstance(error, dict) or not isinstance(error.get("code"), str):
        return None
    return AdapterFailure(
        error["code"],
        str(error.get("message") or error["code"]),
        retryable=bool(error.get("retryable", True)),
        details=error.get("details") if isinstance(error.get("details"), dict) else None,
    )


def _close_quietly(session: Any) -> None:
    try:
        if session is not None and hasattr(session, "close"):
            session.close()
    except Exception:  # noqa: BLE001
        pass


# ────────────────────────── 课程映射（内部 crs_… → 平台课号） ──────────────────────────


class CourseMapper:
    """解析内部课程 ID → 平台课号。来源优先级：显式映射 > B API 的 source_refs。

    B API（``GET {b_api}/api/v1/courses``）返回课程的 source_refs（platform/external_id），
    首次查询时拉取并缓存；查询未命中时强制刷新一次（B 侧可能刚绑定）。刷新失败不清空旧缓存。
    """

    _REFRESH_THROTTLE_S = 10.0

    def __init__(self, *, platform: str = PLATFORM, b_api: Optional[str] = None,
                 explicit: Optional[dict[str, str]] = None) -> None:
        self._platform = platform
        self._b_api = b_api.rstrip("/") if isinstance(b_api, str) and b_api else None
        self._explicit = dict(explicit or {})
        self._cache: Optional[dict[str, str]] = None
        self._last_refresh_at = 0.0

    def platform_id(self, course_id: str) -> Optional[str]:
        if course_id in self._explicit:
            return self._explicit[course_id]
        if not self._b_api:
            return None
        if self._cache is None:
            self.refresh()
        if (self._cache is not None and course_id not in self._cache
                and time.monotonic() - self._last_refresh_at >= self._REFRESH_THROTTLE_S):
            self.refresh()  # 未命中：节流后强制刷新一次（B 侧可能刚绑定来源）
        return (self._cache or {}).get(course_id)

    def refresh(self) -> bool:
        self._last_refresh_at = time.monotonic()
        if not self._b_api:
            return False
        try:
            self._cache = self._fetch_map()
            return True
        except Exception as exc:  # noqa: BLE001 —— B 未运行等，保留旧缓存
            LOG.warning("课程映射刷新失败（B API %s）：%s", self._b_api, type(exc).__name__)
            return False

    def _fetch_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        cursor: Optional[str] = None
        for _ in range(50):  # 分页上限保护
            query = {"limit": "100"}
            if cursor:
                query["cursor"] = cursor
            url = f"{self._b_api}/api/v1/courses?" + urllib.parse.urlencode(query)
            with urllib.request.urlopen(url, timeout=10.0) as resp:  # noqa: S310 —— 本机回环
                payload = json.loads(resp.read().decode("utf-8"))
            for item in payload.get("items") or []:
                course_id = item.get("id")
                for ref in item.get("source_refs") or []:
                    if (isinstance(ref, dict) and ref.get("platform") == self._platform
                            and isinstance(ref.get("external_id"), str) and ref["external_id"]):
                        mapping[str(course_id)] = ref["external_id"]
            cursor = payload.get("next_cursor")
            if not cursor:
                break
        return mapping

    def size(self) -> int:
        return len(self._cache or {}) + len(self._explicit)


# ────────────────────────── 受控浏览器会话 ──────────────────────────


class BrowserSession:
    """受控浏览器（独立 profile）+ 内存会话快照。

    Playwright sync API 绑定创建线程：``start``/``pump_once``/``wait_for_session``/``stop``
    一律在启动线程调用；``session``（cookies 快照 → requests.Session）可从服务线程调用。
    cookie 值仅在内存，不落盘、不打印；快照不记录任何值。
    """

    def __init__(self, *, profile_dir: Path, channel: str = "msedge",
                 entry_url: str = LOGIN_ENTRY_URL) -> None:
        self._profile_dir = profile_dir
        self._channel = channel
        self._entry_url = entry_url
        self._pw: Any = None
        self._context: Any = None
        self._page: Any = None
        self._lock = threading.Lock()
        self._snapshot: list[dict[str, Any]] = []
        self._refresh_requests: queue.Queue[threading.Event] = queue.Queue()
        self._home_url = ZYKT_ENTRY_URL if urllib.parse.urlsplit(entry_url).hostname == "classroom.zju.edu.cn" else XZZD_API_BASE + "/"

    def start(self) -> None:
        self._pw, self._context, self._page = _launch(self._profile_dir, self._channel)
        try:
            # 先进入平台首页；持久化登录/学校 SSO 可直接进入，无须经历登录表单。
            self._page.goto(self._home_url, wait_until="domcontentloaded", timeout=45000)
        except Exception:  # noqa: BLE001 —— 被页面自身跳转接管（ERR_ABORTED）等不阻断启动
            LOG.info("入口导航未按常规完成（不阻断；受控浏览器与登录入口仍可用）")
        self.pump_once()

    def pump_once(self) -> None:
        pending: list[threading.Event] = []
        while True:
            try:
                pending.append(self._refresh_requests.get_nowait())
            except queue.Empty:
                break
        if pending:
            try:
                self._page.goto(self._home_url, wait_until="domcontentloaded", timeout=8000)
            except Exception:
                # 已登录时页面脚本可能接管导航；仍采集最终浏览器会话供 API 复核。
                LOG.info("平台会话刷新导航未常规结束，将以接口复核结果判断登录")
            try:
                self._page.wait_for_timeout(300)
            except Exception:
                pass
        cookies = list(self._context.cookies())
        with self._lock:
            self._snapshot = cookies
        for event in pending:
            event.set()

    def refresh_for_check(self) -> bool:
        """服务线程请求主线程复用平台/SSO 会话，不跨线程操作 Playwright。"""
        event = threading.Event()
        self._refresh_requests.put(event)
        return event.wait(timeout=12)

    def cookies_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._snapshot)

    def session(self) -> Any:
        """cookies 快照 → requests.Session（内存）；快照为空视为无会话。"""
        cookies = self.cookies_snapshot()
        if not cookies:
            return None
        import requests  # 仅内存会话层；值不落盘/不打印

        session = requests.Session()
        # 平台默认直连，避免系统代理导致 TLS EOF，并避免隐式读取 .netrc。
        # 如网络确需代理，须显式设置 STUDY_PLATFORM_PROXY；保留 TLS 证书校验。
        session.trust_env = False
        proxy = os.environ.get("STUDY_PLATFORM_PROXY")
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})
        for ck in cookies:
            try:
                kwargs: dict[str, Any] = {"path": ck.get("path") or "/"}
                domain = str(ck.get("domain") or "")
                if domain:
                    kwargs["domain"] = domain
                session.cookies.set(str(ck.get("name", "")), str(ck.get("value", "")), **kwargs)
            except Exception:  # noqa: BLE001 —— 单条 cookie 形态异常跳过
                continue
        return session

    def wait_for_session(self, timeout_s: float) -> dict[str, Any]:
        """主线程只读探针等待登录（复用 A17 原型）；返回探测信息（不含 cookie 值）。"""
        return _wait_for_session(self._context, timeout_s)

    def stop(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass


# ────────────────────────── 连接器服务（B 链路接入面） ──────────────────────────


class ConnectorService:
    """会话侧请求处理核心（无 HTTP 依赖，便于离线测试）。

    op: check / sync / fetch；body 携带 B 侧任务参数（connection_id/course_id/…），
    不含任何凭据。适配器按请求构造（transport 用会话快照），错误序列化为 A3 形状。
    """

    def __init__(self, sessions: Any, *,
                 resolve_course: Optional[Callable[[str], Optional[str]]] = None,
                 adapter_factory: Optional[Callable[[str, Any], Any]] = None,
                 rate_limit_rps: float = 0.5,
                 download_dir: Optional[Path] = None,
                 max_download_bytes: int = MAX_DOWNLOAD_BYTES) -> None:
        # sessions 为鸭子类型：需提供 session()（→ requests.Session | None）与
        # cookies_snapshot()（真实实现为 BrowserSession；测试可注入替身）。
        self._sessions = sessions
        self._resolve_course = resolve_course
        self._adapter_factory = adapter_factory
        self._rate_limit = rate_limit_rps
        self._download_dir = Path(download_dir) if download_dir is not None else None
        self._max_download_bytes = int(max_download_bytes)
        # 资源可见性缓存：跨请求共享（B 两阶段 sync → fetch 依赖；适配器实例逐请求构造）
        self._resources: dict[str, dict[str, Any]] = {}
        self.platform = PLATFORM
        self._operation_lock = threading.RLock()

    def _login_action(self) -> dict[str, Any]:
        return {**login_action(), "entry_url": ZYKT_ENTRY_URL if self.platform == "zykt" else LOGIN_ENTRY_URL}

    def session_cookie_count(self) -> int:
        return len(self._sessions.cookies_snapshot())

    # —— op 入口 ——

    def handle(self, op: str, body: Any) -> tuple[int, dict[str, Any]]:
        if not isinstance(body, dict):
            return 400, self._error("INVALID_REQUEST", "请求体必须是 JSON 对象", retryable=False)
        try:
            with self._operation_lock:
                return 200, {"ok": True, "result": self._dispatch(op, body)}
        except _InvalidRequest as exc:
            return 400, self._error("INVALID_REQUEST", str(exc), retryable=False)
        except AdapterFailure as exc:
            return 200, {"ok": False, "error": exc.error}
        except Exception as exc:  # noqa: BLE001 —— 兜底不静默：如实标注内部错误
            return 500, self._error(
                "INTERNAL_ERROR", f"连接器服务内部错误：{type(exc).__name__}: {exc}"[:300], retryable=False)

    def _dispatch(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        if body.get("platform", self.platform) != self.platform:
            raise _InvalidRequest("该服务的平台与请求不匹配")
        if op == "check":
            return self._check(body)
        if op == "sync":
            return self._sync(body)
        if op == "fetch":
            return self._fetch(body)
        if op == "courses":
            return self._list_courses(body)
        if op == "content":
            return self._content(body)
        if op == "transcript":
            session = self._sessions.session()
            if session is None:
                raise AdapterFailure("AUTH_EXPIRED", "请在连接器窗口登录", retryable=False,
                                     details={"action": self._login_action()})
            sub_id = body.get("sub_id")
            if not isinstance(sub_id, str) or not sub_id:
                _close_quietly(session)
                raise _InvalidRequest("缺少 sub_id")
            try:
                adapter = self._make_adapter(self._connection_id(body), session)
                return adapter.get_transcript(sub_id)
            except Exception as exc:
                translated = _translate_adapter_error(exc)
                if translated is not None:
                    raise translated from None
                raise
            finally:
                _close_quietly(session)
        raise _InvalidRequest(f"未知操作：{op!r}")

    @staticmethod
    def _error(code: str, message: str, *, retryable: bool,
               details: Optional[dict] = None) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message, "retryable": retryable}
        if details is not None:
            error["details"] = details
        return {"ok": False, "error": error}

    # —— 各操作 ——

    def _content(self, body: dict[str, Any]) -> dict[str, Any]:
        outcome = self._fetch(body)
        if outcome.get("status") != "ok":
            return outcome
        handle = outcome.get("handle") or {}
        ref = handle.get("ref")
        if self._download_dir is None or not isinstance(ref, str) or Path(ref).name != ref or ref in (".", ".."):
            raise AdapterFailure("INVALID_PAYLOAD", "资源句柄不合法", retryable=False)
        root = self._download_dir.resolve()
        path = (root / ref).resolve()
        if path.parent != root or not path.is_file():
            raise AdapterFailure("RESOURCE_NOT_FOUND", "暂存资源不可用", retryable=False)
        with path.open("rb") as stream:
            data = stream.read(self._max_download_bytes + 1)
        if len(data) > self._max_download_bytes or f"sha256:{hashlib.sha256(data).hexdigest()}" != handle.get("sha256"):
            raise AdapterFailure("INVALID_PAYLOAD", "资源大小或哈希不匹配", retryable=False)
        meta = self._resources.get(body["resource_id"], {})
        return {"status": "ok", "filename": meta.get("file_name") or ref,
                "sha256": handle["sha256"], "size": len(data),
                "data_base64": base64.b64encode(data).decode("ascii")}

    def _list_courses(self, body: dict[str, Any]) -> dict[str, Any]:
        for key in ("cursor", "term"):
            if body.get(key) is not None and not isinstance(body[key], str):
                raise _InvalidRequest(f"{key} 必须是字符串或 null")
        session = self._sessions.session()
        if session is None:
            raise AdapterFailure("AUTH_EXPIRED", "请先在连接器窗口登录", retryable=False,
                                 details={"action": self._login_action()})
        try:
            return self._make_adapter(self._connection_id(body), session).list_courses(
                cursor=body.get("cursor"), term=body.get("term"))
        except Exception as exc:
            translated = _translate_adapter_error(exc)
            if translated is not None:
                raise translated from None
            raise
        finally:
            _close_quietly(session)

    def _check(self, body: dict[str, Any]) -> dict[str, Any]:
        result = self._check_snapshot(body)
        refresh = getattr(self._sessions, "refresh_for_check", None)
        if result.get("state") == "auth_required" and callable(refresh):
            if not refresh():
                return {"platform": self.platform, "state": "network_error",
                        "checked_at": utc_now(), "action": None,
                        "detail": "浏览器会话刷新未完成，请稍后重试检查；尚未确认登录失效"}
            result = self._check_snapshot(body)
        return result

    def _check_snapshot(self, body: dict[str, Any]) -> dict[str, Any]:
        session = self._sessions.session()
        if session is None:
            return {**auth_required_state("受控浏览器会话不可用（未登录）"),
                    "platform": self.platform, "action": self._login_action()}
        try:
            adapter = self._make_adapter(self._connection_id(body), session)
            # 检查有独立短超时；不与文件下载共用超时，留出一次 SSO 刷新复核的时间。
            transport = getattr(adapter, "_transport", None)
            if isinstance(transport, RequestsTransport):
                transport._timeout = 6.0
            return adapter.check_connection()
        finally:
            _close_quietly(session)

    def _sync(self, body: dict[str, Any]) -> dict[str, Any]:
        course_id = body.get("course_id")
        if not isinstance(course_id, str) or not course_id:
            raise _InvalidRequest("sync 缺少 course_id")
        kinds = body.get("kinds")
        if kinds is not None and (not isinstance(kinds, list)
                                  or any(not isinstance(k, str) for k in kinds)):
            raise _InvalidRequest("kinds 必须是字符串数组")
        since_cursor = body.get("since_cursor")
        if since_cursor is not None and not isinstance(since_cursor, str):
            raise _InvalidRequest("since_cursor 必须是字符串或 null")
        if self._resolve_course is not None and self._resolve_course(course_id) is None:
            raise AdapterFailure(
                "RESOURCE_NOT_FOUND",
                "课程未映射到平台 ID（请在 B 侧绑定该课程来源，或用 --course-map 显式指定）",
                retryable=False, details={},
            )
        session = self._sessions.session()
        if session is None:
            raise AdapterFailure(
                "AUTH_EXPIRED", "受控浏览器会话不可用（未登录）",
                retryable=False, details={"action": self._login_action()},
            )
        adapter = self._make_adapter(self._connection_id(body), session)
        try:
            return SyncOrchestrator(adapter, rate_limit_rps=self._rate_limit).sync_course(
                course_id, since_cursor=since_cursor, kinds=kinds)
        except Exception as exc:  # noqa: BLE001 —— A3 形状转译；其余上抛给兜底
            translated = _translate_adapter_error(exc)
            if translated is not None:
                raise translated from None
            raise
        finally:
            _close_quietly(session)

    def _fetch(self, body: dict[str, Any]) -> dict[str, Any]:
        """fetch 读取共享可见性缓存（同进程内先行 sync 填充，B 两阶段流程满足）；
        缓存未命中的资源按 gone 终态返回（不伪造内容、不猜测课程重新同步）。"""
        resource_id = body.get("resource_id")
        if not isinstance(resource_id, str) or not resource_id:
            raise _InvalidRequest("fetch 缺少 resource_id")
        session = self._sessions.session()
        if session is None:
            raise AdapterFailure(
                "AUTH_EXPIRED", "受控浏览器会话不可用（未登录）",
                retryable=False, details={"action": self._login_action()},
            )
        adapter = self._make_adapter(self._connection_id(body), session)
        try:
            return adapter.fetch_resource(resource_id)
        except Exception as exc:  # noqa: BLE001
            translated = _translate_adapter_error(exc)
            if translated is not None:
                raise translated from None
            raise
        finally:
            _close_quietly(session)

    # —— 基础设施 ——

    @staticmethod
    def _connection_id(body: dict[str, Any]) -> str:
        connection_id = body.get("connection_id")
        return connection_id if isinstance(connection_id, str) and connection_id else "conn-local"

    def _make_adapter(self, connection_id: str, session: Any) -> Any:
        if self._adapter_factory is not None:
            return self._adapter_factory(connection_id, session)
        if self.platform == "zykt":
            # 官网 getToken() 从 _token cookie 的 PHP 序列化值提取当前会话令牌。
            # 只在本地传输层内存中复用；不进入适配器参数、响应、日志或数据库。
            authorization = None
            for ck in getattr(session, "cookies", []):
                domain = str(getattr(ck, "domain", "")).lstrip(".")
                if getattr(ck, "name", "") != "_token" or domain not in ("classroom.zju.edu.cn", "zju.edu.cn"):
                    continue
                decoded = urllib.parse.unquote(ck.value)
                match = re.search(r'\{i:\d+;s:\d+:"_token";i:\d+;s:\d+:"(.+?)";\}', decoded)
                if match and not any(c in match[1] for c in "\r\n"):
                    authorization = match[1]
                    break
            def headers_for_path(path: str) -> dict[str, str]:
                paths = ("/courseapi/v2/course-live/get-my-course-month",
                         "/courseapi/v3/o-course/recent-learning", "/courseapi/v3/course-collect/lists")
                if path not in paths:
                    return {}
                headers = {"Accept": "application/json"}
                if not authorization:
                    return headers
                if path == "/courseapi/v2/course-live/get-my-course-month":
                    headers["Authorization"] = "Bearer " + authorization
                else:
                    headers["Authorization"] = authorization
                return headers
            def params_for_path(path: str) -> dict[str, str]:
                # 官网回放详情/字幕接口要求查询 token，不能只带 Cookie。
                # token 只在会话侧内存传输，不进入响应、日志或 B 数据库。
                protected = ("/courseapi/v3/portal-home-setting/get-sub-info",
                             "/courseapi/v3/web-socket/search-trans-result")
                return {"token": authorization} if authorization and path in protected else {}
            return ZyktAdapter(connection_id=connection_id,
                               transport=RequestsTransport("https://classroom.zju.edu.cn", session, timeout=_FETCH_TIMEOUT_S,
                                                           headers=headers_for_path, params_for_path=params_for_path),
                               resolve_external_id=self._resolve_course, detail_sync=True)
        return XzzdAdapter(
            connection_id=connection_id,
            transport=RequestsTransport(XZZD_API_BASE, session, timeout=_FETCH_TIMEOUT_S),
            resolve_external_id=self._resolve_course,
            download_dir=self._download_dir,
            max_download_bytes=self._max_download_bytes,
            resources=self._resources,
        )


def make_handler(service: ConnectorService) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        server_version = "zjulearn-connector/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # 统一走脱敏 logger
            LOG.debug("%s %s", self.command, self.path)

        def _respond(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if not self._local_request():
                return
            if self.path.split("?", 1)[0] == "/health":
                cookie_count = service.session_cookie_count()
                self._respond(200, {"ok": True, "result": {
                    "service": "platform-session-runner", "platform": service.platform,
                    "session_available": cookie_count > 0,
                    "cookie_count": cookie_count,
                }})
            else:
                self._respond(404, {"ok": False, "error": {
                    "code": "NOT_FOUND", "message": "未知路径", "retryable": False}})

        def do_POST(self) -> None:  # noqa: N802
            if not self._local_request():
                return
            if self.headers.get_content_type() != "application/json":
                self._respond(415, {"ok": False, "error": {"code": "INVALID_REQUEST", "message": "仅接受 application/json", "retryable": False}})
                return
            op = self.path.split("?", 1)[0].strip("/")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > _BODY_LIMIT_BYTES:
                self._respond(400, {"ok": False, "error": {
                    "code": "INVALID_REQUEST", "message": "请求体缺失或超限", "retryable": False}})
                return
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._respond(400, {"ok": False, "error": {
                    "code": "INVALID_REQUEST", "message": "请求体不是合法 JSON", "retryable": False}})
                return
            status, payload = service.handle(op, body)
            self._respond(status, payload)

        def _local_request(self) -> bool:
            host = urllib.parse.urlsplit("http://" + (self.headers.get("Host") or "")).hostname
            if self.headers.get("Origin") is not None or host not in ("127.0.0.1", "localhost", "::1"):
                self._respond(403, {"ok": False, "error": {"code": "PERMISSION_DENIED", "message": "仅允许本地服务调用", "retryable": False}})
                return False
            return True

    return _Handler


def start_http_server(service: ConnectorService, host: str, port: int) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("连接器只允许绑定本机回环地址")
    server = ThreadingHTTPServer((host, port), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ────────────────────────── 一次性命令（直驱留证） ──────────────────────────


def _write_result(result_dir: Path, tag: str, payload: dict[str, Any]) -> Path:
    result_dir.mkdir(parents=True, exist_ok=True)
    path = result_dir / f"{tag}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _default_result_dir(profile_dir: Path) -> Path:
    return profile_dir.parent / DEFAULT_RESULT_DIRNAME


def _default_download_dir(profile_dir: Path) -> Path:
    return profile_dir.parent / DEFAULT_DOWNLOAD_DIRNAME


def _prepare(args: argparse.Namespace) -> BrowserSession:
    browser = BrowserSession(profile_dir=args.profile_dir, channel=args.channel)
    browser.start()
    if getattr(args, "wait", 0) and args.wait > 0:
        probe = browser.wait_for_session(args.wait)
        LOG.info("会话探测：detected=%s elapsed=%ss attempts=%s",
                 probe.get("detected"), probe.get("elapsed_s"), probe.get("probe_attempts"))
    browser.pump_once()
    return browser


def run_check(args: argparse.Namespace) -> int:
    browser = _prepare(args)
    try:
        session = browser.session()
        if session is None:
            state = auth_required_state("受控浏览器会话不可用（未登录）")
        else:
            adapter = XzzdAdapter(
                connection_id="a20-cli",
                transport=RequestsTransport(XZZD_API_BASE, session, timeout=_FETCH_TIMEOUT_S),
            )
            state = adapter.check_connection()
        payload = {"tag": args.tag, "step": "check", "state": state,
                   "cookie_count": len(browser.cookies_snapshot())}
        path = _write_result(args.result_dir, args.tag, payload)
        LOG.info("检查结果已写入：%s", path)
        print(json.dumps({"tag": args.tag, "verdict": state.get("state")}, ensure_ascii=False))
        return 0 if state.get("state") == "ok" else 1
    finally:
        browser.stop()


def run_sync(args: argparse.Namespace) -> int:
    browser = _prepare(args)
    try:
        session = browser.session()
        if session is None:
            payload = {"tag": args.tag, "step": "sync",
                       "state": auth_required_state("受控浏览器会话不可用（未登录）")}
            path = _write_result(args.result_dir, args.tag, payload)
            LOG.info("无会话，未执行同步；状态已写入：%s", path)
            print(json.dumps({"tag": args.tag, "verdict": "auth_required"}, ensure_ascii=False))
            return 1
        adapter = XzzdAdapter(
            connection_id="a20-cli",
            transport=RequestsTransport(XZZD_API_BASE, session, timeout=_SYNC_TIMEOUT_S),
        )
        try:
            envelope = SyncOrchestrator(adapter, rate_limit_rps=args.rate_limit).sync_course(
                args.course_id, since_cursor=args.since_cursor)
        except Exception as exc:  # noqa: BLE001 —— 整体失败如实落盘
            failure = _translate_adapter_error(exc)
            record = {"tag": args.tag, "step": "sync",
                      "error": failure.error if failure else {"code": "INTERNAL_ERROR",
                                                              "message": f"{type(exc).__name__}: {exc}"[:300]}}
            path = _write_result(args.result_dir, args.tag, record)
            LOG.info("同步失败，记录已写入：%s", path)
            print(json.dumps({"tag": args.tag,
                              "verdict": record["error"]["code"]}, ensure_ascii=False))
            return 1
        payload = {"tag": args.tag, "step": "sync", "envelope": envelope}
        path = _write_result(args.result_dir, args.tag, payload)
        stats = envelope.get("stats") or {}
        LOG.info("同步信封已写入：%s", path)
        print(json.dumps({
            "tag": args.tag, "verdict": "sync", "mode": envelope.get("mode"),
            "scanned": stats.get("scanned"), "upserted": stats.get("upserted"),
            "deleted": stats.get("deleted"), "failed": stats.get("failed"),
            "error_codes": sorted({e.get("code") for e in (envelope.get("errors") or [])}),
        }, ensure_ascii=False))
        return 0
    finally:
        browser.stop()


def run_fetch(args: argparse.Namespace) -> int:
    browser = _prepare(args)
    try:
        session = browser.session()
        if session is None:
            payload = {"tag": args.tag, "step": "fetch",
                       "state": auth_required_state("受控浏览器会话不可用（未登录）")}
            path = _write_result(args.result_dir, args.tag, payload)
            LOG.info("无会话，未执行获取；状态已写入：%s", path)
            print(json.dumps({"tag": args.tag, "verdict": "auth_required"}, ensure_ascii=False))
            return 1
        adapter = XzzdAdapter(
            connection_id="a20-cli",
            transport=RequestsTransport(XZZD_API_BASE, session, timeout=_FETCH_TIMEOUT_S),
            download_dir=args.download_dir,
            max_download_bytes=args.max_download_bytes,
        )
        try:
            # 先同步资源可见性缓存（低频只读；fetch 依赖缓存判定 access_level/size/upload_id）
            envelope = SyncOrchestrator(adapter, rate_limit_rps=args.rate_limit).sync_course(
                args.course_id, kinds=["resources"])
            outcome = adapter.fetch_resource(args.resource_id)
        except Exception as exc:  # noqa: BLE001
            failure = _translate_adapter_error(exc)
            record = {"tag": args.tag, "step": "fetch",
                      "error": failure.error if failure else {"code": "INTERNAL_ERROR",
                                                              "message": f"{type(exc).__name__}: {exc}"[:300]}}
            path = _write_result(args.result_dir, args.tag, record)
            LOG.info("获取失败，记录已写入：%s", path)
            print(json.dumps({"tag": args.tag, "verdict": record["error"]["code"]}, ensure_ascii=False))
            return 1
        stats = envelope.get("stats") or {}
        payload = {"tag": args.tag, "step": "fetch",
                   "course_id": args.course_id, "resource_id": args.resource_id,
                   "visibility_scanned": stats.get("scanned"),
                   "download_dir": str(args.download_dir), "outcome": outcome}
        path = _write_result(args.result_dir, args.tag, payload)
        LOG.info("获取结果已写入：%s", path)
        handle = outcome.get("handle") or {}
        print(json.dumps({"tag": args.tag, "verdict": outcome.get("status"),
                          "ref": handle.get("ref"), "sha256": handle.get("sha256"),
                          "size": handle.get("size")}, ensure_ascii=False))
        return 0 if outcome.get("status") == "ok" else 1
    finally:
        browser.stop()


def run_list_courses(args: argparse.Namespace) -> int:
    browser = _prepare(args)
    try:
        session = browser.session()
        if session is None:
            payload = {"tag": args.tag, "step": "list-courses",
                       "state": auth_required_state("受控浏览器会话不可用（未登录）")}
            path = _write_result(args.result_dir, args.tag, payload)
            LOG.info("无会话，未列出课程；状态已写入：%s", path)
            print(json.dumps({"tag": args.tag, "verdict": "auth_required"}, ensure_ascii=False))
            return 1
        adapter = XzzdAdapter(
            connection_id="a20-cli",
            transport=RequestsTransport(XZZD_API_BASE, session, timeout=_FETCH_TIMEOUT_S),
        )
        items: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(20):  # 分页上限保护
            page = adapter.list_courses(cursor=cursor)
            items.extend(page.get("items") or [])
            errors.extend(page.get("errors") or [])
            cursor = page.get("next_cursor")
            if not cursor:
                break
        payload = {"tag": args.tag, "step": "list-courses",
                   "count": len(items), "items": items, "errors": errors}
        path = _write_result(args.result_dir, args.tag, payload)
        LOG.info("课程清单（%d 门）已写入：%s", len(items), path)
        for item in items:  # 供用户在终端指认要同步的课程（运行期输出，不进入提交物）
            print(f"- [{item.get('id')}] {item.get('title')}  {item.get('term') or ''}")
        print(json.dumps({"tag": args.tag, "verdict": "list-courses",
                          "count": len(items)}, ensure_ascii=False))
        return 0
    finally:
        browser.stop()


def run_serve(args: argparse.Namespace) -> int:
    explicit = _parse_course_map(args.course_map)
    platform = args.platform
    mapper = CourseMapper(platform=platform, b_api=args.b_api, explicit=explicit)
    profile = args.profile_dir if platform == "xzzd" else args.profile_dir.parent / "zykt-profile"
    browser = BrowserSession(profile_dir=profile, channel=args.channel,
                             entry_url=ZYKT_ENTRY_URL if platform == "zykt" else LOGIN_ENTRY_URL)
    browser.start()
    service = ConnectorService(browser, resolve_course=mapper.platform_id,
                               rate_limit_rps=args.rate_limit,
                               download_dir=args.download_dir,
                               max_download_bytes=args.max_download_bytes)
    service.platform = platform
    server = start_http_server(service, args.host, args.port)
    LOG.info("连接器服务已就绪：http://%s:%d（受控浏览器窗口已打开；请在窗口内完成登录）",
             args.host, args.port)
    LOG.info("暂存目录（fetch 落盘）：%s", args.download_dir)
    LOG.info("B 侧配置：STUDY_SYNC_ADAPTERS={\"xzzd\": \"connectors.bridge:XzzdBridgeAdapter\"}")
    try:
        while True:
            browser.pump_once()
            time.sleep(COOKIE_SNAPSHOT_INTERVAL_S)
    except KeyboardInterrupt:
        LOG.info("收到退出信号，关闭服务与受控浏览器…")
    finally:
        server.shutdown()
        browser.stop()
    return 0


def _parse_course_map(items: Optional[list[str]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip() or not value.strip():
            raise SystemExit(f"--course-map 须为 内部课程ID=平台课程号：{item!r}")
        mapping[key.strip()] = value.strip()
    return mapping


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="session_runner",
        description="A20 受控浏览器会话接入（serve=B 链路常驻服务；check/sync/fetch=直驱留证）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--profile-dir", type=Path, default=default_profile_dir(),
                        help="独立持久化 profile 目录（默认在用户数据目录下、仓库之外）")
        sp.add_argument("--channel", default="msedge", help="浏览器 channel（默认 msedge，可 chrome）")
        sp.add_argument("--result-dir", type=Path, default=None, help="结果 JSON 目录（默认 profile 同级）")
        sp.add_argument("--tag", default="a20", help="结果文件标签")

    sp_serve = sub.add_parser("serve", help="常驻：受控浏览器 + 本地连接器服务（127.0.0.1）")
    add_common(sp_serve)
    sp_serve.add_argument("--host", default=DEFAULT_HOST)
    sp_serve.add_argument("--platform", choices=("xzzd", "zykt"), default="xzzd")
    sp_serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    sp_serve.add_argument("--b-api", default=None, help="B 服务基址（如 http://127.0.0.1:8000，用于课程映射）")
    sp_serve.add_argument("--course-map", action="append", default=[],
                          help="显式映射 内部课程ID=平台课程号（可多次）")
    sp_serve.add_argument("--rate-limit", type=float, default=0.5, help="同步限速 rps（默认 0.5）")
    sp_serve.add_argument("--download-dir", type=Path, default=None,
                          help="fetch 暂存目录（默认 profile 同级 downloads/，仓库外）")
    sp_serve.add_argument("--max-download-bytes", type=int, default=MAX_DOWNLOAD_BYTES,
                          help="PD-1 护栏（默认 64MB，须与 B/D 同值）")

    sp_check = sub.add_parser("check", help="一次性：会话只读检查")
    add_common(sp_check)
    sp_check.add_argument("--wait", type=float, default=0.0, help="等待登录秒数（0=立即返回）")

    sp_sync = sub.add_parser("sync", help="一次性：真实同步一门课（sync-result 落盘）")
    add_common(sp_sync)
    sp_sync.add_argument("--course-id", required=True, help="平台课程号（直驱模式）")
    sp_sync.add_argument("--since-cursor", default=None)
    sp_sync.add_argument("--wait", type=float, default=0.0)
    sp_sync.add_argument("--rate-limit", type=float, default=0.5)

    sp_fetch = sub.add_parser("fetch", help="一次性：真实获取一个资源（先同步该课资源，fetch-outcome 落盘）")
    add_common(sp_fetch)
    sp_fetch.add_argument("--course-id", required=True, help="平台课程号（先同步填充可见性缓存）")
    sp_fetch.add_argument("--resource-id", required=True, help="平台资源 external_id")
    sp_fetch.add_argument("--wait", type=float, default=0.0)
    sp_fetch.add_argument("--rate-limit", type=float, default=0.5)
    sp_fetch.add_argument("--download-dir", type=Path, default=None,
                          help="fetch 暂存目录（默认 profile 同级 downloads/，仓库外）")
    sp_fetch.add_argument("--max-download-bytes", type=int, default=MAX_DOWNLOAD_BYTES,
                          help="PD-1 护栏（默认 64MB，须与 B/D 同值）")

    sp_list = sub.add_parser("list-courses", help="一次性：列出平台课程（A21 选课用）")
    add_common(sp_list)
    sp_list.add_argument("--wait", type=float, default=0.0)

    args = parser.parse_args(argv)
    args.result_dir = args.result_dir or _default_result_dir(args.profile_dir)
    if hasattr(args, "download_dir"):
        args.download_dir = args.download_dir or _default_download_dir(args.profile_dir)

    try:
        if args.cmd == "serve":
            return run_serve(args)
        if args.cmd == "check":
            return run_check(args)
        if args.cmd == "sync":
            return run_sync(args)
        if args.cmd == "fetch":
            return run_fetch(args)
        return run_list_courses(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
