import dayjs from 'dayjs'
import { beforeEach, describe, expect, it } from 'vitest'
import type { PlanPreviewResponse } from '../../../types/contract'
import { filterTodayTasks } from '../../tasks'
import { DEFAULT_TIMEZONE, dayKey, toApiIso } from '../../time'
import { api } from '../client'
import { getDb, resetMockDb, tickJobs } from './db'

function at(dayOffset: number, hhmm: string): string {
  const base = dayjs().tz(DEFAULT_TIMEZONE)
  const [h, m] = hhmm.split(':').map(Number)
  return toApiIso(base.add(dayOffset, 'day').hour(h).minute(m).second(0).millisecond(0), DEFAULT_TIMEZONE)
}

/** mock 模式只读取 name 与 size；用 defineProperty 直接设定大小，避免真实分配大内存 */
function fakeFile(name: string, size: number, type = 'application/pdf'): File {
  const file = new File(['x'], name, { type })
  Object.defineProperty(file, 'size', { value: size })
  return file
}

/** 第一次 tick 进入 running，第二次按给定延迟推进到完成 */
function tickToCompletion(delayMs: number): void {
  tickJobs(getDb())
  tickJobs(getDb(), Date.now() + delayMs)
}

/** GET /tasks/{id}（CR004 已补录为契约端点 getTask）：与后端同语义（含归档，未找到 404） */
async function fetchTaskById(taskId: string) {
  return api.getTask(taskId)
}

beforeEach(() => {
  resetMockDb()
})

describe('模拟后端契约形状', () => {
  it('课程 / 任务 / 作业 / 资源 / 回放字段齐全，缺失值不猜测', async () => {
    const courses = await api.getCourses()
    expect(courses.items.length).toBeGreaterThanOrEqual(3)
    const ds = courses.items.find((c) => c.id === 'course_ds')
    expect(ds?.source_refs[0]?.platform).toBe('xzzd')
    expect(ds?.source_refs[0]?.external_id).toBe('XZZD-CS2010-2026F')
    const ml = courses.items.find((c) => c.id === 'course_ml')
    expect(ml?.term).toBeNull()
    expect(ml?.source_refs).toHaveLength(0)

    const tasks = await api.getTasks()
    const fix2 = tasks.items.find((t) => t.id === 'task_ds_fix2')
    expect(fix2?.due_at).not.toBeNull()
    expect(fix2?.status).toBe('pending')

    const dsAssignments = await api.getCourseAssignments('course_ds')
    const noDue = dsAssignments.items.find((a) => a.id === 'asg_ds_lab')
    expect(noDue?.due_at).toBeNull()
    expect(noDue?.due_precision).toBe('unknown')
    expect(noDue?.fact_source).toBe('platform')
    const netAssignments = await api.getCourseAssignments('course_net')
    expect(netAssignments.items.find((a) => a.id === 'asg_net_quiz')?.submission_status).toBe('submitted')

    const resources = await api.getResources('course_ds')
    const ch3 = resources.items.find((r) => r.id === 'res_ds_ch3')
    expect(ch3?.version).toBe(2)
    expect(ch3?.parse_status).toBe('ready')
    expect(ch3?.source_refs).toHaveLength(1)
    const versions = await api.getResourceVersions('res_ds_ch3')
    expect(versions.items.map((v) => v.version)).toEqual([2, 1])

    const jobs = await api.getJobs({ type: 'parse' })
    const failedJob = jobs.items.find((j) => j.id === 'job_parse_1')
    expect(failedJob?.status).toBe('failed')
    expect(failedJob?.errors[0]?.retryable).toBe(true)

    const recordings = await api.getRecordings('course_net')
    expect(recordings.items.find((r) => r.id === 'rec_net_l5')?.transcript_status).toBe('unavailable')

    const chunks = await api.getResourceChunks('res_net_l4_tx')
    expect(chunks.items.length).toBeGreaterThan(0)
    const locator = chunks.items[0].locator
    expect(locator.kind).toBe('timestamp')
    if (locator.kind === 'timestamp') {
      expect(typeof locator.start_seconds).toBe('number')
    }
  })

  it('错误响应带稳定的 code 与 request_id', async () => {
    await expect(api.getCourse('course_missing')).rejects.toMatchObject({
      code: 'NOT_FOUND',
      requestId: expect.stringMatching(/^req_/),
    })
  })

  it('GET /tasks/{id}（CR004 补录端点）：单任务全字段、未找到 404、归档仍可读', async () => {
    const task = await api.getTask('task_ds_read3')
    expect(task.id).toBe('task_ds_read3')
    expect(task.match_key).toBeTruthy()
    expect(task.revision).toBeGreaterThanOrEqual(1)

    await expect(api.getTask('task_missing')).rejects.toMatchObject({
      code: 'NOT_FOUND',
      status: 404,
    })

    await api.deleteTask('task_ds_read3')
    const archived = await api.getTask('task_ds_read3')
    expect(archived.archived_at).not.toBeNull()
  })

  it('今日任务视图过滤（客户端）与时间块/截止语义一致', async () => {
    const tasks = (await api.getTasks()).items
    const plan = (await api.getCurrentPlan()).plan
    const today = filterTodayTasks(tasks, plan)
    expect(today.map((t) => t.id).sort()).toEqual(['task_ds_read3', 'task_net_rec4'])
  })
})

describe('导入去重、版本与解析', () => {
  it('同一幂等键重放返回同一后台任务', async () => {
    const first = await api.importFiles(
      { course_id: 'course_ds' },
      [fakeFile('重放.pdf', 16)],
      'key-replay',
    )
    expect(first.job.status).toBe('queued')
    const replay = await api.importFiles(
      { course_id: 'course_ds' },
      [fakeFile('重放.pdf', 16)],
      'key-replay',
    )
    expect(replay.job.id).toBe(first.job.id)
  })

  it('相同文件指纹重复导入被跳过（不新建资源）', async () => {
    const accepted = await api.importFiles(
      { course_id: 'course_ds' },
      [fakeFile('实验指导书.pdf', 1048576)],
      'key-seeded',
    )
    tickToCompletion(1000)
    const job = await api.getJob(accepted.job.id)
    expect(job.status).toBe('succeeded')
    expect(job.result_refs).toHaveLength(0)

    const resources = await api.getResources('course_ds')
    expect(resources.items.find((r) => r.id === 'res_ds_lab')?.version).toBe(1)
  })

  it('同名新文件保存为新版本并保留历史', async () => {
    await api.importFiles({ course_id: 'course_ds' }, [fakeFile('新讲义.pdf', 4)], 'key-v1')
    tickToCompletion(5000)
    await api.importFiles({ course_id: 'course_ds' }, [fakeFile('新讲义.pdf', 8)], 'key-v2')
    tickToCompletion(5000)

    const resources = await api.getResources('course_ds')
    const current = resources.items.find((r) => r.title === '新讲义.pdf')
    expect(current?.version).toBe(2)
    expect(current?.parse_status).toBe('ready')

    const versions = await api.getResourceVersions(current!.id)
    expect(versions.items.map((v) => v.version)).toEqual([2, 1])

    const chunks = await api.getResourceChunks(current!.id)
    expect(chunks.items).toHaveLength(3)
    expect(chunks.items.every((c) => c.resource_version === 2)).toBe(true)
  })

  it('不支持的类型与超过 64MB 上限的文件被拒绝（PD-1 上限）', async () => {
    await expect(
      api.importFiles({ course_id: 'course_ds' }, [fakeFile('note.txt', 10)], 'key-txt'),
    ).rejects.toMatchObject({ code: 'UNSUPPORTED_MEDIA_TYPE' })
    await expect(
      api.importFiles(
        { course_id: 'course_ds' },
        [fakeFile('big.pdf', 65 * 1024 * 1024)],
        'key-big',
      ),
    ).rejects.toMatchObject({ code: 'FILE_TOO_LARGE' })
  })

  it('42.9MB 真实样例在 64MB 上限内被受理并解析完成（PD-1 回归）', async () => {
    const bytes = 44983910
    const accepted = await api.importFiles(
      { course_id: 'course_ds' },
      [fakeFile('第6章 图算法 讲义（用户上传）.pdf', bytes)],
      'key-huge-ok',
    )
    tickToCompletion(5000)
    const job = await api.getJob(accepted.job.id)
    expect(job.status).toBe('succeeded')

    const resources = await api.getResources('course_ds')
    const uploaded = resources.items.find((r) => r.title === '第6章 图算法 讲义（用户上传）.pdf')
    expect(uploaded?.size_bytes).toBe(bytes)
    expect(uploaded?.parse_status).toBe('ready')
  })

  it('重新解析：处理中不可重复触发，完成后片段数与内容一致', async () => {
    const accepted = await api.reparseResource('res_ds_outline')
    const parsing = await api.getResources('course_ds')
    expect(parsing.items.find((r) => r.id === 'res_ds_outline')?.parse_status).toBe('processing')
    await expect(api.reparseResource('res_ds_outline')).rejects.toMatchObject({ code: 'CONFLICT' })

    tickToCompletion(5000)
    const job = await api.getJob(accepted.job.id)
    expect(job.status).toBe('succeeded')

    const after = await api.getResources('course_ds')
    const outline = after.items.find((r) => r.id === 'res_ds_outline')
    expect(outline?.parse_status).toBe('ready')

    const chunks = await api.getResourceChunks('res_ds_outline')
    expect(chunks.items).toHaveLength(outline?.chunk_count ?? 0)
  })

  it('已解析资料的 chunk_count 与 /chunks 实际内容一致', async () => {
    const resources = await api.getResources('course_ds')
    for (const resource of resources.items.filter((r) => r.chunk_count !== null)) {
      const chunks = await api.getResourceChunks(resource.id)
      expect(chunks.items).toHaveLength(resource.chunk_count ?? 0)
    }
  })
})

describe('计划预览 / 应用 / 撤销', () => {
  it('预览标记无法安排任务的原因码，并保留锁定块', async () => {
    const preview = await api.previewPlan()
    expect(preview.plan.status).toBe('candidate')
    expect(preview.plan.input_revision).toBe('ir_4')
    expect(preview.current_input_revision).toBe('ir_4')

    const fix2 = preview.plan.unassigned.find((u) => u.task_id === 'task_ds_fix2')
    expect(fix2?.reason_code).toBe('DEADLINE_PASSED')
    const mockExam = preview.plan.unassigned.find((u) => u.task_id === 'task_ds_mock')
    expect(mockExam?.reason_code).toBe('TASK_TOO_LONG_INDIVISIBLE')

    const ex3 = preview.plan.blocks.filter((b) => b.task_id === 'task_ds_ex3')
    expect(ex3).toHaveLength(1)
    expect(ex3[0].id).toBe('blk_4_3')
    expect(ex3[0].locked).toBe(true)
  })

  it('同一输入下预览结果确定（按任务与日期比对）', async () => {
    const a = await api.previewPlan()
    const b = await api.previewPlan()
    const pick = (p: PlanPreviewResponse) =>
      p.plan.blocks.map((block) => `${block.task_id}:${dayKey(block.starts_at)}`).sort()
    expect(pick(b)).toEqual(pick(a))
  })

  it('输入变化后再应用返回 PLAN_STALE（retryable=false，需重新预览）', async () => {
    const preview = await api.previewPlan()
    const availability = await api.getAvailability()
    await api.saveAvailability({
      timezone: availability.timezone,
      weekly_windows: availability.weekly_windows,
      date_exceptions: availability.date_exceptions,
      daily_limit_minutes: 165,
    })
    await expect(api.applyPlan(preview.plan.id)).rejects.toMatchObject({
      code: 'PLAN_STALE',
      retryable: false,
      details: { plan_id: preview.plan.id, input_revision: 'ir_4', current_input_revision: 'ir_5' },
    })
  })

  it('应用候选计划后撤销，可回到上一生效版本', async () => {
    const preview = await api.previewPlan()
    const applied = await api.applyPlan(preview.plan.id)
    expect(applied.plan.status).toBe('active')
    expect(applied.previous_plan_id).toBe('plan_active_4')

    const current = await api.getCurrentPlan()
    expect(current.plan?.id).toBe(preview.plan.id)
    expect(current.plan?.status).toBe('active')

    const revoked = await api.revokePlan(preview.plan.id)
    expect(revoked.plan.status).toBe('revoked')
    expect(revoked.restored_plan_id).toBe('plan_active_4')

    const restored = await api.getCurrentPlan()
    expect(restored.plan?.id).toBe('plan_active_4')
    expect(restored.plan?.status).toBe('active')
  })
})

describe('锁定时间块（v0.1 仅锁/解锁，不支持移动）', () => {
  it('可切换锁定状态', async () => {
    const unlocked = await api.lockTask('task_ds_ex3', { locked: false })
    expect(unlocked.plan?.blocks.find((b) => b.task_id === 'task_ds_ex3')?.locked).toBe(false)

    const relocked = await api.lockTask('task_ds_ex3', { locked: true })
    expect(relocked.plan?.blocks.find((b) => b.task_id === 'task_ds_ex3')?.locked).toBe(true)
  })

  it('尝试携带新时间返回明确错误：首版不支持移动', async () => {
    await expect(
      api.lockTask('task_ds_ex3', { locked: true, starts_at: at(1, '20:00') }),
    ).rejects.toMatchObject({
      code: 'VALIDATION_FAILED',
      details: { field: 'starts_at', mock_reason: 'block_move_not_supported' },
    })
  })
})

describe('学习反馈与平台提交状态分离', () => {
  it('完成学习任务只设置学习状态，并提示可重排计划', async () => {
    const response = await api.submitFeedback('task_net_rec4', {
      outcome: 'completed',
      actual_minutes: 50,
      difficulty: 2,
    })
    expect(response.task.status).toBe('completed')
    expect(response.feedback.outcome).toBe('completed')
    expect(response.feedback.actual_minutes).toBe(50)
    expect(response.replan_suggested).toBe(true)
    expect(response.current_input_revision).toBe('ir_5')
  })

  it('本次未做不改任务状态，也不建议重排', async () => {
    const response = await api.submitFeedback('task_ml_read1', { outcome: 'skipped' })
    expect(response.task.status).toBe('pending')
    expect(response.replan_suggested).toBe(false)
  })

  it('非法反馈返回 VALIDATION_FAILED', async () => {
    await expect(
      api.submitFeedback('task_net_rec4', { outcome: 'completed', difficulty: 9 }),
    ).rejects.toMatchObject({ code: 'VALIDATION_FAILED' })
  })
})

describe('后台任务与连接', () => {
  it('失败或部分失败的任务可重试，已成功任务不可重试', async () => {
    const retry = await api.retryJob('job_parse_1')
    expect(['queued', 'running']).toContain(retry.job.status)
    expect(retry.job.id).not.toBe('job_parse_1')

    await expect(api.retryJob('job_export_1')).rejects.toMatchObject({ code: 'CONFLICT' })
  })

  it('排队任务可取消，推进后进入已取消', async () => {
    const created = await api.createExport()
    const cancelled = await api.cancelJob(created.job.id)
    expect(cancelled.cancel_requested).toBe(true)
    const after = await api.getJob(created.job.id)
    expect(after.status).toBe('cancelled')
  })

  it('导出任务完成后提供结果引用', async () => {
    const accepted = await api.createExport()
    tickToCompletion(3000)
    const job = await api.getJob(accepted.job.id)
    expect(job.status).toBe('succeeded')
    expect(job.result_refs[0]?.type).toBe('export_file')
  })

  it('ICS 导出受理并产出独立产物（镜像 B19）', async () => {
    const accepted = await api.createExport({ format: 'ics' })
    tickToCompletion(3000)
    const job = await api.getJob(accepted.job.id)
    expect(job.status).toBe('succeeded')
    expect(job.result_refs[0]?.type).toBe('export_file')
    expect(job.result_refs[0]?.id.startsWith('exp_ics_')).toBe(true)
  })

  it('未知导出格式与 ICS+include 被拒收，同键异体 → 409（镜像 B19）', async () => {
    const badFormat = { format: 'xml' } as unknown as { format?: 'json' | 'ics' }
    await expect(api.createExport(badFormat)).rejects.toMatchObject({
      status: 400,
      code: 'VALIDATION_FAILED',
    })
    await expect(api.createExport({ format: 'ics', include: ['tasks'] })).rejects.toMatchObject({
      status: 400,
      code: 'VALIDATION_FAILED',
    })

    const key = `idem-export-${Date.now()}`
    const first = await api.createExport(undefined, key)
    const replay = await api.createExport(undefined, key)
    expect(replay.job.id).toBe(first.job.id)
    await expect(api.createExport({ format: 'ics' }, key)).rejects.toMatchObject({
      status: 409,
      code: 'IDEMPOTENCY_KEY_REUSED',
    })
  })

  it('登录失效的连接同步返回 AUTH_EXPIRED（不可重试）', async () => {
    await expect(api.syncConnection('conn_zykt')).rejects.toMatchObject({
      code: 'AUTH_EXPIRED',
      retryable: false,
    })
  })

  it('禁用后重新启用智云课堂回到 auth_required 并提供官方登录入口', async () => {
    const disabled = await api.disableConnection('conn_zykt')
    expect(disabled.status).toBe('disabled')
    expect(disabled.recovery_action).toBeNull()

    const enabled = await api.enableConnection('conn_zykt')
    expect(enabled.status).toBe('auth_required')
    expect(enabled.recovery_action?.entry_url).toBe('https://classroom.zju.edu.cn/')
    expect(enabled.entry_url).toBe('https://classroom.zju.edu.cn/')
  })

  it('手动检查连接：由检查结果驱动（ok 保持、未登录不解锁、停用拒绝）', async () => {
    const checked = await api.checkConnection('conn_xzzd')
    expect(checked.status).toBe('ok')
    expect(checked.last_checked_at).toBeTruthy()

    const stillPending = await api.checkConnection('conn_zykt')
    expect(stillPending.status).toBe('auth_required')
    expect(stillPending.recovery_action?.entry_url).toBe('https://classroom.zju.edu.cn/')

    await api.disableConnection('conn_zykt')
    await expect(api.checkConnection('conn_zykt')).rejects.toMatchObject({
      status: 409,
      code: 'CONFLICT',
    })
  })

  it('xzzd 同步产生部分失败任务：403/404 不可重试，5xx 可重试（A5b 语义）', async () => {
    const accepted = await api.syncConnection('conn_xzzd', 'sync-key-1')
    tickToCompletion(10_000)
    const job = await api.getJob(accepted.job.id)
    expect(job.status).toBe('partial_failed')
    const byCode = new Map(job.errors.map((e) => [e.code, e]))
    expect(byCode.get('PERMISSION_DENIED')?.retryable).toBe(false)
    expect(byCode.get('PERMISSION_DENIED')?.message).toContain('权限限制')
    expect(byCode.get('NOT_FOUND')?.retryable).toBe(false)
    expect(byCode.get('FETCH_FAILED')?.retryable).toBe(true)
  })
})

describe('可用时间保存', () => {
  it('合法时整体替换，非法时返回 VALIDATION_FAILED 且不落库', async () => {
    const availability = await api.getAvailability()
    const saved = await api.saveAvailability({
      timezone: availability.timezone,
      weekly_windows: availability.weekly_windows,
      date_exceptions: availability.date_exceptions,
      daily_limit_minutes: 120,
    })
    expect(saved.daily_limit_minutes).toBe(120)
    expect(saved.updated_at).toBeTruthy()

    await expect(
      api.saveAvailability({
        timezone: availability.timezone,
        weekly_windows: availability.weekly_windows,
        date_exceptions: availability.date_exceptions,
        daily_limit_minutes: 5,
      }),
    ).rejects.toMatchObject({
      code: 'VALIDATION_FAILED',
      details: { issues: expect.any(Array) },
    })

    const after = await api.getAvailability()
    expect(after.daily_limit_minutes).toBe(120)
  })
})

describe('平台来源挂接', () => {
  it('已绑定到其他课程的来源返回 409 并指明占用课程；重复绑定本课幂等', async () => {
    await expect(
      api.addCourseSourceRef('course_ml', {
        platform: 'xzzd',
        external_id: 'XZZD-CS2010-2026F',
        canonical_url: 'https://example.com/xzzd/courses/CS2010',
        observed_at: new Date().toISOString(),
      }),
    ).rejects.toMatchObject({
      code: 'CONFLICT',
      details: { course_id: 'course_ds', course_title: '数据结构与算法' },
    })

    const ref = {
      platform: 'xzzd',
      external_id: 'XZZD-ML-2026F',
      canonical_url: 'https://example.com/xzzd/courses/ML',
      observed_at: new Date().toISOString(),
    }
    const attached = await api.addCourseSourceRef('course_ml', ref)
    expect(attached.source_refs.map((r) => r.external_id)).toEqual(['XZZD-ML-2026F'])

    const again = await api.addCourseSourceRef('course_ml', ref)
    expect(again.source_refs).toHaveLength(1)
  })
})

describe('搜索', () => {
  it('命中当前版本的片段并给出得分', async () => {
    const hits = await api.search('哈希')
    expect(hits.items.length).toBeGreaterThan(0)
    expect(hits.items[0].resource_id).toBe('res_ds_ch3')
    expect(hits.items.every((h) => h.score > 0)).toBe(true)
  })
})

describe('任务生成 build_tasks（B13 语义镜像）', () => {
  it('触发生成按稳定 match_key 落库，版本/内容重复告警如实上报', async () => {
    const before = await api.getTasks({ course_id: 'course_ds' })
    const accepted = await api.buildTasks({ course_id: 'course_ds' })
    expect(accepted.job.type).toBe('build_tasks')
    expect(accepted.job.status).toBe('queued')
    expect(accepted.job.warnings).toEqual([])

    tickToCompletion(1000)
    const job = await api.getJob(accepted.job.id)
    expect(job.status).toBe('succeeded')
    const codes = job.warnings.map((w) => w.code)
    expect(codes).toContain('TASK_VERSION_UPDATED')
    expect(codes).toContain('DUPLICATE_CONTENT')
    for (const warning of job.warnings) {
      expect(warning.message.length).toBeGreaterThan(0)
      expect(Array.isArray(warning.task_ids)).toBe(true)
    }
    expect(job.result_refs.length).toBeGreaterThan(0)
    expect(job.result_refs.every((ref) => ref.type === 'task')).toBe(true)

    const after = await api.getTasks({ course_id: 'course_ds' })
    expect(after.items.length).toBeGreaterThan(before.items.length)
    const keys = after.items.filter((t) => t.match_key !== null).map((t) => t.match_key)
    expect(new Set(keys).size).toBe(keys.length)
  })

  it('同范围生成中重复触发复用同一 job；完成后重复触发无新增任务', async () => {
    const first = await api.buildTasks({ course_id: 'course_ds' })
    const second = await api.buildTasks({ course_id: 'course_ds' })
    expect(second.job.id).toBe(first.job.id)
    tickToCompletion(1000)

    const count1 = (await api.getTasks({ course_id: 'course_ds' })).items.length
    const third = await api.buildTasks({ course_id: 'course_ds' })
    expect(third.job.id).not.toBe(first.job.id)
    tickToCompletion(1000)
    const job = await api.getJob(third.job.id)
    expect(job.status).toBe('succeeded')
    expect(job.result_refs).toHaveLength(0)
    const count2 = (await api.getTasks({ course_id: 'course_ds' })).items.length
    expect(count2).toBe(count1)
  })

  it('人工修正（耗时/状态）不被重新生成覆盖', async () => {
    const task = await api.patchTask('task_ds_read3', { estimated_minutes: 42, status: 'completed' }, 1)
    expect(task.estimated_minutes).toBe(42)

    const accepted = await api.buildTasks({ course_id: 'course_ds' })
    tickToCompletion(1000)
    await api.getJob(accepted.job.id)

    const after = await fetchTaskById('task_ds_read3')
    expect(after.estimated_minutes).toBe(42)
    expect(after.status).toBe('completed')
  })

  it('来源消失的既有任务原样保留并告警 EXISTING_TASK_UNMATCHED', async () => {
    const db = getDb()
    const task = db.tasks.find((t) => t.id === 'task_ds_read3')!
    task.match_key = 't:course_ds:reading:已删除的资料.pdf'

    const accepted = await api.buildTasks({ course_id: 'course_ds' })
    tickToCompletion(1000)
    const job = await api.getJob(accepted.job.id)
    const unmatched = job.warnings.find((w) => w.code === 'EXISTING_TASK_UNMATCHED')
    expect(unmatched?.task_ids).toEqual(['t:course_ds:reading:已删除的资料.pdf'])

    const after = await fetchTaskById('task_ds_read3')
    expect(after.match_key).toBe('t:course_ds:reading:已删除的资料.pdf')
  })

  it('归档任务不复活，也不改写', async () => {
    await api.deleteTask('task_ds_read3')

    const accepted = await api.buildTasks({ course_id: 'course_ds' })
    tickToCompletion(1000)
    await api.getJob(accepted.job.id)

    const archived = await fetchTaskById('task_ds_read3')
    expect(archived.archived_at).not.toBeNull()
    const all = await api.getTasks({ course_id: 'course_ds', include_archived: true })
    const sameKey = all.items.filter((t) => t.match_key === 't:course_ds:reading:第3章-哈希表-讲义.pdf')
    expect(sameKey).toHaveLength(1)
    expect(sameKey[0].id).toBe('task_ds_read3')
  })

  it('未知课程返回 404 NOT_FOUND', async () => {
    await expect(api.buildTasks({ course_id: 'course_none' })).rejects.toMatchObject({
      code: 'NOT_FOUND',
    })
  })
})

describe('PD-3 已提交作业任务保留生成但不排程（镜像 B 侧 plan_engine 排除）', () => {
  async function buildNetTasks() {
    const accepted = await api.buildTasks({ course_id: 'course_net' })
    tickToCompletion(1000)
    await api.getJob(accepted.job.id)
    const quiz = (await api.getTasks({ course_id: 'course_net' })).items.find(
      (t) => t.match_key === 't:course_net:assignment:ASG-NET-QUIZ1',
    )
    expect(quiz).toBeDefined()
    return quiz!
  }

  it('已提交作业对应的任务保留在库；预览既不安排也不列入无法安排', async () => {
    const quiz = await buildNetTasks()
    expect(quiz.status).toBe('pending')

    const preview = await api.previewPlan()
    expect(preview.plan.blocks.some((b) => b.task_id === quiz.id)).toBe(false)
    expect(preview.plan.unassigned.some((u) => u.task_id === quiz.id)).toBe(false)
  })

  it('提交状态翻转后旧候选被拒（PLAN_STALE），重新预览恢复参与排程', async () => {
    const quiz = await buildNetTasks()
    const preview = await api.previewPlan()
    expect(preview.plan.blocks.some((b) => b.task_id === quiz.id)).toBe(false)

    await api.patchAssignment('asg_net_quiz', { submission_status: 'not_submitted' }, 1)
    await expect(api.applyPlan(preview.plan.id)).rejects.toMatchObject({ code: 'PLAN_STALE' })

    const preview2 = await api.previewPlan()
    const visible =
      preview2.plan.blocks.some((b) => b.task_id === quiz.id) ||
      preview2.plan.unassigned.some((u) => u.task_id === quiz.id)
    expect(visible).toBe(true)
  })
})
