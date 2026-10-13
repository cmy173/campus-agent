"""领域模型（对齐《协作总纲》4.2 核心对象）。

约定：
- 所有时间字段为带偏移量的 ISO 8601 字符串（如 2026-10-12T19:00+08:00），内部经 timeutil 转为 aware datetime。
- 学习时长为整数分钟；回放时长为秒；页码/幻灯片从 1 开始（本文件只承载元数据，解析产物见后续批次）。
- 事实来源标记：platform / user / inferred；缺失截止时间使用 null，不猜测。
- 本模块仅定义数据结构与 JSON 互转，不做语义校验；语义问题由 taskgen / schedule 以告警或未安排原因返回。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .timeutil import TZ_DEFAULT, parse_dt

PROVENANCES = ("platform", "user", "inferred")
TASK_TYPES = ("reading", "watching", "practice", "assignment", "review")
TASK_STATUSES = ("pending", "completed", "cancelled")
RESOURCE_KINDS = ("pdf", "pptx", "docx")
PARSE_STATUSES = ("pending", "parsed", "needs_ocr", "failed")
TRANSCRIPT_STATUSES = ("none", "raw", "ready")
SUBMISSION_STATUSES = ("unknown", "not_submitted", "submitted", "graded")
EXTENT_UNITS = ("page", "slide", "character")


def _to_dict_list(items: list[Any]) -> list[dict[str, Any]]:
    return [item.to_dict() for item in items]


def _from_dict_list(cls: Any, items: list[dict[str, Any]] | None) -> list[Any]:
    return [cls.from_dict(item) for item in (items or [])]


@dataclass
class SourceRef:
    platform: str
    external_id: str
    canonical_url: str | None = None
    observed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "external_id": self.external_id,
            "canonical_url": self.canonical_url,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceRef":
        return cls(
            platform=data["platform"],
            external_id=data["external_id"],
            canonical_url=data.get("canonical_url"),
            observed_at=data.get("observed_at"),
        )

    def key(self) -> tuple[str, str]:
        return (self.platform, self.external_id)


@dataclass
class Course:
    id: str
    title: str
    term: str | None = None
    teacher: str | None = None
    source_refs: list[SourceRef] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "term": self.term,
            "teacher": self.teacher,
            "source_refs": _to_dict_list(self.source_refs),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Course":
        return cls(
            id=data["id"],
            title=data["title"],
            term=data.get("term"),
            teacher=data.get("teacher"),
            source_refs=_from_dict_list(SourceRef, data.get("source_refs")),
        )


@dataclass
class Resource:
    id: str
    course_id: str
    kind: str
    title: str
    version: int = 1
    content_hash: str | None = None
    parse_status: str = "pending"
    source_refs: list[SourceRef] = field(default_factory=list)
    extent_value: int | None = None
    extent_unit: str | None = None
    # 平台 MIME（契约 resource.schema.json；可为 null——真实样例全为 null，PD-2 视频识别
    # 须同时回退到标题扩展名，见 taskgen._looks_like_video）
    mime_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "course_id": self.course_id,
            "kind": self.kind,
            "title": self.title,
            "version": self.version,
            "content_hash": self.content_hash,
            "parse_status": self.parse_status,
            "source_refs": _to_dict_list(self.source_refs),
            "extent_value": self.extent_value,
            "extent_unit": self.extent_unit,
            "mime_type": self.mime_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Resource":
        return cls(
            id=data["id"],
            course_id=data["course_id"],
            kind=data["kind"],
            title=data["title"],
            version=int(data.get("version", 1)),
            content_hash=data.get("content_hash"),
            parse_status=data.get("parse_status", "pending"),
            source_refs=_from_dict_list(SourceRef, data.get("source_refs")),
            extent_value=data.get("extent_value"),
            extent_unit=data.get("extent_unit"),
            mime_type=data.get("mime_type"),
        )


@dataclass
class Assignment:
    id: str
    course_id: str
    title: str
    instructions: str | None = None
    due_at: str | None = None
    due_precision: str = "unknown"
    submission_status: str = "unknown"
    source_ref: SourceRef | None = None
    estimated_minutes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "course_id": self.course_id,
            "title": self.title,
            "instructions": self.instructions,
            "due_at": self.due_at,
            "due_precision": self.due_precision,
            "submission_status": self.submission_status,
            "source_ref": self.source_ref.to_dict() if self.source_ref else None,
            "estimated_minutes": self.estimated_minutes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Assignment":
        ref = data.get("source_ref")
        due_at = data.get("due_at")
        return cls(
            id=data["id"],
            course_id=data["course_id"],
            title=data["title"],
            instructions=data.get("instructions"),
            due_at=due_at,
            due_precision=data.get("due_precision") or ("datetime" if due_at else "unknown"),
            submission_status=data.get("submission_status", "unknown"),
            source_ref=SourceRef.from_dict(ref) if ref else None,
            estimated_minutes=data.get("estimated_minutes"),
        )


@dataclass
class Recording:
    id: str
    course_id: str
    title: str
    started_at: str | None = None
    duration_seconds: int | None = None
    canonical_url: str | None = None
    transcript_status: str = "unknown"  # 契约六态；D 端仅 ready 视为有内容
    subtitle_languages: list[str] | None = None  # [] 已确认无字幕；null 未知
    transcript_next_check_at: str | None = None  # pending 时的复查时间（由 B 调度）
    source_ref: SourceRef | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "course_id": self.course_id,
            "title": self.title,
            "started_at": self.started_at,
            "duration_seconds": self.duration_seconds,
            "canonical_url": self.canonical_url,
            "transcript_status": self.transcript_status,
            "subtitle_languages": (
                list(self.subtitle_languages) if self.subtitle_languages is not None else None
            ),
            "transcript_next_check_at": self.transcript_next_check_at,
            "source_ref": self.source_ref.to_dict() if self.source_ref else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Recording":
        ref = data.get("source_ref")
        languages = data.get("subtitle_languages")
        return cls(
            id=data["id"],
            course_id=data["course_id"],
            title=data["title"],
            started_at=data.get("started_at"),
            duration_seconds=data.get("duration_seconds"),
            canonical_url=data.get("canonical_url"),
            transcript_status=data.get("transcript_status", "unknown"),
            subtitle_languages=list(languages) if languages is not None else None,
            transcript_next_check_at=data.get("transcript_next_check_at"),
            source_ref=SourceRef.from_dict(ref) if ref else None,
        )


@dataclass
class LearningTask:
    id: str
    course_id: str
    type: str
    title: str
    estimated_minutes: int
    due_at: str | None = None
    due_precision: str = "unknown"
    prerequisite_ids: list[str] = field(default_factory=list)
    source_refs: list[SourceRef] = field(default_factory=list)
    status: str = "pending"
    completion_criteria: str | None = None
    estimate_provenance: str = "inferred"
    estimate_confidence: float | None = None
    splittable: bool | None = None
    priority: int | None = None
    # PD-3（coord v1.8 裁定，2026-10-10）：平台已提交（submission_status ∈ submitted/graded）
    # 的派生标注。保留生成、参与展示（供 C 弱化），但不参与排程（engine/revise 将其视同
    # 已完成的任务从新安排中排除）。与 status（本地学习进度）互不影响——学习完成 ≠ 平台已提交。
    platform_submitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "course_id": self.course_id,
            "type": self.type,
            "title": self.title,
            "estimated_minutes": self.estimated_minutes,
            "due_at": self.due_at,
            "due_precision": self.due_precision,
            "prerequisite_ids": list(self.prerequisite_ids),
            "source_refs": _to_dict_list(self.source_refs),
            "status": self.status,
            "completion_criteria": self.completion_criteria,
            "estimate_provenance": self.estimate_provenance,
            "estimate_confidence": self.estimate_confidence,
            "splittable": self.splittable,
            "priority": self.priority,
            "platform_submitted": self.platform_submitted,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LearningTask":
        due_at = data.get("due_at")
        return cls(
            id=data["id"],
            course_id=data["course_id"],
            type=data["type"],
            title=data["title"],
            estimated_minutes=data["estimated_minutes"],
            due_at=due_at,
            due_precision=data.get("due_precision") or ("datetime" if due_at else "unknown"),
            prerequisite_ids=list(data.get("prerequisite_ids") or []),
            source_refs=_from_dict_list(SourceRef, data.get("source_refs")),
            status=data.get("status", "pending"),
            completion_criteria=data.get("completion_criteria"),
            estimate_provenance=data.get("estimate_provenance", "inferred"),
            estimate_confidence=data.get("estimate_confidence"),
            splittable=data.get("splittable"),
            priority=data.get("priority"),
            platform_submitted=bool(data.get("platform_submitted", False)),
        )


@dataclass
class WindowSpan:
    """一天内的一个时间窗，HH:MM；end <= start 表示跨午夜到次日。"""

    start: str
    end: str

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WindowSpan":
        return cls(start=data["start"], end=data["end"])


@dataclass
class WeeklyWindow:
    """按周重复的可用窗口；weekday 使用 0=周一 … 6=周日。"""

    weekday: int
    start: str
    end: str

    def to_dict(self) -> dict[str, Any]:
        return {"weekday": self.weekday, "start": self.start, "end": self.end}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WeeklyWindow":
        return cls(weekday=int(data["weekday"]), start=data["start"], end=data["end"])


@dataclass
class DateException:
    """日期例外：可整体不可用，或覆盖当日窗口/每日上限（null 表示沿用默认）。"""

    date: str
    unavailable: bool = False
    windows: list[WindowSpan] | None = None
    daily_limit_minutes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "unavailable": self.unavailable,
            "windows": _to_dict_list(self.windows) if self.windows is not None else None,
            "daily_limit_minutes": self.daily_limit_minutes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DateException":
        windows = data.get("windows")
        return cls(
            date=data["date"],
            unavailable=bool(data.get("unavailable", False)),
            windows=[WindowSpan.from_dict(w) for w in windows] if windows is not None else None,
            daily_limit_minutes=data.get("daily_limit_minutes"),
        )


@dataclass
class Availability:
    timezone: str = TZ_DEFAULT
    weekly_windows: list[WeeklyWindow] = field(default_factory=list)
    date_exceptions: list[DateException] = field(default_factory=list)
    daily_limit_minutes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timezone": self.timezone,
            "weekly_windows": _to_dict_list(self.weekly_windows),
            "date_exceptions": _to_dict_list(self.date_exceptions),
            "daily_limit_minutes": self.daily_limit_minutes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Availability":
        return cls(
            timezone=data.get("timezone", TZ_DEFAULT),
            weekly_windows=_from_dict_list(WeeklyWindow, data.get("weekly_windows")),
            date_exceptions=_from_dict_list(DateException, data.get("date_exceptions")),
            daily_limit_minutes=data.get("daily_limit_minutes"),
        )


@dataclass
class PlanBlock:
    id: str
    task_id: str
    starts_at: str
    ends_at: str
    locked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "locked": self.locked,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanBlock":
        return cls(
            id=data["id"],
            task_id=data["task_id"],
            starts_at=data["starts_at"],
            ends_at=data["ends_at"],
            locked=bool(data.get("locked", False)),
        )


@dataclass
class UnassignedItem:
    task_id: str
    reason_code: str
    message: str
    needed_minutes: int | None = None
    available_minutes: int | None = None
    max_contiguous_minutes: int | None = None
    deadline: str | None = None
    suggestion: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "reason_code": self.reason_code,
            "message": self.message,
            "needed_minutes": self.needed_minutes,
            "available_minutes": self.available_minutes,
            "max_contiguous_minutes": self.max_contiguous_minutes,
            "deadline": self.deadline,
            "suggestion": self.suggestion,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UnassignedItem":
        return cls(
            task_id=data["task_id"],
            reason_code=data["reason_code"],
            message=data["message"],
            needed_minutes=data.get("needed_minutes"),
            available_minutes=data.get("available_minutes"),
            max_contiguous_minutes=data.get("max_contiguous_minutes"),
            deadline=data.get("deadline"),
            suggestion=data.get("suggestion"),
        )


@dataclass
class WarningItem:
    code: str
    message: str
    task_ids: list[str] = field(default_factory=list)
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "task_ids": list(self.task_ids),
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WarningItem":
        return cls(
            code=data["code"],
            message=data["message"],
            task_ids=list(data.get("task_ids") or []),
            details=data.get("details"),
        )


@dataclass
class Plan:
    horizon_start: str
    horizon_end: str
    id: str | None = None
    version: int | None = None
    input_revision: int | str | None = None
    blocks: list[PlanBlock] = field(default_factory=list)
    unassigned: list[UnassignedItem] = field(default_factory=list)
    warnings: list[WarningItem] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "input_revision": self.input_revision,
            "horizon_start": self.horizon_start,
            "horizon_end": self.horizon_end,
            "blocks": _to_dict_list(self.blocks),
            "unassigned": _to_dict_list(self.unassigned),
            "warnings": _to_dict_list(self.warnings),
            "summary": dict(self.summary),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Plan":
        return cls(
            id=data.get("id"),
            version=data.get("version"),
            input_revision=data.get("input_revision"),
            horizon_start=data["horizon_start"],
            horizon_end=data["horizon_end"],
            blocks=_from_dict_list(PlanBlock, data.get("blocks")),
            unassigned=_from_dict_list(UnassignedItem, data.get("unassigned")),
            warnings=_from_dict_list(WarningItem, data.get("warnings")),
            summary=dict(data.get("summary") or {}),
        )


def deadline_of(due_at: str | None, due_precision: str | None) -> datetime | None:
    """按截止精度计算排程用截止时刻。

    - due_precision="date" 表示“当日截止”（due_at 为该日 00:00，按用户时区），截止时刻取次日 00:00；
    - due_precision="datetime" 为精确时刻；
    - 缺失或 unknown 返回 None（不猜测）。
    """
    if not due_at:
        return None
    dt = parse_dt(due_at)
    if due_precision == "date":
        return dt + timedelta(days=1)
    return dt
