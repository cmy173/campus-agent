import dayjs from 'dayjs'
import type { ContentChunk, Course, CursorPage, Job, Locator } from '../../../types/contract'
import type {
  BlueprintKnowledgePoint,
  MaterialUnavailableReason,
  ProviderConfig,
  ProviderPatchInput,
  ProviderTestResponse,
  ProviderTestResult,
  ProviderUpsertInput,
  QuestionbankCapabilities,
  ReviewBlueprint,
  ReviewConstraints,
  ReviewDifficulty,
  ReviewMaterialAttachment,
  ReviewMaterialItem,
  ReviewMaterialsPage,
  ReviewMode,
  ReviewPaper,
  ReviewPaperListPage,
  ReviewPaperRevisionListPage,
  ReviewPaperSummary,
  ReviewPaperWarning,
  ReviewPreflight,
  ReviewPreflightBank,
  ReviewPreflightProvider,
  ReviewProviderSnapshot,
  ReviewQuestion,
  ReviewQuestionSourceRef,
  ReviewQuestionType,
  ReviewRequest,
  ReviewRevisionReason,
  ReviewRevisionRequest,
  ReviewTextExport,
  SnapshotItem,
  SnapshotMissing,
  SnapshotNotes,
} from '../../../types/review'
import { DEFAULT_TIMEZONE, toApiIso } from '../../time'
import {
  REVIEW_DIFFICULTY_LABELS,
  REVIEW_MODE_LABELS,
  REVIEW_QUESTION_TYPE_LABELS,
  REVIEW_QUESTION_TYPES,
  reviewPhaseFor,
} from '../../review'
import { ApiError, newRequestId } from '../http'
import { canonicalJson, sha256Hex, sha256Tagged } from './sha256'
import { createJob, type MockDb } from './db'

/**
 * 复习卷模拟后端（CR008）：AI 服务配置、材料清单、预检、生成作业（分阶段/取消/重试/幂等）、
 * 试卷与修订、导出（直返文本）、题库能力。
 *
 * 限定声明：这是模拟管线——题目为确定性结构演示（出处如实指向所选片段/演示来源），
 * 不冒充真实模型输出；错误码/文案/游标/指纹公式逐条镜像 B 侧 review/providers 语义。
 * 密钥只进 providerKeys（模拟进程内存），任何响应/日志字段不含密钥。
 */

/** 模拟层引擎版本（真实模式为 D 引擎版本，由 B 在快照/试卷中给出） */
export const REVIEW_ENGINE_VERSION = 'mock-engine-1.0.0'
export const REVIEW_JOB_DURATION_MS = 6000

const NOTES_MAX_CHARS = 20000
const MATERIAL_MAX_CHARS = 200000
const QUESTION_COUNT_MAX = 100
const DEFAULT_LIMIT = 50
const MAX_LIMIT = 200
/** 材料清单演示页幅：小页幅走通"翻页取全量"路径（真实端默认 50；游标语义与真实端一致） */
const MATERIALS_PAGE_SIZE = 4

// ==================== 基础工具 ====================

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

function fieldsFail(fields: Array<Record<string, unknown>>, extra?: Record<string, unknown>): never {
  fail(400, 'VALIDATION_FAILED', '请求参数校验失败', {
    details: { fields, ...(extra ?? {}) },
  })
}

function invalidCursor(cursor: string): never {
  fail(400, 'VALIDATION_FAILED', '无效的分页游标', { details: { cursor } })
}

function nowIso(db: MockDb): string {
  return toApiIso(dayjs(), db.availability.timezone || DEFAULT_TIMEZONE)
}

/** Unicode 码点长度（与 Python len 一致） */
function cpLen(text: string): number {
  return [...text].length
}

function cmpStr(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0
}

/** 游标编解码（镜像 B 侧 repo.encode_cursor/decode_cursor：base64("k:"+raw)） */
function encodeCursor(value: string): string {
  return btoa(`k:${value}`).replace(/\+/g, '-').replace(/\//g, '_')
}

function decodeCursor(cursor: string): string {
  try {
    const normalized = cursor.replace(/-/g, '+').replace(/_/g, '/')
    const padded = normalized + '='.repeat((4 - (normalized.length % 4)) % 4)
    const raw = atob(padded)
    if (!raw.startsWith('k:')) invalidCursor(cursor)
    return raw.slice(2)
  } catch (err) {
    if (err instanceof ApiError) throw err
    return invalidCursor(cursor)
  }
}

function readLimit(
  query: Record<string, string | number | boolean | undefined>,
  fallback: number,
): number {
  const raw = query.limit
  if (raw === undefined) return fallback
  const n = Number(raw)
  if (!Number.isFinite(n)) return fallback
  return Math.min(Math.max(Math.trunc(n), 1), MAX_LIMIT)
}

function requireCourse(db: MockDb, courseId: string): Course {
  const course = db.courses.find((c) => c.id === courseId)
  if (!course) fail(404, 'NOT_FOUND', `课程不存在：${courseId}`, { details: { kind: '课程', id: courseId } })
  return course
}

/** 幂等存储：同键同体（同指纹）返回首次结果；同键异体 409（镜像 handlers.idempotentExport 模式） */
function idempotentScoped<T>(
  db: MockDb,
  key: string | undefined,
  endpoint: string,
  fingerprint: string,
  produce: () => T,
): T {
  if (!key) return produce()
  const prefix = `${key}#${endpoint}#`
  const storeKey = `${prefix}${fingerprint}`
  const hit = db.idempotency.get(storeKey)
  if (hit !== undefined) return hit as T
  const reused = [...db.idempotency.keys()].some((k) => k.startsWith(prefix))
  if (reused) {
    fail(409, 'IDEMPOTENCY_KEY_REUSED', '幂等键已用于不同请求体', { details: { key, endpoint } })
  }
  const result = produce()
  db.idempotency.set(storeKey, result)
  return result
}

function requestFingerprintOf(rawBody: unknown): string {
  return sha256Hex(canonicalJson(rawBody))
}

// ==================== AI 服务配置（镜像 B ai_providers + providers 语义） ====================

const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '::1'])

function isLocalHttpHost(host: string): boolean {
  const h = host.replace(/^\[|\]$/g, '').toLowerCase()
  if (LOCAL_HOSTS.has(h)) return true
  const v4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(h)
  if (v4) {
    const a = Number(v4[1])
    const b = Number(v4[2])
    return (
      a === 127 ||
      a === 10 ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 168) ||
      (a === 169 && b === 254)
    )
  }
  // IPv6：回环 ::1、唯一本地 fd00::/8、链路本地 fe80::/10（近似 ipaddress.is_private/is_link_local）
  if (h === '::1') return true
  if (/^f[cd][0-9a-f]{2}:/.test(h)) return true
  if (/^fe[89ab]/.test(h)) return true
  return false
}

function badUrl(reason: string): never {
  fail(400, 'VALIDATION_FAILED', `base_url 无效：${reason}`, {
    details: { fields: [{ field: 'base_url', reason }] },
  })
}

/** 规范化 base_url（逐条镜像 B providers.normalize_base_url） */
function normalizeBaseUrl(raw: string): string {
  const text = raw.trim()
  if (!text) badUrl('不能为空')
  if (/\s/.test(text)) badUrl('地址中不得包含空白字符')
  const scheme = /^([a-zA-Z][a-zA-Z0-9+.-]*):/.exec(text)?.[1]?.toLowerCase()
  if (scheme !== 'http' && scheme !== 'https') badUrl('仅支持 http/https 地址')
  let url: URL
  try {
    url = new URL(text)
  } catch {
    badUrl('缺少主机名')
  }
  if (url.username || url.password) badUrl('地址中不得包含用户名/密码')
  if (url.search) badUrl('地址不得包含查询串')
  if (url.hash) badUrl('地址不得包含片段')
  const host = url.hostname
  if (!host) badUrl('缺少主机名')
  let port: number | null
  try {
    port = url.port ? Number(url.port) : null
  } catch {
    return badUrl('端口号不合法')
  }
  if (scheme === 'http' && !isLocalHttpHost(host)) badUrl('非本机/内网地址必须使用 https')
  let path = url.pathname.replace(/\/+$/, '')
  const low = path.toLowerCase()
  if (low.endsWith('/chat/completions')) {
    badUrl('请填写服务根地址或 /v1 地址，不要包含 /chat/completions')
  }
  if (low.includes('/v1/v1')) badUrl('路径出现重复的 /v1，请只保留一个')
  if (!path) path = '/v1'
  const netloc = `${host.includes(':') ? `[${host}]` : host}${port !== null ? `:${port}` : ''}`
  return `${scheme}://${netloc}${path}`
}

function readStr(body: Record<string, unknown>, field: string, options: { min?: number; max?: number }): string {
  const raw = body[field]
  if (typeof raw !== 'string') {
    fieldsFail([{ field, reason: '必须是字符串' }])
  }
  const min = options.min ?? 0
  const max = options.max ?? Infinity
  if (raw.length < min) fieldsFail([{ field, reason: `长度至少 ${min} 个字符` }])
  if (raw.length > max) fieldsFail([{ field, reason: `长度不能超过 ${max} 个字符` }])
  return raw
}

function readInt(
  body: Record<string, unknown>,
  field: string,
  options: { min: number; max: number },
): number {
  const raw = body[field]
  if (typeof raw !== 'number' || !Number.isInteger(raw)) {
    fieldsFail([{ field, reason: '必须是整数' }])
  }
  if (raw < options.min || raw > options.max) {
    fieldsFail([{ field, reason: `需在 ${options.min}–${options.max} 之间` }])
  }
  return raw
}

function readApiKey(body: Record<string, unknown>, allowNull: boolean): string | null | undefined {
  // undefined 视为缺省：真实模式经 JSON 序列化会丢弃该键，模拟层保持一致
  const raw = body.api_key
  if (raw === undefined) return undefined
  if (raw === null) return allowNull ? null : undefined
  if (typeof raw !== 'string') fieldsFail([{ field: 'api_key', reason: '必须是字符串或 null' }])
  if (raw.length === 0) {
    fieldsFail([
      { field: 'api_key', reason: '密钥不能为空字符串（避免误清空；清除密钥请用 DELETE /ai/providers/{id}/key）' },
    ])
  }
  if (raw.length > 4096) fieldsFail([{ field: 'api_key', reason: '长度不能超过 4096 个字符' }])
  return raw
}

function parseCreateProviderBody(rawBody: unknown): ProviderUpsertInput & { api_key: string | null } {
  const body = (rawBody ?? {}) as Record<string, unknown>
  const name = readStr(body, 'name', { min: 1, max: 80 }).trim()
  if (!name) fieldsFail([{ field: 'name', reason: '长度至少 1 个字符' }])
  const protocol = body.protocol === undefined ? 'openai_chat_completions' : body.protocol
  if (protocol !== 'openai_chat_completions') {
    fieldsFail([{ field: 'protocol', reason: '首版仅支持 openai_chat_completions（其他协议不静默套用）' }])
  }
  const baseUrl = normalizeBaseUrl(readStr(body, 'base_url', { min: 1 }))
  const model = readStr(body, 'model', { min: 1, max: 200 }).trim()
  if (!model) fieldsFail([{ field: 'model', reason: '长度至少 1 个字符' }])
  const timeout =
    body.timeout_seconds === undefined || body.timeout_seconds === null
      ? 120
      : readInt(body, 'timeout_seconds', { min: 5, max: 600 })
  const apiKey = readApiKey(body, true) ?? null
  return {
    name,
    protocol,
    base_url: baseUrl,
    model,
    api_key: apiKey,
    timeout_seconds: timeout,
  }
}

/** PATCH：仅"提供了且非 null"的字段生效；api_key 空串 400（镜像 B PatchProviderRequest） */
function parsePatchProviderBody(rawBody: unknown): ProviderPatchInput {
  const body = (rawBody ?? {}) as Record<string, unknown>
  const patch: ProviderPatchInput = {}
  if (body.name !== undefined && body.name !== null) {
    const name = readStr(body, 'name', { min: 1, max: 80 }).trim()
    if (!name) fieldsFail([{ field: 'name', reason: '长度至少 1 个字符' }])
    patch.name = name
  }
  if (body.protocol !== undefined && body.protocol !== null) {
    if (body.protocol !== 'openai_chat_completions') {
      fieldsFail([{ field: 'protocol', reason: '首版仅支持 openai_chat_completions（其他协议不静默套用）' }])
    }
    patch.protocol = body.protocol
  }
  if (body.base_url !== undefined && body.base_url !== null) {
    patch.base_url = normalizeBaseUrl(readStr(body, 'base_url', { min: 1 }))
  }
  if (body.model !== undefined && body.model !== null) {
    const model = readStr(body, 'model', { min: 1, max: 200 }).trim()
    if (!model) fieldsFail([{ field: 'model', reason: '长度至少 1 个字符' }])
    patch.model = model
  }
  if (body.timeout_seconds !== undefined && body.timeout_seconds !== null) {
    patch.timeout_seconds = readInt(body, 'timeout_seconds', { min: 5, max: 600 })
  }
  const apiKey = readApiKey(body, true)
  if (apiKey !== undefined && apiKey !== null) patch.api_key = apiKey
  return patch
}

function providerOf(db: MockDb, id: string): ProviderConfig {
  const provider = db.providers.find((p) => p.id === id)
  if (!provider) fail(404, 'NOT_FOUND', `AI 服务配置不存在：${id}`, { details: { kind: 'AI 服务配置', id } })
  return provider
}

export function listProviders(
  db: MockDb,
  query: Record<string, string | number | boolean | undefined> = {},
): CursorPage<ProviderConfig> {
  const limit = readLimit(query, DEFAULT_LIMIT)
  let rows = [...db.providers].sort(
    (a, b) => cmpStr(b.created_at, a.created_at) || cmpStr(b.id, a.id),
  )
  if (query.cursor !== undefined) {
    const raw = decodeCursor(String(query.cursor))
    const sep = raw.indexOf('|')
    const created = sep >= 0 ? raw.slice(0, sep) : ''
    const lastId = sep >= 0 ? raw.slice(sep + 1) : ''
    if (!created || !lastId) invalidCursor(String(query.cursor))
    rows = rows.filter(
      (p) => p.created_at < created || (p.created_at === created && p.id < lastId),
    )
  }
  const page = rows.slice(0, limit + 1)
  let nextCursor: string | null = null
  if (page.length > limit) {
    page.pop()
    const last = page[page.length - 1]
    nextCursor = encodeCursor(`${last.created_at}|${last.id}`)
  }
  return { items: page, next_cursor: nextCursor }
}

export function getProvider(db: MockDb, id: string): ProviderConfig {
  return providerOf(db, id)
}

export function createProvider(db: MockDb, rawBody: unknown): ProviderConfig {
  const input = parseCreateProviderBody(rawBody)
  db.seq += 1
  const now = nowIso(db)
  const provider: ProviderConfig = {
    id: `prv_${db.seq}`,
    name: input.name,
    protocol: input.protocol,
    base_url: input.base_url,
    model: input.model,
    has_api_key: false,
    timeout_seconds: input.timeout_seconds ?? 120,
    last_tested_at: null,
    last_test_result: null,
    created_at: now,
    updated_at: now,
  }
  db.providers.push(provider)
  if (input.api_key) {
    db.providerKeys.set(provider.id, input.api_key)
    provider.has_api_key = true
  }
  return provider
}

export function updateProvider(db: MockDb, id: string, rawBody: unknown): ProviderConfig {
  const provider = providerOf(db, id)
  const patch = parsePatchProviderBody(rawBody)
  if (patch.name !== undefined) provider.name = patch.name
  if (patch.protocol !== undefined) provider.protocol = patch.protocol
  if (patch.base_url !== undefined) provider.base_url = patch.base_url
  if (patch.model !== undefined) provider.model = patch.model
  if (patch.timeout_seconds !== undefined) provider.timeout_seconds = patch.timeout_seconds
  // api_key 缺省/null=保留；空串已在解析时 400（清密钥走 DELETE /key）
  if (patch.api_key) {
    db.providerKeys.set(id, patch.api_key)
    provider.has_api_key = true
  }
  provider.updated_at = nowIso(db)
  return provider
}

function requestUsesProvider(db: MockDb, jobId: string, providerId: string): boolean {
  const gen = db.reviewJobRequests.get(jobId)
  if (gen && gen.provider_config_id === providerId) return true
  const rev = db.reviewRevisionRequests.get(jobId)
  const raw = rev?.request as { provider_config_id?: unknown } | undefined
  return Boolean(raw && typeof raw.provider_config_id === 'string' && raw.provider_config_id === providerId)
}

export function deleteProvider(db: MockDb, id: string): void {
  providerOf(db, id)
  const busy = db.jobs.find(
    (j) =>
      j.type === 'review_generate' &&
      (j.status === 'queued' || j.status === 'running') &&
      requestUsesProvider(db, j.id, id),
  )
  if (busy) {
    fail(409, 'CONFLICT', '有进行中的出卷任务使用该配置，待任务结束后重试', {
      details: { job_id: busy.id, provider_id: id },
    })
  }
  db.providers = db.providers.filter((p) => p.id !== id)
  db.providerKeys.delete(id)
}

export function clearProviderKey(db: MockDb, id: string): void {
  const provider = providerOf(db, id)
  db.providerKeys.delete(id)
  provider.has_api_key = false
  provider.updated_at = nowIso(db)
}

/** 模拟连接测试（只发最小内容；远端失败也是结果本身，响应不含密钥） */
function simulateConnectionTest(db: MockDb, provider: ProviderConfig): ProviderTestResult {
  const checkedAt = nowIso(db)
  const key = db.providerKeys.get(provider.id)
  if (!key) {
    return {
      ok: false,
      checked_at: checkedAt,
      latency_ms: null,
      http_status: null,
      error: {
        code: 'PROVIDER_KEY_MISSING',
        message: '尚未配置密钥，填写密钥后可测试连通',
        retryable: false,
        details: null,
      },
    }
  }
  const latencySeed = 140 + ((provider.base_url.length + provider.model.length) * 37) % 360
  const failure = (
    code: string,
    message: string,
    retryable: boolean,
    httpStatus: number | null,
    latency: number,
  ): ProviderTestResult => ({
    ok: false,
    checked_at: checkedAt,
    latency_ms: latency,
    http_status: httpStatus,
    error: { code, message, retryable, details: null },
  })
  // 确定性演示钩子：模型名后缀模拟不同失败（真实模式由服务端实际请求得出）
  if (provider.model.endsWith('-timeout-sim')) {
    return failure('PROVIDER_TIMEOUT', `请求超时（${provider.timeout_seconds}s）`, true, null, provider.timeout_seconds * 1000)
  }
  if (provider.model.endsWith('-auth-sim')) {
    return failure('PROVIDER_AUTH_FAILED', '服务端拒绝了凭据（HTTP 401），请检查密钥与地址', false, 401, latencySeed)
  }
  if (provider.model.endsWith('-rate-sim')) {
    return failure('PROVIDER_RATE_LIMITED', '服务端限流（HTTP 429），稍后重试', true, 429, latencySeed)
  }
  if (provider.model.endsWith('-badjson-sim')) {
    return failure('PROVIDER_INVALID_RESPONSE', '响应不是合法 JSON（地址可能不是 OpenAI 兼容服务）', false, 200, latencySeed)
  }
  if (provider.model.endsWith('-unreachable-sim')) {
    return failure('PROVIDER_UNREACHABLE', '无法连接服务地址', true, null, latencySeed)
  }
  if (provider.model.endsWith('-http404-sim')) {
    return failure('PROVIDER_HTTP_ERROR', '服务端返回 HTTP 404，请检查地址与模型名', false, 404, latencySeed)
  }
  if (provider.model.endsWith('-http500-sim')) {
    return failure('PROVIDER_HTTP_ERROR', '服务端返回 HTTP 500，稍后重试', true, 500, latencySeed)
  }
  return { ok: true, checked_at: checkedAt, latency_ms: latencySeed, http_status: 200, error: null }
}

export function testProvider(db: MockDb, id: string): ProviderTestResponse {
  const provider = providerOf(db, id)
  const result = simulateConnectionTest(db, provider)
  provider.last_tested_at = result.checked_at
  provider.last_test_result = result
  provider.updated_at = result.checked_at
  return { provider, result }
}

// ==================== 材料清单（镜像 B review.list_materials） ====================

const UNUSABLE_MESSAGES: Record<MaterialUnavailableReason, string> = {
  not_parsed: '尚未解析；可先对该文件执行解析后重试。',
  parse_failed: '解析失败；可重新解析该文件后重试。',
  type_skipped: '该类型文件不参与正文解析（如压缩包）。',
  no_text: '解析产物无可读文本（如扫描页）。',
  chunk_missing: '所选片段不存在或不属于所选版本；请重新选择片段。',
  attachment_absent: '作业附件缺失或未获取；可重新同步该作业。',
  not_accessible: '平台仅预览、无下载权限，无法获取正文；可在平台内查看或手动上传可读版本。',
}

function unusableMessage(reason: MaterialUnavailableReason): string {
  return UNUSABLE_MESSAGES[reason]
}

/** 资源可用性（权限受限优先于解析状态；chunk_count==0 → no_text） */
function resourceUsability(
  accessLevel: string,
  parseStatus: string,
  chunkCount: number,
): { usable: boolean; reason: MaterialUnavailableReason | null } {
  if (accessLevel !== 'full') return { usable: false, reason: 'not_accessible' }
  if (parseStatus === 'failed') return { usable: false, reason: 'parse_failed' }
  if (parseStatus === 'skipped') return { usable: false, reason: 'type_skipped' }
  if (parseStatus !== 'ready') return { usable: false, reason: 'not_parsed' }
  if (chunkCount === 0) return { usable: false, reason: 'no_text' }
  return { usable: true, reason: null }
}

/** (resource_id, version) → (count, chars)；chars 含片段间 \n 分隔（与正文拼接口径一致） */
function chunkStatsMap(db: MockDb, resourceIds: string[]): Map<string, { count: number; chars: number }> {
  const wanted = new Set(resourceIds)
  const stats = new Map<string, { count: number; chars: number }>()
  for (const chunk of db.chunks) {
    if (!wanted.has(chunk.resource_id)) continue
    const key = `${chunk.resource_id}#${chunk.resource_version}`
    const cur = stats.get(key) ?? { count: 0, chars: 0 }
    cur.count += 1
    cur.chars += cpLen(chunk.text)
    stats.set(key, cur)
  }
  for (const [key, value] of stats) {
    stats.set(key, { count: value.count, chars: value.chars + Math.max(0, value.count - 1) })
  }
  return stats
}

function missingAttachmentItem(resourceId: string): ReviewMaterialAttachment {
  return {
    resource_id: resourceId,
    title: resourceId,
    parse_status: null,
    version: null,
    chunk_count: null,
    chars: null,
    usable: false,
    unavailable_reason: 'attachment_absent',
  }
}

function attachmentItem(
  resource: { id: string; title: string; parse_status: string; version: number; access_level: string; updated_at: string },
  stats: { count: number; chars: number },
): ReviewMaterialAttachment {
  const { usable, reason } = resourceUsability(resource.access_level, resource.parse_status, stats.count)
  return {
    resource_id: resource.id,
    title: resource.title,
    parse_status: resource.parse_status as ReviewMaterialAttachment['parse_status'],
    version: resource.version,
    chunk_count: resource.parse_status === 'ready' ? stats.count : null,
    chars: usable ? stats.chars : null,
    usable,
    unavailable_reason: reason,
  }
}

export function listReviewMaterials(
  db: MockDb,
  courseId: string,
  query: Record<string, string | number | boolean | undefined> = {},
): ReviewMaterialsPage {
  requireCourse(db, courseId)
  const resources = db.resources
    .filter((r) => r.course_id === courseId && r.archived_at === null)
    .sort((a, b) => cmpStr(a.id, b.id))
  const assignments = db.assignments
    .filter((a) => a.course_id === courseId && a.archived_at === null)
    .sort((a, b) => cmpStr(a.id, b.id))

  const resourceMap = new Map(resources.map((r) => [r.id, r]))
  const attachmentIds = assignments
    .flatMap((a) => a.attachment_resource_ids)
    .filter((rid) => !resourceMap.has(rid))
  const extraResources = new Map(
    db.resources.filter((r) => attachmentIds.includes(r.id)).map((r) => [r.id, r]),
  )
  const stats = chunkStatsMap(db, [...resources.map((r) => r.id), ...extraResources.keys()])
  const statsFor = (id: string, version: number) => stats.get(`${id}#${version}`) ?? { count: 0, chars: 0 }

  const merged: Array<{ rank: number; id: string; item: ReviewMaterialItem }> = []
  for (const res of resources) {
    const { count, chars } = statsFor(res.id, res.version)
    const { usable, reason } = resourceUsability(res.access_level, res.parse_status, count)
    merged.push({
      rank: 0,
      id: res.id,
      item: {
        id: res.id,
        kind: 'resource',
        title: res.title,
        course_id: res.course_id,
        updated_at: res.updated_at,
        usable,
        unavailable_reason: reason,
        parse_status: res.parse_status,
        version: res.version,
        chunk_count: res.parse_status === 'ready' ? count : null,
        chars: usable ? chars : null,
        due_at: null,
        due_precision: null,
        attachments: [],
      },
    })
  }
  for (const asg of assignments) {
    const attachments: ReviewMaterialAttachment[] = []
    for (const rid of asg.attachment_resource_ids) {
      const att = resourceMap.get(rid) ?? extraResources.get(rid)
      if (!att || att.course_id !== courseId || att.archived_at !== null) {
        attachments.push(missingAttachmentItem(rid))
        continue
      }
      attachments.push(attachmentItem(att, statsFor(att.id, att.version)))
    }
    const instructions = asg.instructions ?? ''
    const usable = instructions.length > 0
    merged.push({
      rank: 1,
      id: asg.id,
      item: {
        id: asg.id,
        kind: 'assignment',
        title: asg.title,
        course_id: asg.course_id,
        updated_at: asg.updated_at,
        usable,
        unavailable_reason: usable ? null : 'no_text',
        parse_status: null,
        version: null,
        chunk_count: null,
        chars: usable ? cpLen(instructions) : null,
        due_at: asg.due_at,
        due_precision: asg.due_precision,
        attachments,
      },
    })
  }

  let rows = merged
  if (query.cursor !== undefined) {
    const raw = decodeCursor(String(query.cursor))
    const sep = raw.indexOf('|')
    const rankText = sep >= 0 ? raw.slice(0, sep) : ''
    const lastId = sep >= 0 ? raw.slice(sep + 1) : ''
    const afterRank = Number(rankText)
    if (!rankText || !Number.isInteger(afterRank) || !lastId) invalidCursor(String(query.cursor))
    rows = rows.filter(
      (m) => m.rank > afterRank || (m.rank === afterRank && cmpStr(m.id, lastId) > 0),
    )
  }
  const limit = readLimit(query, MATERIALS_PAGE_SIZE)
  const page = rows.slice(0, limit + 1)
  let nextCursor: string | null = null
  if (page.length > limit) {
    page.pop()
    const last = page[page.length - 1]
    nextCursor = encodeCursor(`${last.rank}|${last.id}`)
  }
  return { items: page.map((m) => m.item), next_cursor: nextCursor }
}

// ==================== 题库能力 ====================

export function getQuestionbankCapabilities(db: MockDb): QuestionbankCapabilities {
  return { sources: db.questionbankSources }
}

// ==================== 请求解析（镜像 schemas.ReviewRequestIn 校验层级） ====================

function parseQuestionTypes(raw: unknown): ReviewConstraints['question_types'] {
  if (!Array.isArray(raw) || raw.length === 0) {
    fieldsFail([{ field: 'constraints.question_types', reason: '至少 1 项题型配额（1–100 题/项）' }])
  }
  return raw.map((entry, idx) => {
    const q = (entry ?? {}) as Record<string, unknown>
    if (typeof q.type !== 'string' || !REVIEW_QUESTION_TYPES.includes(q.type as ReviewQuestionType)) {
      fieldsFail([
        {
          field: `constraints.question_types[${idx}].type`,
          reason: `未知题型（可选：${REVIEW_QUESTION_TYPES.join(' / ')}）`,
        },
      ])
    }
    if (typeof q.count !== 'number' || !Number.isInteger(q.count) || q.count < 1 || q.count > 100) {
      fieldsFail([{ field: `constraints.question_types[${idx}].count`, reason: '题数需为 1–100 的整数' }])
    }
    if (q.points_each !== undefined && q.points_each !== null) {
      if (
        typeof q.points_each !== 'number' ||
        !Number.isInteger(q.points_each) ||
        q.points_each < 1 ||
        q.points_each > 200
      ) {
        fieldsFail([{ field: `constraints.question_types[${idx}].points_each`, reason: '每题分值为 1–200 的整数或 null' }])
      }
    }
    return {
      type: q.type as ReviewQuestionType,
      count: q.count,
      points_each: (q.points_each ?? null) as number | null,
    }
  })
}

function parseConstraints(raw: unknown): ReviewConstraints {
  if (raw === undefined || raw === null || typeof raw !== 'object') {
    fieldsFail([{ field: 'constraints', reason: '缺少出卷约束（constraints）' }])
  }
  const body = raw as Record<string, unknown>
  const questionTypes = parseQuestionTypes(body.question_types)
  let difficulty: ReviewConstraints['difficulty'] = null
  if (body.difficulty !== undefined && body.difficulty !== null) {
    if (!['easy', 'medium', 'hard', 'mixed'].includes(body.difficulty as string)) {
      fieldsFail([{ field: 'constraints.difficulty', reason: '难度需为 easy / medium / hard / mixed 或 null' }])
    }
    difficulty = body.difficulty as ReviewConstraints['difficulty']
  }
  const readOptionalInt = (key: string, min: number, max: number): number | null => {
    const v = body[key]
    if (v === undefined || v === null) return null
    if (typeof v !== 'number' || !Number.isInteger(v) || v < min || v > max) {
      fieldsFail([{ field: `constraints.${key}`, reason: `需为 ${min}–${max} 的整数或 null` }])
    }
    return v
  }
  const answerDisplay = body.answer_display === undefined ? 'separate' : body.answer_display
  if (answerDisplay !== 'separate' && answerDisplay !== 'inline') {
    fieldsFail([{ field: 'constraints.answer_display', reason: '答案显示方式需为 separate / inline' }])
  }
  return {
    question_types: questionTypes,
    difficulty,
    total_points: readOptionalInt('total_points', 1, 500),
    duration_minutes: readOptionalInt('duration_minutes', 5, 600),
    answer_display: answerDisplay,
  }
}

function parseMaterials(raw: unknown): ReviewRequest['materials'] {
  if (raw === undefined || raw === null) return { resources: [], assignments: [] }
  if (typeof raw !== 'object') fieldsFail([{ field: 'materials', reason: '必须是对象' }])
  const body = raw as { resources?: unknown; assignments?: unknown }
  const resourcesRaw = body.resources ?? []
  const assignmentsRaw = body.assignments ?? []
  if (!Array.isArray(resourcesRaw)) fieldsFail([{ field: 'materials.resources', reason: '必须是数组' }])
  if (!Array.isArray(assignmentsRaw)) fieldsFail([{ field: 'materials.assignments', reason: '必须是数组' }])
  const resources = (resourcesRaw as unknown[]).map((entry, idx) => {
    const r = (entry ?? {}) as Record<string, unknown>
    if (typeof r.resource_id !== 'string' || !r.resource_id) {
      fieldsFail([{ field: `materials.resources[${idx}].resource_id`, reason: '必须为非空字符串' }])
    }
    let version: number | null = null
    if (r.version !== undefined && r.version !== null) {
      if (typeof r.version !== 'number' || !Number.isInteger(r.version) || r.version < 1) {
        fieldsFail([{ field: `materials.resources[${idx}].version`, reason: '版本需为 ≥1 的整数或 null' }])
      }
      version = r.version
    }
    let chunkIds: string[] | null = null
    if (r.chunk_ids !== undefined && r.chunk_ids !== null) {
      if (!Array.isArray(r.chunk_ids) || r.chunk_ids.length === 0) {
        fieldsFail([{ field: `materials.resources[${idx}].chunk_ids`, reason: '须为非空片段 ID 数组或 null' }])
      }
      for (const cid of r.chunk_ids as unknown[]) {
        if (typeof cid !== 'string' || !cid) {
          fieldsFail([{ field: `materials.resources[${idx}].chunk_ids`, reason: '片段 ID 必须为非空字符串' }])
        }
      }
      chunkIds = [...new Set(r.chunk_ids as string[])]
    }
    return { resource_id: r.resource_id as string, version, chunk_ids: chunkIds }
  })
  const assignments = (assignmentsRaw as unknown[]).map((entry, idx) => {
    const a = (entry ?? {}) as Record<string, unknown>
    if (typeof a.assignment_id !== 'string' || !a.assignment_id) {
      fieldsFail([{ field: `materials.assignments[${idx}].assignment_id`, reason: '必须为非空字符串' }])
    }
    if (a.include_attachments !== undefined && typeof a.include_attachments !== 'boolean') {
      fieldsFail([{ field: `materials.assignments[${idx}].include_attachments`, reason: '必须是布尔值' }])
    }
    return {
      assignment_id: a.assignment_id as string,
      include_attachments: a.include_attachments === undefined ? true : (a.include_attachments as boolean),
    }
  })
  return { resources, assignments }
}

export function parseReviewRequest(rawBody: unknown): ReviewRequest {
  const body = (rawBody ?? {}) as Record<string, unknown>
  if (typeof body.course_id !== 'string' || !body.course_id) {
    fieldsFail([{ field: 'course_id', reason: '必须为非空字符串' }])
  }
  if (body.mode !== 'ai' && body.mode !== 'bank' && body.mode !== 'hybrid') {
    fieldsFail([{ field: 'mode', reason: '出题模式需为 ai / bank / hybrid' }])
  }
  let providerConfigId: string | null = null
  if (body.provider_config_id !== undefined && body.provider_config_id !== null) {
    if (typeof body.provider_config_id !== 'string' || !body.provider_config_id) {
      fieldsFail([{ field: 'provider_config_id', reason: '必须为非空字符串或 null' }])
    }
    providerConfigId = body.provider_config_id
  }
  let title: string | null = null
  if (body.title !== undefined && body.title !== null) {
    if (typeof body.title !== 'string') fieldsFail([{ field: 'title', reason: '必须是字符串或 null' }])
    if (body.title.length > 120) fieldsFail([{ field: 'title', reason: '卷名不能超过 120 个字符' }])
    title = body.title
  }
  if (body.notes !== undefined && typeof body.notes !== 'string') {
    fieldsFail([{ field: 'notes', reason: '备注必须是字符串（可为空字符串）' }])
  }
  return {
    course_id: body.course_id as string,
    mode: body.mode as ReviewMode,
    provider_config_id: providerConfigId,
    title,
    materials: parseMaterials(body.materials),
    notes: typeof body.notes === 'string' ? body.notes : '',
    constraints: parseConstraints(body.constraints),
  }
}

/** 修订请求：全字段可空，未提供=沿用上一版（不做跨字段必填约束） */
export function parseRevisionRequest(rawBody: unknown): ReviewRevisionRequest {
  const body = (rawBody ?? {}) as Record<string, unknown>
  const patch: ReviewRevisionRequest = {}
  if (body.provider_config_id !== undefined && body.provider_config_id !== null) {
    if (typeof body.provider_config_id !== 'string' || !body.provider_config_id) {
      fieldsFail([{ field: 'provider_config_id', reason: '必须为非空字符串或 null' }])
    }
    patch.provider_config_id = body.provider_config_id
  }
  if (body.title !== undefined && body.title !== null) {
    if (typeof body.title !== 'string') fieldsFail([{ field: 'title', reason: '必须是字符串或 null' }])
    if (body.title.length > 120) fieldsFail([{ field: 'title', reason: '卷名不能超过 120 个字符' }])
    patch.title = body.title
  }
  if (body.materials !== undefined && body.materials !== null) {
    patch.materials = parseMaterials(body.materials)
  }
  if (body.notes !== undefined && body.notes !== null) {
    if (typeof body.notes !== 'string') fieldsFail([{ field: 'notes', reason: '备注必须是字符串或 null' }])
    patch.notes = body.notes
  }
  if (body.constraints !== undefined && body.constraints !== null) {
    patch.constraints = parseConstraints(body.constraints)
  }
  return patch
}

function mergeReviewRequest(prev: ReviewRequest, patch: ReviewRevisionRequest): ReviewRequest {
  const merged: ReviewRequest = structuredClone(prev)
  if (patch.provider_config_id !== undefined && patch.provider_config_id !== null) {
    merged.provider_config_id = patch.provider_config_id
  }
  if (patch.title !== undefined && patch.title !== null) merged.title = patch.title
  if (patch.materials !== undefined) merged.materials = patch.materials
  if (patch.notes !== undefined && patch.notes !== null) merged.notes = patch.notes
  if (patch.constraints !== undefined) merged.constraints = patch.constraints
  return merged
}

// ==================== 限制校验 / 备注 / 指纹（镜像 B review.validate_review_limits 等） ====================

function validateReviewLimits(notesText: string, constraints: ReviewConstraints, totalChars: number): void {
  const fields: Array<Record<string, unknown>> = []
  const notesChars = cpLen(notesText)
  if (notesChars > NOTES_MAX_CHARS) {
    fields.push({
      field: 'notes',
      reason: `备注 ${notesChars} 字符，超过上限 ${NOTES_MAX_CHARS}（不静默截断，请精简后重试）`,
      max_chars: NOTES_MAX_CHARS,
    })
  }
  if (totalChars > MATERIAL_MAX_CHARS) {
    fields.push({
      field: 'materials',
      reason: `材料正文合计 ${totalChars} 字符，超过上限 ${MATERIAL_MAX_CHARS}（请减少所选片段）`,
      max_chars: MATERIAL_MAX_CHARS,
    })
  }
  const total = constraints.question_types.reduce((sum, q) => sum + q.count, 0)
  if (total > QUESTION_COUNT_MAX) {
    fields.push({
      field: 'constraints.question_types',
      reason: `题数合计 ${total} 超过单卷上限 ${QUESTION_COUNT_MAX}`,
      max_count: QUESTION_COUNT_MAX,
    })
  }
  if (
    constraints.total_points !== null &&
    constraints.question_types.length > 0 &&
    constraints.question_types.every((q) => q.points_each !== null)
  ) {
    const summed = constraints.question_types.reduce((sum, q) => sum + q.count * (q.points_each ?? 0), 0)
    if (summed !== constraints.total_points) {
      fields.push({
        field: 'constraints.total_points',
        reason: `各题型分值合计 ${summed} 与总分 ${constraints.total_points} 不一致（请调整 points_each，或置 null 由引擎分配）`,
      })
    }
  }
  if (fields.length > 0) fieldsFail(fields)
}

function notesPayload(text: string): SnapshotNotes {
  return { text, sha256: sha256Tagged(text), chars: cpLen(text) }
}

function computeReviewFingerprint(
  courseId: string,
  mode: ReviewMode,
  engineVersion: string,
  notes: SnapshotNotes,
  constraints: ReviewConstraints,
  items: SnapshotItem[],
): string {
  const payload = {
    course_id: courseId,
    mode,
    engine_version: engineVersion,
    notes_sha256: notes.sha256,
    constraints,
    materials: items.map((it) => ({
      kind: it.kind,
      id: it.resource_id ?? it.assignment_id,
      version: it.version,
      content_hash: it.content_hash,
      chunk_ids: [...it.chunk_ids],
      chars: it.chars,
    })),
  }
  return sha256Tagged(canonicalJson(payload))
}

// ==================== 选择解析（镜像 B review.resolve_selection） ====================

function missingEntry(
  kind: SnapshotMissing['kind'],
  resourceId: string | null,
  assignmentId: string | null,
  title: string,
  reason: MaterialUnavailableReason,
  message?: string,
): SnapshotMissing {
  return { kind, resource_id: resourceId, assignment_id: assignmentId, title, reason, message: message ?? unusableMessage(reason) }
}

function materialItemFromChunks(
  kind: SnapshotItem['kind'],
  resourceId: string | null,
  assignmentId: string | null,
  title: string,
  version: number | null,
  contentHash: string | null,
  chunks: ContentChunk[],
): SnapshotItem {
  const ordered = [...chunks].sort((a, b) => cmpStr(a.id, b.id))
  const text = ordered.map((c) => c.text).join('\n')
  return {
    kind,
    resource_id: resourceId,
    assignment_id: assignmentId,
    title,
    version,
    content_hash: contentHash,
    chunk_ids: ordered.map((c) => c.id),
    locators: ordered.map((c) => ({ chunk_id: c.id, locator: c.locator })),
    chars: cpLen(text),
    text,
  }
}

function chunksFor(db: MockDb, resourceId: string, version: number): ContentChunk[] {
  return db.chunks
    .filter((c) => c.resource_id === resourceId && c.resource_version === version)
    .sort((a, b) => a.ordinal - b.ordinal || cmpStr(a.id, b.id))
}

function resolveSelection(
  db: MockDb,
  courseId: string,
  selection: ReviewRequest['materials'],
): { items: SnapshotItem[]; missing: SnapshotMissing[] } {
  const items: SnapshotItem[] = []
  const missing: SnapshotMissing[] = []
  const seenResources = new Set<string>()
  for (const ref of selection.resources) {
    const dedupeKey = JSON.stringify([ref.resource_id, ref.version ?? null])
    if (seenResources.has(dedupeKey)) continue
    seenResources.add(dedupeKey)
    const res = db.resources.find((r) => r.id === ref.resource_id)
    if (!res) fail(404, 'NOT_FOUND', `资源不存在：${ref.resource_id}`, { details: { kind: '资源', id: ref.resource_id } })
    if (res.course_id !== courseId) {
      fieldsFail([{ field: 'materials.resources', reason: `资源 ${ref.resource_id} 不属于该课程` }])
    }
    if (res.archived_at !== null) {
      fieldsFail([{ field: 'materials.resources', reason: `资源 ${ref.resource_id} 已归档，不可用于出卷` }])
    }
    const version = ref.version ?? res.version
    if (ref.version !== null && ref.version > res.version) {
      fieldsFail([
        {
          field: 'materials.resources',
          reason: `资源 ${ref.resource_id} 当前版本为 ${res.version}，请求版本 ${ref.version} 不存在`,
        },
      ])
    }
    const versionRow =
      version === res.version
        ? null
        : db.resourceVersions.find((v) => v.resource_id === res.id && v.version === version)
    if (version !== res.version && !versionRow) {
      fieldsFail([{ field: 'materials', reason: `资源 ${res.id} 无版本 ${version}（引用不存在）` }])
    }
    const parseStatus = versionRow ? versionRow.parse_status : res.parse_status
    const contentHash = versionRow ? versionRow.content_hash : res.content_hash
    const { usable, reason } = resourceUsability(res.access_level, parseStatus, 1)
    if (!usable && reason !== 'no_text' && reason !== null) {
      missing.push(missingEntry('resource', res.id, null, res.title, reason))
      continue
    }
    const chunks = chunksFor(db, res.id, version)
    if (chunks.length === 0) {
      missing.push(missingEntry('resource', res.id, null, res.title, 'no_text'))
      continue
    }
    let selected = chunks
    if (ref.chunk_ids !== null) {
      const wanted = new Set(ref.chunk_ids)
      selected = chunks.filter((c) => wanted.has(c.id))
      const absent = [...wanted].filter((cid) => !selected.some((c) => c.id === cid))
      if (absent.length > 0) {
        missing.push(
          missingEntry(
            'resource',
            res.id,
            null,
            res.title,
            'chunk_missing',
            `所选片段中有 ${absent.length} 个不存在或不属于所选版本（已按可用片段纳入）。`,
          ),
        )
      }
    }
    if (selected.length === 0) continue
    items.push(materialItemFromChunks('resource_text', res.id, null, res.title, version, contentHash, selected))
  }
  const seenAssignments = new Set<string>()
  for (const ref of selection.assignments) {
    if (seenAssignments.has(ref.assignment_id)) continue
    seenAssignments.add(ref.assignment_id)
    const asg = db.assignments.find((a) => a.id === ref.assignment_id)
    if (!asg) fail(404, 'NOT_FOUND', `作业不存在：${ref.assignment_id}`, { details: { kind: '作业', id: ref.assignment_id } })
    if (asg.course_id !== courseId) {
      fieldsFail([{ field: 'materials.assignments', reason: `作业 ${ref.assignment_id} 不属于该课程` }])
    }
    if (asg.archived_at !== null) {
      fieldsFail([{ field: 'materials.assignments', reason: `作业 ${ref.assignment_id} 已归档，不可用于出卷` }])
    }
    const instructions = asg.instructions ?? ''
    if (!instructions) {
      missing.push(missingEntry('assignment', null, asg.id, asg.title, 'no_text'))
    } else {
      items.push({
        kind: 'assignment_text',
        resource_id: null,
        assignment_id: asg.id,
        title: asg.title,
        version: null,
        content_hash: null,
        chunk_ids: [],
        locators: [],
        chars: cpLen(instructions),
        text: instructions,
      })
    }
    if (ref.include_attachments) {
      for (const rid of asg.attachment_resource_ids) {
        const att = db.resources.find((r) => r.id === rid)
        if (!att || att.course_id !== courseId || att.archived_at !== null) {
          missing.push(missingEntry('attachment', rid, asg.id, rid, 'attachment_absent'))
          continue
        }
        const { usable: attUsable, reason: attReason } = resourceUsability(att.access_level, att.parse_status, 1)
        if (!attUsable && attReason !== 'no_text' && attReason !== null) {
          missing.push(missingEntry('attachment', att.id, asg.id, att.title, attReason))
          continue
        }
        const chunks = chunksFor(db, att.id, att.version)
        if (chunks.length === 0) {
          missing.push(missingEntry('attachment', att.id, asg.id, att.title, 'no_text'))
          continue
        }
        items.push(
          materialItemFromChunks(
            'attachment_text',
            att.id,
            asg.id,
            `${att.title}（作业附件）`,
            att.version,
            att.content_hash,
            chunks,
          ),
        )
      }
    }
  }
  return { items, missing }
}

function ensureUsableSelection(items: SnapshotItem[], missing: SnapshotMissing[]): void {
  if (items.length > 0) return
  const reason = missing.length > 0 ? '所选材料均无可用正文（详见 missing 缺口清单）' : '未选择任何材料（resources/assignments 均为空）'
  fail(400, 'VALIDATION_FAILED', '资料不足，无法出卷', {
    details: { fields: [{ field: 'materials', reason }], missing },
  })
}

function totalChars(items: SnapshotItem[]): number {
  return items.reduce((sum, it) => sum + it.chars, 0)
}

// ==================== Provider / 题库摘要（镜像 B routers.resolve_provider_block 等） ====================

function emptyProviderSummary(): ReviewPreflightProvider {
  return { config_id: null, name: null, protocol: null, base_url: null, host: null, model: null, has_api_key: false }
}

function hostOf(baseUrl: string): string {
  try {
    return new URL(baseUrl).host
  } catch {
    return baseUrl
  }
}

function providerSummaryOf(db: MockDb, provider: ProviderConfig): ReviewPreflightProvider {
  return {
    config_id: provider.id,
    name: provider.name,
    protocol: provider.protocol,
    base_url: provider.base_url,
    host: hostOf(provider.base_url),
    model: provider.model,
    has_api_key: db.providerKeys.has(provider.id),
  }
}

function resolveProviderBlock(db: MockDb, request: ReviewRequest): ReviewPreflightProvider {
  if (request.mode === 'bank') {
    if (request.provider_config_id !== null) {
      fieldsFail([
        { field: 'provider_config_id', reason: 'bank 模式不调用模型，provider_config_id 须为 null' },
      ])
    }
    return emptyProviderSummary()
  }
  if (!request.provider_config_id) {
    fieldsFail([{ field: 'provider_config_id', reason: 'AI/混合模式必须选择 AI 服务配置' }])
  }
  const cfg = db.providers.find((p) => p.id === request.provider_config_id)
  if (!cfg) {
    fail(404, 'NOT_FOUND', `AI 服务配置不存在：${request.provider_config_id}`, {
      details: { kind: 'AI 服务配置', id: request.provider_config_id },
    })
  }
  if (!db.providerKeys.has(cfg.id)) {
    fieldsFail([{ field: 'provider_config_id', reason: '该配置尚未保存密钥；请先填写并测试连接' }])
  }
  return providerSummaryOf(db, cfg)
}

function providerSnapshotOf(block: ReviewPreflightProvider): ReviewProviderSnapshot {
  return {
    config_id: block.config_id,
    name: block.name,
    protocol: block.protocol,
    base_url: block.base_url,
    model: block.model,
  }
}

function bankSummary(db: MockDb, mode: ReviewMode): ReviewPreflightBank {
  if (mode === 'ai') {
    return { mode: 'ai', usable: false, configured_sources: 0, message: '当前为 AI 模式，不使用题库检索。' }
  }
  const configured = db.questionbankSources.filter((s) => s.configured).length
  const usable = configured > 0
  const message = usable
    ? null
    : '未配置题库来源：bank/hybrid 模式需要至少一个可用来源（可通过 STUDY_QUESTIONBANK_SOURCES 配置检索服务或 URL 导入列表），或改用 ai 模式。'
  return { mode, usable, configured_sources: configured, message }
}

function gapWarnings(
  missing: SnapshotMissing[],
  notesText: string,
  bank: ReviewPreflightBank,
): Array<{ code: string; message: string }> {
  const warnings: Array<{ code: string; message: string }> = []
  if (missing.length > 0) {
    const detail = missing.map((m) => `${m.title}（${m.reason}，未纳入发送内容）`).join('、')
    warnings.push({ code: 'MATERIAL_GAP', message: `${missing.length} 项所选材料无可用正文：${detail}。` })
  }
  if (!notesText) {
    warnings.push({ code: 'NOTES_EMPTY', message: '未填写备注；出卷将仅依据所选材料。' })
  }
  if (!bank.usable && (bank.mode === 'bank' || bank.mode === 'hybrid')) {
    warnings.push({
      code: 'BANK_NOT_CONFIGURED',
      message: '题库未配置：bank/hybrid 模式无可用来源；bank 模式将被拒绝，hybrid 将仅用本地生成并如实标注。',
    })
  }
  return warnings
}

// ==================== 预检 ====================

export function preflightReview(db: MockDb, rawBody: unknown): ReviewPreflight {
  const request = parseReviewRequest(rawBody)
  const course = requireCourse(db, request.course_id)
  const provider = resolveProviderBlock(db, request)
  const { items, missing } = resolveSelection(db, course.id, request.materials)
  ensureUsableSelection(items, missing)
  const chars = totalChars(items)
  validateReviewLimits(request.notes, request.constraints, chars)
  const bank = bankSummary(db, request.mode)
  const notes = notesPayload(request.notes)
  const fingerprint = computeReviewFingerprint(
    course.id,
    request.mode,
    REVIEW_ENGINE_VERSION,
    notes,
    request.constraints,
    items,
  )
  return {
    fingerprint,
    materials: { items, missing, notes, total_chars: chars },
    provider,
    limits: {
      notes_chars: cpLen(request.notes),
      notes_max_chars: NOTES_MAX_CHARS,
      material_chars: chars,
      material_max_chars: MATERIAL_MAX_CHARS,
      question_count_total: request.constraints.question_types.reduce((sum, q) => sum + q.count, 0),
      question_count_max: QUESTION_COUNT_MAX,
    },
    bank,
    warnings: gapWarnings(missing, request.notes, bank),
  }
}

// ==================== 出卷引擎（模拟：确定性结构演示） ====================

const DEFAULT_QUESTION_POINTS: Record<ReviewQuestionType, number> = {
  single_choice: 2,
  multiple_choice: 3,
  true_false: 1,
  fill_blank: 2,
  calculation: 10,
  short_answer: 8,
  proof: 8,
}

function locatorText(locator: Locator | null): string {
  if (!locator) return ''
  if (locator.kind === 'page') return `第 ${locator.page} 页`
  if (locator.kind === 'slide') return `第 ${locator.slide} 张幻灯片`
  if (locator.kind === 'timestamp') return `回放 ${locator.start_seconds}s 起`
  return '正文片段'
}

function snippet(text: string, max = 36): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > max ? `${flat.slice(0, max)}…` : flat
}

interface EngineUnit {
  title: string
  text: string
  locator: Locator | null
  resourceId: string | null
  assignmentId: string | null
  chunkId: string | null
  kind: 'courseware' | 'homework'
}

function unitsFromItems(db: MockDb, items: SnapshotItem[]): EngineUnit[] {
  const units: EngineUnit[] = []
  for (const item of items) {
    if (item.kind === 'assignment_text') {
      units.push({
        title: item.title,
        text: item.text,
        locator: null,
        resourceId: null,
        assignmentId: item.assignment_id,
        chunkId: null,
        kind: 'homework',
      })
      continue
    }
    for (const cid of item.chunk_ids) {
      const chunk = db.chunks.find((c) => c.id === cid)
      const locator = item.locators.find((l) => l.chunk_id === cid)?.locator ?? null
      units.push({
        title: item.title,
        text: chunk?.text ?? item.text,
        locator,
        resourceId: item.resource_id,
        assignmentId: item.assignment_id,
        chunkId: cid,
        kind: item.kind === 'attachment_text' ? 'homework' : 'courseware',
      })
    }
  }
  return units
}

interface PlannedQuestion {
  type: ReviewQuestionType
  unit: EngineUnit
  difficulty: ReviewDifficulty
  points: number
  originKind: 'ai' | 'bank' | 'adapted_bank'
}

function difficultyFor(pref: ReviewRequest['constraints']['difficulty'], index: number): ReviewDifficulty {
  const cycle: ReviewDifficulty[] = ['easy', 'medium', 'hard']
  if (pref === 'easy' || pref === 'medium' || pref === 'hard') return pref
  // mixed 与 null（由引擎按材料推断）都取确定性轮换，保证结构可预期
  return cycle[index % cycle.length]
}

function allocatePoints(
  quotas: ReviewConstraints['question_types'],
  totalPoints: number | null,
): number[] {
  const slots: Array<{ type: ReviewQuestionType; fixed: number | null }> = []
  for (const quota of quotas) {
    for (let i = 0; i < quota.count; i += 1) slots.push({ type: quota.type, fixed: quota.points_each })
  }
  if (totalPoints === null) {
    return slots.map((s) => s.fixed ?? DEFAULT_QUESTION_POINTS[s.type])
  }
  const fixedSum = slots.reduce((sum, s) => sum + (s.fixed ?? 0), 0)
  const openIndexes = slots.flatMap((s, idx) => (s.fixed === null ? [idx] : []))
  if (openIndexes.length === 0) return slots.map((s) => s.fixed ?? 0)
  const remaining = totalPoints - fixedSum
  const points = slots.map((s) => s.fixed ?? DEFAULT_QUESTION_POINTS[s.type])
  if (remaining >= openIndexes.length) {
    const base = Math.floor(remaining / openIndexes.length)
    const rem = remaining - base * openIndexes.length
    openIndexes.forEach((slotIdx, i) => {
      points[slotIdx] = base + (i < rem ? 1 : 0)
    })
  } else if (remaining > 0) {
    openIndexes.forEach((slotIdx, i) => {
      points[slotIdx] = i < remaining ? 1 : DEFAULT_QUESTION_POINTS[slots[slotIdx].type]
    })
  }
  return points
}

function originKindsFor(mode: ReviewMode, count: number): Array<'ai' | 'bank' | 'adapted_bank'> {
  if (mode === 'ai') return Array.from({ length: count }, () => 'ai' as const)
  if (mode === 'bank') return Array.from({ length: count }, () => 'bank' as const)
  const bankCount = count <= 1 ? 0 : Math.max(1, Math.floor(count * 0.4))
  return Array.from({ length: count }, (_, i) => {
    if (i < bankCount) return 'bank' as const
    if (i === count - 1 && count >= 3) return 'adapted_bank' as const
    return 'ai' as const
  })
}

function webSourceRef(ordinal: number): ReviewQuestionSourceRef {
  return {
    kind: 'web',
    resource_id: null,
    chunk_id: null,
    assignment_id: null,
    url: `https://example.com/questionbank/practice/${ordinal}`,
    site_name: '示例题库（演示来源）',
    locator: `原题 #${ordinal}`,
  }
}

function materialSourceRef(unit: EngineUnit): ReviewQuestionSourceRef {
  return {
    kind: unit.kind === 'homework' && unit.assignmentId && !unit.chunkId ? 'assignment' : 'resource',
    resource_id: unit.resourceId,
    chunk_id: unit.chunkId,
    assignment_id: unit.assignmentId,
    url: null,
    site_name: null,
    locator: locatorText(unit.locator) || null,
  }
}

/** CR009：模拟「模型按备注原文出题」的出处（locator=备注第N行，其余 ID/URL 为 null） */
function noteSourceRef(lineNumber: number): ReviewQuestionSourceRef {
  return {
    kind: 'note',
    resource_id: null,
    chunk_id: null,
    assignment_id: null,
    url: null,
    site_name: null,
    locator: `备注第${lineNumber}行`,
  }
}

function firstNonEmptyNoteLine(notes: string): number | null {
  const lines = notes.split(/\r?\n/)
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].trim() !== '') return i + 1
  }
  return null
}

function kpNameOf(unit: EngineUnit): string {
  const loc = locatorText(unit.locator)
  return `${unit.title}${loc ? `·${loc}` : ''}`
}

function buildQuestionContent(
  planned: PlannedQuestion,
  ordinal: number,
  type: ReviewQuestionType,
): { stem: string; options: ReviewQuestion['options']; answer: string; explanation: string; grading_points: string[] } {
  const title = planned.unit.title
  const loc = locatorText(planned.unit.locator)
  const snip = snippet(planned.unit.text)
  const where = `《${title}》${loc ? `${loc}` : ''}`
  if (type === 'single_choice') {
    const keys = ['A', 'B', 'C', 'D']
    const correctIndex = ordinal % 4
    const correctText = '与所选材料中该知识点的描述一致'
    const distractors = [
      '把相邻概念的定义张冠李戴',
      '扩大了原描述的适用条件范围',
      '颠倒了原描述的前提与结论',
    ]
    const options: string[] = []
    let di = 0
    for (let i = 0; i < 4; i += 1) {
      options.push(i === correctIndex ? correctText : distractors[di++])
    }
    return {
      stem: `关于${where}中「${snip}」的下列说法，正确的是：`,
      options: options.map((text, i) => ({ key: keys[i], text })),
      answer: keys[correctIndex],
      explanation: `依据材料${where}，正确项与原文描述一致；其余选项在概念、条件或因果上有出入。（模拟内容）`,
      grading_points: [],
    }
  }
  if (type === 'multiple_choice') {
    const keys = ['A', 'B', 'C', 'D']
    const texts = [
      '与材料中的定义一致',
      '与材料中的适用条件一致',
      '把无关章节的结论套用过来',
      '与材料中的过程描述矛盾',
    ]
    return {
      stem: `根据${where}（「${snip}」），下列说法中正确的有（多选）：`,
      options: texts.map((text, i) => ({ key: keys[i], text })),
      answer: 'AB',
      explanation: 'A、B 与材料一致；C、D 与材料不符。（模拟内容）',
      grading_points: [],
    }
  }
  if (type === 'true_false') {
    const isTrue = ordinal % 2 === 1
    return {
      stem: `判断：课程材料${where}中提到，「${snip}」。`,
      options: [],
      answer: isTrue ? '正确' : '错误',
      explanation: '请对照材料原文核验该表述；解析用于演示结构。（模拟内容）',
      grading_points: [],
    }
  }
  if (type === 'fill_blank') {
    return {
      stem: `根据${where}，补全关键概念：____（材料原文：「${snip}」）。`,
      options: [],
      answer: snippet(planned.unit.text, 12).replace(/…$/, ''),
      explanation: '答案取自所选材料原文片段。（模拟内容）',
      grading_points: [],
    }
  }
  if (type === 'calculation') {
    return {
      stem: `综合计算题：基于${where}所述方法（「${snip}」），按题目条件完成计算，写出主要步骤与结果。`,
      options: [],
      answer: '按材料中的步骤：①列已知条件；②代入公式逐步计算；③给出结果并核对单位。（模拟答案）',
      explanation: '本题由材料片段推导而来，步骤与评分点见下。（模拟内容）',
      grading_points: ['正确写出已知条件', '列出计算过程与关键公式', '结果与单位正确'],
    }
  }
  if (type === 'proof') {
    return {
      stem: `证明：基于${where}中的定义与结论（「${snip}」），证明相应命题成立，并写出完整推理步骤。`,
      options: [],
      answer: '证明要点：①引用材料中的定义与已知条件；②逐步推导；③得出结论。（模拟答案）',
      explanation: '按材料中的定义与条件组织证明过程。（模拟内容）',
      grading_points: ['正确引用材料中的定义与条件', '推理步骤完整、无跳步', '结论明确'],
    }
  }
  return {
    stem: `简答：说明${where}（「${snip}」）中方法的基本思想与适用条件。`,
    options: [],
    answer: '要点：①基本思想与材料原文一致；②适用条件与限制；③典型场景。（模拟答案）',
    explanation: '按材料要点作答，评分点见下。（模拟内容）',
    grading_points: ['基本思想表述准确', '适用条件完整', '结合材料场景说明'],
  }
}

function estimateMinutes(totalPoints: number, explicit: number | null): number {
  if (explicit !== null) return explicit
  return Math.min(600, Math.max(10, Math.round(totalPoints * 1.5)))
}

function buildSummary(questions: ReviewQuestion[], estimatedMinutes: number): ReviewPaperSummary {
  const typeCounts: Partial<Record<ReviewQuestionType, number>> = {}
  const difficultyCounts: Partial<Record<ReviewDifficulty, number>> = {}
  const originCounts: Partial<Record<'ai' | 'bank' | 'adapted_bank', number>> = {}
  for (const q of questions) {
    typeCounts[q.question_type] = (typeCounts[q.question_type] ?? 0) + 1
    difficultyCounts[q.difficulty] = (difficultyCounts[q.difficulty] ?? 0) + 1
    originCounts[q.origin.kind] = (originCounts[q.origin.kind] ?? 0) + 1
  }
  return {
    question_count: questions.length,
    total_points: questions.reduce((sum, q) => sum + q.points, 0),
    estimated_minutes: estimatedMinutes,
    type_counts: typeCounts,
    difficulty_counts: difficultyCounts,
    origin_counts: originCounts,
  }
}

function buildBlueprint(
  db: MockDb,
  request: ReviewRequest,
  questions: ReviewQuestion[],
  missing: SnapshotMissing[],
  capInfo: { requested: number; generated: number } | null,
): ReviewBlueprint {
  const sectionDefs: Array<{ key: string; id: string; questions: ReviewQuestion[] }> = []
  const sectionIndex = new Map<string, number>()
  for (const q of questions) {
    const matRef = q.source_refs.find((r) => r.kind === 'resource' || r.kind === 'assignment')
    let key = '网络题库'
    if (matRef) {
      if (matRef.kind === 'resource') {
        key = db.resources.find((r) => r.id === matRef.resource_id)?.title ?? matRef.resource_id ?? '课件'
      } else {
        key = db.assignments.find((a) => a.id === matRef.assignment_id)?.title ?? matRef.assignment_id ?? '作业'
      }
    }
    const idx = sectionIndex.get(key)
    if (idx === undefined) {
      sectionIndex.set(key, sectionDefs.length)
      sectionDefs.push({ key, id: `sec_${sectionDefs.length + 1}`, questions: [q] })
    } else {
      sectionDefs[idx].questions.push(q)
    }
  }
  const sections = sectionDefs.map((def) => {
    const kpNames = [...new Set(def.questions.flatMap((q) => q.knowledge_points))]
    const kps: BlueprintKnowledgePoint[] = kpNames.map((name) => {
      const refs = def.questions
        .filter((q) => q.knowledge_points.includes(name))
        .flatMap((q) => q.source_refs.filter((r) => r.kind === 'resource' || r.kind === 'assignment'))
      const evidence = refs.map((r) => ({
        kind: r.kind === 'assignment' ? ('homework' as const) : ('courseware' as const),
        resource_id: r.resource_id,
        chunk_id: r.chunk_id,
        assignment_id: r.assignment_id,
        note_ref: null,
      }))
      const seen = new Set<string>()
      const uniqueEvidence = evidence.filter((e) => {
        const key = JSON.stringify(e)
        if (seen.has(key)) return false
        seen.add(key)
        return true
      })
      const weight =
        def.questions.length > 0
          ? Math.round(((def.questions.filter((q) => q.knowledge_points.includes(name)).length / def.questions.length) * 100)) / 100
          : null
      return { name, weight, evidence: uniqueEvidence }
    })
    const slots: Array<ReviewBlueprint['sections'][number]['question_slots'][number]> = []
    const typeOrder: ReviewQuestionType[] = []
    for (const q of def.questions) {
      if (!typeOrder.includes(q.question_type)) typeOrder.push(q.question_type)
    }
    for (const [ti, qtype] of typeOrder.entries()) {
      const qs = def.questions.filter((q) => q.question_type === qtype)
      const difficulties = [...new Set(qs.map((q) => q.difficulty))]
      slots.push({
        slot_id: `slot_${def.id}_${ti + 1}`,
        type: qtype,
        count: qs.length,
        points_each:
          Math.round((qs.reduce((sum, q) => sum + q.points, 0) / qs.length) * 100) / 100,
        difficulty: difficulties.length === 1 ? difficulties[0] : null,
        source_plan: request.mode === 'ai' ? 'ai' : request.mode === 'bank' ? 'bank' : 'hybrid',
      })
    }
    return { id: def.id, title: def.key, knowledge_points: kps, question_slots: slots }
  })
  if (request.notes && sections.length > 0) {
    const firstLine = request.notes.split('\n').map((line) => line.trim()).find((line) => line.length > 0)
    if (firstLine) {
      sections[0].knowledge_points.push({
        name: '出卷备注（已随请求发送）',
        weight: null,
        evidence: [
          {
            kind: 'note',
            resource_id: null,
            chunk_id: null,
            assignment_id: null,
            note_ref: firstLine.slice(0, 60),
          },
        ],
      })
    }
  }
  const typeQuotas = request.constraints.question_types.map((quota) => {
    const qs = questions.filter((q) => q.question_type === quota.type)
    return {
      type: quota.type,
      count: qs.length,
      points_total: qs.reduce((sum, q) => sum + q.points, 0),
    }
  })
  const difficultyQuota = { easy: 0, medium: 0, hard: 0 }
  for (const q of questions) difficultyQuota[q.difficulty] += 1
  const gaps: ReviewBlueprint['gaps'] = missing.map((m) => ({
    kind: 'material_missing',
    message: `${m.title}（${m.reason}，未纳入发送内容）`,
    knowledge_point: null,
  }))
  if (capInfo && capInfo.generated < capInfo.requested) {
    gaps.push({
      kind: 'constraint_unsatisfiable',
      message: `题量超出材料覆盖：请求 ${capInfo.requested} 题，实际生成 ${capInfo.generated} 题`,
      knowledge_point: null,
    })
  }
  return {
    sections,
    type_quotas: typeQuotas,
    difficulty_quota: difficultyQuota,
    total_points: questions.reduce((sum, q) => sum + q.points, 0),
    estimated_minutes: estimateMinutes(
      questions.reduce((sum, q) => sum + q.points, 0),
      request.constraints.duration_minutes,
    ),
    gaps,
  }
}

function buildPaper(
  db: MockDb,
  request: ReviewRequest,
  items: SnapshotItem[],
  missing: SnapshotMissing[],
  base: ReviewPaper | null,
  reason: ReviewRevisionReason,
  bankUsable: boolean,
): ReviewPaper {
  const course = db.courses.find((c) => c.id === request.course_id)
  const units = unitsFromItems(db, items)
  const warnings: ReviewPaperWarning[] = []
  const requestedCount = request.constraints.question_types.reduce((sum, q) => sum + q.count, 0)
  const capacity = Math.max(units.length * 3, 1)
  let planCount = requestedCount
  let capInfo: { requested: number; generated: number } | null = null
  if (requestedCount > capacity) {
    planCount = capacity
    capInfo = { requested: requestedCount, generated: capacity }
    warnings.push({
      code: 'INSUFFICIENT_MATERIAL',
      message: `材料覆盖不足：请求 ${requestedCount} 题，按材料覆盖生成 ${planCount} 题（未用模板题补足）`,
      question_ids: [],
      details: { requested: requestedCount, generated: planCount },
    })
  }
  if (missing.length > 0) {
    const detail = missing.map((m) => `${m.title}（${m.reason}，未纳入发送内容）`).join('、')
    warnings.push({
      code: 'MATERIAL_GAP',
      message: `${missing.length} 项所选材料无可用正文：${detail}。`,
      question_ids: [],
      details: { missing_count: missing.length },
    })
  }
  if (request.mode === 'hybrid' && !bankUsable) {
    warnings.push({
      code: 'BANK_NOT_CONFIGURED',
      message: '题库未配置：bank/hybrid 模式无可用来源；bank 模式将被拒绝，hybrid 将仅用本地生成并如实标注。',
      question_ids: [],
      details: null,
    })
  }

  const expanded: ReviewQuestionType[] = []
  for (const quota of request.constraints.question_types) {
    for (let i = 0; i < quota.count; i += 1) expanded.push(quota.type)
  }
  const trimmed = expanded.slice(0, planCount)
  const pointsList = allocatePoints(request.constraints.question_types, request.constraints.total_points).slice(0, trimmed.length)
  const originKinds = originKindsFor(request.mode, trimmed.length)

  db.seq += 1
  const paperSeq = db.seq
  const questions: ReviewQuestion[] = trimmed.map((qtype, i) => {
    const unit = units[i % units.length]
    const difficulty = difficultyFor(request.constraints.difficulty, i)
    const originKind = originKinds[i]
    const refs: ReviewQuestionSourceRef[] = []
    if (originKind === 'bank' || originKind === 'adapted_bank') refs.push(webSourceRef(i + 1))
    if (originKind !== 'bank') refs.push(materialSourceRef(unit))
    // 模拟模型按备注行出题：备注非空时最后一题带 note 出处（CR009），供 UI/导出自测
    if (originKind === 'ai' && i === trimmed.length - 1) {
      const noteLine = firstNonEmptyNoteLine(request.notes)
      if (noteLine !== null) refs.push(noteSourceRef(noteLine))
    }
    const ordinal = i + 1
    const content = buildQuestionContent(
      { type: qtype, unit, difficulty, points: pointsList[i], originKind },
      ordinal,
      qtype,
    )
    const validation: ReviewQuestion['validation'] =
      capInfo && i === trimmed.length - 1
        ? { status: 'low_confidence', messages: ['材料覆盖不足，建议人工复核该题'] }
        : { status: 'valid', messages: [] }
    return {
      id: `qst_${paperSeq}_${ordinal}`,
      ordinal,
      question_type: qtype,
      stem: content.stem,
      options: content.options,
      answer: content.answer,
      explanation: content.explanation,
      grading_points: content.grading_points,
      knowledge_points: [kpNameOf(unit)],
      difficulty,
      points: pointsList[i],
      origin: {
        kind: originKind,
        candidate_id: originKind === 'ai' ? null : `qbc_${paperSeq}_${ordinal}`,
        note: originKind === 'adapted_bank' ? '在原题基础上按所选材料改编（保留原出处；模拟内容）' : null,
      },
      validation,
      source_refs: refs,
    }
  })

  const estimatedMinutes = estimateMinutes(
    questions.reduce((sum, q) => sum + q.points, 0),
    request.constraints.duration_minutes,
  )
  const providerBlock = resolveProviderBlock(db, request)
  db.seq += 1
  const snapshotFingerprint = computeReviewFingerprint(
    request.course_id,
    request.mode,
    REVIEW_ENGINE_VERSION,
    notesPayload(request.notes),
    request.constraints,
    items,
  )
  const paper: ReviewPaper = {
    id: base ? base.id : `pap_${db.seq}`,
    course_id: request.course_id,
    revision: base ? base.revision + 1 : 1,
    revision_reason: base ? reason : 'initial',
    based_on_revision: base ? base.revision : null,
    title: request.title !== null && request.title !== '' ? request.title : `${course?.title ?? '课程'} 复习卷`,
    created_at: nowIso(db),
    mode: request.mode,
    constraints: request.constraints,
    provider: providerSnapshotOf(providerBlock),
    engine_version: REVIEW_ENGINE_VERSION,
    snapshot: {
      id: `snp_${db.seq}`,
      course_id: request.course_id,
      created_at: nowIso(db),
      engine_version: REVIEW_ENGINE_VERSION,
      fingerprint: snapshotFingerprint,
      notes: notesPayload(request.notes),
      items,
      missing,
      total_chars: totalChars(items),
    },
    blueprint: buildBlueprint(db, request, questions, missing, capInfo),
    questions,
    warnings,
    summary: buildSummary(questions, estimatedMinutes),
  }
  return paper
}

// ==================== 生成作业（含修订与重试） ====================

function attachPhaseProgress(db: MockDb, jobId: string, mode: ReviewMode): void {
  const runtime = db.runtimeJobs.get(jobId)
  if (!runtime) return
  runtime.onProgress = (job) => {
    Object.assign(job, { phase: reviewPhaseFor(job.progress, mode) })
  }
}

function startGenerateJob(
  db: MockDb,
  request: ReviewRequest,
  base: ReviewPaper | null,
  reason: ReviewRevisionReason,
): Job {
  const job = createJob(db, 'review_generate', REVIEW_JOB_DURATION_MS, () => {
    const { items, missing } = resolveSelection(db, request.course_id, request.materials)
    ensureUsableSelection(items, missing)
    const bank = bankSummary(db, request.mode)
    const paper = buildPaper(db, request, items, missing, base, reason, bank.usable)
    db.papers.unshift(paper)
    db.reviewPaperInputs.set(`${paper.id}#${paper.revision}`, structuredClone(request))
    return {
      status: 'succeeded' as const,
      result_refs: [{ type: 'review_paper', id: paper.id }],
      warnings: paper.warnings.map((w) => ({
        code: w.code,
        message: w.message,
        task_ids: w.question_ids,
        details: w.details,
      })),
    }
  })
  Object.assign(job, { phase: 'materials' })
  attachPhaseProgress(db, job.id, request.mode)
  return job
}

/** 受理前共用校验（镜像 B：课程→provider→选择→可用性→限制→bank 模式拒绝） */
function validateGeneratePipeline(db: MockDb, request: ReviewRequest): SnapshotItem[] {
  const course = requireCourse(db, request.course_id)
  resolveProviderBlock(db, request)
  const { items, missing } = resolveSelection(db, course.id, request.materials)
  ensureUsableSelection(items, missing)
  validateReviewLimits(request.notes, request.constraints, totalChars(items))
  if (request.mode === 'bank') {
    const bank = bankSummary(db, 'bank')
    if (!bank.usable) {
      fail(400, 'BANK_NOT_CONFIGURED', bank.message ?? '题库未配置：bank 模式需要至少一个可用来源。')
    }
  }
  return items
}

export function generateReview(
  db: MockDb,
  rawBody: unknown,
  idempotencyKey: string | undefined,
  expectedFingerprint?: string,
): { job: Job } {
  const request = parseReviewRequest(rawBody)
  validateGeneratePipeline(db, request)
  if (expectedFingerprint && preflightReview(db, request).fingerprint !== expectedFingerprint) {
    fail(409, 'PREFLIGHT_FAILED', '资料或备注已变化，请重新预览后生成')
  }
  return idempotentScoped(
    db,
    idempotencyKey,
    'POST /review-papers/generate',
    requestFingerprintOf(rawBody),
    () => {
      const job = startGenerateJob(db, request, null, 'initial')
      db.reviewJobRequests.set(job.id, structuredClone(request))
      return { job }
    },
  )
}

function latestPaperOf(db: MockDb, paperId: string): ReviewPaper | null {
  const revisions = db.papers.filter((p) => p.id === paperId)
  if (revisions.length === 0) return null
  return revisions.reduce((max, p) => (p.revision > max.revision ? p : max))
}

export function createReviewRevision(
  db: MockDb,
  paperId: string,
  rawBody: unknown,
  idempotencyKey: string | undefined,
  expectedFingerprint?: string,
): { job: Job } {
  const base = latestPaperOf(db, paperId)
  if (!base) {
    fail(404, 'NOT_FOUND', `复习卷不存在：${paperId}`, { details: { kind: '复习卷', id: paperId } })
  }
  const prevInput = db.reviewPaperInputs.get(`${base.id}#${base.revision}`)
  if (!prevInput) {
    fail(409, 'CONFLICT', '该试卷缺少可复用的输入留档，无法修订')
  }
  const patch = parseRevisionRequest(rawBody)
  const merged = mergeReviewRequest(prevInput, patch)
  merged.course_id = base.course_id
  merged.mode = base.mode
  const items = validateGeneratePipeline(db, merged)
  if (expectedFingerprint && preflightReview(db, merged).fingerprint !== expectedFingerprint) {
    fail(409, 'PREFLIGHT_FAILED', '资料或备注已变化，请重新预览后生成')
  }
  const notes = notesPayload(merged.notes)
  const fingerprint = computeReviewFingerprint(
    merged.course_id,
    merged.mode,
    REVIEW_ENGINE_VERSION,
    notes,
    merged.constraints,
    items,
  )
  const reason: ReviewRevisionReason = fingerprint === base.snapshot.fingerprint ? 'regenerate' : 'revise'
  return idempotentScoped(
    db,
    idempotencyKey,
    `POST /review-papers/${paperId}/revisions`,
    requestFingerprintOf(rawBody),
    () => {
      const job = startGenerateJob(db, merged, base, reason)
      db.reviewJobRequests.set(job.id, structuredClone(merged))
      db.reviewRevisionRequests.set(job.id, { paper_id: paperId, request: structuredClone(rawBody) })
      return { job }
    },
  )
}

/** 失败 / 部分失败后的重试：按留档请求原样重跑（mock 仅在测试注入失败时出现） */
export function retryReviewJob(db: MockDb, job: Job): { job: Job } {
  const revisionRecord = db.reviewRevisionRequests.get(job.id)
  if (revisionRecord) {
    const base = latestPaperOf(db, revisionRecord.paper_id)
    if (!base) fail(409, 'CONFLICT', '该复习卷任务没有可重试的请求留档')
    const prevInput = db.reviewPaperInputs.get(`${base.id}#${base.revision}`)
    if (!prevInput) fail(409, 'CONFLICT', '该复习卷任务没有可重试的请求留档')
    const merged = mergeReviewRequest(prevInput, parseRevisionRequest(revisionRecord.request))
    merged.course_id = base.course_id
    merged.mode = base.mode
    const retry = startGenerateJob(db, merged, base, 'revise')
    db.reviewJobRequests.set(retry.id, structuredClone(merged))
    db.reviewRevisionRequests.set(retry.id, structuredClone(revisionRecord))
    return { job: retry }
  }
  const request = db.reviewJobRequests.get(job.id)
  if (!request) fail(409, 'CONFLICT', '该复习卷任务没有可重试的请求留档')
  const retry = startGenerateJob(db, request, null, 'initial')
  db.reviewJobRequests.set(retry.id, structuredClone(request))
  return { job: retry }
}

// ==================== 试卷列表 / 详情 / 修订历史 ====================

export function listReviewPapers(
  db: MockDb,
  query: Record<string, string | number | boolean | undefined> = {},
): ReviewPaperListPage {
  const courseFilter = query.course_id !== undefined && query.course_id !== '' ? String(query.course_id) : null
  let modeFilter: ReviewMode | null = null
  if (query.mode !== undefined && query.mode !== '') {
    const mode = String(query.mode)
    if (mode !== 'ai' && mode !== 'bank' && mode !== 'hybrid') {
      fieldsFail([{ field: 'mode', reason: 'mode 需为 ai / bank / hybrid' }])
    }
    modeFilter = mode
  }
  const byId = new Map<string, ReviewPaper>()
  for (const paper of db.papers) {
    const current = byId.get(paper.id)
    if (!current || paper.revision > current.revision) byId.set(paper.id, paper)
  }
  let items = [...byId.values()].filter(
    (p) => (courseFilter === null || p.course_id === courseFilter) && (modeFilter === null || p.mode === modeFilter),
  )
  items.sort((a, b) => cmpStr(b.created_at, a.created_at) || cmpStr(b.id, a.id))
  if (query.cursor !== undefined) {
    const raw = decodeCursor(String(query.cursor))
    const sep = raw.indexOf('|')
    const created = sep >= 0 ? raw.slice(0, sep) : ''
    const lastId = sep >= 0 ? raw.slice(sep + 1) : ''
    if (!created || !lastId) invalidCursor(String(query.cursor))
    items = items.filter((p) => p.created_at < created || (p.created_at === created && p.id < lastId))
  }
  const limit = readLimit(query, DEFAULT_LIMIT)
  const page = items.slice(0, limit + 1)
  let nextCursor: string | null = null
  if (page.length > limit) {
    page.pop()
    const last = page[page.length - 1]
    nextCursor = encodeCursor(`${last.created_at}|${last.id}`)
  }
  return { items: page, next_cursor: nextCursor }
}

function parseRevisionParam(revisionRaw: string | number | boolean | undefined): number | null {
  if (revisionRaw === undefined || revisionRaw === '') return null
  const revision = Number(revisionRaw)
  if (!Number.isInteger(revision) || revision < 1) {
    fail(400, 'VALIDATION_FAILED', '无效的 revision 参数', { details: { revision: revisionRaw } })
  }
  return revision
}

/** 试卷详情（缺省最新版本；指定 revision 读历史版本，不存在 404）。 */
export function getReviewPaper(
  db: MockDb,
  paperId: string,
  revisionRaw?: string | number | boolean,
): ReviewPaper {
  const revision = parseRevisionParam(revisionRaw)
  const latest = latestPaperOf(db, paperId)
  if (!latest) {
    fail(404, 'NOT_FOUND', `复习卷不存在：${paperId}`, { details: { kind: '复习卷', id: paperId } })
  }
  if (revision === null) return latest
  const target = db.papers.find((p) => p.id === paperId && p.revision === revision)
  if (!target) {
    fail(404, 'NOT_FOUND', `复习卷不存在：${paperId}`, {
      details: { kind: '复习卷', id: paperId, revision },
    })
  }
  return target
}

/** 版本历史（revision 倒序；游标为版本号 keyset）。 */
export function listReviewRevisions(
  db: MockDb,
  paperId: string,
  query: Record<string, string | number | boolean | undefined> = {},
): ReviewPaperRevisionListPage {
  if (!latestPaperOf(db, paperId)) {
    fail(404, 'NOT_FOUND', `复习卷不存在：${paperId}`, { details: { kind: '复习卷', id: paperId } })
  }
  let revisions = db.papers.filter((p) => p.id === paperId)
  revisions.sort((a, b) => b.revision - a.revision)
  if (query.cursor !== undefined) {
    const raw = decodeCursor(String(query.cursor))
    const after = Number(raw)
    if (!Number.isInteger(after) || after < 0) invalidCursor(String(query.cursor))
    revisions = revisions.filter((p) => p.revision < after)
  }
  const limit = readLimit(query, DEFAULT_LIMIT)
  const page = revisions.slice(0, limit + 1)
  let nextCursor: string | null = null
  if (page.length > limit) {
    page.pop()
    const last = page[page.length - 1]
    nextCursor = encodeCursor(String(last.revision))
  }
  return {
    items: page.map((p) => ({
      paper_id: p.id,
      revision: p.revision,
      revision_reason: p.revision_reason,
      based_on_revision: p.based_on_revision,
      title: p.title,
      created_at: p.created_at,
      mode: p.mode,
      engine_version: p.engine_version,
      fingerprint: p.snapshot.fingerprint,
      provider: p.provider,
      summary: p.summary,
    })),
    next_cursor: nextCursor,
  }
}

// ==================== 导出（直返文本） ====================

function sanitizeFilename(name: string): string {
  const cleaned = name.replace(/[\\/:*?"<>|\s]+/g, '')
  return cleaned === '' ? '复习卷' : cleaned
}

function refLabelOf(db: MockDb, ref: ReviewQuestionSourceRef): string {
  if (ref.kind === 'resource') {
    const title = db.resources.find((r) => r.id === ref.resource_id)?.title ?? ref.resource_id ?? '课件'
    return ref.locator ? `课件《${title}》${ref.locator}` : `课件《${title}》`
  }
  if (ref.kind === 'assignment') {
    const title = db.assignments.find((a) => a.id === ref.assignment_id)?.title ?? ref.assignment_id ?? '作业'
    return `作业《${title}》`
  }
  if (ref.kind === 'note') {
    return ref.locator ?? '备注'
  }
  const site = ref.site_name ?? '网络题库'
  return ref.url ? `${site}（${ref.url}）` : site
}

/** markdown 完整卷：题目、答案、解析、评分点与出处（文本导出不截断）。 */
function buildMarkdown(db: MockDb, paper: ReviewPaper): string {
  const course = db.courses.find((c) => c.id === paper.course_id)
  const lines: string[] = []
  lines.push(`# ${paper.title}`)
  lines.push('')
  lines.push(`- 课程：${course?.title ?? paper.course_id}`)
  lines.push(`- 模式：${REVIEW_MODE_LABELS[paper.mode]}`)
  const basedOn = paper.based_on_revision !== null ? `，基于第 ${paper.based_on_revision} 版` : ''
  lines.push(`- 版本：第 ${paper.revision} 版（${paper.revision_reason}${basedOn}）`)
  lines.push(`- 生成时间：${paper.created_at}`)
  lines.push(`- 引擎版本：${paper.engine_version}`)
  lines.push(`- 材料快照：${paper.snapshot.id}（指纹 ${paper.snapshot.fingerprint}）`)
  lines.push(
    `- 卷面：${paper.summary.question_count} 题 / ${paper.summary.total_points} 分 / 预计 ${paper.summary.estimated_minutes} 分钟`,
  )
  lines.push('')
  lines.push('## 材料快照')
  lines.push('')
  for (const item of paper.snapshot.items) {
    const version = item.version !== null ? `，版本 ${item.version}` : ''
    lines.push(`- ${item.title}（${item.chars} 字符${version}）`)
  }
  for (const missing of paper.snapshot.missing) {
    lines.push(`- ${missing.title}：${missing.message}（未纳入发送内容）`)
  }
  lines.push(`- 合计正文 ${paper.snapshot.total_chars} 字符；备注 ${paper.snapshot.notes.chars} 字符`)
  lines.push('')
  if (paper.snapshot.notes.text) {
    lines.push('## 出卷备注（已随请求发送）')
    lines.push('')
    lines.push(paper.snapshot.notes.text)
    lines.push('')
  }
  lines.push('## 题目')
  lines.push('')
  for (const q of paper.questions) {
    lines.push(`### ${q.ordinal}. ${q.stem}`)
    lines.push('')
    lines.push(
      `（${REVIEW_QUESTION_TYPE_LABELS[q.question_type]}，${q.points} 分，难度：${REVIEW_DIFFICULTY_LABELS[q.difficulty]}）`,
    )
    lines.push('')
    for (const opt of q.options) lines.push(`- ${opt.key}. ${opt.text}`)
    if (q.options.length > 0) lines.push('')
    lines.push(`**答案**：${q.answer}`)
    lines.push('')
    lines.push(`**解析**：${q.explanation}`)
    lines.push('')
    if (q.grading_points.length > 0) {
      lines.push('**评分要点**：')
      for (const point of q.grading_points) lines.push(`- ${point}`)
      lines.push('')
    }
    lines.push(`**出处**：${q.source_refs.map((ref) => refLabelOf(db, ref)).join('；')}`)
    lines.push('')
  }
  if (paper.warnings.length > 0) {
    lines.push('## 生成提示')
    lines.push('')
    for (const warning of paper.warnings) lines.push(`- ${warning.message}`)
    lines.push('')
  }
  return lines.join('\n')
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/** 打印用 HTML：student 不含答案与解析；answers 含答案、解析与评分点（出处两版都保留）。 */
function buildPaperHtml(db: MockDb, paper: ReviewPaper, variant: 'student' | 'answers'): string {
  const course = db.courses.find((c) => c.id === paper.course_id)
  const withAnswers = variant === 'answers'
  const parts: string[] = []
  parts.push('<!DOCTYPE html>')
  parts.push('<html lang="zh-CN">')
  parts.push('<head>')
  parts.push('<meta charset="utf-8">')
  parts.push(`<title>${escapeHtml(paper.title)}（${withAnswers ? '答案版' : '学生版'}）</title>`)
  parts.push(
    '<style>body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;max-width:820px;margin:0 auto;padding:24px;line-height:1.7;color:#222}h1{font-size:22px;border-bottom:2px solid #333;padding-bottom:8px}.meta{color:#666;font-size:13px;margin-bottom:16px}.question{margin:18px 0;padding:12px 0;border-bottom:1px dashed #ccc}.stem{font-weight:600}.options{margin:6px 0;padding-left:1.2em}.answer{margin-top:6px;padding:6px 10px;background:#f5f5f5;border-left:3px solid #4a6}.source{color:#888;font-size:12px;margin-top:4px}.warnings{color:#a60;font-size:13px}@media print{body{max-width:none}}</style>',
  )
  parts.push('</head>')
  parts.push('<body>')
  parts.push(`<h1>${escapeHtml(paper.title)}</h1>`)
  parts.push(
    `<p class="meta">${escapeHtml(course?.title ?? paper.course_id)} · ${REVIEW_MODE_LABELS[paper.mode]} · 第 ${paper.revision} 版 · ${paper.summary.question_count} 题 / ${paper.summary.total_points} 分 · 生成于 ${escapeHtml(paper.created_at)}</p>`,
  )
  if (paper.warnings.length > 0) {
    parts.push('<ul class="warnings">')
    for (const warning of paper.warnings) parts.push(`<li>${escapeHtml(warning.message)}</li>`)
    parts.push('</ul>')
  }
  for (const q of paper.questions) {
    parts.push('<div class="question">')
    parts.push(
      `<p class="stem">${q.ordinal}. ${escapeHtml(q.stem)}（${escapeHtml(REVIEW_QUESTION_TYPE_LABELS[q.question_type])}，${q.points} 分）</p>`,
    )
    if (q.options.length > 0) {
      parts.push('<div class="options">')
      for (const opt of q.options) parts.push(`<div>${escapeHtml(opt.key)}. ${escapeHtml(opt.text)}</div>`)
      parts.push('</div>')
    }
    if (withAnswers) {
      parts.push(`<div class="answer"><strong>答案</strong>：${escapeHtml(q.answer)}</div>`)
      parts.push(`<div class="answer"><strong>解析</strong>：${escapeHtml(q.explanation)}</div>`)
      if (q.grading_points.length > 0) {
        parts.push('<div class="answer"><strong>评分要点</strong>：')
        parts.push(`<ul>${q.grading_points.map((point) => `<li>${escapeHtml(point)}</li>`).join('')}</ul>`)
        parts.push('</div>')
      }
    }
    parts.push(
      `<p class="source">出处：${q.source_refs.map((ref) => escapeHtml(refLabelOf(db, ref))).join('；')}</p>`,
    )
    parts.push('</div>')
  }
  parts.push('</body>')
  parts.push('</html>')
  return parts.join('\n')
}

/** 导出（GET /review-papers/{id}/export?format=markdown|student|answers[&revision=N]）：直接返回文本。 */
export function exportReviewPaper(
  db: MockDb,
  paperId: string,
  query: Record<string, string | number | boolean | undefined> = {},
): ReviewTextExport {
  const formatRaw = query.format === undefined || query.format === '' ? 'markdown' : String(query.format)
  if (formatRaw !== 'markdown' && formatRaw !== 'student' && formatRaw !== 'answers') {
    fail(400, 'VALIDATION_FAILED', `不支持的导出格式：${formatRaw}（可选 markdown / student / answers）`, {
      details: { format: formatRaw, allowed: ['markdown', 'student', 'answers'] },
    })
  }
  const revision = parseRevisionParam(query.revision)
  const paper = getReviewPaper(db, paperId, revision === null ? undefined : String(revision))
  const base = sanitizeFilename(paper.title)
  if (formatRaw === 'markdown') {
    return {
      text: buildMarkdown(db, paper),
      filename: `${base}·复习卷.md`,
      content_type: 'text/markdown; charset=utf-8',
    }
  }
  const variant = formatRaw
  const suffix = variant === 'student' ? '学生版' : '答案版'
  return {
    text: buildPaperHtml(db, paper, variant),
    filename: `${base}·${suffix}.html`,
    content_type: 'text/html; charset=utf-8',
  }
}
