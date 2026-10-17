"""资料检索（GET /api/v1/search）：当前版本解析片段的子串匹配。

范围：未归档资源、parse_status=ready、片段属于当前版本（chunk.resource_version == resource.version）。
匹配：q 大小写不敏感子串（% / _ 按字面量匹配）；score = q 在片段文本中的出现次数；
排序：score 降序、chunk id 升序（稳定），keyset 游标 "score|chunk_id"。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import ApiError
from ..models import ContentChunk, Resource
from ..repo import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor

router = APIRouter(tags=["search"])

KINDS = ("slides", "handout", "syllabus", "scan", "document", "link", "other")


def _like_pattern(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/search")
def search(
    db: Session = Depends(get_db),
    q: str = Query(min_length=1),
    course_id: str | None = Query(default=None),
    kind: str | None = Query(default=None, pattern="^(" + "|".join(KINDS) + ")$"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    needle = q.lower()
    lowered = func.lower(ContentChunk.text)
    score_expr = (func.length(lowered) - func.length(func.replace(lowered, needle, ""))) * 1.0 / len(needle)

    stmt = (
        select(ContentChunk, Resource, score_expr.label("score"))
        .join(Resource, Resource.id == ContentChunk.resource_id)
        .where(
            ContentChunk.resource_version == Resource.version,
            Resource.archived_at.is_(None),
            Resource.parse_status == "ready",
            lowered.like(_like_pattern(q), escape="\\"),
        )
    )
    if course_id is not None:
        stmt = stmt.where(Resource.course_id == course_id)
    if kind is not None:
        stmt = stmt.where(Resource.kind == kind)
    if cursor is not None:
        score_text, _, chunk_id = decode_cursor(cursor).partition("|")
        try:
            cursor_score = float(score_text)
        except ValueError:
            raise ApiError(400, "VALIDATION_FAILED", "无效的分页游标", details={"cursor": cursor}) from None
        stmt = stmt.where(
            or_(
                score_expr < cursor_score,
                and_(score_expr == cursor_score, ContentChunk.id > chunk_id),
            )
        )

    rows = db.execute(stmt.order_by(score_expr.desc(), ContentChunk.id.asc()).limit(limit + 1)).all()
    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        last_chunk, _, last_score = rows[-1]
        next_cursor = encode_cursor(f"{float(last_score)!r}|{last_chunk.id}")
    items = [
        {
            "chunk_id": chunk.id,
            "resource_id": resource.id,
            "resource_version": chunk.resource_version,
            "course_id": resource.course_id,
            "resource_title": resource.title,
            "text": chunk.text,
            "locator": chunk.locator,
            "score": float(score),
        }
        for chunk, resource, score in rows
    ]
    return {"items": items, "next_cursor": next_cursor}
