"""偏好与可用时间（/api/v1/preferences…）。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Availability, iso_utc, utc_now
from ..schemas import PutAvailabilityRequest

router = APIRouter(tags=["preferences"])


def _default_view() -> dict[str, Any]:
    """未设置时按契约返回默认值（timezone=Asia/Shanghai，其余为空），不落库。"""
    return {
        "timezone": "Asia/Shanghai",
        "weekly_windows": [],
        "date_exceptions": [],
        "daily_limit_minutes": None,
        "updated_at": iso_utc(utc_now()),
    }


@router.get("/preferences/availability")
def get_availability(db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(Availability, 1)
    if row is None:
        return _default_view()
    return row.to_contract()


@router.put("/preferences/availability")
def put_availability(payload: PutAvailabilityRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """全量替换语义；updated_at 变化使计划输入修订更新（PLAN_STALE 判定依据）。"""
    row = db.get(Availability, 1)
    if row is None:
        row = Availability(id=1)
        db.add(row)
    row.timezone = payload.timezone
    row.weekly_windows = [w.model_dump() for w in payload.weekly_windows]
    row.date_exceptions = [e.model_dump(mode="json") for e in payload.date_exceptions]
    row.daily_limit_minutes = payload.daily_limit_minutes
    row.updated_at = utc_now()
    db.commit()
    return row.to_contract()
