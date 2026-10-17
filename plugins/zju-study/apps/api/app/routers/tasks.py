"""学习任务（/api/v1/tasks…）：列表/详情 + 创建/更新（If-Match）/归档/锁定/反馈（幂等）。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..build_engine import enqueue_build_tasks_job
from ..db import get_db
from ..errors import ApiError
from ..models import OWNER_TASK, LearningTask, Plan, PlanBlock, TaskFeedback, iso_utc, utc_now
from ..plan_input import compute_input_revision
from ..repo import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    check_revision,
    get_or_404,
    idempotency_lookup,
    idempotency_store,
    new_id,
    normalize_dt,
    page,
    refs_for,
    validate_due,
    validate_idempotency_key,
)
from ..schemas import BuildTasksRequest, CreateTaskRequest, FeedbackRequest, LockTaskRequest, PatchTaskRequest
from .courses import ensure_course

router = APIRouter(tags=["tasks"])

STATUSES = ["pending", "in_progress", "completed", "cancelled"]


def task_to_contract(db: Session, task: LearningTask) -> dict[str, Any]:
    return task.to_contract(refs_for(db, OWNER_TASK, task.id))


def _validate_prerequisites(db: Session, ids: list[str]) -> None:
    if len(set(ids)) != len(ids):
        raise ApiError(400, "VALIDATION_FAILED", "前置任务 ID 重复", details={"prerequisite_ids": ids})
    missing = [pid for pid in ids if db.get(LearningTask, pid) is None]
    if missing:
        raise ApiError(400, "VALIDATION_FAILED", "存在不存在的前置任务", details={"missing_prerequisite_ids": missing})


def _active_plan(db: Session) -> Plan | None:
    return db.execute(select(Plan).where(Plan.status == "active").order_by(Plan.id)).scalars().first()


@router.get("/tasks")
def list_tasks(
    db: Session = Depends(get_db),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    course_id: str | None = Query(default=None),
    status: str | None = Query(default=None, pattern="^(" + "|".join(STATUSES) + ")$"),
    due_before: datetime | None = Query(default=None),
    include_archived: bool = Query(default=False),
) -> dict[str, Any]:
    stmt = select(LearningTask)
    if not include_archived:
        stmt = stmt.where(LearningTask.archived_at.is_(None))
    if course_id is not None:
        ensure_course(db, course_id)
        stmt = stmt.where(LearningTask.course_id == course_id)
    if status is not None:
        stmt = stmt.where(LearningTask.status == status)
    if due_before is not None:
        dt = due_before if due_before.tzinfo else due_before.replace(tzinfo=timezone.utc)
        stmt = stmt.where(LearningTask.due_at.is_not(None), LearningTask.due_at <= dt.astimezone(timezone.utc))
    rows, next_cursor = page(db, stmt, LearningTask, cursor, limit)
    return {"items": [task_to_contract(db, t) for t in rows], "next_cursor": next_cursor}


@router.post("/tasks/build", status_code=202)
def trigger_build_tasks(payload: BuildTasksRequest | None = None, db: Session = Depends(get_db)) -> dict[str, Any]:
    """触发任务生成（后台任务，B13）：范围=body.course_id（缺省为全部课程）。

    幂等：重复触发不制造重复任务（worker 按稳定 match_key 对齐既有任务）；
    同范围排队/执行中任务存在时复用（返回既有 job，不重复入队）。
    """
    course_id = payload.course_id if payload is not None else None
    if course_id is not None:
        ensure_course(db, course_id)
    job = enqueue_build_tasks_job(db, course_id=course_id, reason="api")
    db.commit()
    return {"job": job.to_contract()}


@router.get("/tasks/{task_id}")
def get_task(task_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    task = get_or_404(db, LearningTask, task_id, "任务")
    return task_to_contract(db, task)


@router.post("/tasks", status_code=201)
def create_task(payload: CreateTaskRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    if payload.course_id is not None:
        ensure_course(db, payload.course_id)
    _validate_prerequisites(db, payload.prerequisite_ids)
    due_precision = payload.due_precision or ("datetime" if payload.due_at is not None else "unknown")
    validate_due(payload.due_at, due_precision)
    now = utc_now()
    task = LearningTask(
        id=new_id(db, LearningTask, "tsk"),
        course_id=payload.course_id,
        type=payload.type,
        title=payload.title,
        estimated_minutes=payload.estimated_minutes,
        due_at=normalize_dt(payload.due_at) if payload.due_at is not None else None,
        due_precision=due_precision,
        prerequisite_ids=list(payload.prerequisite_ids),
        status="pending",
        fact_source="user",
        match_key=None,
        revision=1,
        created_at=now,
        updated_at=now,
    )
    db.add(task)
    db.commit()
    return task_to_contract(db, task)


@router.patch("/tasks/{task_id}")
def update_task(
    task_id: str,
    payload: PatchTaskRequest,
    db: Session = Depends(get_db),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, Any]:
    task = get_or_404(db, LearningTask, task_id, "任务")
    check_revision(if_match, task.revision, target=task.id)
    fields = payload.model_fields_set

    if "course_id" in fields and payload.course_id is not None:
        ensure_course(db, payload.course_id)
    if "prerequisite_ids" in fields and payload.prerequisite_ids is not None:
        _validate_prerequisites(db, payload.prerequisite_ids)
    if "due_precision" in fields and payload.due_precision is None:
        raise ApiError(400, "VALIDATION_FAILED", "due_precision 不可为 null（清除截止请用 unknown）")

    new_due_at = task.due_at
    new_due_precision = task.due_precision
    if "due_at" in fields:
        new_due_at = normalize_dt(payload.due_at) if payload.due_at is not None else None
        if "due_precision" not in fields:
            new_due_precision = "datetime" if new_due_at is not None else "unknown"
    if "due_precision" in fields:
        new_due_precision = payload.due_precision
    validate_due(new_due_at, new_due_precision)

    if "course_id" in fields:
        task.course_id = payload.course_id
    if "type" in fields and payload.type is not None:
        task.type = payload.type
    if "title" in fields and payload.title is not None:
        task.title = payload.title
    if "estimated_minutes" in fields and payload.estimated_minutes is not None:
        task.estimated_minutes = payload.estimated_minutes
        task.fact_source = "user"
    if "prerequisite_ids" in fields and payload.prerequisite_ids is not None:
        task.prerequisite_ids = list(payload.prerequisite_ids)
    if "status" in fields and payload.status is not None:
        task.status = payload.status
    task.due_at = new_due_at
    task.due_precision = new_due_precision
    task.revision += 1
    task.updated_at = utc_now()
    db.commit()
    return task_to_contract(db, task)


@router.delete("/tasks/{task_id}", status_code=204)
def archive_task(task_id: str, db: Session = Depends(get_db)) -> Response:
    task = get_or_404(db, LearningTask, task_id, "任务")
    if task.archived_at is None:
        task.archived_at = utc_now()
        task.updated_at = task.archived_at
        task.revision += 1
        db.commit()
    return Response(status_code=204)


@router.post("/tasks/{task_id}/lock")
def lock_task(task_id: str, payload: LockTaskRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    task = get_or_404(db, LearningTask, task_id, "任务")
    active = _active_plan(db)
    if active is None:
        return {"task": task_to_contract(db, task), "plan": None}

    stmt = select(PlanBlock).where(PlanBlock.plan_id == active.id, PlanBlock.task_id == task_id)
    blocks = list(db.execute(stmt.order_by(PlanBlock.id)).scalars())
    if payload.starts_at is not None:
        target = normalize_dt(payload.starts_at)
        matched = [b for b in blocks if normalize_dt(b.starts_at) == target]
        if not matched:
            raise ApiError(
                404,
                "NOT_FOUND",
                "生效计划中没有该开始时间的任务块",
                details={"task_id": task_id, "plan_id": active.id, "starts_at": iso_utc(payload.starts_at)},
            )
        blocks = matched
    if not blocks:
        raise ApiError(
            404,
            "NOT_FOUND",
            "任务不在生效计划中",
            details={"task_id": task_id, "plan_id": active.id},
        )
    for block in blocks:
        block.locked = payload.locked
    db.commit()
    return {"task": task_to_contract(db, task), "plan": active.to_contract()}


@router.post("/tasks/{task_id}/feedback", status_code=201)
def submit_feedback(
    task_id: str,
    payload: FeedbackRequest,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    validate_idempotency_key(idempotency_key)
    endpoint = f"POST /tasks/{task_id}/feedback"
    payload_dict = payload.model_dump(mode="json")
    cached = idempotency_lookup(db, idempotency_key, endpoint, payload_dict)
    if cached is not None:
        status_code, body = cached
        return JSONResponse(status_code=status_code, content=body)

    task = get_or_404(db, LearningTask, task_id, "任务")
    if task.archived_at is not None:
        raise ApiError(409, "CONFLICT", "任务已归档，无法提交反馈", details={"task_id": task_id})
    if task.status == "cancelled":
        raise ApiError(409, "CONFLICT", "任务已取消，无法提交反馈", details={"task_id": task_id})

    now = utc_now()
    feedback = TaskFeedback(
        id=new_id(db, TaskFeedback, "fbk"),
        task_id=task_id,
        outcome=payload.outcome,
        actual_minutes=payload.actual_minutes,
        difficulty=payload.difficulty,
        note=payload.note,
        created_at=now,
    )
    db.add(feedback)
    if payload.outcome == "completed" and task.status != "completed":
        task.status = "completed"
        task.revision += 1
        task.updated_at = now
    elif payload.outcome == "partial" and task.status != "in_progress":
        task.status = "in_progress"
        task.revision += 1
        task.updated_at = now

    active = _active_plan(db)
    db.flush()
    body = {
        "feedback": feedback.to_contract(),
        "task": task_to_contract(db, task),
        "current_input_revision": compute_input_revision(db),
        "replan_suggested": payload.outcome in ("completed", "partial") and active is not None,
    }
    idempotency_store(db, idempotency_key, endpoint, payload_dict, 201, body)
    db.commit()
    return JSONResponse(status_code=201, content=body)
