"""适配器接入与 sync-result 消费（B3）。

B ↔ A 边界（A3 §8"待 B"落地）：

- 触发：``sync_course(connection, request)`` 两个纯 dict，**不含任何凭据**（凭据永不进入本系统）；
  - ``connection``: ``{"platform": "xzzd", "connection_id": "conn_…"}``
  - ``request``: ``{"course_id": "crs_…", "since_cursor": "…"|None, "full": bool}``
- 返回：SyncResult 信封（contracts/schemas/sync-result.schema.json）；
- 整体失败（登录失效/网络/限流/形态变化）由适配器抛 AdapterError；单项失败放信封 errors[]（隔离）；
- 注册：进程内 ``register_sync_adapter``（B 原生可调用面），或环境变量 ``STUDY_SYNC_ADAPTERS`` 指向
  ``"module:attr"``。attr 为普通函数时按 B 原生可调用面使用；为类/对象且提供 ``sync_course`` 方法时，
  按 A3 五操作面协议桥接（实例持连接上下文，构造参数 ``platform``/``connection_id``），
  其 ``AdapterError``（``.error`` 字典）自动转译。

消费语义（A3 §3.4 / A4）：

- upsert 主键 ``(platform, external_id)`` 经 source_refs 解析；同指纹仅刷新 observed_at，
  指纹变化（content_hash 优先、无 hash 时看 source_updated_at）由 B 递增 version 并落新 ResourceVersion；
- deletions 仅 ``reason=explicit_delete_signal`` 产生软归档（archived_at），其余一律忽略；
- 单项失败隔离（SAVEPOINT），有效条目照常提交；
- 游标按 (connection_id, course_id) 持久化，任何消费方不解析其结构（A4 §3）。
"""
from __future__ import annotations

import importlib
import inspect
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .models import (
    OWNER_ASSIGNMENT,
    OWNER_COURSE,
    OWNER_RECORDING,
    OWNER_RESOURCE,
    Assignment,
    Connection,
    Course,
    Recording,
    Resource,
    ResourceVersion,
    SourceRef,
    SyncCursor,
    utc_now,
)
from .platforms import entry_url_for
from .repo import find_source_owner, new_id, normalize_dt

log = logging.getLogger("study.sync")


# ────────────────────────── 错误与恢复动作 ──────────────────────────


class AdapterError(Exception):
    """适配器整体失败（AUTH_EXPIRED / NETWORK_ERROR / RATE_LIMITED / UPSTREAM_CHANGED…）。

    单项失败请走信封 errors[]；这里表达"整门课程/整次调用级别"的失败。
    ``action`` 为可选恢复动作（A3 §3.1），仅在 sanitize 通过后透传。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = True,
        details: dict | None = None,
        action: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details
        self.action = sanitize_recovery_action(action)


class AdapterMissing(Exception):
    """适配器未接入或加载失败（任务转 DEPENDENCY_UNAVAILABLE，不静默降级）。"""

    def __init__(self, platform: str, reason: str | None = None):
        self.platform = platform
        self.reason = reason
        super().__init__(reason or f"平台适配器未接入：{platform}")


class SyncResultInvalid(Exception):
    """信封整体不合法（违反契约形状 / 与任务参数不匹配）。"""


class _BadItem(Exception):
    """单条目形状不合法（计入 errors[]，不中断其他条目）。"""


def sanitize_recovery_action(action: Any) -> dict | None:
    """恢复动作只允许 user_login + https 引导链接；不接受任意 URL 或本机路径。"""
    if not isinstance(action, dict) or action.get("type") != "user_login":
        return None
    entry_url = action.get("entry_url")
    if not isinstance(entry_url, str) or not entry_url.startswith("https://"):
        return None
    message = action.get("message")
    return {
        "type": "user_login",
        "entry_url": entry_url,
        "message": message if isinstance(message, str) and message else "登录已失效，请在连接器窗口完成登录后重试",
    }


def recovery_action_for(platform: str) -> dict | None:
    entry_url = entry_url_for(platform)
    if entry_url is None:
        return None
    return {"type": "user_login", "entry_url": entry_url, "message": "登录已失效，请在连接器窗口完成登录后重试"}


# ────────────────────────── 适配器解析 ──────────────────────────

_REGISTRY: dict[str, Callable[[dict, dict], dict]] = {}


def register_sync_adapter(platform: str, fn: Callable[[dict, dict], dict]) -> None:
    """进程内注册平台适配器（B 原生可调用面；本地联调与测试）。"""
    _REGISTRY[platform] = fn


def _translate_protocol_error(exc: Exception) -> AdapterError | None:
    """A3 五操作面 AdapterError（``.error`` 字典）→ B 侧 AdapterError；非适配器错误返回 None。"""
    error = getattr(exc, "error", None)
    if not isinstance(error, dict) or not isinstance(error.get("code"), str):
        return None
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    return AdapterError(
        error["code"],
        str(error.get("message") or error["code"]),
        retryable=bool(error.get("retryable", True)),
        details=details,
        action=details.get("action") if isinstance(details, dict) else None,
    )


def _bridge_protocol_adapter(factory: Any) -> Callable[[dict, dict], dict]:
    """把 A3 协议适配器（类/实例，构造参数持连接上下文）桥接为 B 原生可调用面。"""

    def call(connection: dict, request: dict) -> dict:
        try:
            adapter = factory(platform=connection["platform"], connection_id=connection["connection_id"])
        except Exception as exc:  # noqa: BLE001 —— 实例化失败=接入问题，不静默降级
            raise AdapterMissing(connection["platform"], f"适配器实例化失败：{type(exc).__name__}: {exc}") from None
        try:
            return adapter.sync_course(
                request["course_id"],
                since_cursor=request.get("since_cursor"),
                kinds=None,
            )
        except Exception as exc:  # noqa: BLE001 —— 仅转译协议错误，其余原样上抛
            translated = _translate_protocol_error(exc)
            if translated is not None:
                raise translated from None
            raise

    call.protocol_factory = factory
    return call


def resolve_sync_adapter(platform: str) -> Callable[[dict, dict], dict]:
    fn = _REGISTRY.get(platform)
    if fn is not None:
        return fn
    spec = config.SYNC_ADAPTERS.get(platform)
    if spec:
        module_name, _, attr = spec.partition(":")
        attr = attr or "sync_course"
        try:
            entry = getattr(importlib.import_module(module_name), attr)
        except Exception as exc:  # noqa: BLE001 —— 转成任务可读错误
            raise AdapterMissing(platform, f"适配器加载失败：{spec}（{type(exc).__name__}: {exc}）") from None
        if inspect.isclass(entry) or (not inspect.isfunction(entry) and hasattr(entry, "sync_course")):
            return _bridge_protocol_adapter(entry)
        return entry
    raise AdapterMissing(platform)


# ────────────────────────── 工具 ──────────────────────────


def as_datetime(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return normalize_dt(value)


def _dt_eq(a: datetime | None, b: datetime | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return normalize_dt(a) == normalize_dt(b)


# 会话/凭据类键名（B18 边界兜底）：适配器回传的 details 等自由字典落库/回显前递归剔除整键。
# 键名命中即丢弃（不留占位符）——值是会话物料的唯一载体，键在则值会被 API 回显与日志扩散。
_SENSITIVE_KEY_PARTS = ("cookie", "token", "session", "password", "credential", "authorization", "secret")


def scrub_session_material(value: Any) -> Any:
    """递归剔除字典中会话/凭据类键（列表与嵌套结构同样处理；其余类型原样返回）。

    会话只在 A 的连接器进程与本机调度之间存活；本函数是 B 边界最后一道防线，
    适配器（A 侧）本应已脱敏，此处不依赖其正确性。
    """
    if isinstance(value, dict):
        return {
            str(key): scrub_session_material(item)
            for key, item in value.items()
            if not any(part in str(key).lower() for part in _SENSITIVE_KEY_PARTS)
        }
    if isinstance(value, list):
        return [scrub_session_material(item) for item in value]
    return value


def _error_info(entry: Any) -> dict[str, Any]:
    """信封 errors[] → Job.errors 元素（ErrorInfo，剥离 request_id 等信封专有字段）。"""
    if not isinstance(entry, dict):
        return {"code": "UPSTREAM_ERROR", "message": "适配器返回了非结构化错误", "retryable": False}
    info: dict[str, Any] = {
        "code": str(entry.get("code") or "UPSTREAM_ERROR"),
        "message": str(entry.get("message") or "适配器单项失败"),
        "retryable": bool(entry.get("retryable", False)),
    }
    if entry.get("details") is not None:
        info["details"] = scrub_session_material(entry["details"])
    return info


def adapter_error_entry(exc: AdapterError, course_id: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"code": exc.code, "message": exc.message, "retryable": exc.retryable}
    details: dict[str, Any] = dict(scrub_session_material(dict(exc.details or {})))
    details.setdefault("course_id", course_id)
    if exc.action is not None:
        details["action"] = exc.action
    entry["details"] = details
    return entry


# ────────────────────────── 连接状态 ──────────────────────────


def mark_auth_required(connection: Connection, *, action: dict | None, message: str) -> dict[str, Any]:
    """登录失效：连接置 auth_required + recovery_action，不重试、等待用户（A4 §5）。"""
    recovery = sanitize_recovery_action(action) or recovery_action_for(connection.platform)
    entry: dict[str, Any] = {"code": "AUTH_EXPIRED", "message": message or "登录已失效，需要用户重新登录", "retryable": False}
    details: dict[str, Any] = {"platform": connection.platform}
    if recovery is not None:
        details["action"] = recovery
    entry["details"] = details
    connection.status = "auth_required"
    connection.recovery_action = recovery
    connection.last_error = entry
    connection.last_checked_at = utc_now()
    connection.updated_at = utc_now()
    return entry


def mark_network_error(connection: Connection, message: str) -> None:
    connection.status = "network_error"
    connection.last_error = {
        "code": "NETWORK_ERROR",
        "message": message or "网络失败",
        "retryable": True,
        "details": {"platform": connection.platform},
    }
    connection.last_checked_at = utc_now()
    connection.updated_at = utc_now()


def mark_connection_ok(connection: Connection) -> None:
    connection.status = "ok"
    connection.last_error = None
    connection.recovery_action = None
    connection.last_sync_at = utc_now()
    connection.updated_at = utc_now()


# ───────────────────────── 连接检查（CR001 降级口径） ─────────────────────────
# A12 裁定：本期无平台回跳，连接状态由检查结果驱动（POST /connections/{id}/check），
# 检查面契约为 A 的 ConnectionState（contracts/schemas/connection-state.schema.json）；不作假 ok。


class CheckResultInvalid(Exception):
    """检查结果形状不合法（不改连接状态；调用方转 500 INTERNAL_ERROR）。"""


_CHECK_STATES = ("ok", "auth_required", "network_error", "blocked", "unsupported", "service_unavailable")

# Codes that describe the *request*, not the connection: the caller asked for a filter or a
# shape this platform version does not accept. Never downgrade the connection for these.
_CALLER_PARAMETER_CODES = {"VALIDATION_FAILED", "INVALID_INPUT", "BAD_REQUEST", "INVALID_ARGUMENT"}

_STATE_ERRORS: dict[str, tuple[str, bool, str]] = {
    "auth_required": ("AUTH_EXPIRED", False, "登录已失效，需要用户重新登录"),
    "network_error": ("NETWORK_ERROR", True, "网络失败"),
    "blocked": ("PERMISSION_DENIED", False, "平台拒绝了本次访问"),
    "unsupported": ("NOT_IMPLEMENTED", False, "该平台能力尚未接入"),
    "service_unavailable": ("DEPENDENCY_UNAVAILABLE", False, "本地连接器服务尚未启动"),
}

_CHECK_REGISTRY: dict[str, Callable[[dict], dict]] = {}


def register_check_adapter(platform: str, fn: Callable[[dict], dict]) -> None:
    """进程内注册检查适配器（B 原生可调用面：connection dict → ConnectionState dict）。"""
    _CHECK_REGISTRY[platform] = fn


def _bridge_check_adapter(factory: Any) -> Callable[[dict], dict]:
    """把 A3 协议适配器（类/实例，构造参数持连接上下文）桥接为检查面。"""

    def call(connection: dict) -> dict:
        try:
            adapter = factory(platform=connection["platform"], connection_id=connection["connection_id"])
        except Exception as exc:  # noqa: BLE001 —— 实例化失败=接入问题，不静默降级
            raise AdapterMissing(connection["platform"], f"适配器实例化失败：{type(exc).__name__}: {exc}") from None
        try:
            return adapter.check_connection()
        except Exception as exc:  # noqa: BLE001 —— 仅转译协议错误，其余原样上抛
            translated = _translate_protocol_error(exc)
            if translated is not None:
                raise translated from None
            raise

    return call


def resolve_check_adapter(platform: str) -> Callable[[dict], dict]:
    """解析检查适配器：进程内注册优先；否则按 SYNC_ADAPTERS 的模块加载。

    spec 指名入口为类/实例（含 check_connection）时桥接；指名入口不可作检查面用时
    退回模块级 check_connection（普通函数按 B 原生可调用面直接使用）。
    """
    fn = _CHECK_REGISTRY.get(platform)
    if fn is not None:
        return fn
    spec = config.SYNC_ADAPTERS.get(platform)
    if spec:
        module_name, _, attr = spec.partition(":")
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 —— 转成任务可读错误
            raise AdapterMissing(platform, f"检查适配器加载失败：{spec}（{type(exc).__name__}: {exc}）") from None
        entry = getattr(module, attr, None) if attr else None
        if inspect.isclass(entry) or (not inspect.isfunction(entry) and hasattr(entry, "check_connection")):
            return _bridge_check_adapter(entry)
        entry = getattr(module, "check_connection", None)
        if entry is None:
            raise AdapterMissing(platform, f"模块未提供检查适配器：{spec}")
        if inspect.isclass(entry) or (not inspect.isfunction(entry) and hasattr(entry, "check_connection")):
            return _bridge_check_adapter(entry)
        return entry
    raise AdapterMissing(platform)


def apply_connection_state(connection: Connection, result: Any) -> None:
    """把适配器 ConnectionState 应用到连接行；形状不合法抛 CheckResultInvalid（不改状态）。"""
    if not isinstance(result, dict):
        raise CheckResultInvalid("检查结果不是 JSON 对象")
    state = result.get("state")
    if state not in _CHECK_STATES:
        raise CheckResultInvalid(f"未知连接状态：{state!r}")
    if result.get("platform") != connection.platform:
        raise CheckResultInvalid(f"检查结果平台不匹配：{result.get('platform')!r} != {connection.platform!r}")
    checked_raw = result.get("checked_at")
    if not isinstance(checked_raw, str):
        raise CheckResultInvalid(f"checked_at 不合法：{checked_raw!r}")
    try:
        checked_at = as_datetime(checked_raw)
    except (TypeError, ValueError) as exc:
        raise CheckResultInvalid(f"checked_at 不可解析：{checked_raw!r}") from exc

    detail = result.get("detail")
    if state == "ok":
        connection.status = "ok"
        connection.last_error = None
        connection.recovery_action = None
    else:
        code, retryable, fallback = _STATE_ERRORS[state]
        entry: dict[str, Any] = {
            "code": code,
            "message": detail if isinstance(detail, str) and detail else fallback,
            "retryable": retryable,
            "details": {"platform": connection.platform},
        }
        if state == "auth_required":
            recovery = sanitize_recovery_action(result.get("action")) or recovery_action_for(connection.platform)
            connection.recovery_action = recovery
            if recovery is not None:
                entry["details"]["action"] = recovery
        else:
            connection.recovery_action = None
        connection.status = state
        connection.last_error = entry
    connection.last_checked_at = checked_at
    connection.updated_at = utc_now()


def record_check_failure(connection: Connection, exc: AdapterError) -> None:
    """检查调用整体失败（适配器抛 AdapterError）：按码标记连接，不抛出。

    A caller-parameter rejection says nothing about the connection itself: an agent asking
    for a filter the platform does not implement used to flip the whole connection to
    `blocked`, which then hid the platform from later turns. Those codes only record the
    error and leave the connection state untouched.
    """
    if exc.code in _CALLER_PARAMETER_CODES:
        connection.last_error = {
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
            "details": {"platform": connection.platform, **(scrub_session_material(exc.details) or {})},
        }
        connection.updated_at = utc_now()
        return
    if exc.code == "AUTH_EXPIRED":
        mark_auth_required(connection, action=exc.action, message=exc.message)
        return
    connection.status = (
        "unsupported" if exc.code == "NOT_IMPLEMENTED" else
        "service_unavailable" if exc.code == "DEPENDENCY_UNAVAILABLE" else
        "network_error" if exc.retryable else "blocked"
    )
    connection.last_error = {
        "code": exc.code,
        "message": exc.message,
        "retryable": exc.retryable,
        "details": {"platform": connection.platform, **(scrub_session_material(exc.details) or {})},
    }
    connection.recovery_action = None
    connection.last_checked_at = utc_now()
    connection.updated_at = utc_now()


# ────────────────────────── 课程与游标 ──────────────────────────


def bound_courses(db: Session, platform: str) -> list[str]:
    """已绑定该平台来源的课程 ID（同步任务的作用域）。"""
    stmt = (
        select(Course.id)
        .join(SourceRef, SourceRef.owner_id == Course.id)
        .where(SourceRef.owner_type == OWNER_COURSE, SourceRef.platform == platform)
        .distinct()
        .order_by(Course.id)
    )
    return list(db.execute(stmt).scalars())


def get_cursor(db: Session, connection_id: str, course_id: str) -> str | None:
    row = db.get(SyncCursor, (connection_id, course_id))
    return row.cursor if row is not None else None


def store_cursor(db: Session, *, connection_id: str, course_id: str, cursor: str | None, mode: str) -> None:
    if not isinstance(cursor, str) or not cursor:
        return  # next=null：无可持久化游标，保留旧值
    row = db.get(SyncCursor, (connection_id, course_id))
    if row is None:
        db.add(SyncCursor(connection_id=connection_id, course_id=course_id, cursor=cursor, mode=mode, updated_at=utc_now()))
    else:
        row.cursor = cursor
        row.mode = mode
        row.updated_at = utc_now()


# ────────────────────────── 信封校验与落库 ──────────────────────────

_REQUIRED_KEYS = (
    "platform",
    "connection_id",
    "course_id",
    "mode",
    "cursor",
    "upserts",
    "deletions",
    "errors",
    "stats",
    "observed_at",
)


def _validate_envelope(envelope: Any, *, platform: str, connection_id: str, course_id: str) -> None:
    if not isinstance(envelope, dict):
        raise SyncResultInvalid("同步信封不是 JSON 对象")
    missing = [k for k in _REQUIRED_KEYS if k not in envelope]
    if missing:
        raise SyncResultInvalid(f"同步信封缺少字段：{', '.join(missing)}")
    if envelope["platform"] != platform:
        raise SyncResultInvalid(f"信封平台不匹配：{envelope['platform']!r} != {platform!r}")
    if envelope["connection_id"] != connection_id:
        raise SyncResultInvalid("信封 connection_id 与任务不匹配")
    if envelope["course_id"] != course_id:
        raise SyncResultInvalid("信封 course_id 与任务不匹配")
    if envelope["mode"] not in ("incremental", "resync"):
        raise SyncResultInvalid(f"未知同步模式：{envelope['mode']!r}")
    upserts = envelope["upserts"]
    if not isinstance(upserts, dict) or any(
        not isinstance(upserts.get(k), list) for k in ("resources", "assignments", "recordings")
    ):
        raise SyncResultInvalid("信封 upserts 形状不合法")
    if not isinstance(envelope["deletions"], list) or not isinstance(envelope["errors"], list):
        raise SyncResultInvalid("信封 deletions/errors 形状不合法")
    if not isinstance(envelope["cursor"], dict):
        raise SyncResultInvalid("信封 cursor 形状不合法")


@dataclass
class AppliedStats:
    """单课程信封的落库结果（errors/refs 形状与 Job.errors/result_refs 对齐）。"""

    upserted: int = 0
    deleted: int = 0
    errors: list[dict] = field(default_factory=list)
    refs: list[dict] = field(default_factory=list)


def _fingerprint_changed(resource: Resource, new_hash: str | None, new_updated: datetime | None) -> bool:
    """A4 §2：content_hash 优先，不可得时看 source_updated_at；都不可比则不判变化。"""
    if new_hash is not None:
        return new_hash != resource.content_hash
    if new_updated is not None and resource.source_updated_at is not None:
        return normalize_dt(new_updated) != normalize_dt(resource.source_updated_at)
    return False


def _touch_ref(db: Session, *, owner_type: str, owner_id: str, platform: str, ref: dict, observed_at: datetime) -> None:
    ref_row = db.execute(
        select(SourceRef).where(
            SourceRef.owner_type == owner_type,
            SourceRef.owner_id == owner_id,
            SourceRef.platform == platform,
            SourceRef.external_id == ref["external_id"],
        )
    ).scalars().first()
    if ref_row is None:
        db.add(
            SourceRef(
                owner_type=owner_type,
                owner_id=owner_id,
                platform=platform,
                external_id=ref["external_id"],
                canonical_url=ref.get("canonical_url"),
                observed_at=observed_at,
                seq=0,
            )
        )
        return
    ref_row.observed_at = observed_at
    if ref.get("canonical_url"):
        ref_row.canonical_url = ref["canonical_url"]


def _check_ref_platform(ref: dict, platform: str) -> tuple[str, datetime]:
    if not isinstance(ref, dict):
        raise _BadItem("source_ref 缺失或形状不合法")
    external_id = ref.get("external_id")
    if not isinstance(external_id, str) or not external_id:
        raise _BadItem("source_ref.external_id 缺失")
    if ref.get("platform") != platform:
        raise _BadItem(f"source_ref.platform 与连接不一致：{ref.get('platform')!r} != {platform!r}")
    if "observed_at" not in ref:
        raise _BadItem("source_ref.observed_at 缺失")
    return external_id, as_datetime(ref["observed_at"])


def _upsert_resource(db: Session, *, platform: str, course_id: str, item: dict) -> str:
    if not isinstance(item, dict):
        raise _BadItem("资源条目不是对象")
    external_id, observed_at = _check_ref_platform(item.get("source_ref"), platform)
    title = item.get("title")
    kind = item.get("kind")
    if not isinstance(title, str) or not title or not isinstance(kind, str) or not kind:
        raise _BadItem("资源条目缺少 kind/title")
    new_hash = item.get("content_hash")
    new_updated = as_datetime(item["source_updated_at"]) if item.get("source_updated_at") else None
    rid = find_source_owner(db, OWNER_RESOURCE, platform, external_id)
    if rid is None:
        resource = Resource(
            id=new_id(db, Resource, "res"),
            course_id=course_id,
            kind=kind,
            title=title,
            version=int(item.get("version") or 1),
            content_hash=new_hash,
            parse_status="pending",
            mime_type=item.get("mime_type"),
            size_bytes=item.get("size_bytes"),
            access_level=item.get("access_level") or "full",
            file_id=None,
            extent_value=item.get("extent_value"),
            extent_unit=item.get("extent_unit"),
            fetched_at=None,
            source_updated_at=new_updated,
        )
        db.add(resource)
        db.add(
            ResourceVersion(
                resource_id=resource.id,
                version=resource.version,
                content_hash=new_hash,
                mime_type=resource.mime_type,
                size_bytes=resource.size_bytes,
                file_id=None,
                extent_value=resource.extent_value,
                extent_unit=resource.extent_unit,
                fetched_at=None,
                parse_status="pending",
                source_updated_at=new_updated,
            )
        )
        db.add(
            SourceRef(
                owner_type=OWNER_RESOURCE,
                owner_id=resource.id,
                platform=platform,
                external_id=external_id,
                canonical_url=(item.get("source_ref") or {}).get("canonical_url"),
                observed_at=observed_at,
                seq=0,
            )
        )
        return resource.id

    resource = db.get(Resource, rid)
    if _fingerprint_changed(resource, new_hash, new_updated):
        resource.version += 1
        resource.parse_status = "pending"
        resource.file_id = None
        resource.fetched_at = None
        resource.extent_value = None
        resource.extent_unit = None
        db.add(
            ResourceVersion(
                resource_id=resource.id,
                version=resource.version,
                content_hash=new_hash,
                mime_type=item.get("mime_type", resource.mime_type),
                size_bytes=item.get("size_bytes", resource.size_bytes),
                file_id=None,
                extent_value=item.get("extent_value", resource.extent_value),
                extent_unit=item.get("extent_unit", resource.extent_unit),
                fetched_at=None,
                parse_status="pending",
                source_updated_at=new_updated,
            )
        )
    if new_hash is not None:
        resource.content_hash = new_hash
    if "kind" in item:
        resource.kind = item["kind"]
    if "title" in item:
        resource.title = item["title"]
    if item.get("mime_type") is not None:
        resource.mime_type = item["mime_type"]
    if item.get("size_bytes") is not None:
        resource.size_bytes = item["size_bytes"]
    if item.get("access_level"):
        resource.access_level = item["access_level"]
    if item.get("extent_value") is not None:
        resource.extent_value = item["extent_value"]
    if item.get("extent_unit") is not None:
        resource.extent_unit = item["extent_unit"]
    if "source_updated_at" in item:
        resource.source_updated_at = new_updated
    resource.archived_at = None  # 重新观测到即恢复
    resource.updated_at = utc_now()
    _touch_ref(db, owner_type=OWNER_RESOURCE, owner_id=resource.id, platform=platform, ref=item["source_ref"], observed_at=observed_at)
    return resource.id


def _upsert_assignment(db: Session, *, platform: str, course_id: str, item: dict) -> str:
    if not isinstance(item, dict):
        raise _BadItem("作业条目不是对象")
    external_id, observed_at = _check_ref_platform(item.get("source_ref"), platform)
    title = item.get("title")
    if not isinstance(title, str) or not title:
        raise _BadItem("作业条目缺少 title")
    aid = find_source_owner(db, OWNER_ASSIGNMENT, platform, external_id)
    if aid is None:
        assignment = Assignment(
            id=new_id(db, Assignment, "asg"),
            course_id=course_id,
            title=title,
            instructions=item.get("instructions"),
            due_at=as_datetime(item["due_at"]) if item.get("due_at") else None,
            due_precision=item.get("due_precision") or "unknown",
            submission_status=item.get("submission_status") or "unknown",
            fact_source="platform",
            observed_at=observed_at,
        )
        db.add(assignment)
        db.add(
            SourceRef(
                owner_type=OWNER_ASSIGNMENT,
                owner_id=assignment.id,
                platform=platform,
                external_id=external_id,
                canonical_url=(item.get("source_ref") or {}).get("canonical_url"),
                observed_at=observed_at,
                seq=0,
            )
        )
        return assignment.id

    assignment = db.get(Assignment, aid)
    changed = False
    if "title" in item and item["title"] != assignment.title:
        assignment.title = item["title"]
        changed = True
    if "instructions" in item and item["instructions"] != assignment.instructions:
        assignment.instructions = item["instructions"]
        changed = True
    if "due_at" in item:
        new_due = as_datetime(item["due_at"]) if item["due_at"] else None
        if not _dt_eq(new_due, assignment.due_at):
            assignment.due_at = new_due
            changed = True
    if "due_precision" in item and item["due_precision"] != assignment.due_precision:
        assignment.due_precision = item["due_precision"]
        changed = True
    if "submission_status" in item and item["submission_status"] != assignment.submission_status:
        assignment.submission_status = item["submission_status"]
        changed = True
    if changed:
        assignment.revision += 1
        assignment.updated_at = utc_now()
    assignment.observed_at = observed_at
    assignment.archived_at = None
    _touch_ref(db, owner_type=OWNER_ASSIGNMENT, owner_id=assignment.id, platform=platform, ref=item["source_ref"], observed_at=observed_at)
    return assignment.id


def _upsert_recording(db: Session, *, platform: str, course_id: str, item: dict) -> str:
    if not isinstance(item, dict):
        raise _BadItem("回放条目不是对象")
    external_id, observed_at = _check_ref_platform(item.get("source_ref"), platform)
    title = item.get("title")
    if not isinstance(title, str) or not title:
        raise _BadItem("回放条目缺少 title")
    rcid = find_source_owner(db, OWNER_RECORDING, platform, external_id)
    started_at = as_datetime(item["started_at"]) if item.get("started_at") else None
    next_check = as_datetime(item["transcript_next_check_at"]) if item.get("transcript_next_check_at") else None
    languages = item.get("subtitle_languages")
    if languages is None:
        languages = []
    if rcid is None:
        recording = Recording(
            id=new_id(db, Recording, "rec"),
            course_id=course_id,
            title=title,
            started_at=started_at,
            duration_seconds=item.get("duration_seconds"),
            canonical_url=item.get("canonical_url"),
            transcript_status=item.get("transcript_status") or "unknown",
            subtitle_languages=languages,
            transcript_next_check_at=next_check,
            fact_source="platform",
            observed_at=observed_at,
        )
        db.add(recording)
        db.add(
            SourceRef(
                owner_type=OWNER_RECORDING,
                owner_id=recording.id,
                platform=platform,
                external_id=external_id,
                canonical_url=(item.get("source_ref") or {}).get("canonical_url"),
                observed_at=observed_at,
                seq=0,
            )
        )
        return recording.id

    recording = db.get(Recording, rcid)
    if "title" in item:
        recording.title = item["title"]
    if "started_at" in item:
        recording.started_at = started_at
    if "duration_seconds" in item:
        recording.duration_seconds = item["duration_seconds"]
    if "canonical_url" in item:
        recording.canonical_url = item["canonical_url"]
    if "transcript_status" in item:
        recording.transcript_status = item["transcript_status"]
    if "subtitle_languages" in item:
        recording.subtitle_languages = languages
    if "transcript_next_check_at" in item:
        recording.transcript_next_check_at = next_check
    recording.observed_at = observed_at
    recording.updated_at = utc_now()
    _touch_ref(db, owner_type=OWNER_RECORDING, owner_id=recording.id, platform=platform, ref=item["source_ref"], observed_at=observed_at)
    return recording.id


_OWNER_BY_KIND: dict[str, tuple[str, type]] = {
    "resource": (OWNER_RESOURCE, Resource),
    "assignment": (OWNER_ASSIGNMENT, Assignment),
    "recording": (OWNER_RECORDING, Recording),
}


def _apply_deletion(db: Session, *, platform: str, deletion: dict) -> bool:
    """仅显式删除信号是删除事件（契约条款）；软归档保留历史，不物理删除。"""
    if not isinstance(deletion, dict):
        raise _BadItem("删除条目不是对象")
    if deletion.get("reason") != "explicit_delete_signal":
        return False
    target = _OWNER_BY_KIND.get(deletion.get("kind"))
    external_id = deletion.get("external_id")
    if target is None or not isinstance(external_id, str) or not external_id:
        raise _BadItem(f"删除条目形状不合法：kind={deletion.get('kind')!r}")
    owner_type, model = target
    oid = find_source_owner(db, owner_type, platform, external_id)
    if oid is None:
        return False
    obj = db.get(model, oid)
    if obj is None or obj.archived_at is not None:
        return False
    obj.archived_at = utc_now()
    if hasattr(obj, "updated_at"):
        obj.updated_at = utc_now()
    return True


def apply_sync_result(db: Session, *, connection: Connection, course_id: str, envelope: dict) -> AppliedStats:
    """把一次课程同步的信封落库（幂等、单项隔离）；不提交事务，由调用方按进度提交。"""
    _validate_envelope(envelope, platform=connection.platform, connection_id=connection.id, course_id=course_id)
    stats = AppliedStats()

    def isolate(label: str, external_id: Any, fn: Callable[[], None]) -> None:
        try:
            with db.begin_nested():
                fn()
        except _BadItem as exc:
            stats.errors.append(
                {
                    "code": "INVALID_PAYLOAD",
                    "message": str(exc),
                    "retryable": False,
                    "details": {"kind": label, "external_id": external_id},
                }
            )
        except Exception as exc:  # noqa: BLE001 —— 单项落库异常隔离，不抹除其余有效结果
            log.exception("sync 条目落库异常（%s/%s）", label, external_id)
            stats.errors.append(
                {
                    "code": "UPSTREAM_CHANGED",
                    "message": f"条目落库失败：{type(exc).__name__}: {exc}",
                    "retryable": False,
                    "details": {"kind": label, "external_id": external_id},
                }
            )

    for item in envelope["upserts"]["resources"]:
        external_id = item.get("source_ref", {}).get("external_id") if isinstance(item, dict) else None

        def do_resource(item: dict = item) -> None:
            rid = _upsert_resource(db, platform=connection.platform, course_id=course_id, item=item)
            stats.upserted += 1
            stats.refs.append({"type": "resource", "id": rid})

        isolate("resource", external_id, do_resource)

    for item in envelope["upserts"]["assignments"]:
        external_id = item.get("source_ref", {}).get("external_id") if isinstance(item, dict) else None

        def do_assignment(item: dict = item) -> None:
            aid = _upsert_assignment(db, platform=connection.platform, course_id=course_id, item=item)
            stats.upserted += 1
            stats.refs.append({"type": "assignment", "id": aid})

        isolate("assignment", external_id, do_assignment)

    for item in envelope["upserts"]["recordings"]:
        external_id = item.get("source_ref", {}).get("external_id") if isinstance(item, dict) else None

        def do_recording(item: dict = item) -> None:
            rcid = _upsert_recording(db, platform=connection.platform, course_id=course_id, item=item)
            stats.upserted += 1
            stats.refs.append({"type": "recording", "id": rcid})

        isolate("recording", external_id, do_recording)

    for deletion in envelope["deletions"]:
        external_id = deletion.get("external_id") if isinstance(deletion, dict) else None

        def do_deletion(deletion: dict = deletion) -> None:
            if _apply_deletion(db, platform=connection.platform, deletion=deletion):
                stats.deleted += 1

        isolate("deletion", external_id, do_deletion)

    stats.errors.extend(_error_info(e) for e in envelope["errors"])
    return stats
