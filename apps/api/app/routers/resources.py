"""资源与版本链（/api/v1/resources…）。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import ApiError
from ..models import OWNER_RESOURCE, ContentChunk, Resource, ResourceVersion
from ..repo import DEFAULT_LIMIT, MAX_LIMIT, decode_int_cursor, encode_cursor, get_or_404, refs_for
from ..worker import enqueue_parse_job

router = APIRouter(tags=["resources"])


def resource_to_contract(db: Session, resource: Resource) -> dict[str, Any]:
    """资源响应；chunk_count 为派生只读字段（parse_status=ready 时按当前版本片段数，否则 null）。"""
    chunk_count: int | None = None
    if resource.parse_status == "ready":
        chunk_count = db.execute(
            select(func.count())
            .select_from(ContentChunk)
            .where(ContentChunk.resource_id == resource.id, ContentChunk.resource_version == resource.version)
        ).scalar_one()
    return resource.to_contract(refs_for(db, OWNER_RESOURCE, resource.id), chunk_count=chunk_count)


@router.get("/resources/{resource_id}")
def get_resource(resource_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    resource = get_or_404(db, Resource, resource_id, "资源")
    return resource_to_contract(db, resource)


@router.get("/resources/{resource_id}/versions")
def list_resource_versions(
    resource_id: str,
    db: Session = Depends(get_db),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    get_or_404(db, Resource, resource_id, "资源")
    stmt = select(ResourceVersion).where(ResourceVersion.resource_id == resource_id)
    if cursor is not None:
        stmt = stmt.where(ResourceVersion.version < decode_int_cursor(cursor))
    rows = list(db.execute(stmt.order_by(ResourceVersion.version.desc()).limit(limit + 1)).scalars())
    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(str(rows[-1].version))
    return {"items": [v.to_contract() for v in rows], "next_cursor": next_cursor}


@router.post("/resources/{resource_id}/reparse", status_code=202)
def reparse_resource(resource_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """重新解析资源当前版本：创建（或复用排队中的）parse 任务；旧产物在新任务成功前保持可用。"""
    resource = get_or_404(db, Resource, resource_id, "资源")
    version_row = db.execute(
        select(ResourceVersion).where(
            ResourceVersion.resource_id == resource_id, ResourceVersion.version == resource.version
        )
    ).scalar_one_or_none()
    file_id = resource.file_id or (version_row.file_id if version_row is not None else None)
    if not file_id:
        raise ApiError(
            409,
            "CONFLICT",
            "当前版本没有已入库的受控文件，无法解析",
            details={"resource_id": resource_id, "version": resource.version},
        )
    job = enqueue_parse_job(db, resource, reason="reparse")
    db.commit()
    return {"job": job.to_contract()}


@router.get("/resources/{resource_id}/chunks")
def list_resource_chunks(
    resource_id: str,
    db: Session = Depends(get_db),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    resource = get_or_404(db, Resource, resource_id, "资源")
    stmt = select(ContentChunk).where(
        ContentChunk.resource_id == resource_id,
        ContentChunk.resource_version == resource.version,
    )
    if cursor is not None:
        stmt = stmt.where(ContentChunk.ordinal > decode_int_cursor(cursor))
    rows = list(db.execute(stmt.order_by(ContentChunk.ordinal).limit(limit + 1)).scalars())
    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(str(rows[-1].ordinal))
    return {"items": [c.to_contract() for c in rows], "next_cursor": next_cursor}
