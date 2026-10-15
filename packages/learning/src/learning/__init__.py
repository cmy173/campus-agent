"""浙大学习工作台 · packages/learning（智能体 D）

内容处理与学习计划引擎：文档解析（D2）、回放字幕整理（D4）、规则任务生成、规则排程与局部重排、
可选模型适配（后续批次）。本模块不直接访问学校平台或业务数据库，由 B 调用并持久化。

公开接口（与 B 共同冻结中，详见 docs/D0-第一批交付与集成交接.md（M0）与 docs/D-M1交接.md（M1））：
- parse_document / detect_format（子模块 learning.parse）：文本型 PDF/PPTX/DOCX → ContentChunk
  （页码/幻灯片/偏移定位；扫描件 needs_ocr 非失败；缓存键含 content_hash + 解析器版本）。
- build_tasks：资料/作业/回放/用户提示 → 候选学习任务（稳定 ID、可重复生成）。
- build_tasks_json：B13 集成入口（契约形状 JSON → 候选任务 + 告警；按 match_key 对齐既有任务、
  保留人工修正、忽略已归档行；语义与示例见 docs/D12-B13接入说明.md）。
- generate_plan：任务 + 可用时间 + 锁定块 → 计划候选（时间块、未安排项、告警、摘要）。
- revise_plan：在上一版计划上局部重排（仅调整变化与必要连锁影响；锁定块冲突显式返回；附中文变化摘要）。
- validate_plan：复核任意计划是否满足硬约束（无重叠、在窗口内、每日上限、前置顺序、锁定保留）。
- plan_to_ics：计划 + 任务 → ICS（RFC 5545）文本；时间与时区准确（TZID 或 UTC 回退、全天截止与
  精确时刻区分）、跨午夜与每日上限口径与引擎账本一致、确定性输出（B19 的 POST /exports 入口）。
- normalize_transcript / segment_topics / transcript_chunks：已有字幕时间轴 → 规范化片段、
  主题分段（保留起止秒数）与 ContentChunk（locator=timestamp）；pending/unknown 不当作 ready。
- calibrate_estimates：实际耗时反馈 → 逐类系数校正（样本不足不调整，逐步单步移动）；
  系数经 TaskGenConfig.type_estimate_factors 生效，不改写用户自设耗时。
- ModelConfig / ModelGateway / validate_suggestions：模型接入端口（默认零网络调用；模型输出
  经结构、引用、时长、依赖校验后才可采纳；密钥仅以引用形态出现）。
- 复习卷引擎（子模块 learning.review，见 docs/agent-plan/review-paper/handoff/D.md）：
  build_snapshot → analyze_materials → build_blueprint → select_bank_questions →
  build_generation_request → parse_and_validate_output / validate_bank_questions /
  merge_ai_with_bank → build_repair_request；备注原文逐字进入请求、引用可回溯、结构校验
  不冒充学术正确；本包零网络，模型调用由 B 执行。
"""

from .estimate import (
    CALIBRATION_VERSION,
    CalibrationOptions,
    CalibrationReason,
    CalibrationReport,
    TypeCalibration,
    calibrate_estimates,
)

from .model import (
    FRAMEWORK_SYSTEM_PROMPT,
    MODEL_PORT_VERSION,
    CallRecord,
    ContextBlock,
    ModelClient,
    ModelConfig,
    ModelErrorCode,
    ModelGateway,
    ModelRequest,
    ModelRequestError,
    ModelResponse,
    NullModelClient,
    SuggestionErrorCode,
    SuggestionIssue,
    SuggestionLimits,
    TaskSuggestion,
    build_request,
    validate_suggestions,
)

from .models import (
    Assignment,
    Availability,
    Course,
    DateException,
    LearningTask,
    Plan,
    PlanBlock,
    Recording,
    Resource,
    SourceRef,
    UnassignedItem,
    WarningItem,
    WeeklyWindow,
    WindowSpan,
    deadline_of,
)
from .review import (
    BANK_PROMPT_VERSION,
    GENERATE_PROMPT_VERSION,
    GENERATION_MODES,
    QUESTION_TYPES,
    REPAIR_PROMPT_VERSION,
    REVIEW_ENGINE_VERSION,
    analyze_materials,
    build_bank_questions,
    build_blueprint,
    build_generation_request,
    build_repair_request,
    build_snapshot,
    chunk_label,
    distribute_points,
    kp_id_for,
    material_fingerprint,
    merge_ai_with_bank,
    parse_and_validate_output,
    parse_model_output,
    sanitize_provider_config,
    select_bank_questions,
    snapshot_overview,
    validate_bank_questions,
    validate_questions,
)
from .review import ReviewError, ReviewErrorCode, ReviewLimits, ValidationCode
from .samples import list_samples, list_transcripts, load_sample, load_transcript, run_sample
from .schedule.engine import (
    ReasonCode,
    ScheduleConfig,
    ScheduleOptions,
    WarningCode,
    generate_plan,
)
from .schedule.ics import plan_to_ics
from .schedule.revise import (
    BlockChange,
    ChangeKind,
    ChangeReason,
    ConflictCode,
    PlanRevision,
    RevisionChanges,
    RevisionConflict,
    RevisionWarningCode,
    revise_plan,
)
from .schedule.validate import Violation, validate_plan
from .taskgen import (
    TaskGenConfig,
    TaskGenOptions,
    TaskGenResult,
    TaskHint,
    assignment_task_id,
    build_tasks,
    build_tasks_json,
    recording_task_id,
    resource_task_id,
    resource_task_type,
    scaled_minutes,
    slugify_title,
)
from .timeutil import TZ_DEFAULT
from .transcript import (
    EXTRACTOR_BY_PROVENANCE,
    TRANSCRIPT_VERSION,
    TopicConfig,
    TopicSpan,
    Transcript,
    TranscriptErrorCode,
    TranscriptFormatError,
    TranscriptLimits,
    TranscriptSegment,
    TranscriptStatus,
    normalize_transcript,
    segment_topics,
    transcript_chunks,
)

__version__ = "0.1.0.dev0"

__all__ = [
    "Assignment",
    "Availability",
    "BANK_PROMPT_VERSION",
    "BlockChange",
    "CALIBRATION_VERSION",
    "CallRecord",
    "CalibrationOptions",
    "CalibrationReason",
    "CalibrationReport",
    "ChangeKind",
    "ChangeReason",
    "ConflictCode",
    "ContextBlock",
    "Course",
    "DateException",
    "EXTRACTOR_BY_PROVENANCE",
    "FRAMEWORK_SYSTEM_PROMPT",
    "GENERATE_PROMPT_VERSION",
    "GENERATION_MODES",
    "LearningTask",
    "MODEL_PORT_VERSION",
    "ModelClient",
    "ModelConfig",
    "ModelErrorCode",
    "ModelGateway",
    "ModelRequest",
    "ModelRequestError",
    "ModelResponse",
    "NullModelClient",
    "Plan",
    "PlanBlock",
    "PlanRevision",
    "QUESTION_TYPES",
    "REPAIR_PROMPT_VERSION",
    "REVIEW_ENGINE_VERSION",
    "ReasonCode",
    "Recording",
    "Resource",
    "ReviewError",
    "ReviewErrorCode",
    "ReviewLimits",
    "RevisionChanges",
    "RevisionConflict",
    "RevisionWarningCode",
    "ScheduleConfig",
    "ScheduleOptions",
    "SourceRef",
    "SuggestionErrorCode",
    "SuggestionIssue",
    "SuggestionLimits",
    "TRANSCRIPT_VERSION",
    "TZ_DEFAULT",
    "TaskGenConfig",
    "TaskGenOptions",
    "TaskGenResult",
    "TaskHint",
    "TaskSuggestion",
    "TopicConfig",
    "TopicSpan",
    "Transcript",
    "TranscriptErrorCode",
    "TranscriptFormatError",
    "TranscriptLimits",
    "TranscriptSegment",
    "TranscriptStatus",
    "TypeCalibration",
    "UnassignedItem",
    "ValidationCode",
    "Violation",
    "WarningCode",
    "WarningItem",
    "WeeklyWindow",
    "WindowSpan",
    "__version__",
    "analyze_materials",
    "assignment_task_id",
    "build_bank_questions",
    "build_blueprint",
    "build_generation_request",
    "build_repair_request",
    "build_request",
    "build_snapshot",
    "build_tasks",
    "build_tasks_json",
    "calibrate_estimates",
    "chunk_label",
    "deadline_of",
    "distribute_points",
    "generate_plan",
    "kp_id_for",
    "list_samples",
    "list_transcripts",
    "load_sample",
    "load_transcript",
    "material_fingerprint",
    "merge_ai_with_bank",
    "normalize_transcript",
    "parse_and_validate_output",
    "parse_model_output",
    "plan_to_ics",
    "recording_task_id",
    "resource_task_id",
    "resource_task_type",
    "revise_plan",
    "run_sample",
    "sanitize_provider_config",
    "scaled_minutes",
    "segment_topics",
    "select_bank_questions",
    "slugify_title",
    "snapshot_overview",
    "transcript_chunks",
    "validate_bank_questions",
    "validate_plan",
    "validate_questions",
    "validate_suggestions",
]
