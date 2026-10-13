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
from ..models import Plan
from ..plan_engine import active_plan, preview_candidate
from ..plan_input import compute_input_revision
from ..plan_mutations import apply_plan as mutate_apply_plan, revoke_plan as mutate_revoke_plan
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
    return mutate_apply_plan(db, plan_id)


@router.post("/plans/{plan_id}/revoke")
def revoke_plan(plan_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return mutate_revoke_plan(db, plan_id)
