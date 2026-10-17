"""AI 服务配置端点（CR008）：配置 CRUD、密钥清除、连接测试。

安全红线：密钥仅经此通道写入本地密钥存储（DPAPI），任何读路径只回 has_api_key；
更新语义：PATCH 的 api_key 缺省/null=保留（空串 400 防误清空），清除用 DELETE /key。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .. import secrets_store
from ..db import get_db
from ..errors import ApiError
from ..models import Job, ProviderConfig, utc_now
from ..providers import normalize_base_url, run_connection_test
from ..repo import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor, get_or_404, new_id
from ..schemas import PatchProviderRequest, UpsertProviderRequest

router = APIRouter()

ACTIVE_JOB_STATUSES = ("queued", "running")


def _serialize(cfg: ProviderConfig) -> dict[str, Any]:
    return cfg.to_contract(secrets_store.has_key(cfg.id))


def _ensure_not_in_use(db: Session, provider_id: str) -> None:
    for job in db.execute(select(Job).where(Job.type.in_(["review_generate", "classroom_summary"]), Job.status.in_(ACTIVE_JOB_STATUSES))).scalars():
        if ((job.payload or {}).get("provider_config_id") or (job.payload or {}).get("provider_id")) == provider_id:
            raise ApiError(409, "CONFLICT", "有进行中的 AI 任务使用该配置，请等待结束或先取消任务",
                           details={"job_id": job.id, "provider_id": provider_id})


@router.get("/ai/providers")
def list_providers(
    db: Session = Depends(get_db),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    stmt = select(ProviderConfig)
    if cursor is not None:
        created_text, _, provider_id = decode_cursor(cursor).partition("|")
        try:
            created = datetime.fromisoformat(created_text)
        except ValueError:
            raise ApiError(400, "VALIDATION_FAILED", "无效的分页游标", details={"cursor": cursor}) from None
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        stmt = stmt.where(
            or_(ProviderConfig.created_at < created, and_(ProviderConfig.created_at == created, ProviderConfig.id < provider_id))
        )
    rows = list(
        db.execute(stmt.order_by(ProviderConfig.created_at.desc(), ProviderConfig.id.desc()).limit(limit + 1)).scalars()
    )
    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        created = last.created_at if last.created_at.tzinfo else last.created_at.replace(tzinfo=timezone.utc)
        next_cursor = encode_cursor(f"{created.astimezone(timezone.utc).isoformat()}|{last.id}")
    return {"items": [_serialize(c) for c in rows], "next_cursor": next_cursor}


@router.post("/ai/providers", status_code=201)
def create_provider(req: UpsertProviderRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    cfg = ProviderConfig(
        id=new_id(db, ProviderConfig, "prv"),
        name=req.name,
        protocol=req.protocol,
        base_url=normalize_base_url(req.base_url),
        model=req.model,
        timeout_seconds=req.timeout_seconds,
    )
    db.add(cfg)
    if req.api_key is not None:
        secrets_store.set_key(cfg.id, req.api_key)
    db.commit()
    db.refresh(cfg)
    return _serialize(cfg)


@router.get("/ai/providers/{provider_id}")
def get_provider(provider_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _serialize(get_or_404(db, ProviderConfig, provider_id, "AI 服务配置"))


@router.patch("/ai/providers/{provider_id}")
def update_provider(provider_id: str, req: PatchProviderRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    cfg = get_or_404(db, ProviderConfig, provider_id, "AI 服务配置")
    _ensure_not_in_use(db, provider_id)
    fields = req.model_fields_set
    if req.name is not None and "name" in fields:
        cfg.name = req.name
    if req.protocol is not None and "protocol" in fields:
        cfg.protocol = req.protocol
    if req.base_url is not None and "base_url" in fields:
        cfg.base_url = normalize_base_url(req.base_url)
    if req.model is not None and "model" in fields:
        cfg.model = req.model
    if req.timeout_seconds is not None and "timeout_seconds" in fields:
        cfg.timeout_seconds = req.timeout_seconds
    # api_key 缺省/null=保留现有密钥；空串已被请求体校验拦为 400（清密钥走 DELETE /key）
    if req.api_key is not None and "api_key" in fields:
        secrets_store.set_key(cfg.id, req.api_key)
    cfg.updated_at = utc_now()
    db.commit()
    db.refresh(cfg)
    return _serialize(cfg)


@router.delete("/ai/providers/{provider_id}", status_code=204)
def delete_provider(provider_id: str, db: Session = Depends(get_db)) -> None:
    cfg = get_or_404(db, ProviderConfig, provider_id, "AI 服务配置")
    active = db.execute(
        select(Job).where(Job.type.in_(["review_generate", "classroom_summary"]), Job.status.in_(ACTIVE_JOB_STATUSES))
    ).scalars()
    for job in active:
        if ((job.payload or {}).get("provider_config_id") or (job.payload or {}).get("provider_id")) == provider_id:
            raise ApiError(
                409,
                "CONFLICT",
                "有进行中的 AI 任务使用该配置，待任务结束后重试",
                details={"job_id": job.id, "provider_id": provider_id},
            )
    secrets_store.clear_key(provider_id)
    db.delete(cfg)
    db.commit()
    return None


@router.delete("/ai/providers/{provider_id}/key", status_code=204)
def clear_provider_key(provider_id: str, db: Session = Depends(get_db)) -> None:
    """幂等：无密钥同样 204。清除后 has_api_key=false。"""
    get_or_404(db, ProviderConfig, provider_id, "AI 服务配置")
    _ensure_not_in_use(db, provider_id)
    secrets_store.clear_key(provider_id)
    return None


@router.post("/ai/providers/{provider_id}/test")
def test_provider(provider_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """连接测试（固定短提示，不含用户材料）；远端失败也是结果本身（200 + result.ok=false）。"""
    cfg = get_or_404(db, ProviderConfig, provider_id, "AI 服务配置")
    result = run_connection_test(
        cfg.base_url, cfg.model, secrets_store.get_key(cfg.id), cfg.timeout_seconds
    )
    cfg.last_tested_at = utc_now()
    cfg.last_test_result = result
    db.commit()
    db.refresh(cfg)
    return {"provider": _serialize(cfg), "result": result}
