"""健康检查（契约 /api/v1/health，HealthResponse）。"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from ..config import APP_VERSION

router = APIRouter(tags=["health"])


@router.get("/health")
def get_health() -> dict:
    return {
        "status": "ok",
        "version": APP_VERSION,
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
