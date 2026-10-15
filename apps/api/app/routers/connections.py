"""平台连接与能力（/api/v1/connections…）。

POST /connections/{id}/sync（B3）：为连接下已绑定来源的课程入队一次同步任务；
实际抓取由 worker 经适配器执行（app/sync_engine.py）。重复触发合并去抖：
已有排队/运行中的同步任务时直接返回该任务。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import ApiError
from ..models import Connection, Course, SourceRef, Job, OWNER_COURSE, utc_now
from ..repo import (
    get_or_404,
    idempotency_lookup,
    idempotency_store,
    new_id,
    validate_idempotency_key,
)
from ..schemas import StartSyncRequest, ImportPlatformCoursesRequest
from .. import connectors_registry
from ..sync_engine import (
    AdapterError,
    AdapterMissing,
    CheckResultInvalid,
    apply_connection_state,
    bound_courses,
    record_check_failure,
    recovery_action_for,
    resolve_check_adapter,
    _translate_protocol_error,
)

router = APIRouter(tags=["connections"])
log = logging.getLogger(__name__)


def _resume_after_connected_check(db: Session, connection: Connection) -> None:
    """Only called after a validated successful check has committed; best effort."""
    if connection.status != "ok":
        return
    try:
        from ..worker import resume_auth_sync_jobs
        resume_auth_sync_jobs(db, connection)
    except Exception:
        db.rollback()
        log.exception("恢复认证暂停的同步任务失败")
    try:
        from ..agent_bridge.runtime import get_runtime
        get_runtime().resume_authenticated_tasks()
    except Exception:
        log.exception("恢复认证暂停的助手任务失败")


def discover_platform_courses(connection_id: str, db: Session, *, cursor: str | None = None,
                              term: str | None = None) -> dict[str, Any]:
    """Read the platform's own course list for the signed-in user (no import, no session data).

    Shared by `GET /connections/{id}/courses` and the agent's `connections.courses` tool so a
    dead session is reported as AUTH_EXPIRED in exactly one place.
    """
    connection = get_or_404(db, Connection, connection_id, "连接")
    if connection.status == "disabled":
        raise ApiError(409, "CONFLICT", "请先启用连接")
    try:
        adapter = connectors_registry.protocol_adapter(connection.platform, connection.id)
        method = getattr(adapter, "list_courses", None)
        if method is None:
            raise ApiError(501, "NOT_IMPLEMENTED", "该平台暂不支持课程列表，请手动挂接课号")
        return method(cursor=cursor, term=term) if term is not None else method(cursor=cursor)
    except AdapterMissing as exc:
        raise ApiError(503, "DEPENDENCY_UNAVAILABLE", str(exc), retryable=False) from None
    except ApiError:
        raise
    except Exception as exc:
        error = _translate_protocol_error(exc)
        if error is None:
            raise
        # 某个操作尚未实现，不代表平台的同步/字幕连接不可用。
        if error.code != "NOT_IMPLEMENTED":
            record_check_failure(connection, error)
            db.commit()
        raise ApiError(409 if error.code == "AUTH_EXPIRED" else 502,
                       error.code, error.message, retryable=error.retryable,
                       details={"platform": connection.platform}) from None


def bind_platform_courses(db: Session, connection_id: str, selected: list[Any]) -> dict[str, Any]:
    """Create/attach local courses for already-discovered platform items.

    Shared by `POST /connections/{id}/courses/import` and the agent's
    `connections.courses.import` tool: same transaction, same identity matching.
    """
    from .courses import course_to_contract
    from ..course_identity import matching_course, enrich_course
    connection = get_or_404(db, Connection, connection_id, "连接")
    items = []
    seen: set[str] = set()
    for selected_item in selected:
        # Platform adapters return their own course records keyed by the platform course id
        # (with a source_ref); already-bound local contracts also carry `external_id`.
        if isinstance(selected_item, dict):
            external_id = selected_item.get("external_id") or selected_item.get("id")
            title = selected_item.get("title")
            term = selected_item.get("term")
            teacher = selected_item.get("teacher")
        else:
            external_id = getattr(selected_item, "external_id", None) or getattr(selected_item, "id", None)
            title = getattr(selected_item, "title", None)
            term = getattr(selected_item, "term", None)
            teacher = getattr(selected_item, "teacher", None)
        if not external_id or not isinstance(title, str) or not title.strip():
            continue
        if external_id in seen:
            continue
        seen.add(external_id)
        ref = db.execute(select(SourceRef).where(
            SourceRef.owner_type == OWNER_COURSE, SourceRef.platform == connection.platform,
            SourceRef.external_id == external_id)).scalars().first()
        course = db.get(Course, ref.owner_id) if ref else None
        if course is None:
            course = matching_course(db, connection.platform, title, term, teacher)
        if course is None:
            course = Course(id=new_id(db, Course, "crs"), title=title.strip(), term=term, teacher=teacher)
            db.add(course)
            db.flush()
        enrich_course(course, term, teacher)
        if ref is None:
            db.add(SourceRef(owner_type=OWNER_COURSE, owner_id=course.id,
                             platform=connection.platform, external_id=external_id,
                             canonical_url=None, observed_at=utc_now(), seq=0))
        db.flush()
        items.append(course_to_contract(db, course))
    db.commit()
    return {"items": items, "next_cursor": None}


@router.post("/connections/{connection_id}/courses/import")
def import_platform_courses(connection_id: str, payload: ImportPlatformCoursesRequest,
                            db: Session = Depends(get_db)) -> dict[str, Any]:
    """一次事务建课并挂接来源；同平台课号重复导入返回已有课程。"""
    return bind_platform_courses(db, connection_id, list(payload.courses))


@router.get("/connections/{connection_id}/courses")
def discover_courses(connection_id: str, cursor: str | None = Query(default=None), term: str | None = Query(default=None),
                     db: Session = Depends(get_db)) -> dict[str, Any]:
    """读取当前登录用户的平台课程；不自动导入，也不返回会话数据。"""
    return discover_platform_courses(connection_id, db, cursor=cursor, term=term)


@router.get("/connections")
def list_connections(db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = list(db.execute(select(Connection).order_by(Connection.id)).scalars())
    return {"items": [c.to_contract() for c in rows], "next_cursor": None}


@router.get("/connections/{connection_id}")
def get_connection(connection_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    connection = get_or_404(db, Connection, connection_id, "连接")
    return connection.to_contract()


@router.get("/connections/{connection_id}/capabilities")
def get_capabilities(connection_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    connection = get_or_404(db, Connection, connection_id, "连接")
    if connection.capabilities is None:
        raise ApiError(
            404,
            "NOT_FOUND",
            "连接能力信息尚未提供（同步/检查连接后由适配器写入）",
            details={"kind": "连接能力", "id": connection_id},
        )
    return connection.capabilities


def enqueue_connection_sync(db: Session, connection_id: str, *, full: bool = False,
                            allowed_course_ids: frozenset[str] | None = None) -> tuple[Job, str]:
    """Enqueue (or reuse) one platform sync job.

    Shared by `POST /connections/{id}/sync` and the agent's `connections.sync` tool so
    both report the school's real state with the same codes: a dead session is
    `AUTH_EXPIRED` (recoverable with one manual login), never an invented success. An
    active sync job is returned instead of a second one.

    Returns (job, "queued" | "reused").
    """
    connection = get_or_404(db, Connection, connection_id, "连接")
    if connection.status == "disabled":
        raise ApiError(409, "CONFLICT", "连接已停用，请先启用后再同步",
                       details={"connection_id": connection_id, "status": connection.status})
    if connection.status in ("unsupported", "service_unavailable", "blocked"):
        error = connection.last_error or {}
        raise ApiError(409, error.get("code", "CONFLICT"),
                       error.get("message", "连接不可用，请先检查连接"), retryable=False)
    if connection.status == "auth_required":
        raise ApiError(409, "AUTH_EXPIRED", "登录已失效，请在连接器窗口完成登录后重试", retryable=False,
                       details={"platform": connection.platform,
                                "action": connection.recovery_action or recovery_action_for(connection.platform)})
    course_ids = bound_courses(db, connection.platform)
    if allowed_course_ids is not None:
        course_ids = [cid for cid in course_ids if cid in allowed_course_ids]
    if not course_ids:
        raise ApiError(409, "CONFLICT", "没有绑定该平台的课程，请先绑定课程来源",
                       details={"connection_id": connection_id, "platform": connection.platform})
    active = _active_sync_job(db, connection_id)
    if active is not None:
        if allowed_course_ids is not None and (set(active.payload.get("course_ids", [])) != set(course_ids)
                                              or bool(active.payload.get("full")) != full):
            raise ApiError(409, "SYNC_SCOPE_CONFLICT", "该平台已有不同范围的同步；等待结束后重试，不扩大本会话权限", retryable=True)
        return active, "reused"
    job = Job(id=new_id(db, Job, "job"), type="sync", status="queued", progress=0.0, errors=[],
              result_refs=[], payload={"connection_id": connection_id, "course_ids": course_ids, "full": full})
    db.add(job)
    db.flush()
    return job, "queued"


@router.post("/connections/{connection_id}/sync", status_code=202)
def start_connection_sync(
    connection_id: str,
    db: Session = Depends(get_db),
    body: StartSyncRequest | None = Body(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    validate_idempotency_key(idempotency_key)
    full = bool(body.full) if body is not None else False
    endpoint = f"POST /connections/{connection_id}/sync"
    fingerprint = {"connection_id": connection_id, "full": full}
    replay = idempotency_lookup(db, idempotency_key, endpoint, fingerprint)
    if replay is not None:
        status_code, payload = replay
        return JSONResponse(status_code=status_code, content=payload)

    job, _reason = enqueue_connection_sync(db, connection_id, full=full)
    payload = {"job": job.to_contract()}
    idempotency_store(db, idempotency_key, endpoint, fingerprint, 202, payload)
    db.commit()
    return payload


@router.post("/connections/{connection_id}/check")
def check_connection_state(connection_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """触发一次连接检查并回读结果（CR001 降级口径；状态由检查结果驱动，不作假 ok）。"""
    connection = get_or_404(db, Connection, connection_id, "连接")
    if connection.status == "disabled":
        raise ApiError(
            409,
            "CONFLICT",
            "连接已停用，请先启用后再检查",
            details={"connection_id": connection_id, "status": connection.status},
        )
    try:
        check = resolve_check_adapter(connection.platform)
    except AdapterMissing as exc:
        record_check_failure(connection, AdapterError("DEPENDENCY_UNAVAILABLE", str(exc), retryable=False))
        db.commit()
        return connection.to_contract()
    context = {"platform": connection.platform, "connection_id": connection.id}
    try:
        result = check(context)
    except AdapterError as exc:
        record_check_failure(connection, exc)
        db.commit()
        return connection.to_contract()
    try:
        apply_connection_state(connection, result)
    except CheckResultInvalid as exc:
        raise ApiError(500, "INTERNAL_ERROR", f"连接检查结果不合法：{exc}") from exc
    db.commit()
    _resume_after_connected_check(db, connection)
    return connection.to_contract()


@router.post("/connections/{connection_id}/disable")
def disable_connection(connection_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """用户主动停用：不再发起同步/抓取，已有数据保留；幂等。"""
    connection = get_or_404(db, Connection, connection_id, "连接")
    if connection.status != "disabled":
        connection.status = "disabled"
        connection.recovery_action = None
        connection.updated_at = utc_now()
        db.commit()
    return connection.to_contract()


@router.post("/connections/{connection_id}/enable")
def enable_connection(connection_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """重新启用并立即执行一次连接检查，status 取检查结果（不自动造 ok）；幂等。

    适配器缺失或检查结果不合法时连接仍须解除停用（否则用户被困在 disabled），
    如实落 network_error + 对应错误（DEPENDENCY_UNAVAILABLE / INTERNAL_ERROR）。
    """
    connection = get_or_404(db, Connection, connection_id, "连接")
    if connection.status != "disabled":
        return connection.to_contract()
    try:
        check = resolve_check_adapter(connection.platform)
    except AdapterMissing as exc:
        connection.status = "network_error"
        connection.last_error = {
            "code": "DEPENDENCY_UNAVAILABLE",
            "message": str(exc),
            "retryable": False,
            "details": {"platform": connection.platform},
        }
        connection.recovery_action = None
        connection.last_checked_at = utc_now()
        connection.updated_at = utc_now()
        db.commit()
        return connection.to_contract()
    context = {"platform": connection.platform, "connection_id": connection.id}
    try:
        result = check(context)
    except AdapterError as exc:
        record_check_failure(connection, exc)
        db.commit()
        return connection.to_contract()
    try:
        apply_connection_state(connection, result)
    except CheckResultInvalid as exc:
        connection.status = "network_error"
        connection.last_error = {
            "code": "INTERNAL_ERROR",
            "message": f"连接检查结果不合法：{exc}",
            "retryable": True,
            "details": {"platform": connection.platform},
        }
        connection.recovery_action = None
        connection.last_checked_at = utc_now()
        connection.updated_at = utc_now()
        db.commit()
        return connection.to_contract()
    db.commit()
    _resume_after_connected_check(db, connection)
    return connection.to_contract()


def _active_sync_job(db: Session, connection_id: str) -> Job | None:
    jobs = db.execute(
        select(Job).where(Job.type == "sync", Job.status.in_(("queued", "running"))).order_by(Job.created_at.asc())
    ).scalars()
    for job in jobs:
        if (job.payload or {}).get("connection_id") == connection_id:
            return job
    return None
