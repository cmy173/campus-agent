"""Plan mutations shared by HTTP routes and atomic agent authorization receipts."""
from __future__ import annotations
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session
from .errors import ApiError
from .models import Plan, utc_now
from .plan_engine import active_plan
from .plan_input import compute_input_revision
from .repo import get_or_404


def apply_plan(db: Session, plan_id: str, *, commit: bool = True) -> dict[str, Any]:
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
    if commit:
        db.commit()
    else:
        db.flush()
    return {"plan": plan.to_contract(), "previous_plan_id": previous_plan_id}


def revoke_plan(db: Session, plan_id: str, *, commit: bool = True) -> dict[str, Any]:
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
    if commit:
        db.commit()
    else:
        db.flush()
    return {"plan": plan.to_contract(), "restored_plan_id": restored_plan_id}
