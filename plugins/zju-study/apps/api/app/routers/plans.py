"""计划（/api/v1/plans…）：预览生成候选、应用/撤销、历史查询。

应用/撤销语义（契约 v0.1）：apply 校验候选 input_revision 与当前输入一致（PLAN_STALE）；
应用后旧生效计划 → superseded（superseded_by=新计划）；revoke 仅撤销安排并恢复被其取代的上一版为生效。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import ApiError
from ..models import Plan, utc_now
from ..plan_engine import active_plan, preview_candidate
from ..plan_input import compute_input_revision
from ..repo import DEFAULT_LIMIT, MAX_LIMIT, decode_int_cursor, encode_cursor, get_or_404
from ..schemas import PreviewPlanRequest

router = APIRouter(tags=["plans"])


@router.post("/plans/preview", status_code=201)
def preview_plan(payload: PreviewPlanRequest | None = None, db: Session = Depends(get_db)) -> dict[str, Any]:
    payload = payload or PreviewPlanRequest()
    plan = preview_candidate(db, payload.horizon_start, payload.horizon_end, payload.respect_locked)
    return {"plan": plan.to_contract(), "current_input_revision": plan.input_revision}


@router.get("/plans")
def list_plans(
    db: Session = Depends(get_db),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    status: str | None = Query(default=None, pattern="^(candidate|active|superseded|revoked)$"),
) -> dict[str, Any]:
    stmt = select(Plan)
    if status is not None:
        stmt = stmt.where(Plan.status == status)
    if cursor is not None:
        stmt = stmt.where(Plan.version < decode_int_cursor(cursor))
    rows = list(db.execute(stmt.order_by(Plan.version.desc()).limit(limit + 1)).scalars())
    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(str(rows[-1].version))
    return {"items": [p.to_contract() for p in rows], "next_cursor": next_cursor}


@router.get("/plans/current")
def get_current_plan(db: Session = Depends(get_db)) -> dict[str, Any]:
    active = active_plan(db)
    # current_input_revision 为当前数据指纹（实时计算）；与 plan.input_revision 不同即表示需重新预览
    return {"plan": active.to_contract() if active is not None else None, "current_input_revision": compute_input_revision(db)}


@router.get("/plans/{plan_id}")
def get_plan(plan_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    plan = get_or_404(db, Plan, plan_id, "计划")
    return plan.to_contract()


@router.post("/plans/{plan_id}/apply")
def apply_plan(plan_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    plan = get_or_404(db, Plan, plan_id, "计划")
    if plan.status != "candidate":
        raise ApiError(
            409, "CONFLICT", "仅候选（candidate）计划可应用", details={"plan_id": plan.id, "status": plan.status}
        )
    current_revision = compute_input_revision(db)
    if plan.input_revision != current_revision:
        raise ApiError(
            409,
            "PLAN_STALE",
            "计划生成后输入已变化，请重新预览生成候选计划",
            details={"plan_id": plan.id, "input_revision": plan.input_revision, "current_input_revision": current_revision},
        )
    previous = active_plan(db)
    previous_plan_id: str | None = None
    if previous is not None:
        if plan.version <= previous.version:
            raise ApiError(
                409,
                "CONFLICT",
                "候选计划版本不高于当前生效计划，拒绝应用（并发保护）",
                details={"candidate_version": plan.version, "active_version": previous.version, "active_plan_id": previous.id},
            )
        previous.status = "superseded"
        previous.superseded_by = plan.id
        previous_plan_id = previous.id
    plan.status = "active"
    plan.applied_at = utc_now()
    db.commit()
    return {"plan": plan.to_contract(), "previous_plan_id": previous_plan_id}


@router.post("/plans/{plan_id}/revoke")
def revoke_plan(plan_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    plan = get_or_404(db, Plan, plan_id, "计划")
    if plan.status != "active":
        raise ApiError(
            409, "CONFLICT", "仅生效（active）计划可撤销", details={"plan_id": plan.id, "status": plan.status}
        )
    plan.status = "revoked"
    plan.revoked_at = utc_now()
    restored = (
        db.execute(select(Plan).where(Plan.superseded_by == plan.id).order_by(Plan.applied_at.desc(), Plan.id))
        .scalars()
        .first()
    )
    restored_plan_id: str | None = None
    if restored is not None:
        restored.status = "active"
        restored.superseded_by = None
        restored_plan_id = restored.id
    db.commit()
    return {"plan": plan.to_contract(), "restored_plan_id": restored_plan_id}
