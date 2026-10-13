import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { FeedbackRequest, Plan, SourceRef } from '../../types/contract'
import type {
  ProviderPatchInput,
  ProviderUpsertInput,
  ReviewExportFormat,
  ReviewMaterialItem,
  ReviewPaperRevision,
  ReviewRequest,
} from '../../types/review'
import { api } from './client'
import type {
  AssignmentPatch,
  AssignmentsQuery,
  BuildTasksInput,
  CreateAssignmentInput,
  CreateTaskInput,
  ImportInput,
  JobsQuery,
  TaskPatch,
  TasksQuery,
} from './client'

export const queryKeys = {
  courses: ['courses'] as const,
  course: (id: string) => ['courses', id] as const,
  resources: (courseId: string) => ['courses', courseId, 'resources'] as const,
  resourceChunks: (resourceId: string) => ['resources', resourceId, 'chunks'] as const,
  resourceVersions: (resourceId: string) => ['resources', resourceId, 'versions'] as const,
  assignments: (courseId: string) => ['courses', courseId, 'assignments'] as const,
  recordings: (courseId: string) => ['courses', courseId, 'recordings'] as const,
  globalAssignments: (query?: AssignmentsQuery) => ['assignments', 'global', query ?? {}] as const,
  connections: ['connections'] as const,
  connection: (id: string) => ['connections', id] as const,
  connectionCapabilities: (id: string) => ['connections', id, 'capabilities'] as const,
  jobsList: (query?: JobsQuery) => ['jobs', 'list', query ?? {}] as const,
  job: (id: string) => ['jobs', id] as const,
  availability: ['availability'] as const,
  currentPlan: ['plans', 'current'] as const,
  plans: (status?: Plan['status']) => ['plans', 'list', status ?? null] as const,
  allTasks: ['tasks', 'all'] as const,
  tasks: (query?: TasksQuery) => ['tasks', query ?? {}] as const,
  providers: ['ai', 'providers'] as const,
  reviewMaterials: (courseId: string) => ['courses', courseId, 'review-materials'] as const,
  reviewPapers: (query?: { course_id?: string; mode?: string }) =>
    ['review-papers', 'list', query ?? {}] as const,
  reviewPaper: (id: string, revision?: number) => ['review-papers', id, revision ?? 'latest'] as const,
  reviewPaperRevisions: (id: string) => ['review-papers', id, 'revisions'] as const,
  questionbankCapabilities: ['questionbanks', 'capabilities'] as const,
}

// ---------- 查询 ----------

export function useCourses() {
  return useQuery({ queryKey: queryKeys.courses, queryFn: () => api.getCourses() })
}

export function useCourse(courseId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.course(courseId ?? ''),
    queryFn: () => api.getCourse(courseId!),
    enabled: Boolean(courseId),
  })
}

export function useCourseResources(courseId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.resources(courseId ?? ''),
    queryFn: () => api.getResources(courseId!),
    enabled: Boolean(courseId),
    refetchInterval: query => query.state.data?.items.some(r => r.parse_status === 'pending' || r.parse_status === 'processing') ? 2000 : false,
  })
}

export function useCourseAssignments(courseId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.assignments(courseId ?? ''),
    queryFn: () => api.getCourseAssignments(courseId!),
    enabled: Boolean(courseId),
  })
}

export function useCourseRecordings(courseId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.recordings(courseId ?? ''),
    queryFn: () => api.getRecordings(courseId!),
    enabled: Boolean(courseId),
  })
}

export function useResourceChunks(resourceId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.resourceChunks(resourceId ?? ''),
    queryFn: () => api.getResourceChunks(resourceId!),
    enabled: Boolean(resourceId),
  })
}

export function useResourceVersions(resourceId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.resourceVersions(resourceId ?? ''),
    queryFn: () => api.getResourceVersions(resourceId!),
    enabled: Boolean(resourceId),
  })
}

export function useGlobalAssignments(query?: AssignmentsQuery) {
  return useQuery({
    queryKey: queryKeys.globalAssignments(query),
    queryFn: () => api.getGlobalAssignments(query),
  })
}

export function useConnections() {
  return useQuery({ queryKey: queryKeys.connections, queryFn: () => api.getConnections() })
}

export function useConnectionDetail(connectionId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.connection(connectionId ?? ''),
    queryFn: () => api.getConnection(connectionId!),
    enabled: Boolean(connectionId),
  })
}

export function useConnectionCapabilities(connectionId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.connectionCapabilities(connectionId ?? ''),
    queryFn: () => api.getConnectionCapabilities(connectionId!),
    enabled: Boolean(connectionId),
  })
}

/** 后台任务轮询；到达终态自动停止 */
export function useJob(jobId: string | null | undefined) {
  return useQuery({
    queryKey: queryKeys.job(jobId ?? ''),
    queryFn: () => api.getJob(jobId!),
    enabled: Boolean(jobId),
    refetchInterval: (query) => {
      const job = query.state.data
      if (!job) return 1200
      return job.status === 'queued' || job.status === 'running' ? 1200 : false
    },
  })
}

/** 复习卷生成任务轮询（带 phase 阶段字段）；到达终态自动停止 */
export function useReviewJob(jobId: string | null | undefined) {
  return useQuery({
    queryKey: [...queryKeys.job(jobId ?? ''), 'review'] as const,
    queryFn: () => api.getReviewJob(jobId!),
    enabled: Boolean(jobId),
    refetchInterval: (query) => {
      const job = query.state.data
      if (!job) return 1200
      return job.status === 'queued' || job.status === 'running' ? 1200 : false
    },
  })
}

export function useAvailability() {
  return useQuery({ queryKey: queryKeys.availability, queryFn: () => api.getAvailability() })
}

export function useJobs(query?: JobsQuery) {
  return useQuery({ queryKey: queryKeys.jobsList(query), queryFn: () => api.getJobs(query) })
}

export function useCurrentPlan() {
  return useQuery({ queryKey: queryKeys.currentPlan, queryFn: () => api.getCurrentPlan() })
}

export function usePlans(status?: Plan['status']) {
  return useQuery({ queryKey: queryKeys.plans(status), queryFn: () => api.listPlans(status) })
}

export function useAllTasks(includeArchived = false) {
  return useQuery({ queryKey: [...queryKeys.allTasks, includeArchived], queryFn: () => api.getAllTasks({ include_archived: includeArchived }), refetchInterval: 5000 })
}

export function useTasks(query?: TasksQuery, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.tasks(query),
    queryFn: () => api.getAllTasks(query),
    enabled: options?.enabled ?? true,
  })
}

// ---------- 变更 ----------

export function useCreateCourse() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: { title: string; term?: string | null; teacher?: string | null }) =>
      api.createCourse(input),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.courses })
    },
  })
}

export function useAddSourceRef() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ courseId, sourceRef }: { courseId: string; sourceRef: SourceRef }) =>
      api.addCourseSourceRef(courseId, sourceRef),
    onSuccess: (_course, variables) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.courses })
      void queryClient.invalidateQueries({ queryKey: queryKeys.course(variables.courseId) })
    },
  })
}

export function useCreateAssignment() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: CreateAssignmentInput) => api.createAssignment(input),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.assignments(variables.course_id) })
      void queryClient.invalidateQueries({ queryKey: ['assignments'] })
    },
  })
}

export function usePatchAssignment() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, patch, revision }: { id: string; patch: AssignmentPatch; revision?: number }) =>
      api.patchAssignment(id, patch, revision),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['assignments'] })
      void queryClient.invalidateQueries({ queryKey: queryKeys.courses })
    },
  })
}

export function useCreateTask() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: CreateTaskInput) => api.createTask(input),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['tasks'] })
      void queryClient.invalidateQueries({ queryKey: queryKeys.currentPlan })
    },
  })
}

export function usePatchTask() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ taskId, patch, revision }: { taskId: string; patch: TaskPatch; revision?: number }) =>
      api.patchTask(taskId, patch, revision),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['tasks'] })
      void queryClient.invalidateQueries({ queryKey: queryKeys.currentPlan })
    },
  })
}

export function useDeleteTask() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (taskId: string) => api.deleteTask(taskId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['tasks'] })
      void queryClient.invalidateQueries({ queryKey: queryKeys.currentPlan })
    },
  })
}

/** 触发任务生成（后台任务）；随后以 job 轮询结果、刷新任务列表 */
export function useBuildTasks() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input?: BuildTasksInput) => api.buildTasks(input),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['jobs'] })
      void queryClient.invalidateQueries({ queryKey: ['tasks'] })
    },
  })
}

/** 锁定/解锁任务在生效计划中的时间块（首版不支持借锁定改时间） */
export function useLockTask() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ taskId, locked }: { taskId: string; locked: boolean }) =>
      api.lockTask(taskId, { locked }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.currentPlan })
    },
  })
}

export function useImportFiles() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({
      input,
      files,
      idempotencyKey,
    }: {
      input: ImportInput
      files: File[]
      idempotencyKey: string
    }) => api.importFiles(input, files, idempotencyKey),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.resources(variables.input.course_id) })
      void queryClient.invalidateQueries({ queryKey: queryKeys.courses })
    },
  })
}

export function useReparseResource() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (resourceId: string) => api.reparseResource(resourceId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.courses })
    },
  })
}

export function useSyncConnection() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (connectionId: string) => api.syncConnection(connectionId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.connections })
    },
  })
}

export function useDisableConnection() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (connectionId: string) => api.disableConnection(connectionId),
    onSuccess: (_conn, connectionId) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.connections })
      void queryClient.invalidateQueries({ queryKey: queryKeys.connection(connectionId) })
    },
  })
}

export function useEnableConnection() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (connectionId: string) => api.enableConnection(connectionId),
    onSuccess: (_conn, connectionId) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.connections })
      void queryClient.invalidateQueries({ queryKey: queryKeys.connection(connectionId) })
    },
  })
}

/** 手动触发连接检查（兜底衔接；正常路径由本机连接器登录后自动检查上报） */
export function useCheckConnection() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (connectionId: string) => api.checkConnection(connectionId),
    onSuccess: (conn) => {
      queryClient.setQueryData(queryKeys.connection(conn.id), conn)
      void queryClient.invalidateQueries({ queryKey: queryKeys.connections })
    },
  })
}

export function useCancelJob() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (jobId: string) => api.cancelJob(jobId),
    onSuccess: (job) => {
      queryClient.setQueryData(queryKeys.job(job.id), job)
      void queryClient.invalidateQueries({ queryKey: ['jobs'] })
    },
  })
}

export function useRetryJob() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (jobId: string) => api.retryJob(jobId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['jobs'] })
    },
  })
}

export function useSaveAvailability() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (availability: Parameters<typeof api.saveAvailability>[0]) =>
      api.saveAvailability(availability),
    onSuccess: (saved) => {
      queryClient.setQueryData(queryKeys.availability, saved)
    },
  })
}

export function usePreviewPlan() {
  return useMutation({ mutationFn: () => api.previewPlan({ respect_locked: true }) })
}

export function useApplyPlan() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (planId: string) => api.applyPlan(planId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.currentPlan })
      void queryClient.invalidateQueries({ queryKey: ['plans'] })
      void queryClient.invalidateQueries({ queryKey: ['tasks'] })
    },
  })
}

export function useRevokePlan() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (planId: string) => api.revokePlan(planId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.currentPlan })
      void queryClient.invalidateQueries({ queryKey: ['plans'] })
      void queryClient.invalidateQueries({ queryKey: ['tasks'] })
    },
  })
}

export function useSubmitFeedback() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({
      taskId,
      feedback,
      idempotencyKey,
    }: {
      taskId: string
      feedback: FeedbackRequest
      idempotencyKey?: string
    }) => api.submitFeedback(taskId, feedback, idempotencyKey),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['tasks'] })
      void queryClient.invalidateQueries({ queryKey: queryKeys.currentPlan })
      void queryClient.invalidateQueries({ queryKey: ['assignments'] })
    },
  })
}

export function useExport() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input?: { include?: string[]; format?: 'json' | 'ics' }) => api.createExport(input),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['jobs'] })
    },
  })
}

// ---------- AI 服务商设置（复习卷） ----------

export function useProviders() {
  return useQuery({ queryKey: queryKeys.providers, queryFn: () => api.listProviders() })
}

export function useCreateProvider() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: ProviderUpsertInput) => api.createProvider(input),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.providers })
    },
  })
}

export function useUpdateProvider() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: ProviderPatchInput }) =>
      api.updateProvider(id, patch),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.providers })
    },
  })
}

export function useDeleteProvider() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.deleteProvider(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.providers })
    },
  })
}

export function useClearProviderKey() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.clearProviderKey(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.providers })
    },
  })
}

export function useTestProvider() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.testProvider(id),
    // 测试会更新 last_tested_at/last_test_result，刷新列表以展示最新状态
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.providers })
    },
  })
}

// ---------- 复习卷 ----------

async function fetchAllReviewMaterials(courseId: string) {
  const items: ReviewMaterialItem[] = []
  let cursor: string | undefined
  // 游标翻页取全量（上限防御异常游标导致的死循环）
  for (let page = 0; page < 50; page += 1) {
    const res = await api.getReviewMaterials(courseId, cursor)
    items.push(...res.items)
    if (!res.next_cursor) break
    cursor = res.next_cursor
  }
  return items
}

/** 课程复习卷可选材料（课件/作业/附件）——翻页取全量，不只显示第一批 */
export function useReviewMaterials(courseId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.reviewMaterials(courseId ?? ''),
    queryFn: () => fetchAllReviewMaterials(courseId!),
    enabled: Boolean(courseId),
  })
}

export function usePreflightReview() {
  return useMutation({ mutationFn: (input: ReviewRequest) => api.preflightReview(input) })
}

/** 创建出卷作业；成功后刷新任务与试卷缓存（页面据此轮询恢复进行中的作业） */
export function useGenerateReview() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ input, idempotencyKey, expectedFingerprint }: { input: ReviewRequest; idempotencyKey?: string; expectedFingerprint?: string }) =>
      api.generateReview(input, idempotencyKey, expectedFingerprint),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['jobs'] })
      void queryClient.invalidateQueries({ queryKey: ['review-papers'] })
    },
  })
}

/** 重做/修订（新版本，旧版本保留）；成功后刷新任务、试卷与版本历史 */
export function useCreateReviewRevision() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({
      paperId,
      input,
      idempotencyKey,
        expectedFingerprint,
    }: {
      paperId: string
      input: Parameters<typeof api.createReviewRevision>[1]
      idempotencyKey?: string
        expectedFingerprint?: string
      }) => api.createReviewRevision(paperId, input, idempotencyKey, expectedFingerprint),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ['jobs'] })
      void queryClient.invalidateQueries({ queryKey: ['review-papers'] })
      void queryClient.invalidateQueries({ queryKey: queryKeys.reviewPaperRevisions(variables.paperId) })
      void queryClient.invalidateQueries({ queryKey: queryKeys.reviewPaper(variables.paperId) })
    },
  })
}

/** 试卷列表（游标分页："加载更多"追加） */
export function useReviewPapers(query?: { course_id?: string; mode?: string }) {
  return useInfiniteQuery({
    queryKey: queryKeys.reviewPapers(query),
    queryFn: ({ pageParam }) => api.listReviewPapers({ ...query, cursor: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  })
}

/** 试卷详情；revision 缺省=最新版本；指定为历史版本（只读浏览该版快照） */
export function useReviewPaper(paperId: string | undefined, revision?: number) {
  return useQuery({
    queryKey: queryKeys.reviewPaper(paperId ?? '', revision),
    queryFn: () => api.getReviewPaper(paperId!, revision),
    enabled: Boolean(paperId),
  })
}

async function fetchAllReviewRevisions(paperId: string) {
  const items: ReviewPaperRevision[] = []
  let cursor: string | undefined
  // 游标翻页取全量（上限防御异常游标导致的死循环）
  for (let page = 0; page < 20; page += 1) {
    const res = await api.listReviewRevisions(paperId, cursor)
    items.push(...res.items)
    if (!res.next_cursor) break
    cursor = res.next_cursor
  }
  return items
}

/** 版本历史（revision 倒序）——翻页取全量 */
export function useReviewRevisions(paperId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.reviewPaperRevisions(paperId ?? ''),
    queryFn: () => fetchAllReviewRevisions(paperId!),
    enabled: Boolean(paperId),
  })
}

/** 题库能力（设置页/出卷页标注；未配置来源不得显示为已连接） */
export function useQuestionbankCapabilities() {
  return useQuery({
    queryKey: queryKeys.questionbankCapabilities,
    queryFn: () => api.getQuestionbankCapabilities(),
  })
}

export function useExportReviewPaper() {
  return useMutation({
    mutationFn: ({
      paperId,
      format,
      revision,
    }: {
      paperId: string
      format: ReviewExportFormat
      revision?: number
    }) => api.exportReviewPaper(paperId, format, revision),
  })
}
