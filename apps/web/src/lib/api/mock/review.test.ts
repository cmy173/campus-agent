import { beforeEach, describe, expect, it } from 'vitest'
import type { ProviderUpsertInput, ReviewMaterialItem, ReviewRequest } from '../../../types/review'
import { api } from '../client'
import { getDb, resetMockDb, tickJobs } from './db'
import { REVIEW_JOB_DURATION_MS } from './review'

function tickToCompletion(delayMs: number): void {
  tickJobs(getDb())
  tickJobs(getDb(), Date.now() + delayMs)
}

const API_KEY = 'sk-demo-abcd1234'
const NOTES = '第一行：侧重第三章\n第二行：多考概念辨析'

async function makeProvider(overrides: Partial<ProviderUpsertInput> = {}) {
  return api.createProvider({
    name: '本地推理服务',
    protocol: 'openai_chat_completions',
    base_url: 'http://127.0.0.1:11434/v1',
    model: 'demo-model',
    api_key: API_KEY,
    timeout_seconds: 120,
    ...overrides,
  })
}

function baseRequest(providerId: string | null, overrides: Partial<ReviewRequest> = {}): ReviewRequest {
  return {
    course_id: 'course_ds',
    mode: 'ai',
    provider_config_id: providerId,
    title: null,
    materials: {
      resources: [{ resource_id: 'res_ds_ch3', version: 2, chunk_ids: null }],
      assignments: [],
    },
    notes: NOTES,
    constraints: {
      question_types: [{ type: 'single_choice', count: 3, points_each: null }],
      difficulty: null,
      total_points: null,
      duration_minutes: null,
      answer_display: 'separate',
    },
    ...overrides,
  }
}

async function collectMaterials(courseId: string): Promise<ReviewMaterialItem[]> {
  const items: ReviewMaterialItem[] = []
  let cursor: string | undefined
  for (let i = 0; i < 20; i += 1) {
    const page = await api.getReviewMaterials(courseId, cursor)
    items.push(...page.items)
    if (!page.next_cursor) break
    cursor = page.next_cursor
  }
  return items
}

/** 生成一张卷并推进到完成；返回试卷 ID */
async function generatePaper(request: ReviewRequest, key?: string): Promise<string> {
  const { job } = await api.generateReview(request, key)
  tickToCompletion(REVIEW_JOB_DURATION_MS)
  const done = await api.getReviewJob(job.id)
  expect(done.status).toBe('succeeded')
  return done.result_refs?.[0]?.id as string
}

beforeEach(() => {
  resetMockDb()
})

describe('AI 服务配置（CR008：密钥脱敏 + PATCH 语义）', () => {
  it('创建：密钥只进内存密钥存储，响应与列表不含密钥明文', async () => {
    const created = await makeProvider()
    expect(created.name).toBe('本地推理服务')
    expect(created.protocol).toBe('openai_chat_completions')
    expect(created.base_url).toBe('http://127.0.0.1:11434/v1')
    expect(created.has_api_key).toBe(true)
    expect(created.last_tested_at).toBeNull()
    expect(JSON.stringify(created)).not.toContain(API_KEY)

    const listed = await api.listProviders()
    expect(JSON.stringify(listed)).not.toContain(API_KEY)
    expect(getDb().providerKeys.get(created.id)).toBe(API_KEY)

    const fetched = await api.getProvider(created.id)
    expect(fetched.id).toBe(created.id)
    expect(JSON.stringify(fetched)).not.toContain(API_KEY)
  })

  it('PATCH 语义：省略 api_key=保留、空串 400、清除密钥后 has_api_key=false', async () => {
    const provider = await makeProvider()
    const patched = await api.updateProvider(provider.id, { model: 'demo-model-v2' })
    expect(patched.model).toBe('demo-model-v2')
    expect(patched.has_api_key).toBe(true)
    expect(getDb().providerKeys.get(provider.id)).toBe(API_KEY)

    await expect(api.updateProvider(provider.id, { api_key: '' })).rejects.toMatchObject({
      status: 400,
      code: 'VALIDATION_FAILED',
    })

    await expect(api.clearProviderKey(provider.id)).resolves.toBeUndefined()
    const after = await api.getProvider(provider.id)
    expect(after.has_api_key).toBe(false)
    expect(getDb().providerKeys.has(provider.id)).toBe(false)

    await expect(api.updateProvider('prv_missing', { model: 'x' })).rejects.toMatchObject({
      status: 404,
      code: 'NOT_FOUND',
    })
  })

  it('显式 undefined 与缺省等价（镜像真实模式 JSON 序列化丢键）：创建不带密钥、PATCH 保留密钥', async () => {
    const withoutKey = await makeProvider({ api_key: undefined })
    expect(withoutKey.has_api_key).toBe(false)

    const provider = await makeProvider()
    const patched = await api.updateProvider(provider.id, { model: 'demo-model-v2', api_key: undefined })
    expect(patched.model).toBe('demo-model-v2')
    expect(patched.has_api_key).toBe(true)
    expect(getDb().providerKeys.get(provider.id)).toBe(API_KEY)
  })

  it('地址校验：缺协议头/公网 http/含端点/重复 v1 拒绝；合法地址规范化补 /v1', async () => {
    await expect(makeProvider({ base_url: '127.0.0.1:11434' })).rejects.toMatchObject({
      status: 400,
      code: 'VALIDATION_FAILED',
      message: expect.stringContaining('base_url 无效'),
    })
    await expect(makeProvider({ base_url: 'http://218.108.28.1/v1' })).rejects.toMatchObject({
      status: 400,
      message: expect.stringContaining('非本机/内网地址必须使用 https'),
    })
    await expect(
      makeProvider({ base_url: 'https://api.example.com/v1/chat/completions' }),
    ).rejects.toMatchObject({ status: 400, message: expect.stringContaining('/chat/completions') })
    await expect(makeProvider({ base_url: 'https://api.example.com/v1/v1' })).rejects.toMatchObject({
      status: 400,
      message: expect.stringContaining('重复的 /v1'),
    })

    const normalized = await makeProvider({ base_url: 'https://api.example.com' })
    expect(normalized.base_url).toBe('https://api.example.com/v1')
  })

  it('连接测试：无密钥=结果 ok=false（200 结果非异常）；模拟钩子给出结构化失败；成功写回 last_test_result', async () => {
    const noKey = await makeProvider({ api_key: null })
    const noKeyResult = await api.testProvider(noKey.id)
    expect(noKeyResult.result.ok).toBe(false)
    expect(noKeyResult.result.error?.code).toBe('PROVIDER_KEY_MISSING')
    expect(noKeyResult.result.latency_ms).toBeNull()

    const timeout = await api.testProvider((await makeProvider({ model: 'demo-timeout-sim' })).id)
    expect(timeout.result.ok).toBe(false)
    expect(timeout.result.error?.code).toBe('PROVIDER_TIMEOUT')
    expect(timeout.result.error?.retryable).toBe(true)

    const auth = await api.testProvider((await makeProvider({ model: 'demo-auth-sim' })).id)
    expect(auth.result.error?.code).toBe('PROVIDER_AUTH_FAILED')
    expect(auth.result.http_status).toBe(401)

    const ok = await api.testProvider((await makeProvider()).id)
    expect(ok.result.ok).toBe(true)
    expect(ok.result.error).toBeNull()
    expect(ok.result.latency_ms).toBeGreaterThan(0)
    expect(ok.provider.last_tested_at).toBe(ok.result.checked_at)
    expect(ok.provider.last_test_result?.ok).toBe(true)
    expect(JSON.stringify(ok)).not.toContain(API_KEY)
  })

  it('删除：进行中的出卷任务使用该配置时 409；任务结束后可删除', async () => {
    const provider = await makeProvider()
    const { job } = await api.generateReview(baseRequest(provider.id), 'del-busy-0001')
    await expect(api.deleteProvider(provider.id)).rejects.toMatchObject({
      status: 409,
      code: 'CONFLICT',
    })

    tickToCompletion(REVIEW_JOB_DURATION_MS)
    expect((await api.getReviewJob(job.id)).status).toBe('succeeded')
    await api.deleteProvider(provider.id)
    await expect(api.getProvider(provider.id)).rejects.toMatchObject({ status: 404 })
  })
})

describe('材料清单（游标分页 + 如实标注）', () => {
  it('翻页取全量：资源在前、作业在后，各组按 id 升序且不重复', async () => {
    const seen: ReviewMaterialItem[] = []
    let cursor: string | undefined
    let pages = 0
    do {
      const page = await api.getReviewMaterials('course_ds', cursor)
      seen.push(...page.items)
      cursor = page.next_cursor ?? undefined
      pages += 1
    } while (cursor && pages < 20)
    expect(pages).toBeGreaterThan(1)
    expect(seen).toHaveLength(11)
    expect(seen.slice(0, 8).map((m) => m.id)).toEqual([
      'res_ds_ch3',
      'res_ds_ch3_v1',
      'res_ds_ch6_scan',
      'res_ds_lab',
      'res_ds_lab_copy',
      'res_ds_link',
      'res_ds_outline',
      'res_ds_scan',
    ])
    expect(seen.slice(8).map((m) => m.id)).toEqual(['asg_ds_hw2', 'asg_ds_hw3', 'asg_ds_lab'])
    expect(seen.slice(0, 8).every((m) => m.kind === 'resource')).toBe(true)
    expect(seen.slice(8).every((m) => m.kind === 'assignment')).toBe(true)
  })

  it('不可用原因如实：权限受限优先 not_accessible，失败/未解析/类型跳过不假装可用', async () => {
    const byId = new Map((await collectMaterials('course_ds')).map((m) => [m.id, m]))
    expect(byId.get('res_ds_outline')).toMatchObject({ usable: false, unavailable_reason: 'not_accessible' })
    expect(byId.get('res_ds_scan')).toMatchObject({ usable: false, unavailable_reason: 'not_accessible' })
    expect(byId.get('res_ds_ch6_scan')).toMatchObject({ usable: false, unavailable_reason: 'not_parsed' })
    expect(byId.get('res_ds_link')).toMatchObject({ usable: false, unavailable_reason: 'type_skipped' })
    const ch3 = byId.get('res_ds_ch3')
    expect(ch3).toMatchObject({
      usable: true,
      unavailable_reason: null,
      parse_status: 'ready',
      version: 2,
      chunk_count: 3,
    })
    expect(ch3?.chars).toBeGreaterThan(0)
  })

  it('作业行：正文与截止如实；附件单列且带自身可用性；缺失附件不冒充正文', async () => {
    const byId = new Map((await collectMaterials('course_ds')).map((m) => [m.id, m]))
    const hw2 = byId.get('asg_ds_hw2')
    expect(hw2).toMatchObject({ kind: 'assignment', usable: true, parse_status: null, version: null })
    expect(hw2?.chars).toBeGreaterThan(0)
    expect(hw2?.due_at).not.toBeNull()
    expect(hw2?.attachments[0]).toMatchObject({ resource_id: 'res_ds_lab', usable: true })
    // 清单行附件保留原文标题；（作业附件）后缀只在快照 item 上（B 侧 _attachment_item vs resolve_selection）
    expect(hw2?.attachments[0]?.title).toBe('实验指导书.pdf')
    expect(byId.get('asg_ds_hw3')?.attachments).toEqual([])
  })

  it('无效游标 400：不静默返回首页', async () => {
    await expect(api.getReviewMaterials('course_ds', 'not-a-cursor')).rejects.toMatchObject({
      status: 400,
      code: 'VALIDATION_FAILED',
    })
  })
})

describe('预检（无副作用、不发送正文）', () => {
  it('provider 校验：AI/混合必须选有密钥配置；bank 不得携带；不存在 404', async () => {
    // B 侧 resolve_provider_block：顶层 message 恒为「请求参数校验失败」，具体原因在 details.fields[].reason
    await expect(api.preflightReview(baseRequest(null))).rejects.toMatchObject({
      status: 400,
      code: 'VALIDATION_FAILED',
      message: '请求参数校验失败',
      details: {
        fields: [{ field: 'provider_config_id', reason: 'AI/混合模式必须选择 AI 服务配置' }],
      },
    })
    await expect(api.preflightReview(baseRequest('prv_missing'))).rejects.toMatchObject({
      status: 404,
      code: 'NOT_FOUND',
      message: 'AI 服务配置不存在：prv_missing',
    })

    const noKey = await makeProvider({ api_key: null })
    await expect(api.preflightReview(baseRequest(noKey.id))).rejects.toMatchObject({
      status: 400,
      details: {
        fields: [{ field: 'provider_config_id', reason: '该配置尚未保存密钥；请先填写并测试连接' }],
      },
    })

    const provider = await makeProvider()
    await expect(
      api.preflightReview(baseRequest(provider.id, { mode: 'bank' })),
    ).rejects.toMatchObject({
      status: 400,
      details: {
        fields: [
          { field: 'provider_config_id', reason: 'bank 模式不调用模型，provider_config_id 须为 null' },
        ],
      },
    })

    const bank = await api.preflightReview(baseRequest(null, { mode: 'bank' }))
    expect(bank.provider.config_id).toBeNull()
    expect(bank.bank).toMatchObject({ mode: 'bank', usable: true, configured_sources: 1 })

    const ai = await api.preflightReview(baseRequest(provider.id))
    expect(ai.bank).toMatchObject({ mode: 'ai', usable: false, configured_sources: 0 })
    expect(ai.bank.message).toContain('当前为 AI 模式')
    expect(ai.provider).toMatchObject({
      config_id: provider.id,
      name: provider.name,
      model: 'demo-model',
      host: '127.0.0.1:11434',
      has_api_key: true,
    })
  })

  it('备注超限 400 不静默截断（明细含上限）；限值与字符数如实', async () => {
    const provider = await makeProvider()
    await expect(
      api.preflightReview(baseRequest(provider.id, { notes: '备'.repeat(20001) })),
    ).rejects.toMatchObject({ status: 400, code: 'VALIDATION_FAILED' })

    const pre = await api.preflightReview(baseRequest(provider.id))
    expect(pre.limits).toMatchObject({
      notes_max_chars: 20000,
      material_max_chars: 200000,
      question_count_total: 3,
      question_count_max: 100,
    })
    expect(pre.limits.notes_chars).toBe([...NOTES].length)
    expect(pre.materials.notes.text).toBe(NOTES)
    expect(pre.materials.notes.sha256).toMatch(/^sha256:[0-9a-f]{64}$/)
  })

  it('不可用材料列入 missing 不阻断；全不可用 400 资料不足', async () => {
    const provider = await makeProvider()
    const partial = baseRequest(provider.id, {
      materials: {
        resources: [
          { resource_id: 'res_ds_ch3', version: 2, chunk_ids: null },
          { resource_id: 'res_ds_outline', version: 1, chunk_ids: null },
        ],
        assignments: [],
      },
    })
    const pre = await api.preflightReview(partial)
    expect(pre.materials.items).toHaveLength(1)
    const gap = pre.materials.missing.find((m) => m.resource_id === 'res_ds_outline')
    expect(gap?.reason).toBe('not_accessible')
    expect(gap?.message).toContain('平台仅预览')
    expect(pre.warnings.map((w) => w.code)).toContain('MATERIAL_GAP')

    const onlyBad = baseRequest(provider.id, {
      materials: {
        resources: [{ resource_id: 'res_ds_scan', version: 1, chunk_ids: null }],
        assignments: [],
      },
    })
    await expect(api.preflightReview(onlyBad)).rejects.toMatchObject({
      status: 400,
      message: '资料不足，无法出卷',
    })
  })

  it('片段筛选：缺席片段进 missing 但保留可用片段；跨课程引用 400', async () => {
    const provider = await makeProvider()
    const chunkMissing = baseRequest(provider.id, {
      materials: {
        resources: [
          { resource_id: 'res_ds_ch3', version: 2, chunk_ids: ['chk_ds_ch3_0', 'chk_absent_9'] },
        ],
        assignments: [],
      },
    })
    const pre = await api.preflightReview(chunkMissing)
    expect(pre.materials.items[0]?.chunk_ids).toEqual(['chk_ds_ch3_0'])
    const gap = pre.materials.missing.find((m) => m.reason === 'chunk_missing')
    expect(gap?.message).toContain('已按可用片段纳入')

    const crossCourse = baseRequest(provider.id, {
      materials: {
        resources: [{ resource_id: 'res_net_l4', version: 1, chunk_ids: null }],
        assignments: [],
      },
    })
    await expect(api.preflightReview(crossCourse)).rejects.toMatchObject({ status: 400 })
  })

  it('作业与附件进快照：附件标题带（作业附件）后缀（清单行保留原文标题）', async () => {
    const provider = await makeProvider()
    const pre = await api.preflightReview(
      baseRequest(provider.id, {
        materials: {
          resources: [],
          assignments: [{ assignment_id: 'asg_ds_hw2', include_attachments: true }],
        },
      }),
    )
    const text = pre.materials.items.find((m) => m.kind === 'assignment_text')
    expect(text?.title).toBe('作业2：哈希表实现')
    expect(text?.assignment_id).toBe('asg_ds_hw2')
    const attachment = pre.materials.items.find((m) => m.kind === 'attachment_text')
    expect(attachment?.title).toBe('实验指导书.pdf（作业附件）')
    expect(attachment?.resource_id).toBe('res_ds_lab')
  })

  it('备注为空提示 NOTES_EMPTY；指纹随备注变化且格式合规', async () => {
    const provider = await makeProvider()
    const empty = await api.preflightReview(baseRequest(provider.id, { notes: '' }))
    expect(empty.warnings.map((w) => w.code)).toContain('NOTES_EMPTY')

    const a = await api.preflightReview(baseRequest(provider.id))
    const b = await api.preflightReview(baseRequest(provider.id, { notes: '换了备注' }))
    expect(a.fingerprint).toMatch(/^sha256:[0-9a-f]{64}$/)
    expect(a.fingerprint).not.toBe(b.fingerprint)
  })

  it('题库未配置：预检如实提示不阻断（预检不 400）', async () => {
    getDb().questionbankSources = getDb().questionbankSources.map((s) => ({ ...s, configured: false }))
    const bank = await api.preflightReview(baseRequest(null, { mode: 'bank' }))
    expect(bank.bank.usable).toBe(false)
    expect(bank.bank.message).toContain('未配置题库来源')
    expect(bank.warnings.map((w) => w.code)).toContain('BANK_NOT_CONFIGURED')
  })
})

describe('出卷作业（留档 / 幂等 / 取消 / 模式约束）', () => {
  it('备注换行原样进入请求留档与试卷快照；作业分阶段直到 saving', async () => {
    const provider = await makeProvider()
    const request = baseRequest(provider.id)
    const { job } = await api.generateReview(request, 'gen-notes-0001')
    expect(job.phase).toBe('materials')
    expect(getDb().reviewJobRequests.get(job.id)?.notes).toBe(NOTES)

    tickJobs(getDb())
    const running = await api.getReviewJob(job.id)
    expect(running.status).toBe('running')
    expect(typeof running.phase).toBe('string')

    tickToCompletion(REVIEW_JOB_DURATION_MS)
    const done = await api.getReviewJob(job.id)
    expect(done.status).toBe('succeeded')
    expect(done.phase).toBe('saving')

    const paper = await api.getReviewPaper(done.result_refs?.[0]?.id as string)
    expect(paper.revision).toBe(1)
    expect(paper.revision_reason).toBe('initial')
    expect(paper.based_on_revision).toBeNull()
    expect(paper.title).toBe('数据结构与算法 复习卷')
    expect(paper.snapshot.notes.text).toBe(NOTES)
    expect(paper.snapshot.notes.text).toContain('\n')
    expect(paper.questions).toHaveLength(3)
    expect(paper.questions.every((q) => q.source_refs.length > 0)).toBe(true)
    expect(paper.questions.every((q) => q.origin.kind === 'ai')).toBe(true)
    expect(paper.summary.question_count).toBe(3)
    expect(paper.provider.name).toBe(provider.name)
  })

  it('幂等：同键同体返回同一任务；同键异体 409；换备注为新任务', async () => {
    const provider = await makeProvider()
    const request = baseRequest(provider.id)
    const first = await api.generateReview(request, 'idem-key-0001')
    const second = await api.generateReview({ ...request }, 'idem-key-0001')
    expect(second.job.id).toBe(first.job.id)

    await expect(
      api.generateReview({ ...request, notes: '换了备注' }, 'idem-key-0001'),
    ).rejects.toMatchObject({ status: 409, code: 'IDEMPOTENCY_KEY_REUSED' })

    const other = await api.generateReview({ ...request, notes: '换了备注' }, 'idem-key-0002')
    expect(other.job.id).not.toBe(first.job.id)
  })

  it('取消：运行中任务协作式取消，不再产出试卷', async () => {
    const provider = await makeProvider()
    const { job } = await api.generateReview(baseRequest(provider.id), 'cancel-key-0001')
    tickJobs(getDb())
    const cancelled = await api.cancelJob(job.id)
    expect(cancelled.cancel_requested).toBe(true)

    tickToCompletion(REVIEW_JOB_DURATION_MS)
    expect((await api.getReviewJob(job.id)).status).toBe('cancelled')
    expect(getDb().papers).toHaveLength(0)
  })

  it('bank 模式无可用来源：生成 400 BANK_NOT_CONFIGURED；hybrid 放行但警告如实', async () => {
    getDb().questionbankSources = getDb().questionbankSources.map((s) => ({ ...s, configured: false }))
    await expect(api.generateReview(baseRequest(null, { mode: 'bank' }))).rejects.toMatchObject({
      status: 400,
      code: 'BANK_NOT_CONFIGURED',
    })

    const provider = await makeProvider()
    const paperId = await generatePaper(baseRequest(provider.id, { mode: 'hybrid' }))
    const paper = await api.getReviewPaper(paperId)
    expect(paper.mode).toBe('hybrid')
    expect(paper.warnings.map((w) => w.code)).toContain('BANK_NOT_CONFIGURED')
    expect(paper.summary.origin_counts).toMatchObject({ bank: 1, ai: 1, adapted_bank: 1 })
  })
})

describe('试卷 / 修订 / 导出（CR008）', () => {
  it('修订产生新版本并保留旧版；未改动的重做标记 regenerate；版本历史倒序', async () => {
    const provider = await makeProvider()
    const request = baseRequest(provider.id)
    const paperId = await generatePaper(request, 'rev-base-0001')

    const { job: reviseJob } = await api.createReviewRevision(paperId, { notes: '修订后的备注' })
    tickToCompletion(REVIEW_JOB_DURATION_MS)
    expect((await api.getReviewJob(reviseJob.id)).status).toBe('succeeded')
    const v2 = await api.getReviewPaper(paperId)
    expect(v2.revision).toBe(2)
    expect(v2.revision_reason).toBe('revise')
    expect(v2.based_on_revision).toBe(1)
    expect(v2.snapshot.notes.text).toBe('修订后的备注')

    const v1 = await api.getReviewPaper(paperId, 1)
    expect(v1.revision).toBe(1)
    expect(v1.snapshot.notes.text).toBe(NOTES)

    const { job: regenJob } = await api.createReviewRevision(paperId, {})
    tickToCompletion(REVIEW_JOB_DURATION_MS)
    expect((await api.getReviewJob(regenJob.id)).status).toBe('succeeded')
    const v3 = await api.getReviewPaper(paperId)
    expect(v3.revision).toBe(3)
    expect(v3.revision_reason).toBe('regenerate')
    expect(v3.based_on_revision).toBe(2)
    expect(v3.snapshot.notes.text).toBe('修订后的备注')

    const revisions = await api.listReviewRevisions(paperId)
    expect(revisions.items.map((r) => r.revision)).toEqual([3, 2, 1])
    expect(revisions.items[0]?.fingerprint).toMatch(/^sha256:/)
    expect(revisions.items[0]?.summary.question_count).toBe(3)

    await expect(api.getReviewPaper(paperId, 99)).rejects.toMatchObject({ status: 404 })
    await expect(api.createReviewRevision('pap_missing', {})).rejects.toMatchObject({ status: 404 })
  })

  it('列表：仅取各卷最新版本，course_id/mode 过滤可用', async () => {
    const provider = await makeProvider()
    await generatePaper(baseRequest(provider.id), 'list-a-0001')
    await generatePaper(baseRequest(provider.id, { notes: '第二张' }), 'list-b-0002')

    const all = await api.listReviewPapers({ course_id: 'course_ds' })
    expect(all.items).toHaveLength(2)
    expect(all.items.every((p) => p.revision === 1)).toBe(true)

    expect(await api.listReviewPapers({ course_id: 'course_ml' })).toMatchObject({ items: [] })
    expect(await api.listReviewPapers({ mode: 'bank' })).toMatchObject({ items: [] })
    expect((await api.listReviewPapers({ mode: 'ai' })).items).toHaveLength(2)

    await expect(api.getReviewPaper('pap_missing')).rejects.toMatchObject({
      status: 404,
      code: 'NOT_FOUND',
    })
  })

  it('导出：markdown 含备注原文与答案解析出处、中文文件名；学生版不含答案；答案版含', async () => {
    const provider = await makeProvider()
    const paperId = await generatePaper(baseRequest(provider.id), 'export-base-01')

    const md = await api.exportReviewPaper(paperId, 'markdown')
    expect(md.filename).toContain('数据结构与算法')
    expect(md.filename).toContain('复习卷.md')
    expect(md.content_type).toBe('text/markdown; charset=utf-8')
    expect(md.text).toContain(NOTES)
    expect(md.text).toContain('**答案**：')
    expect(md.text).toContain('**解析**：')
    expect(md.text).toContain('《第3章 哈希表 讲义.pdf》')

    const student = await api.exportReviewPaper(paperId, 'student')
    expect(student.filename).toContain('学生版.html')
    expect(student.content_type).toBe('text/html; charset=utf-8')
    expect(student.text).not.toContain('<strong>答案</strong>')
    expect(student.text).toContain('学生版')

    const answers = await api.exportReviewPaper(paperId, 'answers')
    expect(answers.filename).toContain('答案版.html')
    expect(answers.text).toContain('<strong>答案</strong>')

    await expect(
      api.exportReviewPaper(paperId, 'pdf' as never),
    ).rejects.toMatchObject({ status: 400 })
  })

  it('CR009：备注非空时最后一题带 note 出处（locator=备注第N行，其余 ID/URL 为 null）；导出文案为 locator', async () => {
    const provider = await makeProvider()
    const paperId = await generatePaper(baseRequest(provider.id), 'note-ref-01')
    const paper = await api.getReviewPaper(paperId)
    const last = paper.questions[paper.questions.length - 1]
    const noteRef = last.source_refs.find((r) => r.kind === 'note')
    expect(noteRef).toMatchObject({
      kind: 'note',
      resource_id: null,
      chunk_id: null,
      assignment_id: null,
      url: null,
      site_name: null,
      locator: '备注第1行',
    })
    expect(paper.questions[0].source_refs.some((r) => r.kind === 'note')).toBe(false)

    const md = await api.exportReviewPaper(paperId, 'markdown')
    expect(md.text).toContain('备注第1行')

    const bareId = await generatePaper(baseRequest(provider.id, { notes: '' }), 'note-ref-02')
    const bare = await api.getReviewPaper(bareId)
    expect(
      bare.questions.every((q) => q.source_refs.every((r) => r.kind !== 'note')),
    ).toBe(true)
  })

  it('导出指定 revision：历史版本导出的是该版快照', async () => {
    const provider = await makeProvider()
    const paperId = await generatePaper(baseRequest(provider.id), 'export-rev-001')
    const { job } = await api.createReviewRevision(paperId, { notes: '修订后的备注' })
    tickToCompletion(REVIEW_JOB_DURATION_MS)
    expect((await api.getReviewJob(job.id)).status).toBe('succeeded')

    const v1 = await api.exportReviewPaper(paperId, 'markdown', 1)
    expect(v1.text).toContain(NOTES)
    expect(v1.text).not.toContain('修订后的备注')

    const latest = await api.exportReviewPaper(paperId, 'markdown')
    expect(latest.text).toContain('修订后的备注')
  })

  it('题库能力：透传来源与配置状态，未配置来源不标已连接', async () => {
    const caps = await api.getQuestionbankCapabilities()
    expect(caps.sources).toHaveLength(2)
    expect(caps.sources.find((s) => s.id === 'qb_demo')?.configured).toBe(true)
    const pending = caps.sources.find((s) => s.id === 'qb_school_past')
    expect(pending?.configured).toBe(false)
    expect(pending?.requires_config).toBe(true)
  })
})
