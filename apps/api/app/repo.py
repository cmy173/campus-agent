"""查询辅助：source_refs 取用、游标分页、404 取用、ID 生成、幂等与并发控制。

游标为不透明的 base64（编码 keyset 列值）；列表统一按主键升序、keyset 分页。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from .errors import ApiError, not_found
from .models import IdempotencyRecord, SourceRef, iso_utc

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
IDEMPOTENCY_TTL = timedelta(hours=24)


def refs_for(db: Session, owner_type: str, owner_id: str) -> list[SourceRef]:
    stmt = (
        select(SourceRef)
        .where(SourceRef.owner_type == owner_type, SourceRef.owner_id == owner_id)
        .order_by(SourceRef.seq, SourceRef.platform, SourceRef.external_id)
    )
    return list(db.execute(stmt).scalars())


def get_or_404(db: Session, model: type, object_id: str, kind: str):
    obj = db.get(model, object_id)
    if obj is None:
        raise not_found(kind, object_id)
    return obj


def find_source_owner(db: Session, owner_type: str, platform: str, external_id: str) -> str | None:
    """按 (owner_type, platform, external_id) 查来源引用的宿主 ID（同步 upsert 主键解析）。"""
    stmt = select(SourceRef.owner_id).where(
        SourceRef.owner_type == owner_type,
        SourceRef.platform == platform,
        SourceRef.external_id == external_id,
    )
    return db.execute(stmt).scalars().first()


def encode_cursor(value: str) -> str:
    return base64.urlsafe_b64encode(f"k:{value}".encode()).decode()


def decode_cursor(cursor: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise ApiError(400, "VALIDATION_FAILED", "无效的分页游标", details={"cursor": cursor})
    if not raw.startswith("k:"):
        raise ApiError(400, "VALIDATION_FAILED", "无效的分页游标", details={"cursor": cursor})
    return raw[2:]


def decode_int_cursor(cursor: str) -> int:
    """整数 keyset 游标（版本号降序列表等使用）。"""
    raw = decode_cursor(cursor)
    try:
        return int(raw)
    except ValueError:
        raise ApiError(400, "VALIDATION_FAILED", "无效的分页游标", details={"cursor": cursor}) from None


def page(db: Session, stmt: Select, model: type, cursor: str | None, limit: int) -> tuple[list, str | None]:
    """keyset 分页：按主键升序，返回 (rows, next_cursor)。"""
    if cursor is not None:
        stmt = stmt.where(model.id > decode_cursor(cursor))
    rows = list(db.execute(stmt.order_by(model.id).limit(limit + 1)).scalars())
    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(rows[-1].id)
    return rows, next_cursor


def normalize_dt(dt: datetime) -> datetime:
    """统一为 UTC（SQLite 存墙钟时区信息会丢失，写入前必须归一）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def new_id(db: Session, model: type, prefix: str) -> str:
    for _ in range(10):
        candidate = f"{prefix}_{secrets.token_hex(4)}"
        if db.get(model, candidate) is None:
            return candidate
    raise ApiError(500, "INTERNAL_ERROR", "ID 生成失败，请重试")


def parse_if_match(value: str | None) -> int | None:
    """If-Match: "<revision>"（引号可省）。缺失返回 None（可选并发控制）。"""
    if value is None:
        return None
    text = value.strip()
    if text.startswith("W/"):
        text = text[2:].strip()
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        text = text[1:-1]
    try:
        return int(text)
    except ValueError:
        raise ApiError(400, "VALIDATION_FAILED", "If-Match 需为整数 revision（如 \"3\"）", details={"if_match": value})


def check_revision(if_match: str | None, current_revision: int, target: str | None = None) -> None:
    provided = parse_if_match(if_match)
    if provided is not None and provided != current_revision:
        details: dict[str, Any] = {}
        if target is not None:
            details["target"] = target
        details["expected_revision"] = provided
        details["current_revision"] = current_revision
        raise ApiError(412, "REVISION_MISMATCH", "对象已被修改，请刷新后重试", details=details)


def validate_due(due_at: datetime | None, due_precision: str) -> None:
    """due_at 与 due_precision 一致性（不猜测缺失值；契约 PatchAssignmentRequest 描述同口径）。"""
    if due_precision == "unknown":
        if due_at is not None:
            raise ApiError(
                400,
                "VALIDATION_FAILED",
                "due_precision=unknown 时 due_at 必须为 null",
                details={"due_at": iso_utc(due_at), "due_precision": due_precision},
            )
    elif due_at is None:
        raise ApiError(
            400,
            "VALIDATION_FAILED",
            f"due_precision={due_precision} 时必须提供 due_at",
            details={"due_precision": due_precision},
        )


def _fingerprint(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def idempotency_lookup(db: Session, key: str | None, endpoint: str, payload: Any) -> tuple[int, dict] | None:
    """命中返回 (status_code, response)；未命中返回 None；键复用但请求体不同 → 409。"""
    if key is None:
        return None
    record = db.get(IdempotencyRecord, key)
    if record is None:
        return None
    created = record.created_at if record.created_at.tzinfo else record.created_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - created > IDEMPOTENCY_TTL:
        db.delete(record)
        return None
    if record.endpoint == endpoint and record.request_hash == _fingerprint(payload):
        return record.status_code, record.response
    raise ApiError(
        409,
        "IDEMPOTENCY_KEY_REUSED",
        "幂等键已用于不同请求体",
        details={"key": key, "endpoint": endpoint},
    )


def idempotency_store(db: Session, key: str | None, endpoint: str, payload: Any, status_code: int, response: dict) -> None:
    if key is None:
        return
    db.merge(
        IdempotencyRecord(
            key=key,
            endpoint=endpoint,
            request_hash=_fingerprint(payload),
            status_code=status_code,
            response=response,
        )
    )


def validate_idempotency_key(key: str | None) -> None:
    if key is not None and not (8 <= len(key) <= 128):
        raise ApiError(
            400,
            "VALIDATION_FAILED",
            "Idempotency-Key 长度须在 8-128 之间",
            details={"idempotency_key_length": len(key)},
        )
