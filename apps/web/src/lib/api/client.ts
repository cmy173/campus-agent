import type {
  ApplyPlanResponse,
  Assignment,
  Availability,
  Capabilities,
  Connection,
  ContentChunk,
  Course,
  CursorPage,
  CurrentPlanResponse,
  FeedbackRequest,
  FeedbackResponse,
  Job,
  JobAcceptedResponse,
  LearningTask,
  LockTaskResponse,
  Plan,
  PlanPreviewResponse,
  Recording,
  Resource,
  ResourceVersion,
  RevokePlanResponse,
  SearchHit,
  SourceRef,
} from '../../types/contract'
import type {
  ProviderConfig,
  ProviderPatchInput,
  ProviderTestResponse,
  ProviderUpsertInput,
  QuestionbankCapabilities,
  ReviewExportFormat,
  ReviewJob,
  ReviewMaterialsPage,
  ReviewPaper,
  ReviewPaperListPage,
  ReviewPaperRevisionListPage,
  ReviewPreflight,
  ReviewRequest,
  ReviewRevisionRequest,
  ReviewTextExport,
} from '../../types/review'
import { API_MODE, apiBaseUrl, apiRequest, apiRequestText } from './http'

export interface TaskPatch {
  course_id?: string | null
  type?: LearningTask['type']
  title?: string
  estimated_minutes?: number
  due_at?: string | null
  due_precision?: LearningTask['due_precision']
  prerequisite_ids?: string[]
  status?: LearningTask['status']
}

export interface AssignmentPatch {
  title?: string
  instructions?: string | null
  due_at?: string | null
  due_precision?: Assignment['due_precision']
  submission_status?: Assignment['submission_status']
}

export interface CreateTaskInput {
  course_id?: string | null
  type?: LearningTask['type']
  title: string
  estimated_minutes: number
  due_at?: string | null
  due_precision?: LearningTask['due_precision']
  prerequisite_ids?: string[]
}

export interface CreateAssignmentInput {
  course_id: string
  title: string
  instructions?: string | null
  due_at?: string | null
  due_precision?: Assignment['due_precision']
}

export interface ImportInput {
  course_id: string
  kind?: Resource['kind']
  title?: string
  source_ref?: SourceRef | null
}

export interface AssignmentsQuery {
  due_before?: string
  due_after?: string
  include_archived?: boolean
}

export interface TasksQuery {
  cursor?: string
  limit?: number
  course_id?: string
  status?: LearningTask['status']
  due_before?: string
  include_archived?: boolean
}

export interface JobsQuery {
  status?: Job['status']
  type?: Job['type']
}

export interface BuildTasksInput {
  /** 仅重建指定课程；缺省/null 为全部课程（契约 BuildTasksRequest） */
  course_id?: string | null
}

export const api = {
  getRecordingTranscript: (recordingId: string) => apiRequest<{ segments: { start_seconds: number; end_seconds: number; text: string; translated_text?: string }[] }>(`/recordings/${recordingId}/transcript`),
  discoverCourses: (connectionId: string, cursor?: string, term?: string) =>
    apiRequest<CursorPage<Course> & { errors?: { message: string }[] }>(`/connections/${connectionId}/courses`, { query: { cursor, term } }),
  importPlatformCourses: (connectionId: string, courses: { external_id: string; title: string; term?: string | null; teacher?: string | null }[]) =>
    apiRequest<CursorPage<Course>>(`/connections/${connectionId}/courses/import`, { method: 'POST', body: { courses } }),
  // ---------- 课程 ----------
  getCourses: (includeArchived = false) =>
    apiRequest<CursorPage<Course>>('/courses', {
      query: { include_archived: includeArchived || undefined },
    }),
  createCourse: (input: { title: string; term?: string | null; teacher?: string | null }) =>
    apiRequest<Course>('/courses', { method: 'POST', body: input }),
  getCourse: (id: string) => apiRequest<Course>(`/courses/${id}`),
  addCourseSourceRef: (courseId: string, sourceRef: SourceRef) =>
    apiRequest<Course>(`/courses/${courseId}/source-refs`, {
      method: 'POST',
      body: { source_ref: sourceRef },
    }),
  getResources: (courseId: string) =>
    apiRequest<CursorPage<Resource>>(`/courses/${courseId}/resources`),
  getCourseAssignments: (courseId: string) =>
    apiRequest<CursorPage<Assignment>>(`/courses/${courseId}/assignments`),
  getRecordings: (courseId: string) =>
    apiRequest<CursorPage<Recording>>(`/courses/${courseId}/recordings`),

  // ---------- 资源 ----------
  getResource: (resourceId: string) => apiRequest<Resource>(`/resources/${resourceId}`),
  getResourceVersions: (resourceId: string) =>
    apiRequest<CursorPage<ResourceVersion>>(`/resources/${resourceId}/versions`),
  getResourceChunks: (resourceId: string) =>
    apiRequest<CursorPage<ContentChunk>>(`/resources/${resourceId}/chunks`),
  reparseResource: (resourceId: string) =>
    apiRequest<JobAcceptedResponse>(`/resources/${resourceId}/reparse`, { method: 'POST' }),
  /** 受控文件下载地址：仅真实模式可用；mock 模式返回 null（界面不提供下载入口） */
  fileDownloadUrl: (fileId: string): string | null =>
    API_MODE === 'real' ? `${apiBaseUrl()}/api/v1/files/${fileId}` : null,

  // ---------- 作业 ----------
  getGlobalAssignments: (query?: AssignmentsQuery) =>
    apiRequest<CursorPage<Assignment>>('/assignments', {
      query: {
        due_before: query?.due_before,
        due_after: query?.due_after,
        include_archived: query?.include_archived || undefined,
      },
    }),
  getAssignment: (id: string) => apiRequest<Assignment>(`/assignments/${id}`),
  createAssignment: (input: CreateAssignmentInput) =>
    apiRequest<Assignment>('/assignments', { method: 'POST', body: input }),
  patchAssignment: (id: string, patch: AssignmentPatch, revision?: number) =>
    apiRequest<Assignment>(`/assignments/${id}`, {
      method: 'PATCH',
      body: patch,
      ifMatch: revision !== undefined ? String(revision) : undefined,
    }),

  // ---------- 学习任务 ----------
  getTasks: (query?: TasksQuery) =>
    apiRequest<CursorPage<LearningTask>>('/tasks', {
      query: {
        cursor: query?.cursor,
        limit: query?.limit,
        course_id: query?.course_id,
        status: query?.status,
        due_before: query?.due_before,
        include_archived: query?.include_archived || undefined,
      },
    }),
  getAllTasks: async (query?: TasksQuery): Promise<CursorPage<LearningTask>> => {
    const tasks = new Map<string, LearningTask>()
    const seen = new Set<string>()
    let cursor: string | undefined
    do {
      const page = await api.getTasks({ ...query, cursor, limit: 200 })
      page.items.forEach(task => tasks.set(task.id, task))
      if (!page.next_cursor) return { items: [...tasks.values()], next_cursor: null }
      if (seen.has(page.next_cursor) || seen.size >= 1000) throw new Error('任务列表分页异常，请刷新重试')
      seen.add(page.next_cursor)
      cursor = page.next_cursor
    } while (cursor)
    return { items: [...tasks.values()], next_cursor: null }
  },
  /** 任务详情（CR004 补录进契约：GET /tasks/{task_id}，含归档任务，未找到 404） */
  getTask: (taskId: string) => apiRequest<LearningTask>(`/tasks/${taskId}`),
  createTask: (input: CreateTaskInput) =>
    apiRequest<LearningTask>('/tasks', { method: 'POST', body: input }),
  patchTask: (taskId: string, patch: TaskPatch, revision?: number) =>
    apiRequest<LearningTask>(`/tasks/${taskId}`, {
      method: 'PATCH',
      body: patch,
      ifMatch: revision !== undefined ? String(revision) : undefined,
    }),
  deleteTask: (taskId: string) => apiRequest<void>(`/tasks/${taskId}`, { method: 'DELETE' }),
  lockTask: (taskId: string, input: { locked: boolean; starts_at?: string | null }) =>
    apiRequest<LockTaskResponse>(`/tasks/${taskId}/lock`, { method: 'POST', body: input }),
  submitFeedback: (taskId: string, feedback: FeedbackRequest, idempotencyKey?: string) =>
    apiRequest<FeedbackResponse>(`/tasks/${taskId}/feedback`, {
      method: 'POST',
      body: feedback,
      idempotencyKey,
    }),
  /** 由资料/作业/回放生成候选学习任务（后台任务，B13）；重复触发按 match_key 对齐不造重复 */
  buildTasks: (input?: BuildTasksInput) =>
    apiRequest<JobAcceptedResponse>('/tasks/build', { method: 'POST', body: input ?? {} }),

  // ---------- 可用时间 ----------
  getAvailability: () => apiRequest<Availability>('/preferences/availability'),
  saveAvailability: (availability: Omit<Availability, 'updated_at'>) =>
    apiRequest<Availability>('/preferences/availability', { method: 'PUT', body: availability }),

  // ---------- 计划 ----------
  getCurrentPlan: () => apiRequest<CurrentPlanResponse>('/plans/current'),
  listPlans: (status?: Plan['status']) =>
    apiRequest<CursorPage<Plan>>('/plans', { query: { status } }),
  getPlan: (planId: string) => apiRequest<Plan>(`/plans/${planId}`),
  previewPlan: (input?: { respect_locked?: boolean }) =>
    apiRequest<PlanPreviewResponse>('/plans/preview', { method: 'POST', body: input ?? {} }),
  applyPlan: (planId: string) =>
    apiRequest<ApplyPlanResponse>(`/plans/${planId}/apply`, { method: 'POST' }),
  revokePlan: (planId: string) =>
    apiRequest<RevokePlanResponse>(`/plans/${planId}/revoke`, { method: 'POST' }),

  // ---------- 搜索 ----------
  search: (q: string, query?: { course_id?: string; kind?: Resource['kind'] }) =>
    apiRequest<CursorPage<SearchHit>>('/search', {
      query: { q, course_id: query?.course_id, kind: query?.kind },
    }),

  // ---------- 连接（跳转式登录） ----------
  getConnections: () => apiRequest<CursorPage<Connection>>('/connections'),
  getConnection: (id: string) => apiRequest<Connection>(`/connections/${id}`),
  getConnectionCapabilities: (id: string) =>
    apiRequest<Capabilities>(`/connections/${id}/capabilities`),
  checkConnection: (id: string) =>
    apiRequest<Connection>(`/connections/${id}/check`, { method: 'POST' }),
  syncConnection: (id: string, idempotencyKey?: string) =>
    apiRequest<JobAcceptedResponse>(`/connections/${id}/sync`, { method: 'POST', idempotencyKey }),
  disableConnection: (id: string) =>
    apiRequest<Connection>(`/connections/${id}/disable`, { method: 'POST' }),
  enableConnection: (id: string) =>
    apiRequest<Connection>(`/connections/${id}/enable`, { method: 'POST' }),

  // ---------- 后台任务 ----------
  getJobs: (query?: JobsQuery) =>
    apiRequest<CursorPage<Job>>('/jobs', { query: { status: query?.status, type: query?.type } }),
  getJob: (jobId: string) => apiRequest<Job>(`/jobs/${jobId}`),
  retryJob: (jobId: string, idempotencyKey?: string) =>
    apiRequest<JobAcceptedResponse>(`/jobs/${jobId}/retry`, { method: 'POST', idempotencyKey }),
  cancelJob: (jobId: string) => apiRequest<Job>(`/jobs/${jobId}/cancel`, { method: 'POST' }),

  // ---------- 导出 ----------
  createExport: (input?: { include?: string[]; format?: 'json' | 'ics' }, idempotencyKey?: string) =>
    apiRequest<JobAcceptedResponse>('/exports', { method: 'POST', body: input ?? {}, idempotencyKey }),

  // ---------- 导入（mock：元数据；real：multipart） ----------
  importFiles: (input: ImportInput, files: File[], idempotencyKey: string) => {
    if (API_MODE === 'real') {
      const formData = new FormData()
      formData.set('payload', JSON.stringify(input))
      for (const file of files) formData.append('files', file)
      return apiRequest<JobAcceptedResponse>('/imports', { method: 'POST', formData, idempotencyKey })
    }
    return apiRequest<JobAcceptedResponse>('/imports', {
      method: 'POST',
      body: { ...input, files: files.map((f) => ({ name: f.name, size: f.size })) },
      idempotencyKey,
    })
  },

  // ---------- AI 服务商设置（复习卷；密钥只在服务端存储，GET 不返回密钥） ----------
  listProviders: () => apiRequest<CursorPage<ProviderConfig>>('/ai/providers'),
  getProvider: (id: string) => apiRequest<ProviderConfig>(`/ai/providers/${id}`),
  createProvider: (input: ProviderUpsertInput) =>
    apiRequest<ProviderConfig>('/ai/providers', { method: 'POST', body: input }),
  /** 部分更新：省略/null 的字段不动；api_key 省略或 null=保留（清除须显式调 clearProviderKey） */
  updateProvider: (id: string, patch: ProviderPatchInput) =>
    apiRequest<ProviderConfig>(`/ai/providers/${id}`, { method: 'PATCH', body: patch }),
  deleteProvider: (id: string) => apiRequest<void>(`/ai/providers/${id}`, { method: 'DELETE' }),
  /** 清除已保存密钥（幂等：无密钥同样 204；不影响其他字段） */
  clearProviderKey: (id: string) =>
    apiRequest<void>(`/ai/providers/${id}/key`, { method: 'DELETE' }),
  /** 连接测试：只发最小测试内容，不自动发送课件、不启动生成；失败也是 200 + result.ok=false */
  testProvider: (id: string) =>
    apiRequest<ProviderTestResponse>(`/ai/providers/${id}/test`, { method: 'POST' }),

  // ---------- 复习卷：材料 / 预检 / 生成 ----------
  /** 课程可选材料（课件/作业/附件），游标分页；客户端负责翻页取全量 */
  getReviewMaterials: (courseId: string, cursor?: string) =>
    apiRequest<ReviewMaterialsPage>(`/courses/${courseId}/review-materials`, {
      query: { cursor },
    }),
  /** 预检：将发送的材料、缺口、目标 API 与备注（不实际发送正文） */
  preflightReview: (input: ReviewRequest) =>
    apiRequest<ReviewPreflight>('/review-papers/preflight', { method: 'POST', body: input }),
  /** 创建出卷后台任务；同内容由 Idempotency-Key 保护（重复点击不产生两份卷） */
  generateReview: (input: ReviewRequest, idempotencyKey?: string, expectedFingerprint?: string) =>
    apiRequest<JobAcceptedResponse>('/review-papers/generate', {
      method: 'POST',
      body: input,
      idempotencyKey,
        ifMatch: expectedFingerprint,
    }),

  // ---------- 复习卷：试卷与修订 ----------
  listReviewPapers: (query?: { course_id?: string; mode?: string; cursor?: string }) =>
    apiRequest<ReviewPaperListPage>('/review-papers', {
      query: { course_id: query?.course_id, mode: query?.mode, cursor: query?.cursor },
    }),
  /** 缺省最新版本；指定 revision 读取该版原快照（不存在 404） */
  getReviewPaper: (paperId: string, revision?: number) =>
    apiRequest<ReviewPaper>(`/review-papers/${paperId}`, { query: { revision } }),
  /** 版本历史（仅索引；完整内容经 getReviewPaper 指定 revision） */
  listReviewRevisions: (paperId: string, cursor?: string) =>
    apiRequest<ReviewPaperRevisionListPage>(`/review-papers/${paperId}/revisions`, {
      query: { cursor },
    }),
  /** 重做/修订：生成新版本保留旧版；未提供的字段沿用上一版；失败不产生空版本 */
  createReviewRevision: (paperId: string, input: ReviewRevisionRequest, idempotencyKey?: string, expectedFingerprint?: string) =>
    apiRequest<JobAcceptedResponse>(`/review-papers/${paperId}/revisions`, {
      method: 'POST',
      body: input,
      idempotencyKey,
        ifMatch: expectedFingerprint,
    }),
  /** 导出（服务端直接返回文本；markdown 全卷 / student 学生版 / answers 答案版）；中文文件名经 Content-Disposition */
  exportReviewPaper: (paperId: string, format: ReviewExportFormat, revision?: number) =>
    apiRequestText(`/review-papers/${paperId}/export`, {
      query: { format, revision },
    }) as Promise<ReviewTextExport>,

  // ---------- 题库能力 ----------
  /** 题库模块能力（未安装 502 DEPENDENCY_UNAVAILABLE，不伪造空列表；未配置来源 configured=false） */
  getQuestionbankCapabilities: () =>
    apiRequest<QuestionbankCapabilities>('/questionbanks/capabilities'),

  /** 复习卷生成任务详情（含阶段字段 phase，服务端更新） */
  getReviewJob: (jobId: string) => apiRequest<ReviewJob>(`/jobs/${jobId}`),
}
