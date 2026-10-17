"""作业（/api/v1/assignments…）：列表/详情 + 手动创建 + 用户修正（If-Match）。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import ApiError
from ..models import OWNER_ASSIGNMENT, Assignment, iso_utc, utc_now
from ..repo import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    check_revision,
    get_or_404,
    new_id,
    normalize_dt,
    page,
    refs_for,
    validate_due,
)
from ..schemas import CreateAssignmentRequest, PatchAssignmentRequest
from .courses import ensure_course

router = APIRouter(tags=["assignments"])

_NOT_NULLABLE = ("title", "due_precision", "submission_status")


def _due_or_reject(due_at_input: datetime | None, due_precision_input: str | None) -> tuple[datetime | None, str]:
    """契约口径：due_at 有值必须显式提供 precision；纯缺省按 unknown。"""
    due_at = normalize_dt(due_at_input) if due_at_input is not None else None
    if due_at is not None and due_precision_input is None:
        raise ApiError(
            400,
            "VALIDATION_FAILED",
            "提供 due_at 时必须显式给出 due_precision（不猜测截止精度）",
            details={"due_at": iso_utc(due_at)},
        )
    due_precision = due_precision_input or "unknown"
    validate_due(due_at, due_precision)
    return due_at, due_precision


@router.get("/assignments")
def list_assignments(
    db: Session = Depends(get_db),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    due_before: datetime | None = Query(default=None),
    due_after: datetime | None = Query(default=None),
    include_archived: bool = Query(default=False),
) -> dict[str, Any]:
    stmt = select(Assignment)
    if not include_archived:
        stmt = stmt.where(Assignment.archived_at.is_(None))
    if due_before is not None:
        dt = due_before if due_before.tzinfo else due_before.replace(tzinfo=timezone.utc)
        stmt = stmt.where(Assignment.due_at.is_not(None), Assignment.due_at <= dt.astimezone(timezone.utc))
    if due_after is not None:
        dt = due_after if due_after.tzinfo else due_after.replace(tzinfo=timezone.utc)
        stmt = stmt.where(Assignment.due_at.is_not(None), Assignment.due_at >= dt.astimezone(timezone.utc))
    rows, next_cursor = page(db, stmt, Assignment, cursor, limit)
    return {"items": [a.to_contract(refs_for(db, OWNER_ASSIGNMENT, a.id)) for a in rows], "next_cursor": next_cursor}


@router.post("/assignments", status_code=201)
def create_assignment(payload: CreateAssignmentRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    ensure_course(db, payload.course_id)
    due_at, due_precision = _due_or_reject(payload.due_at, payload.due_precision)
    assignment = Assignment(
        id=new_id(db, Assignment, "asg"),
        course_id=payload.course_id,
        title=payload.title,
        instructions=payload.instructions,
        due_at=due_at,
        due_precision=due_precision,
        fact_source="user",
        revision=1,
    )
    db.add(assignment)
    db.commit()
    return assignment.to_contract([])


@router.get("/assignments/{assignment_id}")
def get_assignment(assignment_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    assignment = get_or_404(db, Assignment, assignment_id, "作业")
    return assignment.to_contract(refs_for(db, OWNER_ASSIGNMENT, assignment.id))


@router.patch("/assignments/{assignment_id}")
def update_assignment(
    assignment_id: str,
    payload: PatchAssignmentRequest,
    db: Session = Depends(get_db),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, Any]:
    """部分更新：缺省=不修改；显式 null 仅可清空可空字段（instructions/due_at）。"""
    assignment = get_or_404(db, Assignment, assignment_id, "作业")
    check_revision(if_match, assignment.revision, target=assignment.id)
    fields = payload.model_fields_set

    for name in _NOT_NULLABLE:
        if name in fields and getattr(payload, name) is None:
            raise ApiError(400, "VALIDATION_FAILED", f"{name} 不可为 null", details={"field": name})

    new_due_at = assignment.due_at
    new_due_precision = assignment.due_precision
    if "due_at" in fields:
        if payload.due_at is None:
            new_due_at = None
            if "due_precision" not in fields:
                new_due_precision = "unknown"  # 显式清空即无截止（不猜测）
        else:
            if "due_precision" not in fields:
                raise ApiError(
                    400,
                    "VALIDATION_FAILED",
                    "提供 due_at 时必须显式给出 due_precision（不猜测截止精度）",
                    details={"due_at": iso_utc(normalize_dt(payload.due_at))},
                )
            new_due_at = normalize_dt(payload.due_at)
    if "due_precision" in fields:
        new_due_precision = payload.due_precision
    validate_due(new_due_at, new_due_precision)

    if "title" in fields:
        assignment.title = payload.title
    if "instructions" in fields:
        assignment.instructions = payload.instructions
    assignment.due_at = new_due_at
    assignment.due_precision = new_due_precision
    if "submission_status" in fields:
        assignment.submission_status = payload.submission_status
    if fields:
        assignment.fact_source = "user"
        assignment.revision += 1
        assignment.updated_at = utc_now()
    db.commit()
    return assignment.to_contract(refs_for(db, OWNER_ASSIGNMENT, assignment.id))
