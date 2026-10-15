import type {
  MaterialUnavailableReason,
  ReviewDifficulty,
  ReviewDifficultyPreference,
  ReviewExportFormat,
  ReviewJobPhase,
  ReviewMode,
  ReviewPaper,
  ReviewQuestionType,
  ReviewQuestionValidation,
  ReviewRevisionReason,
  SnapshotItem,
} from '../types/review'
import type { Locator } from '../types/contract'

// ---------- 展示文案 ----------

export const REVIEW_MODE_LABELS: Record<ReviewMode, string> = {
  ai: 'AI 生成',
  bank: '网上题库',
  hybrid: '混合（题库 + AI）',
}

export const REVIEW_QUESTION_TYPE_LABELS: Record<ReviewQuestionType, string> = {
  single_choice: '单选题',
  multiple_choice: '多选题',
  true_false: '判断题',
  fill_blank: '填空题',
  calculation: '计算题',
  short_answer: '简答题',
  proof: '证明题',
}

export const REVIEW_QUESTION_TYPES: ReviewQuestionType[] = [
  'single_choice',
  'multiple_choice',
  'true_false',
  'fill_blank',
  'calculation',
  'short_answer',
  'proof',
]

export const REVIEW_DIFFICULTY_LABELS: Record<ReviewDifficulty, string> = {
  easy: '简单',
  medium: '中等',
  hard: '较难',
}

export const REVIEW_DIFFICULTY_PREFERENCE_LABELS: Record<ReviewDifficultyPreference, string> = {
  easy: '简单',
  medium: '中等',
  hard: '较难',
  mixed: '混合',
}

export const REVIEW_ORIGIN_LABELS: Record<'ai' | 'bank' | 'adapted_bank', string> = {
  ai: 'AI 生成',
  bank: '网络原题',
  adapted_bank: '改编题（保留出处）',
}

export const REVIEW_ORIGIN_COLORS: Record<'ai' | 'bank' | 'adapted_bank', string> = {
  ai: 'blue',
  bank: 'green',
  adapted_bank: 'purple',
}

export const REVIEW_VALIDATION_LABELS: Record<ReviewQuestionValidation['status'], string> = {
  valid: '校验通过',
  repaired: '修复后通过',
  low_confidence: '低置信度',
  incomplete: '不完整',
}

export const REVIEW_VALIDATION_COLORS: Record<ReviewQuestionValidation['status'], string> = {
  valid: 'green',
  repaired: 'gold',
  low_confidence: 'orange',
  incomplete: 'red',
}

export const REVIEW_REVISION_REASON_LABELS: Record<ReviewRevisionReason, string> = {
  initial: '首次生成',
  regenerate: '原参数重做',
  revise: '修改后重做',
}

/** 材料不可用原因的短标签（详情建议见 REVIEW_MATERIAL_REASON_ADVICE，文案与后端一致） */
export const REVIEW_MATERIAL_REASON_LABELS: Record<MaterialUnavailableReason, string> = {
  not_parsed: '尚未解析',
  parse_failed: '解析失败',
  type_skipped: '该类型不参与解析',
  no_text: '解析产物无可读文本',
  chunk_missing: '所选片段不存在',
  attachment_absent: '作业附件缺失',
  not_accessible: '权限受限（仅预览）',
}

/** 处理建议（与 B 侧 _UNUSABLE_MESSAGES 同文案；界面如实转述，不弱化） */
export const REVIEW_MATERIAL_REASON_ADVICE: Record<MaterialUnavailableReason, string> = {
  not_parsed: '尚未解析；可先对该文件执行解析后重试。',
  parse_failed: '解析失败；可重新解析该文件后重试。',
  type_skipped: '该类型文件不参与正文解析（如压缩包）。',
  no_text: '解析产物无可读文本（如扫描页）。',
  chunk_missing: '所选片段不存在或不属于所选版本；请重新选择片段。',
  attachment_absent: '作业附件缺失或未获取；可重新同步该作业。',
  not_accessible: '平台仅预览、无下载权限，无法获取正文；可在平台内查看或手动上传可读版本。',
}

export function reviewMaterialReasonLabel(reason: MaterialUnavailableReason | null): string {
  return reason ? REVIEW_MATERIAL_REASON_LABELS[reason] : ''
}

export const REVIEW_EXPORT_FORMAT_LABELS: Record<ReviewExportFormat, string> = {
  markdown: 'Markdown 文档（题目+答案+解析+出处）',
  student: '学生版（打印用，不含答案与解析）',
  answers: '答案版（打印用，含答案、解析与评分点）',
}

/** 快照材料项类别（材料快照表与发送预览共用） */
export const REVIEW_SNAPSHOT_KIND_LABELS: Record<SnapshotItem['kind'], string> = {
  resource_text: '课件/资料',
  attachment_text: '作业附件',
  assignment_text: '作业',
}

export const REVIEW_SNAPSHOT_KIND_COLORS: Record<SnapshotItem['kind'], string> = {
  resource_text: 'cyan',
  attachment_text: 'geekblue',
  assignment_text: 'gold',
}

// ---------- 生成阶段 ----------

export interface ReviewPhaseDef {
  key: ReviewJobPhase
  label: string
  /** 该阶段结束时的进度上界（0–1） */
  until: number
}

/**
 * 界面进度分段（CR008 job.phase 六段：materials→blueprint→bank_search→generation→validation→saving）。
 * 不同模式阶段不同：AI 无题库检索；纯题库无模型生成。
 * 服务端返回 job.phase 时以其为准；缺失时按进度与模式估计并标注。
 */
export function reviewPhasesForMode(mode: ReviewMode): ReviewPhaseDef[] {
  if (mode === 'bank') {
    return [
      { key: 'materials', label: '资料准备', until: 0.15 },
      { key: 'blueprint', label: '出卷蓝图', until: 0.25 },
      { key: 'bank_search', label: '题库检索', until: 0.7 },
      { key: 'generation', label: '组卷', until: 0.8 },
      { key: 'validation', label: '校验', until: 0.92 },
      { key: 'saving', label: '保存', until: 1 },
    ]
  }
  if (mode === 'hybrid') {
    return [
      { key: 'materials', label: '资料准备', until: 0.12 },
      { key: 'blueprint', label: '出卷蓝图', until: 0.2 },
      { key: 'bank_search', label: '题库检索', until: 0.38 },
      { key: 'generation', label: '模型生成', until: 0.75 },
      { key: 'validation', label: '校验', until: 0.9 },
      { key: 'saving', label: '保存', until: 1 },
    ]
  }
  return [
    { key: 'materials', label: '资料准备', until: 0.15 },
    { key: 'blueprint', label: '出卷蓝图', until: 0.25 },
    { key: 'generation', label: '模型生成', until: 0.75 },
    { key: 'validation', label: '校验', until: 0.9 },
    { key: 'saving', label: '保存', until: 1 },
  ]
}

export const REVIEW_PHASE_LABELS: Record<ReviewJobPhase, string> = {
  materials: '资料准备',
  blueprint: '出卷蓝图',
  bank_search: '题库检索',
  generation: '模型生成',
  validation: '校验',
  saving: '保存',
}

export function reviewPhaseLabel(phase: string): string {
  return REVIEW_PHASE_LABELS[phase as ReviewJobPhase] ?? phase
}

export function reviewPhaseFor(progress: number | null, mode: ReviewMode = 'ai'): ReviewJobPhase | null {
  if (progress === null) return null
  const clamped = Math.min(Math.max(progress, 0), 1)
  return reviewPhasesForMode(mode).find((p) => clamped <= p.until)?.key ?? 'saving'
}

// ---------- 其他 ----------

/** 服务端备注上限（契约 maxLength 20000）；客户端阻止超限发送并提示，不静默截断 */
export const REVIEW_NOTES_LIMIT = 20000

export interface ReviewQuestionTypeOption {
  value: ReviewQuestionType
  label: string
}

export const REVIEW_QUESTION_TYPE_OPTIONS: ReviewQuestionTypeOption[] = REVIEW_QUESTION_TYPES.map(
  (value) => ({ value, label: REVIEW_QUESTION_TYPE_LABELS[value] }),
)

export const REVIEW_PROTOCOL_OPTIONS: Array<{ value: string; label: string; supported: boolean }> = [
  { value: 'openai_chat_completions', label: 'OpenAI 兼容（Chat Completions）', supported: true },
  { value: 'anthropic_messages', label: 'Anthropic Messages（暂不支持）', supported: false },
  { value: 'gemini_generate', label: 'Google Gemini（暂不支持）', supported: false },
]

export function reviewModeLabel(mode: ReviewMode): string {
  return REVIEW_MODE_LABELS[mode] ?? mode
}

export function reviewHostOf(baseUrl: string): string {
  try {
    return new URL(baseUrl).host
  } catch {
    return baseUrl
  }
}

/** 材料 ID → 标题（由页面从本卷快照 items/missing 构建）。解析不到时显示原始 ID，不编造标题。 */
export type MaterialTitleLookup = (id: string) => string | undefined

/** 从本卷快照构建 ID→标题：items 含资源/作业正文，missing 兜底（含未纳入材料） */
export function materialTitlesOf(paper: ReviewPaper): MaterialTitleLookup {
  const map = new Map<string, string>()
  // 先到先得：作业附件条目同时携带父作业 id，不能覆盖作业正文条目自身的标题
  for (const item of paper.snapshot.items) {
    if (item.resource_id && !map.has(item.resource_id)) map.set(item.resource_id, item.title)
    if (item.assignment_id && !map.has(item.assignment_id)) map.set(item.assignment_id, item.title)
  }
  for (const miss of paper.snapshot.missing) {
    if (miss.resource_id && !map.has(miss.resource_id)) map.set(miss.resource_id, miss.title)
    if (miss.assignment_id && !map.has(miss.assignment_id)) map.set(miss.assignment_id, miss.title)
  }
  return (id) => map.get(id)
}

/** 材料定位的人读文案（页码/幻灯/时间戳；缺失返回空串，不编造位置） */
export function reviewLocatorText(locator: Locator | null): string {
  if (!locator) return ''
  if (locator.kind === 'page') return `第 ${locator.page} 页`
  if (locator.kind === 'slide') return `第 ${locator.slide} 张幻灯片`
  if (locator.kind === 'timestamp') return `回放 ${locator.start_seconds}s 起`
  return '正文片段'
}
