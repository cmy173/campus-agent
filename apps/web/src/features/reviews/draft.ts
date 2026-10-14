import type {
  ReviewAnswerDisplay,
  ReviewAssignmentSelection,
  ReviewDifficultyPreference,
  ReviewMode,
  ReviewPaper,
  ReviewQuestionTypeConstraint,
  ReviewResourceSelection,
} from '../../types/review'

/**
 * 出卷表单草稿：仅保存非密钥表单状态（含备注原文）到本机 localStorage。
 * 不包含任何密钥/令牌；备注与中文、换行原样保存。
 * 形状对齐 CR008 契约：资源选择用 chunk_ids（片段 ID），作业选择为 {assignment_id, include_attachments}。
 */
export interface ReviewDraftState {
  title: string
  mode: ReviewMode
  resources: ReviewResourceSelection[]
  assignments: ReviewAssignmentSelection[]
  question_types: ReviewQuestionTypeConstraint[]
  /** 难度偏好；null=由引擎按材料推断 */
  difficulty: ReviewDifficultyPreference | null
  total_points: number | null
  duration_minutes: number | null
  answer_display: ReviewAnswerDisplay
  provider_config_id: string | null
  notes: string
}

export function defaultDraft(): ReviewDraftState {
  return {
    title: '',
    mode: 'ai',
    resources: [],
    assignments: [],
    question_types: [
      { type: 'single_choice', count: 5, points_each: null },
      { type: 'short_answer', count: 2, points_each: null },
    ],
    difficulty: 'mixed',
    total_points: null,
    duration_minutes: null,
    answer_display: 'separate',
    provider_config_id: null,
    notes: '',
  }
}

/** 草稿键 v2：CR008 形状；旧 v1 形状不兼容，直接弃用不再读取（避免把旧字段带进新请求） */
const DRAFT_PREFIX = 'review-draft-v2:'
const ACTIVE_JOB_PREFIX = 'review-active-job-v1:'

export function loadReviewDraft(courseId: string): ReviewDraftState | null {
  try {
    const raw = localStorage.getItem(`${DRAFT_PREFIX}${courseId}`)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<ReviewDraftState>
    const base = defaultDraft()
    return {
      ...base,
      title: typeof parsed.title === 'string' ? parsed.title : base.title,
      mode: parsed.mode === 'bank' || parsed.mode === 'hybrid' ? parsed.mode : 'ai',
      resources: Array.isArray(parsed.resources) ? parsed.resources : [],
      assignments: Array.isArray(parsed.assignments) ? parsed.assignments : [],
      question_types:
        Array.isArray(parsed.question_types) && parsed.question_types.length > 0
          ? parsed.question_types
          : base.question_types,
      difficulty: parsed.difficulty === undefined ? base.difficulty : parsed.difficulty,
      total_points: typeof parsed.total_points === 'number' ? parsed.total_points : null,
      duration_minutes: typeof parsed.duration_minutes === 'number' ? parsed.duration_minutes : null,
      answer_display: parsed.answer_display === 'inline' ? 'inline' : 'separate',
      provider_config_id: typeof parsed.provider_config_id === 'string' ? parsed.provider_config_id : null,
      notes: typeof parsed.notes === 'string' ? parsed.notes : '',
    }
  } catch {
    return null
  }
}

export function saveReviewDraft(courseId: string, draft: ReviewDraftState): void {
  try {
    const {
      title,
      mode,
      resources,
      assignments,
      question_types,
      difficulty,
      total_points,
      duration_minutes,
      answer_display,
      provider_config_id,
      notes,
    } = draft
    localStorage.setItem(
      `${DRAFT_PREFIX}${courseId}`,
      JSON.stringify({
        title,
        mode,
        resources,
        assignments,
        question_types,
        difficulty,
        total_points,
        duration_minutes,
        answer_display,
        provider_config_id,
        notes,
      }),
    )
  } catch {
    // 存储不可用（隐私模式等）时静默降级：表单仍可用，仅不持久化
  }
}

export function clearReviewDraft(courseId: string): void {
  try {
    localStorage.removeItem(`${DRAFT_PREFIX}${courseId}`)
  } catch {
    // 同上
  }
}

/**
 * 幂等键代数（按课程）：取消/失败终态后代数+1 并持久化，
 * 使刷新后再生成同内容不会重放已取消/失败的旧任务（成功终态不变，保持重放语义）。
 */
const GEN_NONCE_PREFIX = 'review-gen-nonce-v1:'

export function loadGenNonce(courseId: string): number {
  try {
    const raw = localStorage.getItem(`${GEN_NONCE_PREFIX}${courseId}`)
    if (raw === null) return 0
    const parsed = Number(raw)
    return Number.isInteger(parsed) && parsed >= 0 ? parsed : 0
  } catch {
    return 0
  }
}

export function saveGenNonce(courseId: string, nonce: number): void {
  try {
    localStorage.setItem(`${GEN_NONCE_PREFIX}${courseId}`, String(nonce))
  } catch {
    // 同上：存储不可用时静默降级（仅丢失跨刷新放行能力）
  }
}

/** 进行中生成作业的恢复锚点（作业 ID 与模式；非密钥） */
export interface ActiveReviewJob {
  jobId: string
  mode: ReviewMode
}

export function loadActiveReviewJob(courseId: string): ActiveReviewJob | null {
  try {
    const raw = localStorage.getItem(`${ACTIVE_JOB_PREFIX}${courseId}`)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<ActiveReviewJob>
    if (typeof parsed.jobId !== 'string' || !parsed.jobId) return null
    const mode = parsed.mode === 'bank' || parsed.mode === 'hybrid' ? parsed.mode : 'ai'
    return { jobId: parsed.jobId, mode }
  } catch {
    return null
  }
}

export function saveActiveReviewJob(courseId: string, job: ActiveReviewJob): void {
  try {
    localStorage.setItem(`${ACTIVE_JOB_PREFIX}${courseId}`, JSON.stringify(job))
  } catch {
    // 同上
  }
}

export function clearActiveReviewJob(courseId: string): void {
  try {
    localStorage.removeItem(`${ACTIVE_JOB_PREFIX}${courseId}`)
  } catch {
    // 同上
  }
}

/**
 * 由既有试卷回填草稿（重做/修订：保留旧卷，生成新修订）。
 * 直接复用卷内约束与材料快照原文，不猜测：缺失材料不在快照 items 中，不会被回填勾选。
 */
export function draftFromPaper(paper: ReviewPaper): ReviewDraftState {
  const resources: ReviewResourceSelection[] = []
  const assignmentIds: string[] = []
  const attachmentsByAssignment = new Set<string>()
  for (const item of paper.snapshot.items) {
    if (item.kind === 'resource_text' && item.resource_id) {
      resources.push({
        resource_id: item.resource_id,
        version: item.version,
        chunk_ids: item.chunk_ids.length > 0 ? [...item.chunk_ids] : null,
      })
    } else if (item.kind === 'attachment_text' && item.assignment_id) {
      attachmentsByAssignment.add(item.assignment_id)
    } else if (item.kind === 'assignment_text' && item.assignment_id && !assignmentIds.includes(item.assignment_id)) {
      assignmentIds.push(item.assignment_id)
    }
  }
  return {
    title: paper.title,
    mode: paper.mode,
    resources,
    assignments: assignmentIds.map((id) => ({
      assignment_id: id,
      include_attachments: attachmentsByAssignment.has(id),
    })),
    question_types: paper.constraints.question_types.map((q) => ({ ...q })),
    difficulty: paper.constraints.difficulty,
    total_points: paper.constraints.total_points,
    duration_minutes: paper.constraints.duration_minutes,
    answer_display: paper.constraints.answer_display,
    provider_config_id: paper.provider.config_id,
    notes: paper.snapshot.notes.text,
  }
}
