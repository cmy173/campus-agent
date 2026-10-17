"""请求体模型（与 OpenAPI components.schemas 对齐；extra=forbid 对应 additionalProperties: false）。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TaskType = Literal["study", "reading", "watching", "assignment", "practice", "review", "other"]
TaskStatus = Literal["pending", "in_progress", "completed", "cancelled"]
DuePrecision = Literal["datetime", "date", "unknown"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTaskRequest(StrictModel):
    course_id: str | None = None
    type: TaskType = "other"
    title: str = Field(min_length=1, max_length=512)
    estimated_minutes: int = Field(ge=1)
    due_at: datetime | None = None
    due_precision: DuePrecision | None = None
    prerequisite_ids: list[str] = Field(default_factory=list)


class CreateCourseRequest(StrictModel):
    title: str = Field(min_length=1, max_length=512)
    term: str | None = None
    teacher: str | None = None


class PlatformCourseSelection(CreateCourseRequest):
    external_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")

    @model_validator(mode="after")
    def nonblank_title(self):
        self.title = self.title.strip()
        if not self.title:
            raise ValueError("课程名称不能为空白")
        return self


class ImportPlatformCoursesRequest(StrictModel):
    courses: list[PlatformCourseSelection] = Field(min_length=1, max_length=100)


class CreateAssignmentRequest(StrictModel):
    course_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=512)
    instructions: str | None = None
    due_at: datetime | None = None
    due_precision: DuePrecision | None = None


SubmissionStatus = Literal["unknown", "not_submitted", "submitted", "graded"]


class PatchAssignmentRequest(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=512)
    instructions: str | None = None
    due_at: datetime | None = None
    due_precision: DuePrecision | None = None
    submission_status: SubmissionStatus | None = None


class PatchTaskRequest(StrictModel):
    course_id: str | None = None
    type: TaskType | None = None
    title: str | None = Field(default=None, min_length=1, max_length=512)
    estimated_minutes: int | None = Field(default=None, ge=1)
    due_at: datetime | None = None
    due_precision: DuePrecision | None = None
    prerequisite_ids: list[str] | None = None
    status: TaskStatus | None = None


class LockTaskRequest(StrictModel):
    locked: bool
    starts_at: datetime | None = None


class BuildTasksRequest(StrictModel):
    """POST /tasks/build 的请求体（可省略）；course_id 限定课程范围，缺省/null 为全部课程。"""

    course_id: str | None = None


class FeedbackRequest(StrictModel):
    outcome: Literal["completed", "partial", "skipped"]
    actual_minutes: int | None = Field(default=None, ge=1)
    difficulty: int | None = Field(default=None, ge=1, le=5)
    note: str | None = Field(default=None, max_length=2000)


class SourceRefInput(StrictModel):
    platform: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    canonical_url: str | None = None
    observed_at: datetime


class AddSourceRefRequest(StrictModel):
    source_ref: SourceRefInput


ImportKind = Literal["slides", "handout", "syllabus", "scan", "document", "other"]


class ImportPayload(StrictModel):
    """POST /imports 的 payload 字段（multipart 中的 JSON 字符串）。link 类无文件，不入导入通道。"""

    course_id: str = Field(min_length=1)
    kind: ImportKind
    title: str | None = Field(default=None, min_length=1, max_length=512)
    source_ref: SourceRefInput | None = None
    textbook_id: str | None = Field(default=None, min_length=1)


class PreviewPlanRequest(StrictModel):
    horizon_start: date | None = None
    horizon_end: date | None = None
    respect_locked: bool = True


class StartSyncRequest(StrictModel):
    full: bool = False


ExportSection = Literal["courses", "resources", "assignments", "recordings", "tasks", "plans", "feedback"]


class CreateExportRequest(StrictModel):
    include: list[ExportSection] | None = None
    format: Literal["json", "ics"] = "json"


TIME_POINT = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class _TimeWindow(StrictModel):
    start: str = Field(pattern=TIME_POINT)
    end: str = Field(pattern=TIME_POINT)

    @model_validator(mode="after")
    def _end_after_start(self) -> "_TimeWindow":
        if self.end <= self.start:
            raise ValueError("end 须晚于 start（跨天窗口拆分为两条）")
        return self


class WeeklyWindow(_TimeWindow):
    weekday: int = Field(ge=1, le=7)


class DateException(StrictModel):
    date: date
    windows: list[_TimeWindow]
    note: str | None


class PutAvailabilityRequest(StrictModel):
    timezone: str = Field(min_length=1)
    weekly_windows: list[WeeklyWindow]
    date_exceptions: list[DateException]
    daily_limit_minutes: int | None = Field(ge=1)


class UpsertProviderRequest(StrictModel):
    """POST /ai/providers（CreateProviderRequest；密钥明文仅此一次传入）。"""

    name: str = Field(min_length=1, max_length=80)
    protocol: Literal["openai_chat_completions"] = "openai_chat_completions"
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1, max_length=200)
    timeout_seconds: int = Field(default=120, ge=5, le=600)
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)


class PatchProviderRequest(StrictModel):
    """PATCH /ai/providers/{id}：仅更新提供的字段；api_key 缺省/null=保留，空串 400（min_length=1）。"""

    name: str | None = Field(default=None, min_length=1, max_length=80)
    protocol: Literal["openai_chat_completions"] | None = None
    base_url: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    timeout_seconds: int | None = Field(default=None, ge=5, le=600)
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)


ReviewMode = Literal["ai", "bank", "hybrid"]
QuestionType = Literal[
    "single_choice", "multiple_choice", "true_false", "fill_blank", "calculation", "short_answer", "proof"
]


class ReviewQuestionQuota(StrictModel):
    type: QuestionType
    count: int = Field(ge=1, le=100)
    points_each: int | None = Field(default=None, ge=1, le=200)


class ReviewConstraints(StrictModel):
    question_types: list[ReviewQuestionQuota] = Field(min_length=1)
    difficulty: Literal["easy", "medium", "hard", "mixed"] | None = None
    total_points: int | None = Field(default=None, ge=1, le=500)
    duration_minutes: int | None = Field(default=None, ge=5, le=600)
    answer_display: Literal["separate", "inline"] = "separate"


class ReviewResourceRef(StrictModel):
    resource_id: str = Field(min_length=1)
    version: int | None = Field(default=None, ge=1)
    chunk_ids: list[str] | None = Field(default=None, min_length=1)


class ReviewAssignmentRef(StrictModel):
    assignment_id: str = Field(min_length=1)
    include_attachments: bool = True


class ReviewMaterialSelection(StrictModel):
    resources: list[ReviewResourceRef] = Field(default_factory=list)
    assignments: list[ReviewAssignmentRef] = Field(default_factory=list)


class ReviewRequestIn(StrictModel):
    """POST /review-papers/preflight|generate（review-request.schema.json）。

    notes 不设 max_length：超限由领域层返回 400 且 details 含 max_chars（不静默截断）。
    """

    course_id: str = Field(min_length=1)
    mode: ReviewMode
    provider_config_id: str | None = None
    title: str | None = Field(default=None, max_length=120)
    materials: ReviewMaterialSelection = Field(default_factory=ReviewMaterialSelection)
    notes: str = ""
    constraints: ReviewConstraints


class ReviewRevisionRequest(StrictModel):
    """POST /review-papers/{paper_id}/revisions（全部可空：null/缺省=沿用上一版）。

    notes 提供空字符串 = 清空备注；materials 缺省 = 沿用上一版快照输入（重新按当前解析结果冻结）。
    course_id/mode 不在本请求内（修订不更换课程与模式）。
    """

    provider_config_id: str | None = None
    title: str | None = Field(default=None, max_length=120)
    materials: ReviewMaterialSelection | None = None
    notes: str | None = None
    constraints: ReviewConstraints | None = None
