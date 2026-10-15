/**
 * 复习卷功能契约类型层（网页端侧）。
 *
 * 形状对齐 B 侧冻结契约 CR008（commit 530ff04），逐一对照：
 *   - contracts/schemas/provider-config.schema.json
 *   - contracts/schemas/review-material.schema.json
 *   - contracts/schemas/review-request.schema.json（含 Constraints / MaterialSelection）
 *   - contracts/schemas/review-preflight.schema.json
 *   - contracts/schemas/material-snapshot.schema.json（Item / Missing / Notes）
 *   - contracts/schemas/paper-blueprint.schema.json
 *   - contracts/schemas/review-question.schema.json
 *   - contracts/schemas/review-paper.schema.json（含 ProviderSnapshot / Summary / Warning）
 *   - contracts/schemas/review-paper-revision.schema.json
 *   - contracts/schemas/questionbank-capabilities.schema.json
 *   - contracts/openapi/openapi.yaml（ReviewRevisionRequest 全可空子集）
 * mock 与真实后端均返回本文件的形状；页面不得依赖契约之外的字段。
 */
import type { CursorPage, DuePrecision, ErrorInfo, Job, Locator, ParseStatus } from './contract'

// ---------- AI 服务商设置（ProviderConfig） ----------

/** 首版唯一支持的协议；其他协议界面显示"不支持"，不可静默套用 */
export type ProviderProtocol = 'openai_chat_completions'

export interface ProviderTestResult {
  ok: boolean
  checked_at: string
  /** 往返耗时毫秒；未发出请求（如缺少密钥）为 null */
  latency_ms: number | null
  http_status: number | null
  /** 失败时的结构化结果；成功为 null。code 见 PROVIDER_* 码表，绝不包含密钥 */
  error: ErrorInfo | null
}

export interface ProviderConfig {
  /** 配置 ID（prv_ 前缀） */
  id: string
  /** 显示名称（1–80 字符，用户可读） */
  name: string
  protocol: ProviderProtocol
  /**
   * 规范化后的服务基础地址。规则：去首尾空白与末尾斜杠；仅 http/https、无查询串/片段/用户信息；
   * http 仅限本机与内网；路径为空时补 /v1，其余原样保留；含 /chat/completions 或 v1/v1 重复段时拒绝（400）。
   */
  base_url: string
  model: string
  /** 缺省视为启用，兼容旧配置；false 时不向该服务发送请求 */
  enabled?: boolean
  /** 本地密钥存储是否已有密钥；密钥本身永不出现在任何响应/日志/导出 */
  has_api_key: boolean
  /** 单次模型请求超时（5–600 秒） */
  timeout_seconds: number
  /** 最近一次连接测试时间；从未测试为 null */
  last_tested_at: string | null
  last_test_result: ProviderTestResult | null
  created_at: string
  updated_at: string
}

/** 创建请求（POST /ai/providers → 201 ProviderConfig） */
export interface ProviderUpsertInput {
  name: string
  protocol: ProviderProtocol
  base_url: string
  model: string
  /** 可省略：先保存设置、稍后补密钥（has_api_key=false） */
  api_key?: string | null
  enabled?: boolean
  timeout_seconds?: number
}

/** 更新请求（PATCH /ai/providers/{id}；仅更新提供的字段） */
export interface ProviderPatchInput {
  name?: string
  protocol?: ProviderProtocol
  base_url?: string
  model?: string
  /** 缺省或 null=保留现有密钥（不覆盖、不清除）；空字符串 400（避免误清空）；清除请用 DELETE .../key */
  api_key?: string | null
  enabled?: boolean
  timeout_seconds?: number
}

/** POST /ai/providers/{id}/test → 200；远端失败也是 200 且 result.ok=false */
export interface ProviderTestResponse {
  provider: ProviderConfig
  result: ProviderTestResult
}

// ---------- 材料清单（GET /courses/{id}/review-materials） ----------

/** 不可用原因（与 MaterialSnapshot.Missing.reason 同枚举） */
export type MaterialUnavailableReason =
  | 'not_parsed'
  | 'parse_failed'
  | 'type_skipped'
  | 'no_text'
  | 'chunk_missing'
  | 'attachment_absent'
  | 'not_accessible'

export interface ReviewMaterialAttachment {
  resource_id: string
  title: string
  parse_status: ParseStatus | null
  version: number | null
  chunk_count: number | null
  chars: number | null
  usable: boolean
  unavailable_reason: MaterialUnavailableReason | null
}

/**
 * 扁平材料项：课件/附件资源（kind='resource'）与作业（kind='assignment'）同一行形状。
 * parse_status/version/chunk_count 对作业为 null；chars 对作业=作业正文字符数。
 * usable=false 时 unavailable_reason 必填（不以标题冒充正文）。
 */
export interface ReviewMaterialItem {
  /** 资源（res_）或作业（asg_）ID */
  id: string
  kind: 'resource' | 'assignment'
  title: string
  course_id: string
  updated_at: string
  /** 是否含至少一段可读正文（选中后可实际用于出卷） */
  usable: boolean
  unavailable_reason: MaterialUnavailableReason | null
  parse_status: ParseStatus | null
  /** 当前资源版本；作业为 null */
  version: number | null
  /** 当前版本可读片段数；非 ready 为 null */
  chunk_count: number | null
  chars: number | null
  /** 作业截止（资源为 null） */
  due_at: string | null
  due_precision: DuePrecision | null
  /** 作业附件（资源为空数组） */
  attachments: ReviewMaterialAttachment[]
}

/** 游标分页；客户端负责翻页取全量 */
export type ReviewMaterialsPage = CursorPage<ReviewMaterialItem>

// ---------- 出卷请求（POST /review-papers/preflight | /review-papers/generate） ----------

export type ReviewMode = 'ai' | 'bank' | 'hybrid'

export type ReviewQuestionType =
  | 'single_choice'
  | 'multiple_choice'
  | 'true_false'
  | 'fill_blank'
  | 'calculation'
  | 'short_answer'
  | 'proof'

/** 单题难度 */
export type ReviewDifficulty = 'easy' | 'medium' | 'hard'

/** 约束中的难度偏好（mixed=混合；null=由引擎按材料推断） */
export type ReviewDifficultyPreference = ReviewDifficulty | 'mixed'

export type ReviewAnswerDisplay = 'separate' | 'inline'

export interface ReviewResourceSelection {
  resource_id: string
  /** 使用的资源版本；null=当前版本（历史版本只读） */
  version: number | null
  /** 选中片段 ID（升序去重，须属所选版本）；null=该版本全部可用片段 */
  chunk_ids: string[] | null
}

export interface ReviewAssignmentSelection {
  assignment_id: string
  /** 是否包含该作业的附件正文（作业正文本身必含） */
  include_attachments: boolean
}

export interface ReviewMaterialSelection {
  resources: ReviewResourceSelection[]
  assignments: ReviewAssignmentSelection[]
}

export interface ReviewQuestionTypeConstraint {
  type: ReviewQuestionType
  /** 1–100 */
  count: number
  /** 每题分值（1–200）；null=在总分约束下由蓝图分配 */
  points_each: number | null
}

export interface ReviewConstraints {
  /** 题型配额（至少一项） */
  question_types: ReviewQuestionTypeConstraint[]
  difficulty: ReviewDifficultyPreference | null
  /** 1–500；与每题分值冲突时预检报错 */
  total_points: number | null
  /** 5–600；影响题量建议与预计用时 */
  duration_minutes: number | null
  answer_display: ReviewAnswerDisplay
}

export interface ReviewRequest {
  course_id: string
  mode: ReviewMode
  /** AI/混合模式必填；bank 模式应为 null（不调用模型） */
  provider_config_id: string | null
  /** 卷名（≤120 字符）；null=由服务端按课程名给出可读默认标题（不返回内部 ID 兜底） */
  title: string | null
  materials: ReviewMaterialSelection
  /** 备注原文（≤20000 字符，保留换行/中文；超限 400 不静默截断；空字符串=无备注） */
  notes: string
  constraints: ReviewConstraints
}

/** 重做/修订请求（POST /review-papers/{id}/revisions）；全字段可空，未提供=沿用上一版；不换课程与模式 */
export interface ReviewRevisionRequest {
  /** null/缺省=沿用上一版 */
  provider_config_id?: string | null
  title?: string | null
  materials?: ReviewMaterialSelection
  /** 提供空字符串=清空备注；缺省/null=沿用上一版 */
  notes?: string | null
  constraints?: ReviewConstraints
}

// ---------- 预检（POST /review-papers/preflight → 200） ----------

export interface SnapshotNotes {
  /** 备注原文；无备注为空字符串 */
  text: string
  /** 备注原文 UTF-8 字节的 sha256（形如 sha256:<64hex>） */
  sha256: string
  chars: number
}

/** 快照材料项（含发送正文原文，不截断、不摘要） */
export interface SnapshotItem {
  kind: 'resource_text' | 'assignment_text' | 'attachment_text'
  /** resource_text/attachment_text 时为资源 ID；assignment_text 为 null */
  resource_id: string | null
  /** assignment_text 时为作业 ID；否则 null */
  assignment_id: string | null
  title: string
  /** 资源版本；作业正文为 null */
  version: number | null
  /** 内容哈希；作业正文无法计算为 null（不猜测） */
  content_hash: string | null
  /** 纳入的片段 ID（升序）；作业正文为空数组 */
  chunk_ids: string[]
  /** 片段定位，与 chunk_ids 一一对应 */
  locators: SnapshotLocator[]
  chars: number
  /** 实际发送的正文原文 */
  text: string
}

export interface SnapshotLocator {
  chunk_id: string
  locator: Locator | null
}

/** 选中但无法纳入正文的材料（不以标题冒充正文） */
export interface SnapshotMissing {
  kind: 'resource' | 'assignment' | 'attachment'
  resource_id: string | null
  assignment_id: string | null
  title: string
  reason: MaterialUnavailableReason
  /** 面向用户的建议（如"重新解析该文件"） */
  message: string
}

export interface MaterialSnapshot {
  /** 快照 ID（snp_ 前缀） */
  id: string
  course_id: string
  created_at: string
  engine_version: string
  /** 材料+备注+约束+引擎版本的规范化 sha256（^sha256:[0-9a-f]{64}$） */
  fingerprint: string
  notes: SnapshotNotes
  items: SnapshotItem[]
  missing: SnapshotMissing[]
  /** items 正文总字符数（Unicode 码点） */
  total_chars: number
}

/** 预检的将发送材料（items/missing 与生成时快照同形状；notes 为备注留痕） */
export interface ReviewPreflightMaterials {
  items: SnapshotItem[]
  missing: SnapshotMissing[]
  notes: SnapshotNotes
  total_chars: number
}

export interface ReviewPreflightProvider {
  /** bank 模式未用模型时各字段为 null */
  config_id: string | null
  name: string | null
  protocol: ProviderProtocol | null
  base_url: string | null
  /** 展示用主机名 */
  host: string | null
  model: string | null
  has_api_key: boolean
}

export interface ReviewPreflightLimits {
  notes_chars: number
  notes_max_chars: number
  material_chars: number
  material_max_chars: number
  question_count_total: number
  question_count_max: number
}

export interface ReviewPreflightBank {
  mode: ReviewMode
  usable: boolean
  configured_sources: number
  /** 不可用原因与建议（不得静默切换模式、不得假装搜到题）；可用为 null */
  message: string | null
}

export interface ReviewPreflightWarning {
  code: string
  message: string
}

export interface ReviewPreflight {
  /** 请求 fingerprint 预览（公式同 MaterialSnapshot.fingerprint） */
  fingerprint: string
  materials: ReviewPreflightMaterials
  provider: ReviewPreflightProvider
  limits: ReviewPreflightLimits
  bank: ReviewPreflightBank
  /** 预检提示（材料缺口、备注为空、题库未配置等；不阻断生成的提醒） */
  warnings: ReviewPreflightWarning[]
}

// ---------- 题目（ReviewQuestion） ----------

export interface ReviewQuestionOption {
  key: string
  text: string
}

export interface ReviewQuestionOrigin {
  /** ai=模型生成 / bank=网络原题 / adapted_bank=改编题（保留原出处） */
  kind: 'ai' | 'bank' | 'adapted_bank'
  /** kind=bank/adapted_bank 时对应 BankCandidate.id；ai 为 null */
  candidate_id: string | null
  /** 补充说明（如改编范围）；无则 null */
  note: string | null
}

export interface ReviewQuestionSourceRef {
  /** resource=课件/附件片段 / assignment=作业 / web=网络原题 / note=用户备注原文（locator=备注第N行，其余 ID/URL 为 null） */
  kind: 'resource' | 'assignment' | 'web' | 'note'
  resource_id: string | null
  chunk_id: string | null
  assignment_id: string | null
  /** kind=web 时的原题页 URL（非搜索页）；其余为 null */
  url: string | null
  site_name: string | null
  /** 页/幻灯片/题号定位说明；缺失为 null（客户端不得显示假链接） */
  locator: string | null
}

export interface ReviewQuestionValidation {
  /** valid=通过校验 / repaired=经有限修复 / low_confidence=低置信度需人工检查 / incomplete=缺答案或缺解析等 */
  status: 'valid' | 'repaired' | 'low_confidence' | 'incomplete'
  messages: string[]
}

export interface ReviewQuestion {
  /** 题目 ID（qst_ 前缀） */
  id: string
  /** 卷内题号（1 起，连续） */
  ordinal: number
  question_type: ReviewQuestionType
  stem: string
  /** 选择题选项（键 A/B/C/D，顺序即展示顺序）；非选择题为空数组 */
  options: ReviewQuestionOption[]
  /** 标准答案（选择题为选项键如 "A"/"AB"）；缺失由校验标 incomplete，不留空 */
  answer: string
  explanation: string
  /** 评分要点/步骤（主观题与计算题；客观题可为空数组） */
  grading_points: string[]
  knowledge_points: string[]
  difficulty: ReviewDifficulty
  points: number
  origin: ReviewQuestionOrigin
  validation: ReviewQuestionValidation
  /** 来源引用（至少一项；kind=resource 时须存在于本卷材料快照） */
  source_refs: ReviewQuestionSourceRef[]
}

// ---------- 蓝图（PaperBlueprint） ----------

export interface BlueprintEvidence {
  /** courseware=课件正文 / homework=作业（正文或附件）/ note=用户备注 */
  kind: 'courseware' | 'homework' | 'note'
  resource_id: string | null
  chunk_id: string | null
  assignment_id: string | null
  /** kind=note 时指向备注原文的子串；不得编造 */
  note_ref: string | null
}

export interface BlueprintKnowledgePoint {
  name: string
  /** 权重 0–1；无法估计为 null */
  weight: number | null
  evidence: BlueprintEvidence[]
}

export interface BlueprintQuestionSlot {
  slot_id: string
  type: ReviewQuestionType
  count: number
  points_each: number
  /** null=由引擎推断 */
  difficulty: ReviewDifficulty | null
  /** 该槽位的来源计划；最终以每题 origin 为准 */
  source_plan: 'ai' | 'bank' | 'hybrid'
}

export interface ReviewBlueprintSection {
  id: string
  title: string
  knowledge_points: BlueprintKnowledgePoint[]
  question_slots: BlueprintQuestionSlot[]
}

export interface ReviewBlueprintTypeQuota {
  type: ReviewQuestionType
  count: number
  points_total: number
}

export interface ReviewBlueprintDifficultyQuota {
  easy: number
  medium: number
  hard: number
}

export interface ReviewBlueprintGap {
  /** 缺口类别（开放集合，如 material_missing / constraint_unsatisfiable / low_coverage） */
  kind: string
  message: string
  knowledge_point: string | null
}

export interface ReviewBlueprint {
  sections: ReviewBlueprintSection[]
  type_quotas: ReviewBlueprintTypeQuota[]
  difficulty_quota: ReviewBlueprintDifficultyQuota
  total_points: number
  estimated_minutes: number
  gaps: ReviewBlueprintGap[]
}

// ---------- 试卷（ReviewPaper） ----------

export interface ReviewPaperWarning {
  code: string
  message: string
  question_ids: string[]
  details: Record<string, unknown> | null
}

export interface ReviewPaperSummary {
  question_count: number
  total_points: number
  estimated_minutes: number
  /** 题型 → 题目数（键为 question_type 值） */
  type_counts: Partial<Record<ReviewQuestionType, number>>
  /** 难度 → 题目数（键为 easy/medium/hard） */
  difficulty_counts: Partial<Record<ReviewDifficulty, number>>
  /** 来源 → 题目数（键为 ai/bank/adapted_bank） */
  origin_counts: Partial<Record<'ai' | 'bank' | 'adapted_bank', number>>
}

export interface ReviewProviderSnapshot {
  /** bank 模式未用模型时各字段为 null */
  config_id: string | null
  name: string | null
  protocol: ProviderProtocol | null
  base_url: string | null
  model: string | null
}

export type ReviewRevisionReason = 'initial' | 'regenerate' | 'revise'

export interface ReviewPaper {
  /** 试卷稳定 ID（pap_ 前缀）；修订不改变 */
  id: string
  course_id: string
  /** 版本号（从 1 递增；历史版本可读） */
  revision: number
  /** initial=首次生成 / regenerate=原参数重做 / revise=修改备注或约束后重做 */
  revision_reason: ReviewRevisionReason
  /** 修订所基于的上一版 revision；首版为 null */
  based_on_revision: number | null
  /** 用户可读标题（不以内部 ID 兜底） */
  title: string
  created_at: string
  mode: ReviewMode
  constraints: ReviewConstraints
  provider: ReviewProviderSnapshot
  engine_version: string
  /** 本版材料与备注快照（含实际发送正文原文） */
  snapshot: MaterialSnapshot
  blueprint: ReviewBlueprint
  questions: ReviewQuestion[]
  warnings: ReviewPaperWarning[]
  summary: ReviewPaperSummary
}

/** GET /review-papers 的 items 为该卷最新 revision 的完整内容（契约如此），列表页只读所需字段 */
export type ReviewPaperListPage = CursorPage<ReviewPaper>

// ---------- 修订历史（GET/POST /review-papers/{id}/revisions） ----------

export interface ReviewPaperRevision {
  paper_id: string
  revision: number
  revision_reason: ReviewRevisionReason
  based_on_revision: number | null
  title: string
  created_at: string
  mode: ReviewMode
  engine_version: string
  /** 该版材料快照的 fingerprint（与快照一致） */
  fingerprint: string
  provider: ReviewProviderSnapshot
  summary: ReviewPaperSummary
}

export type ReviewPaperRevisionListPage = CursorPage<ReviewPaperRevision>

// ---------- 导出（GET /review-papers/{id}/export?format=...） ----------

export type ReviewExportFormat = 'markdown' | 'student' | 'answers'

/** 文本导出结果（服务端直接返回文本；filename 取自 Content-Disposition 中文文件名） */
export interface ReviewTextExport {
  text: string
  filename: string | null
  content_type: string | null
}

// ---------- 题库能力（GET /questionbanks/capabilities） ----------

export type QuestionbankSourceKind = 'search_service' | 'questionbank_api' | 'user_url'

export type QuestionbankFormulaSupport = 'none' | 'text' | 'latex' | 'unknown'

export interface QuestionbankCapabilitySource {
  id: string
  name: string
  kind: QuestionbankSourceKind
  /** 是否已配置可用；未配置不得显示为"已连接" */
  configured: boolean
  requires_config: boolean
  searchable: boolean
  fetchable: boolean
  answer_available: boolean
  formula_support: QuestionbankFormulaSupport
  notes: string | null
}

export interface QuestionbankCapabilities {
  sources: QuestionbankCapabilitySource[]
}

// ---------- 后台任务（复习卷） ----------

export type ReviewJobPhase =
  | 'materials'
  | 'blueprint'
  | 'bank_search'
  | 'generation'
  | 'validation'
  | 'saving'

/** 复习卷生成任务：阶段由服务端在 job.phase 给出（开放集合）；缺失时界面按进度估计（标注） */
export interface ReviewJob extends Job {
  phase: ReviewJobPhase | string | null
}
