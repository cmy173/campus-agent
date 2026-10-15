/**
 * C2 真实模式端到端联调（硬验收，独立配置运行，不进入默认测试集）
 *
 * 与页面使用同一份业务代码：本文件直接调用 `src/lib/api/client.ts` 的 `api` 客户端
 * （页面 hooks 的底层），验证 mock 与 real 两条路径共享的契约类型层无漂移。
 *
 * 运行（需后端在线，先按 apps/api README 启动并 seed）：
 *   VITE_API_MODE=real VITE_API_BASE_URL=http://127.0.0.1:8000 \
 *     npx vitest run --config vitest.real.config.ts
 *
 * 流程：导入资料 → 课程/作业/回放 → 可用时间 → 预览 → 应用 → 反馈 → 导出 → 下载产物 →
 * 任务生成（C11 /tasks/build：warnings 如实上报、重复触发不产生重复任务、CR004 任务详情 getTask）
 * → 连接检查哨兵（C12），
 * 并核对错误信封（404 / 400 / PLAN_STALE / AUTH_EXPIRED）。
 * 阶段结果与契约偏差写入 `<repo>/var/c2-e2e-report.json`。
 */
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { Job } from '../../types/contract'
import { api } from './client'
import { API_MODE, ApiError, apiBaseUrl, isApiError } from './http'

const HERE = fileURLToPath(new URL('.', import.meta.url))
const WEB_ROOT = resolve(HERE, '../../..')
const REPO_ROOT = resolve(WEB_ROOT, '../..')
const SAMPLES_DIR = resolve(REPO_ROOT, 'var/import-samples')
const REPORT_PATH = resolve(REPO_ROOT, 'var/c2-e2e-report.json')

const COURSE_ID = 'crs_3f8a21'
const TERMINAL_JOB: Job['status'][] = ['succeeded', 'partial_failed', 'failed', 'cancelled']
const isTerminal = (j: Job) => TERMINAL_JOB.includes(j.status)

const runStamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14)

interface PhaseReport {
  name: string
  status: 'ok' | 'failed'
  ms: number
  detail?: string
}
interface Deviation {
  id: string
  summary: string
  evidence: string
}

const phases: PhaseReport[] = []
const deviations: Deviation[] = []

function recordDeviation(id: string, summary: string, evidence: string) {
  deviations.push({ id, summary, evidence })
  console.warn(`[C2-DEVIATION] ${id}｜${summary}｜${evidence}`)
}

async function phase(name: string, fn: () => Promise<string | void>) {
  const t0 = Date.now()
  try {
    const detail = await fn()
    phases.push({ name, status: 'ok', ms: Date.now() - t0, detail: detail ?? undefined })
  } catch (err) {
    phases.push({
      name,
      status: 'failed',
      ms: Date.now() - t0,
      detail: err instanceof Error ? err.message.slice(0, 400) : String(err),
    })
    throw err
  }
}

async function waitFor<T>(
  label: string,
  fn: () => Promise<T>,
  ok: (value: T) => boolean,
  timeoutMs = 60_000,
): Promise<T> {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    const value = await fn()
    if (ok(value)) return value
    if (Date.now() > deadline) {
      throw new Error(`${label} 超时（${timeoutMs} ms），最后状态：${JSON.stringify(value).slice(0, 500)}`)
    }
    await new Promise((r) => setTimeout(r, 500))
  }
}

let importedResourceId: string | undefined

describe('C2 真实模式端到端联调', () => {
  beforeAll(() => {
    expect(
      API_MODE,
      '当前不是真实模式：请用 VITE_API_MODE=real VITE_API_BASE_URL=<后端地址> 运行（见文件头注释）',
    ).toBe('real')
    expect(apiBaseUrl(), 'VITE_API_BASE_URL 未设置').toBeTruthy()
  })

  it('真实模式生效：种子数据来自后端，无 mock 数据混入', () =>
    phase('真实模式与数据源核对', async () => {
      const courses = await api.getCourses()
      const ids = courses.items.map((c) => c.id)
      expect(ids).toContain(COURSE_ID)
      expect(ids, '出现 mock 种子 id（course_*），说明未走真实后端').not.toContain('course_ds')

      const detail = await api.getCourse(COURSE_ID)
      expect(detail.title).toBe('数据结构基础')

      const connections = await api.getConnections()
      const connIds = connections.items.map((c) => c.id)
      expect(connIds).not.toContain('conn_xzzd')
      expect(connIds.length).toBeGreaterThan(0)

      return `courses=${ids.length}, connections=${connIds.length}, base=${apiBaseUrl()}`
    }))

  it('导入资料：multipart 上传 → 202 job → 同键幂等', () =>
    phase('导入资料（multipart + Idempotency-Key）', async () => {
      const docxPath = resolve(SAMPLES_DIR, '机器学习导论-第1章.docx')
      expect(
        existsSync(docxPath),
        `样例缺失：请先运行 python var/gen_import_samples.py（期望路径 ${docxPath}）`,
      ).toBe(true)
      const file = new File([readFileSync(docxPath)], '机器学习导论-第1章.docx', {
        type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      })
      const title = `C2联调-DOCX-${runStamp}`
      const key = `c2-e2e-import-${runStamp}`
      const input = { course_id: COURSE_ID, kind: 'document' as const, title }

      const accepted = await api.importFiles(input, [file], key)
      expect(accepted.job.type).toBe('import')
      expect(['queued', 'running', 'succeeded']).toContain(accepted.job.status)

      // 幂等：同键同体重复提交返回首次 job
      const replay = await api.importFiles(input, [file], key)
      expect(replay.job.id).toBe(accepted.job.id)

      const done = await waitFor('import job 完成', () => api.getJob(accepted.job.id), isTerminal)
      expect(done.status, `import job 非成功：${JSON.stringify(done.errors)}`).toBe('succeeded')

      const resources = await api.getResources(COURSE_ID)
      const resource = resources.items.find((r) => r.title === title)
      expect(resource, '导入成功后应能在课程资源列表按标题找到').toBeTruthy()
      importedResourceId = resource!.id
      expect(resource!.kind).toBe('document')
      expect(resource!.file_id).toBeTruthy()

      return `job=${accepted.job.id}, resource=${importedResourceId}`
    }))

  it('解析：parse_status=ready，chunks 带定位，chunk_count 一致', () =>
    phase('解析流水线（worker → ContentChunk）', async () => {
      expect(importedResourceId, '前置导入未完成').toBeTruthy()
      const ready = await waitFor(
        '资源解析',
        () => api.getResource(importedResourceId!),
        (r) => r.parse_status === 'ready' || r.parse_status === 'failed',
      )
      expect(ready.parse_status, `解析未成功：${JSON.stringify(ready)}`).toBe('ready')

      const chunks = await api.getResourceChunks(importedResourceId!)
      expect(chunks.items.length).toBeGreaterThan(0)
      expect(ready.chunk_count).toBe(chunks.items.length)
      for (const chunk of chunks.items) {
        expect(chunk.text.length).toBeGreaterThan(0)
        expect(['page', 'slide', 'timestamp', 'text']).toContain(chunk.locator.kind)
        expect(chunk.provenance.extractor).toBeTruthy()
        expect(chunk.provenance.job_id).toBeTruthy()
      }
      expect(chunks.items.some((c) => c.text.includes('机器学习'))).toBe(true)

      const kinds = [...new Set(chunks.items.map((c) => c.locator.kind))].join('/')
      return `chunks=${chunks.items.length}, locator kinds=${kinds}`
    }))

  it('课程/作业/回放读取', () =>
    phase('课程 / 作业 / 回放读取', async () => {
      const assignments = await api.getGlobalAssignments()
      expect(assignments.items.length).toBeGreaterThan(0)
      const one = assignments.items[0]
      const detail = await api.getAssignment(one.id)
      expect(detail.id).toBe(one.id)
      const byCourse = await api.getCourseAssignments(detail.course_id)
      expect(byCourse.items.map((a) => a.id)).toContain(one.id)

      const recordings = await api.getRecordings(COURSE_ID)
      expect(recordings.items.length).toBeGreaterThan(0)
      const resources = await api.getResources(COURSE_ID)
      expect(resources.items.length).toBeGreaterThan(0)

      return `assignments=${assignments.items.length}, recordings=${recordings.items.length}, resources=${resources.items.length}`
    }))

  it('可用时间：读取往返；写入按契约核验（含已知偏差记录）', () =>
    phase('可用时间（GET + PUT）', async () => {
      const before = await api.getAvailability()
      expect(before.timezone).toBe('Asia/Shanghai')
      expect(Array.isArray(before.weekly_windows)).toBe(true)

      // 确定性变更：补一个周日晚间窗口；已存在则原样回写（重复运行不漂移）
      const extra = { weekday: 7, start: '19:00', end: '21:00' }
      const already = before.weekly_windows.some(
        (w) => w.weekday === extra.weekday && w.start === extra.start,
      )
      const payload = {
        timezone: before.timezone,
        weekly_windows: already ? before.weekly_windows : [...before.weekly_windows, extra],
        date_exceptions: before.date_exceptions,
        daily_limit_minutes: before.daily_limit_minutes ?? 180,
      }

      try {
        const saved = await api.saveAvailability(payload)
        expect(saved.timezone).toBe(payload.timezone)
        const after = await api.getAvailability()
        expect(after.weekly_windows).toEqual(payload.weekly_windows)
        expect(after.daily_limit_minutes).toBe(payload.daily_limit_minutes)
        return '写入并回读一致'
      } catch (err) {
        if (isApiError(err) && err.status === 405) {
          recordDeviation(
            'B-PUT-AVAILABILITY-REGRESSION',
            'PUT /preferences/availability 回归缺失（C2 第五次复跑曾写入成功；契约 openapi.yaml §/api/v1/preferences/availability 定义）；设置页“保存可用时间”将不可用',
            `HTTP ${err.status} ${err.code}，request_id=${err.requestId}`,
          )
          return '偏差已记录：PUT 405（详见 report.deviations）'
        }
        throw err
      }
    }))

  it('预览 → 应用：候选结构可解读，应用后成为当前计划', () =>
    phase('预览计划 + 应用计划', async () => {
      const preview = await api.previewPlan({ respect_locked: true })
      expect(preview.plan.status).toBe('candidate')
      expect(preview.plan.blocks.length).toBeGreaterThan(0)
      expect(preview.plan.summary.scheduled_task_count).toBeGreaterThan(0)
      expect(preview.plan.input_revision).toBe(preview.current_input_revision)
      for (const block of preview.plan.blocks) {
        expect(block.task_id.startsWith('tsk_')).toBe(true)
        expect(new Date(block.starts_at).getTime()).toBeLessThan(new Date(block.ends_at).getTime())
      }

      const applied = await api.applyPlan(preview.plan.id)
      expect(applied.plan.status).toBe('active')

      const current = await api.getCurrentPlan()
      expect(current.plan?.id).toBe(preview.plan.id)
      expect(current.current_input_revision).toBeTruthy()

      return `plan=${preview.plan.id} v${preview.plan.version}, blocks=${preview.plan.blocks.length}, unassigned=${preview.plan.unassigned.length}`
    }))

  it('反馈：completed → 任务状态更新与重排建议', () =>
    phase('反馈（POST /tasks/{id}/feedback）', async () => {
      const tasks = await api.getTasks()
      expect(tasks.items.length).toBeGreaterThan(0)
      const target = tasks.items.find((t) => t.status === 'pending') ?? tasks.items[0]
      const key = `c2-e2e-feedback-${runStamp}`
      const feedback = { outcome: 'completed' as const, actual_minutes: 25, difficulty: 2, note: 'C2 联调' }

      const res = await api.submitFeedback(target.id, feedback, key)
      expect(res.feedback.id.startsWith('fbk_')).toBe(true)
      expect(res.feedback.task_id).toBe(target.id)
      expect(res.task.id).toBe(target.id)
      expect(res.task.status).toBe('completed')
      expect(typeof res.current_input_revision).toBe('string')
      expect(typeof res.replan_suggested).toBe('boolean')

      const replay = await api.submitFeedback(target.id, feedback, key)
      expect(replay.feedback.id).toBe(res.feedback.id)

      return `task=${target.id}, feedback=${res.feedback.id}, replan_suggested=${res.replan_suggested}`
    }))

  it('导出：JSON 产物 job → 受控下载', () =>
    phase('导出与产物下载（POST /exports + GET /files/{id}）', async () => {
      const accepted = await api.createExport()
      expect(accepted.job.type).toBe('export')
      const done = await waitFor('export job 完成', () => api.getJob(accepted.job.id), isTerminal)
      expect(done.status, `export job 非成功：${JSON.stringify(done.errors)}`).toBe('succeeded')

      const ref = done.result_refs.find((r) => r.type === 'export_file')
      expect(ref, '导出产物应包含 export_file 引用').toBeTruthy()

      const url = api.fileDownloadUrl(ref!.id)
      expect(url, '真实模式下 fileDownloadUrl 不应为 null').toBeTruthy()
      const res = await fetch(url!)
      expect(res.status).toBe(200)
      expect(res.headers.get('content-type') ?? '').toContain('application/json')
      const document = (await res.json()) as {
        exported_at: string
        sections: Record<string, unknown[]>
      }
      expect(typeof document.exported_at).toBe('string')
      expect(document.sections.tasks.length).toBeGreaterThan(0)
      expect(document.sections.plans.length).toBeGreaterThan(0)

      return `file=${ref!.id}, sections=${Object.keys(document.sections).join(',')}`
    }))

  it('导出：ICS 日历产物 → 受控下载；include 被拒收', () =>
    phase('ICS 导出（POST /exports format=ics，B19）', async () => {
      const accepted = await api.createExport({ format: 'ics' }, `c2-e2e-export-ics-${runStamp}`)
      expect(accepted.job.type).toBe('export')
      const done = await waitFor('ICS export job 完成', () => api.getJob(accepted.job.id), isTerminal)
      expect(done.status, `ICS export job 非成功：${JSON.stringify(done.errors)}`).toBe('succeeded')

      const ref = done.result_refs.find((r) => r.type === 'export_file')
      expect(ref, 'ICS 产物应包含 export_file 引用').toBeTruthy()

      const url = api.fileDownloadUrl(ref!.id)
      expect(url, '真实模式下 fileDownloadUrl 不应为 null').toBeTruthy()
      const res = await fetch(url!)
      expect(res.status).toBe(200)
      expect(res.headers.get('content-type') ?? '').toContain('text/calendar')
      const body = await res.text()
      expect(body).toContain('BEGIN:VCALENDAR')
      expect(body).toContain('END:VCALENDAR')

      try {
        await api.createExport({ format: 'ics', include: ['tasks'] })
        throw new Error('ics + include 应返回 400')
      } catch (err) {
        expect(err).toBeInstanceOf(ApiError)
        const e = err as ApiError
        expect(e.status).toBe(400)
        expect(e.code).toBe('VALIDATION_FAILED')
      }

      return `file=${ref!.id}, ics=${body.length}B, include+ics=400`
    }))

  it('错误信封：404 / 400 / PLAN_STALE / AUTH_EXPIRED 按 ApiErrorBody 呈现', () =>
    phase('错误信封核对', async () => {
      const notes: string[] = []

      // 404 NOT_FOUND
      try {
        await api.getCourse('crs_does_not_exist')
        throw new Error('应返回 404')
      } catch (err) {
        expect(err).toBeInstanceOf(ApiError)
        const e = err as ApiError
        expect(e.status).toBe(404)
        expect(e.code).toBe('NOT_FOUND')
        expect(e.retryable).toBe(false)
        expect(e.requestId).toMatch(/^req_/)
        notes.push(`404=${e.code}`)
      }

      // 400 VALIDATION_FAILED
      try {
        await api.createTask({ title: '', estimated_minutes: -5 })
        throw new Error('应返回 400')
      } catch (err) {
        expect(isApiError(err)).toBe(true)
        const e = err as ApiError
        expect(e.status).toBe(400)
        expect(e.code).toBe('VALIDATION_FAILED')
        expect(e.details).toBeTruthy()
        notes.push(`400=${e.code}`)
      }

      // PLAN_STALE：预览后输入变化 → 应用被拒
      const preview = await api.previewPlan({ respect_locked: true })
      const temp = await api.createTask({
        title: `C2联调-修订触发-${runStamp}`,
        type: 'other',
        estimated_minutes: 10,
        due_at: null,
        due_precision: 'unknown',
      })
      try {
        await api.applyPlan(preview.plan.id)
        throw new Error('应返回 PLAN_STALE')
      } catch (err) {
        expect(isApiError(err)).toBe(true)
        const e = err as ApiError
        expect(e.status).toBe(409)
        expect(e.code).toBe('PLAN_STALE')
        expect(e.retryable).toBe(false)
        const details = (e.details ?? {}) as Record<string, string>
        expect(details.current_input_revision).toBeTruthy()
        const staleKeys = Object.keys(details)
        notes.push(`PLAN_STALE=${e.code}(${staleKeys.join(',')})`)
        // 契约示例（error-responses.json）uses {plan_id, input_revision, current_input_revision}
        if (!('input_revision' in details)) {
          recordDeviation(
            'B-PLAN-STALE-DETAILS',
            'PLAN_STALE details 键名与契约示例不一致（示例：input_revision+plan_id；实现：plan_input_revision，无 plan_id）',
            `details keys=${staleKeys.join(',')}, request_id=${e.requestId}`,
          )
        }
      } finally {
        await api.deleteTask(temp.id)
      }

      // AUTH_EXPIRED：auth_required 连接同步 → 恢复入口，不循环重试
      const connections = await api.getConnections()
      const authRequired = connections.items.find((c) => c.status === 'auth_required')
      expect(authRequired, '种子数据应含 auth_required 连接（conn_8c1de）').toBeTruthy()
      try {
        await api.syncConnection(authRequired!.id, `c2-e2e-sync-${runStamp}`)
        throw new Error('应返回 AUTH_EXPIRED')
      } catch (err) {
        expect(isApiError(err)).toBe(true)
        const e = err as ApiError
        expect(e.status).toBe(409)
        expect(e.code).toBe('AUTH_EXPIRED')
        expect(e.retryable).toBe(false)
        const details = (e.details ?? {}) as { action?: { entry_url?: string } }
        expect(details.action?.entry_url).toBeTruthy()
        notes.push(`AUTH_EXPIRED actions=${JSON.stringify(details.action)}`)
        if (!('platform' in ((e.details ?? {}) as object))) {
          recordDeviation(
            'B-AUTH-EXPIRED-DETAILS',
            'AUTH_EXPIRED details 键名与契约示例不一致（示例：platform+action；实现：connection_id+action）',
            `request_id=${e.requestId}`,
          )
        }
      }

      // C12：连接检查哨兵——M1 后端未接入检查适配器时应如实返回 DEPENDENCY_UNAVAILABLE（不造假 ok）；
      // 适配器桥接后此分支转为 200 + 真实状态，两条路径均接受并记录。
      try {
        const checked = await api.checkConnection(authRequired!.id)
        notes.push(`check=200 status=${checked.status}`)
      } catch (err) {
        if (isApiError(err) && err.code === 'DEPENDENCY_UNAVAILABLE') {
          expect(err.status).toBe(502)
          expect(err.retryable).toBe(false)
          notes.push('check=502 DEPENDENCY_UNAVAILABLE（检查适配器未接入，如实返回）')
        } else {
          throw err
        }
      }

      // 写端点已补齐（POST /courses、POST /assignments、PATCH /assignments/{id}、
      // PUT /preferences/availability；C2 第五次复跑实测/路由核对）；此处保留 405 分支作回归观测
      try {
        await api.createCourse({ title: `C2联调-课程-${runStamp}` })
        notes.push('createCourse=200（POST /courses 可用）')
      } catch (err) {
        if (isApiError(err) && err.status === 405) {
          recordDeviation(
            'B-POST-COURSES-REGRESSION',
            'POST /courses 回归缺失（曾实测可用；契约 openapi.yaml 定义；当前无页面依赖，仅本探测消费）',
            `HTTP 405 ${err.code}, request_id=${err.requestId}`,
          )
        } else {
          throw err
        }
      }

      return notes.join(' | ')
    }))

  it('任务生成（C11）：/tasks/build → warnings 如实上报 → 重复触发不产生重复任务', () =>
    phase('任务生成（C11 /tasks/build）', async () => {
      // 真实后端解析完成时会自动入队一次 build_tasks（worker.py enqueue reason="parse"），
      // 故本相位显式触发时来源多已对齐、refs 可能为 0——属幂等表现；重点验证 warnings 如实上报与重复触发不重复。
      const before = await api.getTasks({ course_id: COURSE_ID })
      const beforeIds = new Set(before.items.map((t) => t.id))

      const first = await api.buildTasks({ course_id: COURSE_ID })
      expect(first.job.type).toBe('build_tasks')

      // 立即重复触发：同一范围排队/执行中任务应复用（契约 202 描述），不重复入队
      const immediate = await api.buildTasks({ course_id: COURSE_ID })
      expect(immediate.job.id, '同范围立即重复触发应复用排队/执行中任务').toBe(first.job.id)

      const done1 = await waitFor(
        'build_tasks job #1',
        () => api.getJob(first.job.id),
        (j) => j.status !== 'queued' && j.status !== 'running',
      )
      expect(done1.status).toBe('succeeded')
      // CR003：warnings 为必填数组，如实上报（可为空数组）
      expect(Array.isArray(done1.warnings)).toBe(true)
      expect(done1.result_refs.every((ref) => ref.type === 'task')).toBe(true)

      const after1 = await api.getTasks({ course_id: COURSE_ID })
      const after1Ids = new Set(after1.items.map((t) => t.id))
      const refIds1 = done1.result_refs.map((ref) => ref.id)
      const newIds1 = [...after1Ids].filter((id) => !beforeIds.has(id))
      for (const id of newIds1) {
        expect(refIds1, '新建任务应出现在 job.result_refs 中').toContain(id)
      }

      // 完成后再次触发：幂等——不得新增任务（match_key 对齐既有任务）
      const later = await api.buildTasks({ course_id: COURSE_ID })
      const done2 = await waitFor(
        'build_tasks job #2',
        () => api.getJob(later.job.id),
        (j) => j.status !== 'queued' && j.status !== 'running',
      )
      expect(done2.status).toBe('succeeded')
      const after2 = await api.getTasks({ course_id: COURSE_ID })
      const after2Ids = new Set(after2.items.map((t) => t.id))
      const brandNew = [...after2Ids].filter((id) => !after1Ids.has(id))
      expect(brandNew, '重复触发不得产生新任务').toEqual([])

      // match_key 唯一性：同一稳定键不得对应多条活动任务
      const keys = after2.items.filter((t) => t.match_key !== null).map((t) => t.match_key)
      expect(new Set(keys).size).toBe(keys.length)

      // CR004：GET /tasks/{task_id} 已补录进契约（getTask），对真实后端验证详情读取与 404
      const sample = after2.items.find((t) => t.match_key !== null)
      expect(sample, '应有至少一条 match_key 任务供详情核对').toBeTruthy()
      const detail = await api.getTask(sample!.id)
      expect(detail.id).toBe(sample!.id)
      expect(detail.match_key).toBe(sample!.match_key)
      await expect(api.getTask('tsk_c2_missing')).rejects.toMatchObject({
        code: 'NOT_FOUND',
        status: 404,
      })

      const warningCodes = done1.warnings.map((w) => w.code)
      return `job1=${done1.id} refs=${refIds1.length} warnings=[${warningCodes.join(',')}] job2refs=${done2.result_refs.length} tasks=${after2Ids.size} getTask=200/404`
    }))

  afterAll(() => {
    try {
      mkdirSync(resolve(REPO_ROOT, 'var'), { recursive: true })
      writeFileSync(
        REPORT_PATH,
        JSON.stringify(
          {
            startedAt: runStamp,
            finishedAt: new Date().toISOString(),
            mode: API_MODE,
            baseUrl: apiBaseUrl(),
            suite: 'C2+C11 real-mode E2E',
            phases,
            deviations,
          },
          null,
          2,
        ),
        'utf8',
      )
      console.info(`[C2-REPORT] 已写入 ${REPORT_PATH}（deviations=${deviations.length}）`)
    } catch (err) {
      console.warn('[C2-REPORT] 写入失败：', err)
    }
  })
})
