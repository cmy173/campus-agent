#!/usr/bin/env python3
"""学在浙大（xzzd）真实适配器（A5）——以 A1 §3.3 与 A5b 受控实测（登录态只读观察）为准。

实测基线（A5b）：
  宿主与路由：API 宿主为 courses.zju.edu.cn（个人工作台），接口前缀 /api/；
    门户 course.zju.edu.cn 不承载这些 API（实测 405 / SPA HTML）。
  信封与分页：POST /api/my-courses → {courses[], start, end, page, page_size, pages, total}；
    GET /api/course/{id}/coursewares → {activities[], …}；
    GET /api/courses/{id}/homework-activities → {homework_activities[], …}。
    超界 page 由服务端钳制到有效页（实测钳到末页），不返回空页；page_size 未见服务端校验
    （-1 / 1000 均被接受）——适配器仍按保守页长请求。
  字段：课程含 instructors[{id,name}] 与 semester{…,name,…}（对象，非字符串）；
    课件活动为 title + uploads[]（allow_download / origin_allow_download / deleted / size /
    type 位于 uploads 元素上；活动时间 created_at/updated_at 为 ISO 8601 UTC Z）；
    作业为 submitted(bool) + submitted_status(字符串，可能为空)，列表载荷未见 status_code。
  错误形态：未知路由/资源 → 404 application/json {"message": "未找到资源"}（单键信封）；
    发签端点对受限上传 → 403 application/json {"message": "您没有权限完成此操作"}（A5b 实测）；
    非法方法或非法 conditions JSON → 500 text/html；200 非 JSON → 登录态失效（SSO 页，A5b 佐证）。
  会话形态：无凭据/失效 → 重定向至认证入口（A5b 实测）；40 分钟闲置不失效（实测）；
    用户侧登出动作后受控会话未失效（观测；登出失效形态终值待复测）。
  时间：实测均为 ISO 8601 UTC（Z 结尾）；未见 epoch 秒形态。
  限速：0.5 rps 持续平稳（实测至 ~0.8 rps、约 40s 窗口无 429、无限速响应头）；阈值未探到。
  下载链路：会话内发签端点 GET /api/uploads/{upload_id}/download-url-for-ai →
    tcmedia.zju.edu.cn 签名 URL（timestamp/token/name；按整点桶确定性签发，同级桶重复签发同值，
    链接跨小时边界 ≥50 分钟可用，第二小时桶复签复测 200）。
    发签受 uploads[].allow_download 门控（许可为真 → 200；非真 → 403）。
    A22（本轮）开放真实获取：full 资源经发签 → 流式下载（绝对签名 URL，不复用旧链接）→
    sha256 暂存件（fetch-outcome.handle=local_file）；护栏 PD-1（单文件 64MB）在元数据
    与下载中途双重把关（超限 → denial，记契约观察点：denial 枚举无"策略超限"）。

实现状态（禁止把未验证项表述为真实可用）：
  已实现（离线单测，合成响应；A22 下载路径另经本地 HTTP 实链单测）；尚未验证：会话失效
    路径的响应码终值（登出失效形态未取得）、conditions 必填键值全集、限速上限阈值、
    uploads[].deleted 置位真实样本、异地网络失效形态、受限/仅预览资源的真实样本
    （denial 分支为合成测试覆盖）。

凭据红线：不自建登录、不接触凭据；会话由调用方注入的 transport 持有（见 transport.py），
适配器不读取、不记录凭据材料；日志经 RedactingFilter 剔除 URL 查询参数；
签名下载 URL（含 token 查询参数）不落日志、不进错误详情、不进返回值（仅作暂时句柄消费）。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .base import AdapterError
from .transport import DownloadError, DownloadTooLarge, HttpTransport, is_true
from ..redact import RedactingFilter

PLATFORM = "xzzd"
CONNECTOR_VERSION = "0.2.0-xzzd"
LOGIN_ENTRY_URL = "https://course.zju.edu.cn/ua/login"

# PD-1（协调者代裁，须与 B/D 同值）：单文件获取/解析护栏上限 64MB
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024

_DOWNLOAD_SIGN_PATH = "/api/uploads/{upload_id}/download-url-for-ai"
_DOWNLOAD_CHUNK_BYTES = 256 * 1024
_STAGING_TTL_HOURS = 24

_COURSE_PAGE_SIZE = 20
_LIST_PAGE_SIZE = 50
_MAX_PAGES = 100
# 课件列表 conditions：实测为 JSON 序列化字符串；提供时必须是合法 JSON（非法值 → 500，A5b），
# 缺省亦可 200（A5b）。当前保守发送 itemsSortBy；category/class_ids 等键非本连接器场景必需。
_COURSEWARE_CONDITIONS = {"itemsSortBy": {"predicate": "updated_at", "reverse": True}}
_COURSE_ARRAY_KEYS = ("courses", "data", "items", "list")
_COURSEWARE_ARRAY_KEYS = ("activities", "data", "items", "list")
_HOMEWORK_ARRAY_KEYS = ("homework_activities", "data", "items", "list")
_SUBMISSION_STATUS_MAP = {
    "not_submitted": "not_submitted",
    "unsubmitted": "not_submitted",
    "submitted": "submitted",
    "graded": "graded",
}

_logger = logging.getLogger("connectors.adapters.xzzd")
if not any(isinstance(f, RedactingFilter) for f in _logger.filters):
    _logger.addFilter(RedactingFilter())


def to_contract_time(value: Any) -> Optional[str]:
    """ISO 8601（平台实测为 UTC Z）→ 契约时间（UTC，Z 结尾）；无法解析/缺失 → None（不猜测）。"""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def map_access_level(allow_download: Any, origin_allow_download: Any) -> str:
    """下载许可标记 → 契约 access_level 三态真值表（保守，不猜测）。

    | allow_download | origin_allow_download | 结果 |
    |---|---|---|
    | true | 任意 | full |
    | false | true | preview_only（源许可存在，当前被上下文策略限制） |
    | false/未知 | false/未知 | restricted |

    实测仅确认字段存在与名称（A1 §3.3-4）；真值表待 A5b 以更多样本固化（A1 §5-11）。
    """
    allow = is_true(allow_download)
    if allow is True:
        return "full"
    origin = is_true(origin_allow_download)
    if allow is False and origin is True:
        return "preview_only"
    return "restricted"


def map_submission_status(submitted: Any, submitted_status: Any = None) -> str:
    """平台提交事实 → 契约枚举；未知值 → unknown（不猜测，A4 §2）。

    A5b 实测（列表载荷）：作业含 submitted(bool) 与 submitted_status(字符串，可能为空串)，
    未见 M0 记录的 status_code 双语字段。真值表（bool 为主源，graded 允许字符串升级）：
      submitted=True → submitted；submitted=False → not_submitted；
      submitted_status 可识别为 graded 时升级为 graded；
      bool 缺失（None/未知形态）时回退字符串枚举；其余 → unknown。
    """
    mapped = None
    if isinstance(submitted_status, str) and submitted_status.strip():
        mapped = _SUBMISSION_STATUS_MAP.get(submitted_status.strip().lower())
    if mapped == "graded":
        return "graded"
    flag = is_true(submitted)
    if flag is True:
        return "submitted"
    if flag is False:
        return "not_submitted"
    if mapped is not None:
        return mapped
    return "unknown"


def map_resource_kind(name: Any) -> str:
    """文件名扩展名 → 契约 kind 启发式（待 A5b 校正；无法判定 → other）。"""
    ext = ""
    if isinstance(name, str) and "." in name:
        ext = name.rsplit(".", 1)[-1].lower()
    if ext in ("ppt", "pptx", "pptm"):
        return "slides"
    if ext in ("pdf", "doc", "docx", "xls", "xlsx", "md", "txt"):
        return "document"
    if ext in ("png", "jpg", "jpeg", "gif", "bmp", "webp"):
        return "scan"
    return "other"


def _login_action() -> dict[str, Any]:
    return {
        "type": "user_login",
        "entry_url": LOGIN_ENTRY_URL,
        "message": "登录已失效，请在连接器窗口完成登录后重试",
    }


def _denial(reason: str, message: str) -> dict[str, Any]:
    return {"status": "denied", "handle": None, "denial": {"reason": reason, "message": message}}


def _staged_ref(resource_id: str, file_name: Any) -> str:
    """暂存件相对标识：xzzd-{external_id}[.{ext}]——确定性（两次获取同名）、不含课程名。"""
    base = re.sub(r"[^A-Za-z0-9._-]", "_", f"xzzd-{resource_id}")[:80]
    ext = ""
    if isinstance(file_name, str) and "." in file_name:
        ext = re.sub(r"[^a-z0-9]", "", file_name.rsplit(".", 1)[-1].lower())[:8]
    return f"{base}.{ext}" if ext else base


def _item_error(code: str, message: str, external_id: Optional[str] = None) -> dict[str, Any]:
    details = {"external_id": external_id} if external_id is not None else None
    return {
        "code": code,
        "message": message,
        "retryable": False,
        "details": details,
        "request_id": "req_xzzd",
    }


class XzzdAdapter:
    """五操作面实现；会话由 transport 持有，适配器只构造相对路径与查询参数。"""

    platform = PLATFORM
    connector_version = CONNECTOR_VERSION

    def __init__(
        self,
        connection_id: str,
        *,
        transport: HttpTransport,
        now: Optional[Callable[[], datetime]] = None,
        resolve_external_id: Optional[Callable[[str], Optional[str]]] = None,
        download_dir: Optional[Path] = None,
        max_download_bytes: int = MAX_DOWNLOAD_BYTES,
        resources: Optional[dict[str, dict[str, Any]]] = None,
    ) -> None:
        self.connection_id = connection_id
        self._transport = transport
        self._now = now or (lambda: datetime.now(timezone.utc))
        # 内部课程 ID → 平台课程 ID 映射（A9 联调：B 提供映射函数；缺省视为同一值）
        self._resolve = resolve_external_id or (lambda course_id: course_id)
        # 最近一次同步观测到的资源可见性缓存（external_id → 元数据），供 fetch_resource 判定。
        # resources 可注入共享字典（A20 serve：跨请求共享，B 两阶段流程 sync→fetch 依赖）。
        self._resources: dict[str, dict[str, Any]] = resources if resources is not None else {}
        # A22 真实获取：暂存目录（fetch 必配）与 PD-1 护栏（须与 B/D 同值 64MB）
        self._download_dir = Path(download_dir) if download_dir is not None else None
        self._max_download_bytes = int(max_download_bytes)

    def _platform_course_id(self, course_id: str) -> str:
        external = self._resolve(course_id)
        if not external:
            raise AdapterError(
                "RESOURCE_NOT_FOUND",
                "课程未映射到 xzzd 平台 ID（A9 联调：B 提供内部→平台映射）",
                retryable=False,
            )
        return external

    # ---------- 基础设施 ----------

    def _observed_at(self) -> str:
        dt = self._now()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        """发送请求并解析 JSON。非 2xx / 非 JSON 统一映射为 AdapterError（形状见错误码表）。"""
        try:
            resp = self._transport.request(method, path, params=params, json_body=json_body)
        except Exception as exc:  # 传输层异常（超时/断连）→ NETWORK_ERROR
            raise AdapterError(
                "NETWORK_ERROR", f"传输失败：{type(exc).__name__}", retryable=True,
                details={"path": path},
            ) from exc
        status = resp.status_code
        if status in (401, 419):
            raise AdapterError(
                "AUTH_EXPIRED", f"HTTP {status}（登录态失效）", retryable=False,
                details={"action": _login_action(), "http_status": status},
            )
        if status == 403:
            # 实测（A5b）：403 为单键 JSON {"message": "您没有权限完成此操作"}（发签端点受限上传样本）
            # ——权限类拒绝，非登录态失效；携带平台文案供上层展示
            message = None
            try:
                body = resp.json()
                if isinstance(body, dict) and isinstance(body.get("message"), str):
                    message = body["message"]
            except ValueError:
                pass
            details_403: dict[str, Any] = {"http_status": status}
            if message:
                details_403["message"] = message
            raise AdapterError("PERMISSION_DENIED", "HTTP 403（平台拒绝该操作/资源）", retryable=False,
                               details=details_403)
        if status == 429:
            raise AdapterError("RATE_LIMITED", "HTTP 429（限流）", retryable=True,
                               details={"http_status": status})
        if status == 404:
            # 实测（A5b）：404 为 application/json 单键信封 {"message": "未找到资源"}（未知路由/资源）
            message = None
            try:
                body = resp.json()
                if isinstance(body, dict) and isinstance(body.get("message"), str):
                    message = body["message"]
            except ValueError:
                pass
            details: dict[str, Any] = {"http_status": status}
            if message:
                details["message"] = message
            raise AdapterError("RESOURCE_NOT_FOUND", "HTTP 404（资源/路由不存在）", retryable=False,
                               details=details)
        if status >= 500:
            # 实测（A5b）：500 为 text/html 错误页（非法方法/非法 conditions 亦可触发）
            raise AdapterError("NETWORK_ERROR", f"HTTP {status}", retryable=True,
                               details={"http_status": status})
        if not 200 <= status < 300:
            raise AdapterError("UPSTREAM_CHANGED", f"未登记状态码 HTTP {status}", retryable=False,
                               details={"http_status": status})
        try:
            return resp.json()
        except ValueError as exc:
            # 200 但非 JSON：SSO 登录页 HTML（A5b 佐证：无凭据请求被重定向至认证入口）
            raise AdapterError(
                "AUTH_EXPIRED", "响应非 JSON（登录态失效/SSO 页面）", retryable=False,
                details={"action": _login_action(), "path": path},
            ) from exc

    @staticmethod
    def _extract_items(envelope: Any, preferred: Sequence[str]) -> list[Any]:
        if not isinstance(envelope, dict):
            raise AdapterError("UPSTREAM_CHANGED", "响应信封非对象", retryable=False)
        for key in preferred:
            value = envelope.get(key)
            if isinstance(value, list):
                return value
        for value in envelope.values():
            if isinstance(value, list):
                return value
        raise AdapterError("UPSTREAM_CHANGED", "响应信封中未找到业务数组（形态待 A5b 校正）",
                           retryable=False)

    @staticmethod
    def _page_count(envelope: Any) -> int:
        if not isinstance(envelope, dict):
            return 1
        try:
            return max(1, int(envelope.get("pages", 1)))
        except (TypeError, ValueError):
            return 1

    # ---------- 操作面 1：check_connection ----------

    def check_connection(self) -> dict[str, Any]:
        checked_at = self._observed_at()
        try:
            resp = self._transport.request(
                "POST", "/api/my-courses", json_body={"page": 1, "page_size": 1}
            )
        except Exception as exc:
            return self._state("network_error", checked_at, detail=f"传输失败：{type(exc).__name__}")

        status = resp.status_code
        if status == 200:
            try:
                resp.json()
            except ValueError:
                # 200 但非 JSON：SSO 登录页 HTML（A5b 佐证：无凭据请求被重定向至认证入口）
                return self._state("auth_required", checked_at, action=_login_action())
            return self._state("ok", checked_at)
        if status in (401, 419):
            return self._state("auth_required", checked_at, action=_login_action(),
                               detail=f"HTTP {status}")
        if status == 429:
            return self._state("blocked", checked_at, detail="HTTP 429（限流/风控，稍后再试）")
        if status >= 500:
            return self._state("network_error", checked_at, detail=f"HTTP {status}")
        return self._state("blocked", checked_at, detail=f"平台拒绝（HTTP {status}）")

    def _state(
        self, state: str, checked_at: str, *, action: Optional[dict] = None, detail: Optional[str] = None
    ) -> dict[str, Any]:
        return {
            "platform": PLATFORM,
            "state": state,
            "checked_at": checked_at,
            "action": action,
            "detail": detail,
        }

    # ---------- 操作面 2：get_capabilities ----------

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "platform": PLATFORM,
            "connection_id": self.connection_id,
            "connector_version": CONNECTOR_VERSION,
            "auth": {
                "mode": "user_session",
                "sso": "zjuam-cas",
                "recovery_hint": "登录已失效，请在连接器窗口完成登录后重试",
            },
            "features": {
                "course_list": {"status": "supported", "note": "分页信封 start/end/page/page_size/pages/total；超界 page 被钳到末页（A5b）"},
                "course_meta": {"status": "supported", "note": "课程详情端点已实测（A1 §3.3-1）；instructors/semester 形态见 A5b"},
                "attachments_meta": {"status": "supported", "note": "活动含 uploads[] 附件元数据（allow_download/origin_allow_download/deleted/size/type）（A5b）"},
                "attachment_download": {
                    "status": "supported",
                    "note": "A22 实测（2026-10-10）：full 资源真实下载成功（3/3，2.4–3.7MB）——发签 /api/uploads/{id}/download-url-for-ai → tcmedia 媒体链流式下载 → sha256 暂存件；每次获取重新发签（临时链接过期重取）；护栏 PD-1 单文件 64MB（超限拒绝）；发签/下载 403 门控与受限分支未遇真实样本（合成测试覆盖）",
                },
                "assignments": {"status": "supported", "note": "submitted/submitted_status 真值表按实测校正（A5b）；列表载荷未见 status_code"},
                "recordings": {"status": "unknown", "note": "未观测到回放目录接口；不承诺"},
                "subtitles": {"status": "unknown", "note": "未观测到字幕接口；不承诺"},
                "playback_seek": {"status": "unknown", "note": "online_video 播放定位未验证"},
                "deletion_signal": {
                    "status": "unknown",
                    "note": "uploads[].deleted 字段实测存在；置位样本未采集（A5b 两课附件均未删除）",
                },
            },
            "limits": {
                "page_size_max": None,
                "rate_limit_note": "M1 默认 0.5 rps（A4 §6）；A5b 实测至 ~0.8 rps 约 40s 无 429/无限速头，阈值未探到",
                "campus_network_required": "unknown",
            },
            "checked_at": self._observed_at(),
        }

    # ---------- 操作面 3：list_courses ----------

    def list_courses(self, cursor: Optional[str] = None, term: Optional[str] = None) -> dict[str, Any]:
        if term is not None:
            raise AdapterError(
                "VALIDATION_FAILED",
                "学期筛选参数未在实测中确认（A1 §5-4）；M1 不启用学期筛选",
                retryable=False,
            )
        page = self._decode_cursor(cursor)
        observed_at = self._observed_at()
        envelope = self._request(
            "POST", "/api/my-courses",
            json_body={"page": page, "page_size": _COURSE_PAGE_SIZE},
        )
        raws = self._extract_items(envelope, _COURSE_ARRAY_KEYS)
        items: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for raw in raws:
            course, err = self._map_course(raw, observed_at)
            if course is not None:
                items.append(course)
            if err is not None:
                errors.append(err)
        pages = self._page_count(envelope)
        next_cursor = f"p{page + 1}" if page < pages else None
        return {"items": items, "next_cursor": next_cursor, "errors": errors}

    @staticmethod
    def _decode_cursor(cursor: Optional[str]) -> int:
        if cursor is None:
            return 1
        if isinstance(cursor, str) and cursor.startswith("p") and cursor[1:].isdigit():
            return int(cursor[1:])
        raise AdapterError("VALIDATION_FAILED", "游标无效（不透明游标仅接受本适配器返回值）",
                           retryable=False)

    def _map_course(self, raw: Any, observed_at: str) -> tuple[Optional[dict[str, Any]], Optional[dict]]:
        if not isinstance(raw, dict) or raw.get("id") is None:
            return None, _item_error("UPSTREAM_CHANGED", "课程项缺少平台 ID（形态待 A5b 校正）")
        external_id = str(raw["id"])
        title = raw.get("name") or raw.get("title")
        if not isinstance(title, str) or not title.strip():
            return None, _item_error("UPSTREAM_CHANGED", "课程项缺少名称", external_id)
        # A5b 实测：semester 为对象 {code,id,name,real_name,sort}；instructors 为 [{id,name}]
        term = None
        semester = raw.get("semester")
        if isinstance(semester, dict):
            name = semester.get("name") or semester.get("real_name")
            term = name if isinstance(name, str) and name.strip() else None
        elif isinstance(semester, str) and semester.strip():
            term = semester
        elif isinstance(raw.get("term"), str) and raw["term"].strip():
            term = raw["term"]
        teacher = None
        instructors = raw.get("instructors")
        if isinstance(instructors, list):
            names = [i.get("name") for i in instructors
                     if isinstance(i, dict) and isinstance(i.get("name"), str) and i["name"].strip()]
            if names:
                teacher = "、".join(names)
        if teacher is None:
            for key in ("teacher_name", "teacher", "instructor"):
                value = raw.get(key)
                if isinstance(value, str) and value.strip():
                    teacher = value
                    break
        created = to_contract_time(raw.get("created_at")) or observed_at
        updated = to_contract_time(raw.get("updated_at")) or observed_at
        course = {
            "id": external_id,
            "title": title,
            "term": term if isinstance(term, str) and term else None,
            "teacher": teacher if isinstance(teacher, str) and teacher else None,
            "source_refs": [{
                "platform": PLATFORM,
                "external_id": external_id,
                "canonical_url": None,
                "observed_at": observed_at,
            }],
            "archived_at": None,
            "created_at": created,
            "updated_at": updated,
        }
        return course, None

    # ---------- 操作面 4：sync_course ----------

    def sync_course(
        self,
        course_id: str,
        since_cursor: Optional[str] = None,
        kinds: Optional[Sequence[str]] = None,
    ) -> dict[str, Any]:
        """全量对照（mode=resync）：平台无增量接口，每次列出权威列表交由 B 做指纹比对（A4 §2）。

        游标为不透明标记（平台分页为页码制，不跨调用表达变更）；不因空响应/失败产生删除。
        """
        allowed = None if kinds is None else set(kinds)
        observed_at = self._observed_at()
        platform_id = self._platform_course_id(course_id)
        upserts: dict[str, list[dict[str, Any]]] = {"resources": [], "assignments": [], "recordings": []}
        deletions: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        scanned = 0

        if allowed is None or "resources" in allowed:
            n, u, d, e = self._sync_coursewares(platform_id, observed_at)
            scanned += n
            upserts["resources"] = u
            deletions.extend(d)
            errors.extend(e)
        if allowed is None or "assignments" in allowed:
            n, u, d, e = self._sync_homework(platform_id, observed_at)
            scanned += n
            upserts["assignments"] = u
            deletions.extend(d)
            errors.extend(e)

        upserted = sum(len(v) for v in upserts.values())
        token = f"xzzd-full:{course_id}:{observed_at}"
        return {
            "platform": PLATFORM,
            "connection_id": self.connection_id,
            "course_id": course_id,
            "mode": "resync",
            "cursor": {"previous": since_cursor, "next": token},
            "upserts": upserts,
            "deletions": deletions,
            "errors": errors,
            "stats": {
                "scanned": scanned,
                "upserted": upserted,
                "deleted": len(deletions),
                "failed": len(errors),
            },
            "observed_at": observed_at,
        }

    def _iter_pages(
        self, path: str, params_base: dict[str, Any], preferred: Sequence[str]
    ) -> list[Any]:
        """逐页收集请求级失败直接上抛（暂时性错误由 SyncOrchestrator 有界重试；幂等因此安全）。"""
        raws: list[Any] = []
        page = 1
        while True:
            envelope = self._request("GET", path, params=dict(params_base, page=page))
            raws.extend(self._extract_items(envelope, preferred))
            pages = self._page_count(envelope)
            if pages > _MAX_PAGES:
                raise AdapterError("UPSTREAM_CHANGED", f"分页数 {pages} 超过上限 {_MAX_PAGES}",
                                   retryable=False)
            if page >= pages:
                return raws
            page += 1

    def _sync_coursewares(
        self, platform_id: str, observed_at: str
    ) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        raws = self._iter_pages(
            f"/api/course/{platform_id}/coursewares",
            {
                "conditions": json.dumps(_COURSEWARE_CONDITIONS),
                "page_size": _LIST_PAGE_SIZE,
            },
            _COURSEWARE_ARRAY_KEYS,
        )
        upserts: list[dict[str, Any]] = []
        deletions: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        scanned = 0
        for raw in raws:
            scanned += 1
            if not isinstance(raw, dict) or raw.get("id") is None:
                errors.append(_item_error("UPSTREAM_CHANGED", "课件项缺少平台 ID"))
                continue
            external_id = str(raw["id"])
            # A5b 实测：活动含 uploads[]；下载许可与删除信号位于 uploads 元素上
            uploads = [u for u in (raw.get("uploads") or []) if isinstance(u, dict)]
            live = [u for u in uploads if is_true(u.get("deleted")) is not True]
            if is_true(raw.get("deleted")) is True or (uploads and not live):
                self._resources.pop(external_id, None)  # 显式删除信号同时失效可见性缓存
                deletions.append({
                    "kind": "resource",
                    "external_id": external_id,
                    "reason": "explicit_delete_signal",
                    "observed_at": observed_at,
                })
                continue
            if not uploads:
                # 非文件型活动（无附件载荷）；不是本连接器管理的资源，跳过且不产出删除信号
                continue
            file = live[0]
            title = raw.get("title") or file.get("name")
            if not isinstance(title, str) or not title.strip():
                errors.append(_item_error("UPSTREAM_CHANGED", "课件项缺少标题/文件名", external_id))
                continue
            size = file.get("size")
            try:
                size_bytes = int(size) if size is not None else None
            except (TypeError, ValueError):
                size_bytes = None
            mime = file.get("type")
            upsert = {
                "kind": map_resource_kind(file.get("name") or title),
                "title": title,
                "source_ref": {
                    "platform": PLATFORM,
                    "external_id": external_id,
                    "canonical_url": None,
                    "observed_at": observed_at,
                },
                "content_hash": None,
                "size_bytes": size_bytes,
                "mime_type": mime if isinstance(mime, str) and "/" in mime else None,
                "access_level": map_access_level(file.get("allow_download"), file.get("origin_allow_download")),
                "source_updated_at": to_contract_time(raw.get("updated_at") or file.get("updated_at")),
            }
            upserts.append(upsert)
            # 可见性缓存（供 fetch_resource）：含发签所需 upload_id 与暂存件命名所需的文件名
            self._resources[external_id] = {
                "access_level": upsert["access_level"],
                "size_bytes": size_bytes,
                "title": title,
                "mime_type": upsert["mime_type"],
                "upload_id": str(file["id"]) if file.get("id") is not None else None,
                "file_name": file.get("name") if isinstance(file.get("name"), str) else None,
            }
        return scanned, upserts, deletions, errors

    def _sync_homework(
        self, platform_id: str, observed_at: str
    ) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        raws = self._iter_pages(
            f"/api/courses/{platform_id}/homework-activities",
            {"page_size": _LIST_PAGE_SIZE, "reloadPage": "false"},
            _HOMEWORK_ARRAY_KEYS,
        )
        upserts: list[dict[str, Any]] = []
        deletions: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        scanned = 0
        for raw in raws:
            scanned += 1
            if not isinstance(raw, dict) or raw.get("id") is None:
                errors.append(_item_error("UPSTREAM_CHANGED", "作业项缺少平台 ID"))
                continue
            external_id = str(raw["id"])
            if is_true(raw.get("deleted")) is True:
                deletions.append({
                    "kind": "assignment",
                    "external_id": external_id,
                    "reason": "explicit_delete_signal",
                    "observed_at": observed_at,
                })
                continue
            title = raw.get("title") or raw.get("name")
            if not isinstance(title, str) or not title.strip():
                errors.append(_item_error("UPSTREAM_CHANGED", "作业项缺少标题", external_id))
                continue
            data = raw.get("data")
            instructions = data.get("description") if isinstance(data, dict) else None
            due_raw = raw.get("deadline") or raw.get("end_time")
            due = to_contract_time(due_raw)
            upserts.append({
                "title": title,
                "instructions": instructions if isinstance(instructions, str) and instructions else None,
                "due_at": due,
                "due_precision": "datetime" if due else "unknown",
                "submission_status": map_submission_status(raw.get("submitted"), raw.get("submitted_status")),
                "source_ref": {
                    "platform": PLATFORM,
                    "external_id": external_id,
                    "canonical_url": None,
                    "observed_at": observed_at,
                },
            })
        return scanned, upserts, deletions, errors

    # ---------- 操作面 5：fetch_resource ----------

    def fetch_resource(self, resource_id: str) -> dict[str, Any]:
        """A22：依据最近一次同步（同进程内先行）的可见性缓存执行真实获取。

        - 未观测（缓存无此 id）→ gone；restricted / preview_only → denied（不绕过）；
        - full：先按元数据尺寸套 PD-1 护栏（64MB；超限 → denied + 策略说明），再发签
          （GET /api/uploads/{upload_id}/download-url-for-ai，每次重新发签、不复用旧链接）→
          流式下载（签名 URL 为绝对地址）→ sha256 → ok(handle=local_file 暂存件)。
        发签 403 → denied；下载通道 403 → denied、404/410 → gone、401/419 →
        AUTH_EXPIRED、429/5xx/网络 → AdapterError（retryable，由编排层有界重试）。
        护栏超限以 denial(reason=permission_denied) + message 表达——fetch-outcome 契约
        denial 枚举无「策略超限」，记契约观察点（与 B/D 对齐后再议）。
        resource_id 约定为平台 external_id（与 B 的联调口径见 A9）。
        """
        meta = self._resources.get(resource_id)
        if meta is None:
            return {"status": "gone", "handle": None, "denial": None}
        access = meta.get("access_level")
        if access == "restricted":
            return _denial("permission_denied", "资源受限：平台许可标记不允许下载")
        if access == "preview_only":
            return _denial("preview_only", "仅可预览：当前上下文不允许下载（不绕过）")
        if self._download_dir is None:
            raise AdapterError(
                "VALIDATION_FAILED",
                "fetch_resource 需配置 download_dir（暂存目录）", retryable=False,
            )
        download = getattr(self._transport, "download", None)
        if download is None:
            raise AdapterError(
                "VALIDATION_FAILED",
                "传输层不支持流式下载（需提供 DownloadTransport.download）", retryable=False,
            )
        size = meta.get("size_bytes")
        if isinstance(size, int) and size > self._max_download_bytes:
            return _denial(
                "permission_denied",
                f"超出解析护栏（PD-1）：文件 {size} 字节 > 上限 {self._max_download_bytes} 字节，"
                "按策略拒绝获取",
            )
        upload_id = meta.get("upload_id")
        if not upload_id:
            raise AdapterError(
                "UPSTREAM_CHANGED", "资源缺少 upload id，无法发签（平台形态变更待观察）",
                retryable=False,
            )
        try:
            envelope = self._request("GET", _DOWNLOAD_SIGN_PATH.format(upload_id=upload_id))
        except AdapterError as exc:
            if exc.code == "PERMISSION_DENIED":
                platform_message = (exc.error.get("details") or {}).get("message")
                message = "平台拒绝发签（HTTP 403）——资源不可获取"
                if platform_message:
                    message += f"：{platform_message}"
                return _denial("permission_denied", message)
            raise
        url = envelope.get("url") if isinstance(envelope, dict) else None
        if not isinstance(url, str) or not url:
            raise AdapterError(
                "UPSTREAM_CHANGED", "发签响应缺少 url（平台形态变更待观察）", retryable=False,
            )
        ref = _staged_ref(resource_id, meta.get("file_name"))
        try:
            actual_size, digest = download(
                url, Path(self._download_dir) / ref,
                max_bytes=self._max_download_bytes, chunk_size=_DOWNLOAD_CHUNK_BYTES,
            )
        except DownloadTooLarge:
            return _denial(
                "permission_denied",
                f"超出解析护栏（PD-1）：下载中途超过上限 {self._max_download_bytes} 字节"
                "（元数据 size 与实际不符），已中止并清理临时件",
            )
        except DownloadError as exc:
            status = exc.status_code
            if status in (401, 419):
                raise AdapterError(
                    "AUTH_EXPIRED", f"下载通道 HTTP {status}（登录态失效）", retryable=False,
                    details={"action": _login_action(), "http_status": status},
                ) from exc
            if status == 403:
                return _denial("permission_denied", "媒体下载被拒绝（HTTP 403）——资源不可获取")
            if status in (404, 410):
                return {"status": "gone", "handle": None, "denial": None}
            if status == 429:
                raise AdapterError(
                    "RATE_LIMITED", "下载通道 HTTP 429（限流）", retryable=True,
                    details={"http_status": status},
                ) from exc
            raise AdapterError(
                "NETWORK_ERROR", f"下载通道 HTTP {status}", retryable=True,
                details={"http_status": status},
            ) from exc
        except Exception as exc:  # noqa: BLE001 —— 传输层异常（超时/断连）→ NETWORK_ERROR
            raise AdapterError(
                "NETWORK_ERROR", f"下载传输失败：{type(exc).__name__}", retryable=True,
            ) from exc
        dt = self._now()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        expires_at = (dt + timedelta(hours=_STAGING_TTL_HOURS)).astimezone(timezone.utc)
        return {
            "status": "ok",
            "handle": {
                "kind": "local_file",
                "ref": ref,
                "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
                "sha256": f"sha256:{digest}",
                "size": actual_size,
            },
            "denial": None,
        }
