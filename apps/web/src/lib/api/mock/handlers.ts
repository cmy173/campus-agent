import dayjs from 'dayjs'
import type {
  Assignment,
  Availability,
  Connection,
  Course,
  CursorPage,
  FeedbackRecord,
  FeedbackRequest,
  Job,
  LearningTask,
  Plan,
  Resource,
  ResourceVersion,
  SearchHit,
  SourceRef,
} from '../../../types/contract'
import { DEFAULT_TIMEZONE, toApiIso } from '../../time'
import { validateAvailability } from '../../validation'
import { ApiError, newRequestId } from '../http'
import { createJob, getDb, generateChunks, importTypeOk, IMPORT_MAX_BYTES, kindFromName, revisionStr, tickJobs, type MockDb } from './db'
import { runBuildTasks } from './buildTasks'
import { generatePreview } from './engine'
import {
  clearProviderKey,
  createProvider,
  createReviewRevision,
  deleteProvider,
  exportReviewPaper,
  generateReview,
  getProvider,
  getQuestionbankCapabilities,
  getReviewPaper,
  listProviders,
  listReviewMaterials,
  listReviewPapers,
  listReviewRevisions,
  preflightReview,
  retryReviewJob,
  testProvider,
  updateProvider,
} from './review'

const IS_TEST = import.meta.env.MODE === 'test'

export interface MockRequestInput {
  method: string
  path: string
  query?: Record<string, string | number | boolean | undefined>
  body?: unknown
  idempotencyKey?: string
  ifMatch?: string
  signal?: AbortSignal
}

const RESOURCE_KINDS: Resource['kind'][] = [
  'slides', 'handout', 'syllabus', 'scan', 'document', 'link', 'other',
]
const TASK_TYPES: LearningTask['type'][] = [
  'study', 'reading', 'watching', 'assignment', 'practice', 'review', 'other',
]
const TASK_STATUSES: LearningTask['status'][] = ['pending', 'in_progress', 'completed', 'cancelled']
const FEEDBACK_OUTCOMES = ['completed', 'partial', 'skipped'] as const

function fail(
  status: number,
  code: string,
  message: string,
  options?: { retryable?: boolean; details?: Record<string, unknown> | null },
): never {
  throw new ApiError(status, {
    code,
    message,
    retryable: options?.retryable ?? false,
    details: options?.details ?? null,
    request_id: newRequestId(),
  })
}

function okPage<T>(items: T[]): CursorPage<T> {
  return { items, next_cursor: null }
}

function sleep(ms: number): Promise<void> {
  if (IS_TEST || ms <= 0) return Promise.resolve()
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function latencyFor(method: string, path: string): number {
  if (path === '/plans/preview') return 650
  if (path === '/imports') return 300
  if (method !== 'GET') return 220
  return 140
}

export async function mockRequest<T>(req: MockRequestInput): Promise<T> {
  const db = getDb()
  tickJobs(db)
  await sleep(latencyFor(req.method, req.path))
  const result = route(db, req)
  return structuredClone(result) as T
}

function nowIso(db: MockDb): string {
  return toApiIso(dayjs(), db.availability.timezone || DEFAULT_TIMEZONE)
}

function idempotent<T>(db: MockDb, key: string | undefined, produce: () => T): T {
  if (key && db.idempotency.has(key)) return db.idempotency.get(key) as T
  const result = produce()
  if (key) db.idempotency.set(key, result)
  return result
}

function asBool(value: unknown, fallback = false): boolean {
  if (value === undefined) return fallback
  return value === true || value === 'true' || value === 1 || value === '1'
}

function route(db: MockDb, req: MockRequestInput): unknown {
  const { method, path } = req
  const query = req.query ?? {}
  let match: RegExpMatchArray | null

  // ---------- 课程 ----------
  if (method === 'GET' && path === '/courses') {
    const includeArchived = asBool(query.include_archived)
    return okPage(db.courses.filter((c) => includeArchived || c.archived_at === null))
  }
  if (method === 'POST' && path === '/courses') return handleCreateCourse(db, req.body)
  if ((match = path.match(/^\/courses\/([^/]+)\/source-refs$/)) && method === 'POST') {
    return handleAddSourceRef(db, match[1], req.body)
  }
  if ((match = path.match(/^\/courses\/([^/]+)\/resources$/)) && method === 'GET') {
    requireCourse(db, match[1])
    const includeArchived = asBool(query.include_archived)
    return okPage(
      db.resources.filter((r) => r.course_id === match![1] && (includeArchived || r.archived_at === null)),
    )
  }
  if ((match = path.match(/^\/courses\/([^/]+)\/assignments$/)) && method === 'GET') {
    requireCourse(db, match[1])
    const includeArchived = asBool(query.include_archived)
    return okPage(
      db.assignments.filter(
        (a) => a.course_id === match![1] && (includeArchived || a.archived_at === null),
      ),
    )
  }
  if ((match = path.match(/^\/courses\/([^/]+)\/recordings$/)) && method === 'GET') {
    requireCourse(db, match[1])
    return okPage(db.recordings.filter((r) => r.course_id === match![1]))
  }
  if ((match = path.match(/^\/courses\/([^/]+)$/)) && method === 'GET') {
    return requireCourse(db, match[1])
  }

  // ---------- 资源 ----------
  if ((match = path.match(/^\/resources\/([^/]+)\/versions$/)) && method === 'GET') {
    const resource = requireResource(db, match[1])
    const versions = db.resourceVersions
      .filter((v) => v.resource_id === resource.id)
      .sort((a, b) => b.version - a.version)
    return okPage(versions)
  }
  if ((match = path.match(/^\/resources\/([^/]+)\/chunks$/)) && method === 'GET') {
    const resource = requireResource(db, match[1])
    const chunks = db.chunks
      .filter((c) => c.resource_id === resource.id && c.resource_version === resource.version)
      .sort((a, b) => a.ordinal - b.ordinal)
    return okPage(chunks)
  }
  if ((match = path.match(/^\/resources\/([^/]+)\/reparse$/)) && method === 'POST') {
    return handleReparse(db, match[1])
  }
  if ((match = path.match(/^\/resources\/([^/]+)$/)) && method === 'GET') {
    return requireResource(db, match[1])
  }

  // ---------- 作业 ----------
  if (method === 'GET' && path === '/assignments') {
    const includeArchived = asBool(query.include_archived)
    const dueBefore = query.due_before !== undefined ? Date.parse(String(query.due_before)) : null
    const dueAfter = query.due_after !== undefined ? Date.parse(String(query.due_after)) : null
    const items = db.assignments
      .filter((a) => {
        if (!includeArchived && a.archived_at !== null) return false
        if (dueBefore !== null && (a.due_at === null || Date.parse(a.due_at) > dueBefore)) return false
        if (dueAfter !== null && (a.due_at === null || Date.parse(a.due_at) < dueAfter)) return false
        return true
      })
      .sort((a, b) => {
        const da = a.due_at ? Date.parse(a.due_at) : Infinity
        const db2 = b.due_at ? Date.parse(b.due_at) : Infinity
        if (da !== db2) return da - db2
        return a.title.localeCompare(b.title)
      })
    return okPage(items)
  }
  if (method === 'POST' && path === '/assignments') return handleCreateAssignment(db, req.body)
  if ((match = path.match(/^\/assignments\/([^/]+)$/)) && method === 'GET') {
    return requireAssignment(db, match[1])
  }
  if ((match = path.match(/^\/assignments\/([^/]+)$/)) && method === 'PATCH') {
    return handleAssignmentPatch(db, match[1], req.body, req.ifMatch)
  }

  // ---------- 学习任务 ----------
  if (method === 'GET' && path === '/tasks') return handleTaskList(db, query)
  if (method === 'POST' && path === '/tasks') return handleCreateTask(db, req.body)
  if (method === 'POST' && path === '/tasks/build') return handleBuildTasks(db, req.body)
  if ((match = path.match(/^\/tasks\/([^/]+)\/lock$/)) && method === 'POST') {
    return handleLockTask(db, match[1], req.body)
  }
  if ((match = path.match(/^\/tasks\/([^/]+)\/feedback$/)) && method === 'POST') {
    return idempotent(db, req.idempotencyKey, () => handleFeedback(db, match![1], req.body))
  }
  if ((match = path.match(/^\/tasks\/([^/]+)$/)) && method === 'GET') {
    // CR004 补录端点：与后端同语义——不筛归档，未找到 404
    return requireTask(db, match[1])
  }
  if ((match = path.match(/^\/tasks\/([^/]+)$/)) && method === 'PATCH') {
    return handleTaskPatch(db, match[1], req.body, req.ifMatch)
  }
  if ((match = path.match(/^\/tasks\/([^/]+)$/)) && method === 'DELETE') {
    const task = requireTask(db, match[1])
    task.archived_at = nowIso(db)
    task.updated_at = nowIso(db)
    db.inputRevision += 1
    return undefined
  }

  // ---------- 可用时间 ----------
  if (path === '/preferences/availability') {
    if (method === 'GET') return db.availability
    if (method === 'PUT') return handleSaveAvailability(db, req.body)
  }

  // ---------- 计划 ----------
  if (method === 'POST' && path === '/plans/preview') return handlePreview(db, req.body)
  if (method === 'GET' && path === '/plans') {
    const statusFilter = query.status !== undefined ? String(query.status) : null
    const all = [
      ...(db.activePlan ? [db.activePlan] : []),
      ...Array.from(db.previews.values()),
      ...db.planHistory,
    ]
      .filter((p) => statusFilter === null || p.status === statusFilter)
      .sort((a, b) => b.version - a.version)
    return okPage(all)
  }
  if (method === 'GET' && path === '/plans/current') {
    return { plan: db.activePlan, current_input_revision: revisionStr(db) }
  }
  if ((match = path.match(/^\/plans\/([^/]+)\/apply$/)) && method === 'POST') {
    return handleApply(db, match[1])
  }
  if ((match = path.match(/^\/plans\/([^/]+)\/revoke$/)) && method === 'POST') {
    return handleRevoke(db, match[1])
  }
  if ((match = path.match(/^\/plans\/([^/]+)$/)) && method === 'GET') {
    return requirePlan(db, match[1])
  }

  // ---------- 导入 ----------
  if (method === 'POST' && path === '/imports') return handleImport(db, req)

  // ---------- 搜索 ----------
  if (method === 'GET' && path === '/search') return handleSearch(db, query)

  // ---------- 连接 ----------
  if (method === 'GET' && path === '/connections') return okPage(db.connections)
  if ((match = path.match(/^\/connections\/([^/]+)\/capabilities$/)) && method === 'GET') {
    requireConnection(db, match[1])
    const caps = db.capabilities.get(match[1])
    if (!caps) fail(404, 'NOT_FOUND', '该连接的适配器能力尚未登记')
    return caps
  }
  if ((match = path.match(/^\/connections\/([^/]+)\/sync$/)) && method === 'POST') {
    return handleSync(db, match[1], req.idempotencyKey)
  }
  if ((match = path.match(/^\/connections\/([^/]+)\/disable$/)) && method === 'POST') {
    return handleDisable(db, match[1])
  }
  if ((match = path.match(/^\/connections\/([^/]+)\/enable$/)) && method === 'POST') {
    return handleEnable(db, match[1])
  }
  if ((match = path.match(/^\/connections\/([^/]+)\/check$/)) && method === 'POST') {
    return handleCheck(db, match[1])
  }
  if ((match = path.match(/^\/connections\/([^/]+)$/)) && method === 'GET') {
    return requireConnection(db, match[1])
  }

  // ---------- 后台任务 ----------
  if (method === 'GET' && path === '/jobs') {
    const statusFilter = query.status !== undefined ? String(query.status) : null
    const typeFilter = query.type !== undefined ? String(query.type) : null
    const items = db.jobs
      .filter((j) => (statusFilter === null || j.status === statusFilter) && (typeFilter === null || j.type === typeFilter))
      .slice()
      .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at) || b.id.localeCompare(a.id))
    return okPage(items)
  }
  if ((match = path.match(/^\/jobs\/([^/]+)\/retry$/)) && method === 'POST') {
    return idempotent(db, req.idempotencyKey, () => handleRetryJob(db, match![1]))
  }
  if ((match = path.match(/^\/jobs\/([^/]+)\/cancel$/)) && method === 'POST') {
    return handleCancelJob(db, match[1])
  }
  if ((match = path.match(/^\/jobs\/([^/]+)$/)) && method === 'GET') {
    return requireJob(db, match[1])
  }

  // ---------- 导出 ----------
  if (method === 'POST' && path === '/exports') {
    return handleExport(db, req)
  }

  // ---------- AI 服务商设置 ----------
  if (method === 'GET' && path === '/ai/providers') return listProviders(db, query)
  if (method === 'POST' && path === '/ai/providers') return createProvider(db, req.body)
  if ((match = path.match(/^\/ai\/providers\/([^/]+)\/key$/)) && method === 'DELETE') {
    clearProviderKey(db, match[1])
    return undefined
  }
  if ((match = path.match(/^\/ai\/providers\/([^/]+)\/test$/)) && method === 'POST') {
    return testProvider(db, match[1])
  }
  if ((match = path.match(/^\/ai\/providers\/([^/]+)$/)) && method === 'PATCH') {
    return updateProvider(db, match[1], req.body)
  }
  if ((match = path.match(/^\/ai\/providers\/([^/]+)$/)) && method === 'GET') {
    return getProvider(db, match[1])
  }
  if ((match = path.match(/^\/ai\/providers\/([^/]+)$/)) && method === 'DELETE') {
    deleteProvider(db, match[1])
    return undefined
  }

  // ---------- 复习卷 ----------
  if (method === 'GET' && path === '/questionbanks/capabilities') return getQuestionbankCapabilities(db)
  if ((match = path.match(/^\/courses\/([^/]+)\/review-materials$/)) && method === 'GET') {
    return listReviewMaterials(db, match[1], query)
  }
  if (method === 'POST' && path === '/review-papers/preflight') return preflightReview(db, req.body)
  if (method === 'POST' && path === '/review-papers/generate') {
    return generateReview(db, req.body, req.idempotencyKey, req.ifMatch)
  }
  if (method === 'GET' && path === '/review-papers') return listReviewPapers(db, query)
  if ((match = path.match(/^\/review-papers\/([^/]+)\/revisions$/)) && method === 'GET') {
    return listReviewRevisions(db, match[1], query)
  }
  if ((match = path.match(/^\/review-papers\/([^/]+)\/revisions$/)) && method === 'POST') {
    return createReviewRevision(db, match[1], req.body, req.idempotencyKey, req.ifMatch)
  }
  if ((match = path.match(/^\/review-papers\/([^/]+)\/export$/)) && method === 'GET') {
    return exportReviewPaper(db, match[1], query)
  }
  if ((match = path.match(/^\/review-papers\/([^/]+)$/)) && method === 'GET') {
    return getReviewPaper(db, match[1], query.revision)
  }

  // ---------- 受控文件 ----------
  if ((match = path.match(/^\/files\/([^/]+)$/)) && method === 'GET') {
    fail(404, 'NOT_FOUND', '模拟模式不提供文件下载；真实模式由服务端受控文件流返回', {
      details: { mock: true },
    })
  }

  fail(404, 'NOT_FOUND', `接口不存在：${method} ${path}`)
}

// ---------- 查询辅助 ----------

function requireCourse(db: MockDb, courseId: string): Course {
  const course = db.courses.find((c) => c.id === courseId)
  if (!course) fail(404, 'NOT_FOUND', '课程不存在')
  return course
}

function requireResource(db: MockDb, resourceId: string): Resource {
  const resource = db.resources.find((r) => r.id === resourceId)
  if (!resource) fail(404, 'NOT_FOUND', '资料不存在')
  return resource
}

function requireAssignment(db: MockDb, assignmentId: string): Assignment {
  const assignment = db.assignments.find((a) => a.id === assignmentId)
  if (!assignment) fail(404, 'NOT_FOUND', '作业不存在')
  return assignment
}

function requireTask(db: MockDb, taskId: string): LearningTask {
  const task = db.tasks.find((t) => t.id === taskId)
  if (!task) fail(404, 'NOT_FOUND', '学习任务不存在')
  return task
}

function requireConnection(db: MockDb, connId: string): Connection {
  const conn = db.connections.find((c) => c.id === connId)
  if (!conn) fail(404, 'NOT_FOUND', '连接不存在')
  return conn
}

function requireJob(db: MockDb, jobId: string): Job {
  const job = db.jobs.find((j) => j.id === jobId)
  if (!job) fail(404, 'NOT_FOUND', '后台任务不存在')
  return job
}

function requirePlan(db: MockDb, planId: string): Plan {
  const plan =
    (db.activePlan?.id === planId ? db.activePlan : undefined) ??
    db.previews.get(planId) ??
    db.planHistory.find((p) => p.id === planId)
  if (!plan) fail(404, 'NOT_FOUND', '计划不存在')
  return plan
}

// ---------- 课程 ----------

function handleCreateCourse(db: MockDb, rawBody: unknown): Course {
  const body = (rawBody ?? {}) as { title?: string; term?: string | null; teacher?: string | null }
  const title = String(body.title ?? '').trim()
  if (!title) fail(400, 'VALIDATION_FAILED', '课程名称不能为空', { details: { field: 'title' } })
  if (db.courses.some((c) => c.archived_at === null && c.title === title)) {
    fail(409, 'CONFLICT', `已存在同名课程「${title}」`)
  }
  db.seq += 1
  const now = nowIso(db)
  const course: Course = {
    id: `course_${db.seq}`,
    title,
    term: body.term ?? null,
    teacher: body.teacher ?? null,
    source_refs: [],
    archived_at: null,
    created_at: now,
    updated_at: now,
  }
  db.courses.push(course)
  return course
}

function handleAddSourceRef(db: MockDb, courseId: string, rawBody: unknown): Course {
  const course = requireCourse(db, courseId)
  const ref = (rawBody as { source_ref?: SourceRef } | undefined)?.source_ref
  if (!ref || !ref.platform || !ref.external_id) {
    fail(400, 'VALIDATION_FAILED', '来源信息不完整：需要 platform 与 external_id', {
      details: { field: 'source_ref' },
    })
  }
  const owner = db.courses.find(
    (c) =>
      c.id !== courseId &&
      c.archived_at === null &&
      c.source_refs.some((r) => r.platform === ref.platform && r.external_id === ref.external_id),
  )
  if (owner) {
    fail(409, 'CONFLICT', `该平台来源已挂接到课程「${owner.title}」，不能重复挂接`, {
      details: { course_id: owner.id, course_title: owner.title },
    })
  }
  const existing = course.source_refs.find(
    (r) => r.platform === ref.platform && r.external_id === ref.external_id,
  )
  if (!existing) {
    course.source_refs.push(ref)
    course.updated_at = nowIso(db)
  }
  return course
}

// ---------- 作业 ----------

function handleCreateAssignment(db: MockDb, rawBody: unknown): Assignment {
  const body = (rawBody ?? {}) as {
    course_id?: string
    title?: string
    instructions?: string | null
    due_at?: string | null
    due_precision?: Assignment['due_precision']
  }
  if (!body.course_id) fail(400, 'VALIDATION_FAILED', '请选择课程', { details: { field: 'course_id' } })
  requireCourse(db, body.course_id)
  const title = String(body.title ?? '').trim()
  if (!title) fail(400, 'VALIDATION_FAILED', '作业标题不能为空', { details: { field: 'title' } })
  const dueAt = body.due_at ?? null
  const precision = body.due_precision ?? (dueAt ? undefined : 'unknown')
  if (precision === undefined) {
    fail(400, 'VALIDATION_FAILED', '提供截止时间时必须显式给出 due_precision', {
      details: { field: 'due_precision' },
    })
  }
  if (precision === 'unknown' && dueAt !== null) {
    fail(400, 'VALIDATION_FAILED', 'due_precision=unknown 时截止时间必须为空')
  }
  if (precision !== 'unknown' && dueAt === null) {
    fail(400, 'VALIDATION_FAILED', `due_precision=${precision} 时必须提供截止时间`)
  }
  db.seq += 1
  const now = nowIso(db)
  const assignment: Assignment = {
    id: `asg_${db.seq}`,
    course_id: body.course_id,
    title,
    instructions: body.instructions ?? null,
    due_at: dueAt,
    due_precision: precision,
    submission_status: 'unknown',
    fact_source: 'user',
    confidence: null,
    source_ref: null,
    attachment_resource_ids: [],
    revision: 1,
    observed_at: now,
    archived_at: null,
    created_at: now,
    updated_at: now,
  }
  db.assignments.push(assignment)
  return assignment
}

function handleAssignmentPatch(
  db: MockDb,
  assignmentId: string,
  rawBody: unknown,
  ifMatch: string | undefined,
): Assignment {
  const assignment = requireAssignment(db, assignmentId)
  if (ifMatch !== undefined && ifMatch !== String(assignment.revision)) {
    fail(412, 'REVISION_MISMATCH', `作业已被更新（当前 revision=${assignment.revision}），请刷新后重试`, {
      details: { revision: assignment.revision },
    })
  }
  const body = (rawBody ?? {}) as Record<string, unknown>
  let changed = false
  if (body.title !== undefined) {
    const title = String(body.title).trim()
    if (!title) fail(400, 'VALIDATION_FAILED', '标题不能为空')
    assignment.title = title
    changed = true
  }
  if (body.instructions !== undefined) {
    assignment.instructions = body.instructions === null ? null : String(body.instructions)
    changed = true
  }
  if (body.due_at !== undefined) {
    assignment.due_at = body.due_at === null ? null : String(body.due_at)
    changed = true
  }
  if (body.due_precision !== undefined) {
    const precision = String(body.due_precision) as Assignment['due_precision']
    if (!['datetime', 'date', 'unknown'].includes(precision)) {
      fail(400, 'VALIDATION_FAILED', 'due_precision 取值无效')
    }
    assignment.due_precision = precision
    changed = true
  }
  if (body.submission_status !== undefined) {
    const status = String(body.submission_status) as Assignment['submission_status']
    if (!['unknown', 'not_submitted', 'submitted', 'graded'].includes(status)) {
      fail(400, 'VALIDATION_FAILED', 'submission_status 取值无效')
    }
    assignment.submission_status = status
    changed = true
  }
  if (assignment.due_precision === 'unknown' && assignment.due_at !== null) {
    fail(400, 'VALIDATION_FAILED', 'due_precision=unknown 时截止时间必须为空')
  }
  if (changed) {
    assignment.fact_source = 'user'
    assignment.revision += 1
    assignment.updated_at = nowIso(db)
    // PD-3 镜像后端 plan_input 指纹（作业 revision/submission_status 属排程输入）
    db.inputRevision += 1
  }
  return assignment
}

// ---------- 学习任务 ----------

function handleTaskList(db: MockDb, query: Record<string, string | number | boolean | undefined>): CursorPage<LearningTask> {
  const includeArchived = asBool(query.include_archived)
  const courseId = query.course_id !== undefined ? String(query.course_id) : null
  const status = query.status !== undefined ? String(query.status) : null
  const dueBefore = query.due_before !== undefined ? Date.parse(String(query.due_before)) : null

  const items = db.tasks.filter((t) => {
    if (!includeArchived && t.archived_at !== null) return false
    if (courseId !== null && t.course_id !== courseId) return false
    if (status !== null && t.status !== status) return false
    if (dueBefore !== null && (t.due_at === null || Date.parse(t.due_at) > dueBefore)) return false
    return true
  })
  return okPage(items)
}

function handleCreateTask(db: MockDb, rawBody: unknown): LearningTask {
  const body = (rawBody ?? {}) as {
    course_id?: string | null
    type?: LearningTask['type']
    title?: string
    estimated_minutes?: number
    due_at?: string | null
    due_precision?: LearningTask['due_precision']
    prerequisite_ids?: string[]
  }
  const title = String(body.title ?? '').trim()
  if (!title) fail(400, 'VALIDATION_FAILED', '任务标题不能为空', { details: { field: 'title' } })
  const estimated = Number(body.estimated_minutes)
  if (!Number.isInteger(estimated) || estimated < 1) {
    fail(400, 'VALIDATION_FAILED', '预计耗时需为正整数（分钟）', { details: { field: 'estimated_minutes' } })
  }
  if (body.course_id != null) requireCourse(db, body.course_id)
  const type = body.type ?? 'other'
  if (!TASK_TYPES.includes(type)) fail(400, 'VALIDATION_FAILED', '任务类型无效')
  const dueAt = body.due_at ?? null
  const precision = body.due_precision ?? (dueAt ? undefined : 'unknown')
  if (precision === undefined) {
    fail(400, 'VALIDATION_FAILED', '提供截止时间时必须显式给出 due_precision', {
      details: { field: 'due_precision' },
    })
  }
  if (precision === 'unknown' && dueAt !== null) {
    fail(400, 'VALIDATION_FAILED', 'due_precision=unknown 时截止时间必须为空')
  }
  db.seq += 1
  const now = nowIso(db)
  const task: LearningTask = {
    id: `task_${db.seq}`,
    match_key: null,
    course_id: body.course_id ?? null,
    type,
    title,
    estimated_minutes: estimated,
    due_at: dueAt,
    due_precision: precision,
    prerequisite_ids: body.prerequisite_ids ?? [],
    source_refs: [],
    status: 'pending',
    fact_source: 'user',
    revision: 1,
    archived_at: null,
    created_at: now,
    updated_at: now,
  }
  db.tasks.push(task)
  db.inputRevision += 1
  return task
}

/** 由资料/作业/回放生成候选任务（B13）：同范围排队/执行中任务复用，重复触发不重复入队 */
function handleBuildTasks(db: MockDb, rawBody: unknown): { job: Job } {
  const body = (rawBody ?? {}) as { course_id?: string | null }
  const courseId = body.course_id ?? null
  if (courseId !== null) requireCourse(db, courseId)

  const active = db.jobs.find(
    (j) =>
      j.type === 'build_tasks' &&
      (j.status === 'queued' || j.status === 'running') &&
      db.buildScopes.get(j.id) === courseId,
  )
  if (active) return { job: active }

  const job = createJob(db, 'build_tasks', 600, () => {
    const outcome = runBuildTasks(db, courseId)
    if (outcome.created.length > 0 || outcome.updated.length > 0) db.inputRevision += 1
    return {
      status: 'succeeded' as const,
      warnings: outcome.warnings,
      result_refs: [
        ...outcome.created.map((t) => ({ type: 'task' as const, id: t.id })),
        ...outcome.updated.map((t) => ({ type: 'task' as const, id: t.id })),
      ],
    }
  })
  db.buildScopes.set(job.id, courseId)
  return { job }
}

function handleTaskPatch(
  db: MockDb,
  taskId: string,
  rawBody: unknown,
  ifMatch: string | undefined,
): LearningTask {
  const task = requireTask(db, taskId)
  if (ifMatch !== undefined && ifMatch !== String(task.revision)) {
    fail(412, 'REVISION_MISMATCH', `任务已被更新（当前 revision=${task.revision}），请刷新后重试`, {
      details: { revision: task.revision },
    })
  }
  const body = (rawBody ?? {}) as Record<string, unknown>
  let changed = false
  let touchedPlanInput = false

  if (body.title !== undefined) {
    const title = String(body.title).trim()
    if (!title) fail(400, 'VALIDATION_FAILED', '标题不能为空')
    task.title = title
    changed = true
  }
  if (body.course_id !== undefined) {
    if (body.course_id === null) {
      task.course_id = null
    } else {
      requireCourse(db, String(body.course_id))
      task.course_id = String(body.course_id)
    }
    changed = true
    touchedPlanInput = true
  }
  if (body.type !== undefined) {
    const type = String(body.type) as LearningTask['type']
    if (!TASK_TYPES.includes(type)) fail(400, 'VALIDATION_FAILED', '任务类型无效')
    task.type = type
    changed = true
  }
  if (body.estimated_minutes !== undefined) {
    const estimated = Number(body.estimated_minutes)
    if (!Number.isInteger(estimated) || estimated < 1) {
      fail(400, 'VALIDATION_FAILED', '预计耗时需为正整数（分钟）', {
        details: { field: 'estimated_minutes' },
      })
    }
    if (estimated !== task.estimated_minutes) {
      task.estimated_minutes = estimated
      changed = true
      touchedPlanInput = true
    }
  }
  if (body.due_at !== undefined) {
    task.due_at = body.due_at === null ? null : String(body.due_at)
    changed = true
    touchedPlanInput = true
  }
  if (body.due_precision !== undefined) {
    const precision = String(body.due_precision) as LearningTask['due_precision']
    if (!['datetime', 'date', 'unknown'].includes(precision)) {
      fail(400, 'VALIDATION_FAILED', 'due_precision 取值无效')
    }
    task.due_precision = precision
    changed = true
    touchedPlanInput = true
  }
  if (task.due_precision === 'unknown' && task.due_at !== null) {
    fail(400, 'VALIDATION_FAILED', 'due_precision=unknown 时截止时间必须为空')
  }
  if (body.prerequisite_ids !== undefined) {
    if (!Array.isArray(body.prerequisite_ids)) fail(400, 'VALIDATION_FAILED', '前置任务列表格式无效')
    task.prerequisite_ids = body.prerequisite_ids.map((v) => String(v))
    changed = true
    touchedPlanInput = true
  }
  if (body.status !== undefined) {
    const status = String(body.status) as LearningTask['status']
    if (!TASK_STATUSES.includes(status)) fail(400, 'VALIDATION_FAILED', '任务状态无效')
    if (status !== task.status) {
      task.status = status
      changed = true
      touchedPlanInput = true
    }
  }
  if (changed) {
    task.revision += 1
    task.updated_at = nowIso(db)
  }
  if (touchedPlanInput) db.inputRevision += 1
  return task
}

function handleLockTask(db: MockDb, taskId: string, rawBody: unknown): { task: LearningTask; plan: Plan | null } {
  const task = requireTask(db, taskId)
  const body = (rawBody ?? {}) as { locked?: boolean; starts_at?: string | null }
  if (typeof body.locked !== 'boolean') {
    fail(400, 'VALIDATION_FAILED', '缺少 locked 字段', { details: { field: 'locked' } })
  }
  if (db.activePlan) {
    const block = db.activePlan.blocks.find((b) => b.task_id === taskId)
    if (block) {
      block.locked = body.locked
      // 首版不支持拖动改时间（E7）：starts_at 仅在校验通过时被忽略，绝不静默改块时间
      if (body.starts_at) {
        const parsed = Date.parse(body.starts_at)
        if (Number.isNaN(parsed)) {
          fail(400, 'VALIDATION_FAILED', 'starts_at 时间格式无效')
        }
        if (parsed !== Date.parse(block.starts_at)) {
          fail(400, 'VALIDATION_FAILED', '首版不支持通过锁定操作调整时间；请解锁后重新生成计划', {
            details: { field: 'starts_at', mock_reason: 'block_move_not_supported' },
          })
        }
      }
    }
  }
  return { task, plan: db.activePlan }
}

function handleFeedback(db: MockDb, taskId: string, rawBody: unknown): {
  feedback: FeedbackRecord
  task: LearningTask
  current_input_revision: string
  replan_suggested: boolean
} {
  const task = requireTask(db, taskId)
  const body = (rawBody ?? {}) as FeedbackRequest
  const outcome = String(body.outcome ?? '')
  if (!FEEDBACK_OUTCOMES.includes(outcome as (typeof FEEDBACK_OUTCOMES)[number])) {
    fail(400, 'VALIDATION_FAILED', '反馈结果无效：应为 completed / partial / skipped', {
      details: { field: 'outcome' },
    })
  }
  if (body.actual_minutes != null) {
    if (!Number.isInteger(body.actual_minutes) || body.actual_minutes < 1 || body.actual_minutes > 1440) {
      fail(400, 'VALIDATION_FAILED', '实际耗时需在 1–1440 分钟之间')
    }
  }
  if (body.difficulty != null) {
    if (!Number.isInteger(body.difficulty) || body.difficulty < 1 || body.difficulty > 5) {
      fail(400, 'VALIDATION_FAILED', '难度取值为 1–5')
    }
  }
  if (body.note != null && String(body.note).length > 2000) {
    fail(400, 'VALIDATION_FAILED', '备注不能超过 2000 字')
  }
  db.seq += 1
  const now = nowIso(db)
  const record: FeedbackRecord = {
    id: `fb_${db.seq}`,
    task_id: taskId,
    outcome: outcome as FeedbackRecord['outcome'],
    actual_minutes: body.actual_minutes ?? null,
    difficulty: body.difficulty ?? null,
    note: body.note ?? null,
    created_at: now,
  }
  db.feedback.push(record)
  if (outcome === 'completed') {
    task.status = 'completed'
  } else if (outcome === 'partial') {
    task.status = 'in_progress'
  }
  task.revision += 1
  task.updated_at = now
  db.inputRevision += 1
  return {
    feedback: record,
    task,
    current_input_revision: revisionStr(db),
    replan_suggested: outcome !== 'skipped',
  }
}

// ---------- 可用时间 ----------

function handleSaveAvailability(db: MockDb, rawBody: unknown): Availability {
  const body = (rawBody ?? {}) as Partial<Availability>
  const payload: Availability = {
    timezone: body.timezone || DEFAULT_TIMEZONE,
    weekly_windows: body.weekly_windows ?? [],
    date_exceptions: body.date_exceptions ?? [],
    daily_limit_minutes: body.daily_limit_minutes ?? null,
    updated_at: nowIso(db),
  }
  const issues = validateAvailability(payload)
  if (issues.length > 0) {
    fail(400, 'VALIDATION_FAILED', '可用时间设置存在校验问题，请修正后再保存', {
      details: { issues },
    })
  }
  db.availability = payload
  db.inputRevision += 1
  return payload
}

// ---------- 计划 ----------

function handlePreview(db: MockDb, rawBody: unknown): { plan: Plan; current_input_revision: string } {
  const body = (rawBody ?? {}) as { respect_locked?: boolean }
  if (body.respect_locked === false) {
    fail(400, 'VALIDATION_FAILED', '首版必须保留锁定块（respect_locked 必须为 true）')
  }
  const plan = generatePreview(db)
  db.previews.set(plan.id, plan)
  while (db.previews.size > 5) {
    const oldest = db.previews.keys().next().value
    if (oldest === undefined) break
    db.previews.delete(oldest)
  }
  return { plan, current_input_revision: revisionStr(db) }
}

function handleApply(db: MockDb, planId: string): { plan: Plan; previous_plan_id: string | null } {
  const preview = db.previews.get(planId)
  if (!preview) {
    fail(404, 'NOT_FOUND', '候选计划不存在或已被替换，请重新生成预览')
  }
  const currentRevision = revisionStr(db)
  if (preview.input_revision !== currentRevision) {
    // 与契约示例（contracts/examples/api/error-responses.json）及真实后端一致：retryable=false
    fail(409, 'PLAN_STALE', '输入已更新，候选计划已过期，请重新生成预览后再应用', {
      retryable: false,
      details: {
        plan_id: planId,
        input_revision: preview.input_revision,
        current_input_revision: currentRevision,
      },
    })
  }
  const previous = db.activePlan
  if (previous) {
    previous.status = 'superseded'
    previous.superseded_by = preview.id
    db.planHistory.push(previous)
  }
  const applied: Plan = {
    ...preview,
    status: 'active',
    applied_at: nowIso(db),
    revoked_at: null,
    superseded_by: null,
  }
  db.activePlan = applied
  db.planVersion = applied.version
  db.previews.delete(planId)
  return { plan: applied, previous_plan_id: previous?.id ?? null }
}

function handleRevoke(db: MockDb, planId: string): { plan: Plan; restored_plan_id: string | null } {
  const active = db.activePlan
  if (!active || active.id !== planId) {
    fail(409, 'CONFLICT', '只有当前生效的计划可以撤销')
  }
  const revoked: Plan = {
    ...active,
    status: 'revoked',
    revoked_at: nowIso(db),
  }
  let restored: Plan | null = null
  const basedOnIndex = active.based_on_plan_id
    ? db.planHistory.findIndex((p) => p.id === active.based_on_plan_id && p.status === 'superseded')
    : -1
  if (basedOnIndex >= 0) {
    const previous = db.planHistory[basedOnIndex]
    db.planHistory.splice(basedOnIndex, 1)
    restored = {
      ...previous,
      status: 'active',
      superseded_by: null,
    }
  }
  db.planHistory.push(revoked)
  db.activePlan = restored
  return { plan: revoked, restored_plan_id: restored?.id ?? null }
}

// ---------- 搜索 ----------

function handleSearch(db: MockDb, query: Record<string, string | number | boolean | undefined>): CursorPage<SearchHit> {
  const q = String(query.q ?? '').trim().toLowerCase()
  if (!q) fail(400, 'VALIDATION_FAILED', '搜索词不能为空', { details: { field: 'q' } })
  const courseFilter = query.course_id !== undefined ? String(query.course_id) : null
  const kindFilter = query.kind !== undefined ? String(query.kind) : null
  const hits: SearchHit[] = []
  for (const resource of db.resources) {
    if (resource.archived_at !== null) continue
    if (courseFilter !== null && resource.course_id !== courseFilter) continue
    if (kindFilter !== null && resource.kind !== kindFilter) continue
    const inTitle = resource.title.toLowerCase().includes(q)
    for (const chunk of db.chunks) {
      if (chunk.resource_id !== resource.id || chunk.resource_version !== resource.version) continue
      const inText = chunk.text.toLowerCase().includes(q)
      if (!inTitle && !inText) continue
      hits.push({
        chunk_id: chunk.id,
        resource_id: resource.id,
        resource_version: chunk.resource_version,
        course_id: resource.course_id,
        resource_title: resource.title,
        text: chunk.text,
        locator: chunk.locator,
        score: inTitle && inText ? 1 : inTitle ? 0.9 : 0.6,
      })
    }
  }
  hits.sort((a, b) => b.score - a.score || a.resource_title.localeCompare(b.resource_title))
  return okPage(hits.slice(0, 20))
}

// ---------- 连接 ----------

function handleSync(db: MockDb, connId: string, idempotencyKey: string | undefined): { job: Job } {
  return idempotent(db, idempotencyKey, () => {
    const conn = requireConnection(db, connId)
    if (conn.status === 'disabled') {
      fail(409, 'CONFLICT', '连接已停用，请先启用后再同步')
    }
    if (conn.status === 'auth_required') {
      fail(409, 'AUTH_EXPIRED', '登录已失效，请重新登录后继续同步。', {
        retryable: false,
        details: { connection_id: conn.id, platform: conn.platform },
      })
    }
    if (conn.status !== 'ok') {
      fail(409, 'CONFLICT', `连接当前不可用（${conn.status}），请稍后再试`)
    }
    const job = createJob(db, 'sync', 6000, () => {
      const now = nowIso(db)
      conn.last_sync_at = now
      conn.last_checked_at = now
      conn.updated_at = now
      if (conn.platform === 'zykt') {
        return { status: 'succeeded' as const }
      }
      return {
        status: 'partial_failed' as const,
        errors: [
          {
            code: 'PERMISSION_DENIED',
            message: '第6章 图算法 讲义.pdf：该内容受平台权限限制，已跳过',
            retryable: false,
            details: { remote_status: 403, access_level: 'restricted' },
          },
          {
            code: 'NOT_FOUND',
            message: '第3讲 回放.mp4：该内容在平台侧不存在或已被移除',
            retryable: false,
            details: { remote_status: 404 },
          },
          {
            code: 'FETCH_FAILED',
            message: '第7讲 习题解答.pdf：平台暂时异常（HTTP 502）',
            retryable: true,
            details: { remote_status: 502 },
          },
        ],
      }
    })
    return { job }
  })
}

function handleDisable(db: MockDb, connId: string): Connection {
  const conn = requireConnection(db, connId)
  if (conn.status !== 'disabled') {
    conn.status = 'disabled'
    conn.recovery_action = null
    conn.updated_at = nowIso(db)
  }
  return conn
}

function handleEnable(db: MockDb, connId: string): Connection {
  const conn = requireConnection(db, connId)
  if (conn.status !== 'disabled') return conn
  const now = nowIso(db)
  conn.last_checked_at = now
  conn.updated_at = now
  if (conn.platform === 'zykt') {
    conn.status = 'auth_required'
    conn.last_error = {
      code: 'AUTH_EXPIRED',
      message: '登录已失效，请重新登录后继续同步。',
      retryable: false,
      details: { platform: 'zykt' },
    }
    conn.recovery_action = {
      type: 'user_login',
      entry_url: 'https://classroom.zju.edu.cn/',
      message: '登录已失效，请在连接器窗口完成登录后重试',
    }
  } else {
    conn.status = 'ok'
    conn.last_error = null
    conn.recovery_action = null
  }
  return conn
}

/** 连接检查（POST /connections/{id}/check）：状态由检查结果驱动，不作假 ok（镜像 B12 语义） */
function handleCheck(db: MockDb, connId: string): Connection {
  const conn = requireConnection(db, connId)
  if (conn.status === 'disabled') {
    fail(409, 'CONFLICT', '连接已停用，请先启用后再检查')
  }
  const now = nowIso(db)
  conn.last_checked_at = now
  conn.updated_at = now
  if (conn.platform === 'zykt') {
    conn.status = 'auth_required'
    conn.last_error = {
      code: 'AUTH_EXPIRED',
      message: '登录已失效，请重新登录后继续同步。',
      retryable: false,
      details: { platform: 'zykt' },
    }
    conn.recovery_action = {
      type: 'user_login',
      entry_url: 'https://classroom.zju.edu.cn/',
      message: '登录已失效，请在连接器窗口完成登录后重试',
    }
  }
  return conn
}

// ---------- 后台任务 ----------

function handleRetryJob(db: MockDb, jobId: string): { job: Job } {
  const job = requireJob(db, jobId)
  if (job.status !== 'failed' && job.status !== 'partial_failed') {
    fail(409, 'CONFLICT', '仅失败或部分失败的任务可重试')
  }
  if (job.type === 'review_generate') return retryReviewJob(db, job)
  const retry = createJob(db, job.type, 2500, () => {
    if (job.type === 'sync') {
      const conn = db.connections.find((c) => c.status === 'ok')
      if (conn) {
        const now = nowIso(db)
        conn.last_sync_at = now
        conn.updated_at = now
      }
    }
    return { status: 'succeeded' as const }
  })
  return { job: retry }
}

function handleCancelJob(db: MockDb, jobId: string): Job {
  const job = requireJob(db, jobId)
  if (['succeeded', 'failed', 'partial_failed', 'cancelled'].includes(job.status)) {
    fail(409, 'CONFLICT', '任务已结束，无法取消')
  }
  const runtime = db.runtimeJobs.get(job.id)
  if (job.status === 'queued' && runtime && runtime.startedMs === null) {
    job.status = 'cancelled'
    job.finished_at = nowIso(db)
    db.runtimeJobs.delete(job.id)
  } else {
    // 协作式取消：标记请求，运行中的任务在下次推进时结束
    job.cancel_requested = true
  }
  return job
}

// ---------- 导出 ----------

/** 幂等指纹含格式与类别（镜像真实端 exports 端点的 request_hash）：同键异体 → 409 */
function idempotentExport<T>(db: MockDb, key: string | undefined, fingerprint: string, produce: () => T): T {
  if (!key) return produce()
  const storeKey = `${key}#${fingerprint}`
  const hit = db.idempotency.get(storeKey)
  if (hit !== undefined) return hit as T
  const reused = [...db.idempotency.keys()].some((k) => k.startsWith(`${key}#`))
  if (reused) {
    fail(409, 'IDEMPOTENCY_KEY_REUSED', '幂等键已用于不同请求体', { details: { key, endpoint: 'POST /exports' } })
  }
  const result = produce()
  db.idempotency.set(storeKey, result)
  return result
}

function handleExport(db: MockDb, req: MockRequestInput): { job: Job } {
  const body = (req.body ?? {}) as { include?: string[]; format?: string }
  const fmt = body.format ?? 'json'
  if (fmt !== 'json' && fmt !== 'ics') {
    fail(400, 'VALIDATION_FAILED', `不支持的导出格式：${fmt}（可选 json / ics）`, { details: { format: fmt } })
  }
  if (fmt === 'ics' && body.include !== undefined) {
    fail(400, 'VALIDATION_FAILED', 'ICS 导出为计划与任务截止整体，不支持 include（该参数仅对 format=json 有效）', {
      details: { format: fmt },
    })
  }
  const fingerprint = `${fmt}|${body.include !== undefined ? JSON.stringify(body.include) : 'all'}`
  return idempotentExport(db, req.idempotencyKey, fingerprint, () => {
    const job = createJob(db, 'export', 2500, () => {
      db.seq += 1
      return {
        status: 'succeeded' as const,
        result_refs: [{ type: 'export_file' as const, id: `${fmt === 'ics' ? 'exp_ics' : 'exp'}_${db.seq}` }],
      }
    })
    return { job }
  })
}

// ---------- 导入 ----------

function mimeFromName(name: string): string | null {
  if (/\.pdf$/i.test(name)) return 'application/pdf'
  if (/\.pptx$/i.test(name)) return 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
  if (/\.docx$/i.test(name)) return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
  return null
}

function mockHash(db: MockDb): string {
  return `sha256:${String(db.seq).padStart(64, '0')}`
}

function snapshotResourceVersion(resource: Resource): ResourceVersion {
  return {
    resource_id: resource.id,
    version: resource.version,
    content_hash: resource.content_hash,
    mime_type: resource.mime_type,
    size_bytes: resource.size_bytes,
    file_id: resource.file_id,
    extent_value: resource.extent_value,
    extent_unit: resource.extent_unit,
    fetched_at: resource.fetched_at,
    parse_status: resource.parse_status,
    source_updated_at: resource.source_updated_at,
    created_at: resource.created_at,
  }
}

interface MockImportPayload {
  course_id?: string
  kind?: Resource['kind']
  title?: string
  source_ref?: SourceRef | null
  files?: { name: string; size: number }[]
}

function handleImport(db: MockDb, req: MockRequestInput): { job: Job } {
  return idempotent(db, req.idempotencyKey, () => {
    const payload = (req.body ?? {}) as MockImportPayload
    if (!payload.course_id) {
      fail(400, 'VALIDATION_FAILED', '请选择导入到哪门课程', { details: { field: 'course_id' } })
    }
    const course = requireCourse(db, payload.course_id)
    if (payload.kind !== undefined && !RESOURCE_KINDS.includes(payload.kind)) {
      fail(400, 'VALIDATION_FAILED', '资料类型无效', { details: { field: 'kind' } })
    }
    const files = payload.files ?? []
    if (files.length === 0) {
      fail(400, 'VALIDATION_FAILED', '至少需要上传一个文件', { details: { field: 'files' } })
    }
    for (const file of files) {
      if (!file.name) fail(400, 'VALIDATION_FAILED', '缺少文件名', { details: { field: 'files' } })
      if (!importTypeOk(file.name)) {
        fail(415, 'UNSUPPORTED_MEDIA_TYPE', `「${file.name}」类型不受支持：仅支持 PDF、PPTX、DOCX`)
      }
      if (!Number.isFinite(file.size) || file.size <= 0) {
        fail(400, 'VALIDATION_FAILED', `「${file.name}」文件大小无效`)
      }
      if (file.size > IMPORT_MAX_BYTES) {
        fail(
          413,
          'FILE_TOO_LARGE',
          `「${file.name}」超过 ${Math.round(IMPORT_MAX_BYTES / (1024 * 1024))}MB 上限，请压缩或拆分后重试`,
          { details: { max_bytes: IMPORT_MAX_BYTES, file_name: file.name } },
        )
      }
    }

    const created: Resource[] = []
    for (const file of files) {
      const fingerprint = `${file.name}:${file.size}`
      const knownId = db.fileHashes.get(fingerprint)
      if (knownId && db.resources.some((r) => r.id === knownId && r.archived_at === null)) {
        continue
      }
      const title = payload.title?.trim() || file.name
      const now = nowIso(db)
      db.seq += 1
      const current = db.resources.find(
        (r) => r.course_id === course.id && r.title === title && r.archived_at === null,
      )
      if (current) {
        if (!db.resourceVersions.some((v) => v.resource_id === current.id && v.version === current.version)) {
          db.resourceVersions.push(snapshotResourceVersion(current))
        }
        current.version += 1
        current.content_hash = mockHash(db)
        current.mime_type = mimeFromName(file.name)
        current.size_bytes = file.size
        current.file_id = `file_${db.seq}`
        current.parse_status = 'processing'
        current.chunk_count = null
        current.source_updated_at = null
        current.fetched_at = now
        current.updated_at = now
        if (payload.source_ref) current.source_refs = [payload.source_ref]
        db.resourceVersions.push(snapshotResourceVersion(current))
        created.push(current)
      } else {
        const resource: Resource = {
          id: `res_${db.seq}`,
          course_id: course.id,
          kind: payload.kind ?? kindFromName(file.name),
          title,
          source_refs: payload.source_ref ? [payload.source_ref] : [],
          version: 1,
          content_hash: mockHash(db),
          parse_status: 'processing',
          mime_type: mimeFromName(file.name),
          size_bytes: file.size,
          access_level: 'full',
          file_id: `file_${db.seq}`,
          extent_value: null,
          extent_unit: null,
          chunk_count: null,
          fetched_at: now,
          source_updated_at: null,
          archived_at: null,
          created_at: now,
          updated_at: now,
        }
        db.resources.push(resource)
        db.resourceVersions.push(snapshotResourceVersion(resource))
        created.push(resource)
      }
      db.fileHashes.set(fingerprint, created[created.length - 1].id)
    }

    let jobId = ''
    const job = createJob(db, 'import', created.length === 0 ? 500 : 4500, () => {
      for (const resource of created) {
        generateChunks(db, resource, jobId)
        const versionRecord = db.resourceVersions.find(
          (v) => v.resource_id === resource.id && v.version === resource.version,
        )
        if (versionRecord) {
          versionRecord.parse_status = 'ready'
          versionRecord.content_hash = resource.content_hash
        }
      }
      return {
        status: 'succeeded' as const,
        result_refs: created.map((r) => ({ type: 'resource' as const, id: r.id })),
      }
    })
    jobId = job.id
    return { job }
  })
}

// ---------- 重新解析 ----------

function handleReparse(db: MockDb, resourceId: string): { job: Job } {
  const resource = requireResource(db, resourceId)
  if (resource.kind === 'link') {
    fail(400, 'VALIDATION_FAILED', '外链资料没有可解析的文件')
  }
  if (resource.parse_status === 'processing') {
    fail(409, 'CONFLICT', '该资料正在解析中，请稍候')
  }
  resource.parse_status = 'processing'
  resource.updated_at = nowIso(db)
  const versionRecord = db.resourceVersions.find(
    (v) => v.resource_id === resource.id && v.version === resource.version,
  )
  if (versionRecord) versionRecord.parse_status = 'processing'

  let jobId = ''
  const job = createJob(db, 'parse', 3500, () => {
    generateChunks(db, resource, jobId)
    const record = db.resourceVersions.find(
      (v) => v.resource_id === resource.id && v.version === resource.version,
    )
    if (record) record.parse_status = 'ready'
    return {
      status: 'succeeded' as const,
      result_refs: [{ type: 'resource' as const, id: resource.id }],
    }
  })
  jobId = job.id
  return { job }
}
