"""领域模型（SQLAlchemy 2.0 声明式，与 contracts/schemas 一一对齐）。

约定：
- ID 为不透明字符串（带前缀，如 crs_/res_/tsk_），平台 ID 只存在于 source_refs.external_id；
- 时间统一按 UTC 存储（DateTime(timezone=True)；SQLite 取回为 naive，序列化按 UTC 处理）；
- source_refs 为多态子表（owner_type + owner_id + seq 保序）；
- JSON 列仅用于契约中的自由结构（summary/errors/windows 等），有明确字段的对象用独立列。

序列化方法 to_contract() 输出与契约样例同形状的 dict；字段顺序不影响语义。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .platforms import entry_url_for

OWNER_COURSE = "course"
OWNER_RESOURCE = "resource"
OWNER_ASSIGNMENT = "assignment"
OWNER_RECORDING = "recording"
OWNER_TASK = "task"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_date(d: date | None) -> str | None:
    return d.isoformat() if d is not None else None


class SourceRef(Base):
    """多态来源引用（contracts/schemas/source-ref.schema.json）。"""

    __tablename__ = "source_refs"

    owner_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    canonical_url: Mapped[str | None] = mapped_column(String(1024))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    seq: Mapped[int] = mapped_column(Integer, default=0)

    def to_contract(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "external_id": self.external_id,
            "canonical_url": self.canonical_url,
            "observed_at": iso_utc(self.observed_at),
        }


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str] = mapped_column(String(512))
    term: Mapped[str | None] = mapped_column(String(64))
    teacher: Mapped[str | None] = mapped_column(String(256))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    def to_contract(self, source_refs: list[SourceRef]) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "term": self.term,
            "teacher": self.teacher,
            "source_refs": [r.to_contract() for r in source_refs],
            "archived_at": iso_utc(self.archived_at),
            "created_at": iso_utc(self.created_at),
            "updated_at": iso_utc(self.updated_at),
        }


class Resource(Base):
    __tablename__ = "resources"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(512))
    version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(128))
    parse_status: Mapped[str] = mapped_column(String(32))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    access_level: Mapped[str] = mapped_column(String(32), default="full")
    file_id: Mapped[str | None] = mapped_column(String(128))
    extent_value: Mapped[int | None] = mapped_column(Integer)
    extent_unit: Mapped[str | None] = mapped_column(String(32))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    def to_contract(self, source_refs: list[SourceRef], chunk_count: int | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "course_id": self.course_id,
            "kind": self.kind,
            "title": self.title,
            "source_refs": [r.to_contract() for r in source_refs],
            "version": self.version,
            "content_hash": self.content_hash,
            "parse_status": self.parse_status,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "access_level": self.access_level,
            "file_id": self.file_id,
            "extent_value": self.extent_value,
            "extent_unit": self.extent_unit,
            "chunk_count": chunk_count,
            "fetched_at": iso_utc(self.fetched_at),
            "source_updated_at": iso_utc(self.source_updated_at),
            "archived_at": iso_utc(self.archived_at),
            "created_at": iso_utc(self.created_at),
            "updated_at": iso_utc(self.updated_at),
        }


class Textbook(Base):
    """课程教材的精确版本；文件与习题沿用受控资源存储。"""
    __tablename__ = "textbooks"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), index=True)
    title: Mapped[str] = mapped_column(String(512))
    author: Mapped[str] = mapped_column(String(256), default="")
    edition: Mapped[str] = mapped_column(String(128), default="")
    isbn: Mapped[str] = mapped_column(String(32), default="")
    source_url: Mapped[str] = mapped_column(String(1024), default="")
    resource_id: Mapped[str | None] = mapped_column(ForeignKey("resources.id"))
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    def to_contract(self):
        return {key: getattr(self, key) for key in (
            "id", "course_id", "title", "author", "edition", "isbn", "source_url", "resource_id", "confirmed"
        )}


class TextbookExercise(Base):
    __tablename__ = "textbook_exercises"
    textbook_id: Mapped[str] = mapped_column(ForeignKey("textbooks.id"), primary_key=True)
    chapter: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[str] = mapped_column(String(32), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    page: Mapped[int | None] = mapped_column(Integer)
    resource_version: Mapped[int | None] = mapped_column(Integer)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)


class TextbookSettings(Base):
    __tablename__ = 'textbook_settings'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rights_check_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    source_base_url: Mapped[str] = mapped_column(String(1024), default='')


class TextbookRightsReport(Base):
    __tablename__ = 'textbook_rights_reports'
    textbook_id: Mapped[str] = mapped_column(ForeignKey('textbooks.id'), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(128))
    result: Mapped[dict] = mapped_column(JSON)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ResourceVersion(Base):
    __tablename__ = "resource_versions"

    resource_id: Mapped[str] = mapped_column(ForeignKey("resources.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_hash: Mapped[str | None] = mapped_column(String(128))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    file_id: Mapped[str | None] = mapped_column(String(128))
    extent_value: Mapped[int | None] = mapped_column(Integer)
    extent_unit: Mapped[str | None] = mapped_column(String(32))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parse_status: Mapped[str] = mapped_column(String(32))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    def to_contract(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "version": self.version,
            "content_hash": self.content_hash,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "file_id": self.file_id,
            "extent_value": self.extent_value,
            "extent_unit": self.extent_unit,
            "fetched_at": iso_utc(self.fetched_at),
            "parse_status": self.parse_status,
            "source_updated_at": iso_utc(self.source_updated_at),
            "created_at": iso_utc(self.created_at),
        }


class ContentChunk(Base):
    """解析片段（contracts/schemas/content-chunk.schema.json）。"""

    __tablename__ = "content_chunks"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    resource_id: Mapped[str] = mapped_column(ForeignKey("resources.id"), index=True)
    resource_version: Mapped[int] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    locator: Mapped[dict] = mapped_column(JSON)
    provenance: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    def to_contract(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "resource_id": self.resource_id,
            "resource_version": self.resource_version,
            "ordinal": self.ordinal,
            "text": self.text,
            "locator": self.locator,
            "provenance": self.provenance,
            "created_at": iso_utc(self.created_at),
        }


class FileRecord(Base):
    """受控文件存储的元数据（blob 在 FILES_DIR，路径由 id 派生，从不存库/不外发）。

    file_id 即受控访问标识；下载统一经 GET /files/{file_id}（含所有权检查）。
    """

    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    original_name: Mapped[str] = mapped_column(String(512))
    mime_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    def to_contract(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.original_name,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "content_hash": self.content_hash,
            "created_at": iso_utc(self.created_at),
        }


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), index=True)
    title: Mapped[str] = mapped_column(String(512))
    instructions: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_precision: Mapped[str] = mapped_column(String(16), default="unknown")
    submission_status: Mapped[str] = mapped_column(String(32), default="unknown")
    fact_source: Mapped[str] = mapped_column(String(16), default="platform")
    confidence: Mapped[float | None] = mapped_column(Float)
    attachment_resource_ids: Mapped[list] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    def to_contract(self, source_refs: list[SourceRef]) -> dict[str, Any]:
        return {
            "id": self.id,
            "course_id": self.course_id,
            "title": self.title,
            "instructions": self.instructions,
            "due_at": iso_utc(self.due_at),
            "due_precision": self.due_precision,
            "submission_status": self.submission_status,
            "fact_source": self.fact_source,
            "confidence": self.confidence,
            "source_ref": source_refs[0].to_contract() if source_refs else None,
            "attachment_resource_ids": list(self.attachment_resource_ids or []),
            "revision": self.revision,
            "observed_at": iso_utc(self.observed_at),
            "archived_at": iso_utc(self.archived_at),
            "created_at": iso_utc(self.created_at),
            "updated_at": iso_utc(self.updated_at),
        }


class Recording(Base):
    __tablename__ = "recordings"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), index=True)
    title: Mapped[str] = mapped_column(String(512))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    canonical_url: Mapped[str | None] = mapped_column(String(1024))
    transcript_status: Mapped[str] = mapped_column(String(32), default="unknown")
    subtitle_languages: Mapped[list] = mapped_column(JSON, default=list)
    transcript_next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fact_source: Mapped[str] = mapped_column(String(16), default="platform")
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    def to_contract(self, source_refs: list[SourceRef]) -> dict[str, Any]:
        return {
            "id": self.id,
            "course_id": self.course_id,
            "title": self.title,
            "started_at": iso_utc(self.started_at),
            "duration_seconds": self.duration_seconds,
            "canonical_url": self.canonical_url,
            "transcript_status": self.transcript_status,
            "subtitle_languages": list(self.subtitle_languages or []),
            "transcript_next_check_at": iso_utc(self.transcript_next_check_at),
            "fact_source": self.fact_source,
            "source_ref": source_refs[0].to_contract() if source_refs else None,
            "observed_at": iso_utc(self.observed_at),
            "created_at": iso_utc(self.created_at),
            "updated_at": iso_utc(self.updated_at),
        }


class LearningTask(Base):
    __tablename__ = "learning_tasks"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    course_id: Mapped[str | None] = mapped_column(ForeignKey("courses.id"), index=True)
    type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(512))
    estimated_minutes: Mapped[int] = mapped_column(Integer)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_precision: Mapped[str] = mapped_column(String(16), default="unknown")
    prerequisite_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    fact_source: Mapped[str] = mapped_column(String(16), default="user")
    match_key: Mapped[str | None] = mapped_column(String(256), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    def to_contract(self, source_refs: list[SourceRef]) -> dict[str, Any]:
        return {
            "id": self.id,
            "match_key": self.match_key,
            "course_id": self.course_id,
            "type": self.type,
            "title": self.title,
            "estimated_minutes": self.estimated_minutes,
            "due_at": iso_utc(self.due_at),
            "due_precision": self.due_precision,
            "prerequisite_ids": list(self.prerequisite_ids or []),
            "source_refs": [r.to_contract() for r in source_refs],
            "status": self.status,
            "fact_source": self.fact_source,
            "revision": self.revision,
            "archived_at": iso_utc(self.archived_at),
            "created_at": iso_utc(self.created_at),
            "updated_at": iso_utc(self.updated_at),
        }


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="candidate")
    input_revision: Mapped[str] = mapped_column(String(128))
    horizon_start: Mapped[date] = mapped_column(Date)
    horizon_end: Mapped[date] = mapped_column(Date)
    summary: Mapped[dict] = mapped_column(JSON)
    based_on_plan_id: Mapped[str | None] = mapped_column(String(128))
    superseded_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    blocks: Mapped[list["PlanBlock"]] = relationship(
        back_populates="plan",
        order_by="(PlanBlock.starts_at, PlanBlock.task_id, PlanBlock.id)",
    )
    unassigned: Mapped[list["PlanUnassigned"]] = relationship(back_populates="plan", order_by="PlanUnassigned.task_id")
    warnings: Mapped[list["PlanWarning"]] = relationship(back_populates="plan", order_by="PlanWarning.id")

    def to_contract(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "status": self.status,
            "input_revision": self.input_revision,
            "horizon_start": iso_date(self.horizon_start),
            "horizon_end": iso_date(self.horizon_end),
            "blocks": [b.to_contract() for b in self.blocks],
            "unassigned": [u.to_contract() for u in self.unassigned],
            "warnings": [w.to_contract() for w in self.warnings],
            "summary": self.summary,
            "based_on_plan_id": self.based_on_plan_id,
            "created_at": iso_utc(self.created_at),
            "applied_at": iso_utc(self.applied_at),
            "revoked_at": iso_utc(self.revoked_at),
            "superseded_by": self.superseded_by,
        }


class PlanBlock(Base):
    """计划时间块。引擎块 ID（blk:{task_id}:{i}）只在计划内唯一 → 复合主键 (plan_id, id)。"""

    __tablename__ = "plan_blocks"

    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("learning_tasks.id"), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    locked: Mapped[bool] = mapped_column(Boolean, default=False)

    plan: Mapped[Plan] = relationship(back_populates="blocks")

    def to_contract(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "starts_at": iso_utc(self.starts_at),
            "ends_at": iso_utc(self.ends_at),
            "locked": self.locked,
        }


class PlanUnassigned(Base):
    __tablename__ = "plan_unassigned"

    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("learning_tasks.id"), primary_key=True)
    reason_code: Mapped[str] = mapped_column(String(64))
    shortage_minutes: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)
    suggestion: Mapped[str | None] = mapped_column(Text)

    plan: Mapped[Plan] = relationship(back_populates="unassigned")

    def to_contract(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "reason_code": self.reason_code,
            "shortage_minutes": self.shortage_minutes,
            "message": self.message,
            "suggestion": self.suggestion,
        }


class PlanWarning(Base):
    __tablename__ = "plan_warnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), index=True)
    code: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    task_ids: Mapped[list] = mapped_column(JSON, default=list)
    details: Mapped[dict | None] = mapped_column(JSON)

    plan: Mapped[Plan] = relationship(back_populates="warnings")

    def to_contract(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "task_ids": list(self.task_ids or []),
            "details": self.details,
        }


class Connection(Base):
    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), unique=True)
    display_name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="ok")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[dict | None] = mapped_column(JSON)
    recovery_action: Mapped[dict | None] = mapped_column(JSON)
    capabilities: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    def to_contract(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "platform": self.platform,
            "display_name": self.display_name,
            "status": self.status,
            "last_checked_at": iso_utc(self.last_checked_at),
            "last_sync_at": iso_utc(self.last_sync_at),
            "last_error": self.last_error,
            "recovery_action": self.recovery_action,
            "entry_url": entry_url_for(self.platform),
            "created_at": iso_utc(self.created_at),
            "updated_at": iso_utc(self.updated_at),
        }


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    result_refs: Mapped[list] = mapped_column(JSON, default=list)
    payload: Mapped[dict | None] = mapped_column(JSON)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    phase: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def to_contract(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "status": self.status,
            "progress": self.progress,
            "errors": list(self.errors or []),
            "warnings": list(self.warnings or []),
            "result_refs": list(self.result_refs or []),
            "cancel_requested": self.cancel_requested,
            "phase": self.phase,
            "created_at": iso_utc(self.created_at),
            "started_at": iso_utc(self.started_at),
            "finished_at": iso_utc(self.finished_at),
        }


class SyncCursor(Base):
    """每 (连接, 课程) 的增量游标。游标不透明，任何消费方不得解析其结构（A4 §3）。"""

    __tablename__ = "sync_cursors"

    connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id"), primary_key=True)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), primary_key=True)
    cursor: Mapped[str] = mapped_column(String(512))
    mode: Mapped[str] = mapped_column(String(16), default="incremental")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Availability(Base):
    """单用户阶段 1：恒定单行（id=1）。"""

    __tablename__ = "availability"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    weekly_windows: Mapped[list] = mapped_column(JSON, default=list)
    date_exceptions: Mapped[list] = mapped_column(JSON, default=list)
    daily_limit_minutes: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    def to_contract(self) -> dict[str, Any]:
        return {
            "timezone": self.timezone,
            "weekly_windows": list(self.weekly_windows or []),
            "date_exceptions": list(self.date_exceptions or []),
            "daily_limit_minutes": self.daily_limit_minutes,
            "updated_at": iso_utc(self.updated_at),
        }


class TaskFeedback(Base):
    """学习反馈事件（FeedbackRecord）；不改写平台事实，只影响本地任务状态。"""

    __tablename__ = "task_feedback"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("learning_tasks.id"), index=True)
    outcome: Mapped[str] = mapped_column(String(16))
    actual_minutes: Mapped[int | None] = mapped_column(Integer)
    difficulty: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    def to_contract(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "outcome": self.outcome,
            "actual_minutes": self.actual_minutes,
            "difficulty": self.difficulty,
            "note": self.note,
            "created_at": iso_utc(self.created_at),
        }


class IdempotencyRecord(Base):
    """Idempotency-Key 记录（POST /imports、任务反馈等写操作复用）。"""

    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(256))
    request_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(Integer)
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ProviderConfig(Base):
    """AI 服务配置（contracts/schemas/provider-config.schema.json，CR008）。

    密钥不落库：本地密钥存储（Windows DPAPI；STUDY_SECRETS_DIR）按配置 ID 存独立文件，
    序列化时由调用方传入 has_api_key（读取密钥存储存在性），任何路径不返回密钥本身。
    """

    __tablename__ = "provider_configs"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    protocol: Mapped[str] = mapped_column(String(32), default="openai_chat_completions")
    base_url: Mapped[str] = mapped_column(String(1024))
    model: Mapped[str] = mapped_column(String(256))
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=60)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    def to_contract(self, has_api_key: bool) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "model": self.model,
            "has_api_key": has_api_key,
            "timeout_seconds": self.timeout_seconds,
            "created_at": iso_utc(self.created_at),
            "updated_at": iso_utc(self.updated_at),
            "last_tested_at": iso_utc(self.last_tested_at),
            "last_test_result": self.last_test_result,
        }


class ReviewMaterialSnapshot(Base):
    """出卷材料快照（contracts/schemas/material-snapshot.schema.json）：受理时冻结，正文原文存 JSON。

    engine_version/fingerprint 与本行同存；revision 通过 snapshot_id 复读当时材料与备注。
    """

    __tablename__ = "review_material_snapshots"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), index=True)
    engine_version: Mapped[str] = mapped_column(String(32))
    fingerprint: Mapped[str] = mapped_column(String(128), index=True)
    constraints: Mapped[dict] = mapped_column(JSON)
    notes: Mapped[dict] = mapped_column(JSON)
    items: Mapped[list] = mapped_column(JSON)
    missing: Mapped[list] = mapped_column(JSON)
    total_chars: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    def to_contract(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "course_id": self.course_id,
            "created_at": iso_utc(self.created_at),
            "engine_version": self.engine_version,
            "fingerprint": self.fingerprint,
            "notes": self.notes,
            "items": list(self.items or []),
            "missing": list(self.missing or []),
            "total_chars": self.total_chars,
        }


class ReviewPaper(Base):
    """复习卷身份行（contracts/schemas/review-paper.schema.json）：latest_revision=0 表示尚未生成成功。"""

    __tablename__ = "review_papers"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(512))
    latest_revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    revisions: Mapped[list["ReviewPaperRevision"]] = relationship(
        back_populates="paper", order_by="ReviewPaperRevision.revision",
    )


class ReviewPaperRevision(Base):
    """试卷历史版本（contracts/schemas/review-paper-revision.schema.json；复合主键 (paper_id, revision)）。

    蓝图/题目/告警/摘要存 JSON（契约结构，由出卷流水线写入）；fingerprint 与快照行一致，供索引直查。
    """

    __tablename__ = "review_paper_revisions"

    paper_id: Mapped[str] = mapped_column(ForeignKey("review_papers.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision_reason: Mapped[str] = mapped_column(String(16))
    based_on_revision: Mapped[int | None] = mapped_column(Integer)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("review_material_snapshots.id"))
    title: Mapped[str] = mapped_column(String(512))
    mode: Mapped[str] = mapped_column(String(16))
    engine_version: Mapped[str] = mapped_column(String(32))
    fingerprint: Mapped[str] = mapped_column(String(128))
    constraints: Mapped[dict] = mapped_column(JSON)
    provider: Mapped[dict] = mapped_column(JSON)
    blueprint: Mapped[dict] = mapped_column(JSON)
    questions: Mapped[list] = mapped_column(JSON)
    warnings: Mapped[list] = mapped_column(JSON)
    summary: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    paper: Mapped[ReviewPaper] = relationship(back_populates="revisions")

    def to_index_contract(self) -> dict[str, Any]:
        """修订历史索引条目（review-paper-revision.schema.json）。"""
        return {
            "paper_id": self.paper_id,
            "revision": self.revision,
            "revision_reason": self.revision_reason,
            "based_on_revision": self.based_on_revision,
            "title": self.title,
            "created_at": iso_utc(self.created_at),
            "mode": self.mode,
            "engine_version": self.engine_version,
            "fingerprint": self.fingerprint,
            "provider": self.provider,
            "summary": self.summary,
        }

    def to_paper_contract(self, paper: ReviewPaper, snapshot: ReviewMaterialSnapshot) -> dict[str, Any]:
        """完整试卷（review-paper.schema.json）：身份行 + 本版内容 + 快照行。"""
        return {
            "id": self.paper_id,
            "course_id": paper.course_id,
            "revision": self.revision,
            "revision_reason": self.revision_reason,
            "based_on_revision": self.based_on_revision,
            "title": self.title,
            "created_at": iso_utc(self.created_at),
            "mode": self.mode,
            "constraints": self.constraints,
            "provider": self.provider,
            "engine_version": self.engine_version,
            "snapshot": snapshot.to_contract(),
            "blueprint": self.blueprint,
            "questions": list(self.questions or []),
            "warnings": list(self.warnings or []),
            "summary": self.summary,
        }
