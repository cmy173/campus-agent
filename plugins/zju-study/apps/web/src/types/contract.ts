/**
 * 契约类型层：与 contracts/schemas/*.json + openapi.yaml（v0.1 冻结版，main@8ce5b43）保持一致。
 * 枚举取值一律小写下划线；错误码为 SCREAMING_SNAKE_CASE。
 * 若本文件与 contracts/ 不一致，以 contracts/ 为准并走 CHANGE-REQUEST。
 */

// ---------- 基础 ----------

/** 平台标识：xzzd（学在浙大）、zykt（智云课堂）、manual（手动导入）；开放集合 */
export type PlatformId = string

export interface SourceRef {
  platform: string
  external_id: string
  canonical_url: string | null
  observed_at: string
}

export type FactSource = 'platform' | 'user' | 'inferred'
export type DuePrecision = 'datetime' | 'date' | 'unknown'

export interface CursorPage<T> {
  items: T[]
  next_cursor: string | null
}

/** 嵌套错误（Job.errors 等内部结构） */
export interface ErrorInfo {
  code: string
  message: string
  retryable: boolean
  details?: Record<string, unknown> | null
}

/** 统一错误响应体的 error 字段（error.schema.json#/$defs/Error） */
export interface ApiErrorBody extends ErrorInfo {
  request_id: string
}

// ---------- 课程 ----------

export interface Course {
  id: string
  title: string
  term: string | null
  teacher: string | null
  source_refs: SourceRef[]
  archived_at: string | null
  created_at: string
  updated_at: string
}

// ---------- 资源 ----------

export type ResourceKind = 'slides' | 'handout' | 'syllabus' | 'scan' | 'document' | 'link' | 'other'
export type ParseStatus = 'pending' | 'processing' | 'ready' | 'failed' | 'skipped'
export type AccessLevel = 'full' | 'preview_only' | 'restricted'
export type ExtentUnit = 'page' | 'slide'

export interface Resource {
  id: string
  course_id: string
  kind: ResourceKind
  title: string
  source_refs: SourceRef[]
  version: number
  content_hash: string | null
  parse_status: ParseStatus
  mime_type: string | null
  size_bytes: number | null
  access_level: AccessLevel
  file_id: string | null
  extent_value: number | null
  extent_unit: ExtentUnit | null
  chunk_count: number | null
  fetched_at: string | null
  source_updated_at: string | null
  archived_at: string | null
  created_at: string
  updated_at: string
}

export interface ResourceVersion {
  resource_id: string
  version: number
  content_hash: string | null
  mime_type: string | null
  size_bytes: number | null
  file_id: string | null
  extent_value: number | null
  extent_unit: ExtentUnit | null
  fetched_at: string | null
  parse_status: ParseStatus
  source_updated_at: string | null
  created_at: string
}

// ---------- 内容片段 ----------

export type Locator =
  | { kind: 'page'; page: number }
  | { kind: 'slide'; slide: number }
  | { kind: 'timestamp'; start_seconds: number; end_seconds?: number }
  | { kind: 'text'; start_offset: number; end_offset?: number }

export interface ChunkProvenance {
  extractor: string
  extractor_version: string
  job_id: string
  fact_source: FactSource
  observed_at: string
}

export interface ContentChunk {
  id: string
  resource_id: string
  resource_version: number
  ordinal: number
  text: string
  locator: Locator
  provenance: ChunkProvenance
  created_at: string
}

// ---------- 学习任务 ----------

export type TaskType = 'study' | 'reading' | 'watching' | 'assignment' | 'practice' | 'review' | 'other'
export type TaskStatus = 'pending' | 'in_progress' | 'completed' | 'cancelled'

export interface LearningTask {
  id: string
  match_key: string | null
  course_id: string | null
  type: TaskType
  title: string
  estimated_minutes: number
  due_at: string | null
  due_precision: DuePrecision
  prerequisite_ids: string[]
  source_refs: SourceRef[]
  status: TaskStatus
  fact_source: FactSource
  revision: number
  archived_at: string | null
  created_at: string
  updated_at: string
}

// ---------- 作业 / 回放 ----------

export type SubmissionStatus = 'unknown' | 'not_submitted' | 'submitted' | 'graded'

export interface Assignment {
  id: string
  course_id: string
  title: string
  instructions: string | null
  due_at: string | null
  due_precision: DuePrecision
  submission_status: SubmissionStatus
  fact_source: FactSource
  confidence: number | null
  source_ref: SourceRef | null
  attachment_resource_ids: string[]
  revision: number
  observed_at: string | null
  archived_at: string | null
  created_at: string
  updated_at: string
}

export type TranscriptStatus = 'pending' | 'processing' | 'ready' | 'unavailable' | 'unknown' | 'failed'

export interface Recording {
  id: string
  course_id: string
  title: string
  started_at: string | null
  duration_seconds: number | null
  canonical_url: string | null
  transcript_status: TranscriptStatus
  subtitle_languages: string[] | null
  transcript_next_check_at: string | null
  fact_source: FactSource
  source_ref: SourceRef | null
  observed_at: string | null
  created_at: string
  updated_at: string
}

// ---------- 可用时间 ----------

export interface WeeklyWindow {
  weekday: number
  start: string
  end: string
}

export interface DateException {
  date: string
  /** 空数组 = 该日完全不可用 */
  windows: { start: string; end: string }[]
  note: string | null
}

export interface Availability {
  timezone: string
  weekly_windows: WeeklyWindow[]
  date_exceptions: DateException[]
  daily_limit_minutes: number | null
  updated_at: string
}

// ---------- 计划 ----------

export type PlanStatus = 'candidate' | 'active' | 'superseded' | 'revoked'

export interface PlanBlock {
  id: string
  task_id: string
  starts_at: string
  ends_at: string
  locked: boolean
}

export type UnassignedReasonCode =
  | 'DEADLINE_PASSED'
  | 'INSUFFICIENT_WINDOW_BEFORE_DUE'
  | 'INSUFFICIENT_TOTAL_TIME'
  | 'TASK_TOO_LONG_INDIVISIBLE'
  | 'FRAGMENTED_WINDOW'
  | 'DEPENDENCY_CONFLICT'
  | 'INVALID_TASK'

export interface UnassignedItem {
  task_id: string
  reason_code: UnassignedReasonCode
  shortage_minutes: number | null
  message: string | null
  suggestion: string | null
}

export interface PlanWarning {
  code: string
  message: string
  task_ids: string[]
  details: Record<string, unknown> | null
}

export interface PlanSummary {
  capacity_minutes: number
  scheduled_minutes: number
  locked_minutes: number
  scheduled_task_count: number
  unassigned_task_count: number
}

export interface Plan {
  id: string
  version: number
  status: PlanStatus
  input_revision: string
  horizon_start: string
  horizon_end: string
  blocks: PlanBlock[]
  unassigned: UnassignedItem[]
  warnings: PlanWarning[]
  summary: PlanSummary
  based_on_plan_id: string | null
  created_at: string
  applied_at: string | null
  revoked_at: string | null
  superseded_by: string | null
}

/**
 * 预览差异（客户端按 task_id 对比生成，非契约对象；Q1 口径：
 * 应用前预览只展示 新增/移动/保留/移除 四态，不在 PlanBlock 上加 change 字段）
 */
export type PlanDiffKind = 'added' | 'moved' | 'kept' | 'removed'

export interface PlanDiffEntry {
  task_id: string
  kind: PlanDiffKind
  /** 预览中的块（removed 为 null） */
  block: PlanBlock | null
  /** 当前生效计划中的块（added 为 null） */
  previous_block: PlanBlock | null
}

// ---------- 后台任务 ----------

export type JobType =
  | 'import'
  | 'parse'
  | 'sync'
  | 'export'
  | 'build_tasks'
  /** 复习卷生成（CR008 新增；导出为 GET 直返文本，不是 job） */
  | 'review_generate'
  | 'textbook_sync'
  | 'classroom_summary'
export type JobStatus = 'queued' | 'running' | 'succeeded' | 'partial_failed' | 'failed' | 'cancelled'

export interface ResultRef {
  type: string
  id: string
}

/** 非致命告警（CR003）：成功任务也可带告警，与 errors（失败）语义分离 */
export interface JobWarning {
  code: string
  message: string
  /** 相关任务 ID 或稳定匹配键（可为空数组） */
  task_ids: string[]
  details: Record<string, unknown> | null
}

export interface Job {
  id: string
  type: JobType
  status: JobStatus
  /** 0–1；无法估计为 null */
  progress: number | null
  /** 阶段进度（CR008；仅部分任务类型使用，其余省略或 null；review_generate 见 ReviewJobPhase） */
  phase?: string | null
  errors: ErrorInfo[]
  warnings: JobWarning[]
  result_refs: ResultRef[]
  cancel_requested: boolean
  created_at: string
  started_at: string | null
  finished_at: string | null
}

// ---------- 连接（跳转式登录） ----------

export type ConnectionStatus = 'ok' | 'auth_required' | 'network_error' | 'blocked' | 'disabled' | 'unsupported' | 'service_unavailable'

export interface RecoveryAction {
  type: 'user_login'
  entry_url: string
  message: string | null
}

export interface Connection {
  id: string
  platform: string
  display_name: string
  status: ConnectionStatus
  last_checked_at: string | null
  last_sync_at: string | null
  last_error: ErrorInfo | null
  recovery_action: RecoveryAction | null
  /** 平台权威登录入口（服务端常量；打开即跳官方登录页。未收录平台为 null） */
  entry_url?: string | null
  created_at: string
  updated_at: string
}

// ---------- 适配器能力 ----------

export type FeatureStatus = 'supported' | 'unsupported' | 'unknown' | 'preview_only'

export interface CapabilityFeature {
  status: FeatureStatus
  note: string | null
}

export interface Capabilities {
  platform: string
  connection_id?: string
  connector_version: string
  auth: { mode: 'user_session'; sso: string | null; recovery_hint: string | null }
  features: Record<string, CapabilityFeature>
  limits: {
    page_size_max: number | null
    rate_limit_note: string | null
    campus_network_required: boolean | 'unknown'
  }
  checked_at: string
}

// ---------- 搜索 ----------

export interface SearchHit {
  chunk_id: string
  resource_id: string
  resource_version: number
  course_id: string | null
  resource_title: string
  text: string
  locator: Locator
  score: number
}

// ---------- 响应包装（openapi components/schemas） ----------

export interface JobAcceptedResponse {
  job: Job
}

export interface CurrentPlanResponse {
  plan: Plan | null
  current_input_revision: string
}

export interface PlanPreviewResponse {
  plan: Plan
  current_input_revision: string
}

export interface ApplyPlanResponse {
  plan: Plan
  previous_plan_id: string | null
}

export interface RevokePlanResponse {
  plan: Plan
  restored_plan_id: string | null
}

export interface LockTaskResponse {
  task: LearningTask
  plan: Plan | null
}

export type FeedbackOutcome = 'completed' | 'partial' | 'skipped'

export interface FeedbackRequest {
  outcome: FeedbackOutcome
  actual_minutes?: number | null
  difficulty?: number | null
  note?: string | null
}

export interface FeedbackRecord {
  id: string
  task_id: string
  outcome: FeedbackOutcome
  actual_minutes: number | null
  difficulty: number | null
  note: string | null
  created_at: string
}

export interface FeedbackResponse {
  feedback: FeedbackRecord
  task: LearningTask
  current_input_revision: string
  replan_suggested: boolean
}
