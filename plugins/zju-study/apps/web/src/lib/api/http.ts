import type { ApiErrorBody } from '../../types/contract'
import { mockRequest } from './mock/handlers'

export class ApiError extends Error {
  readonly code: string
  readonly retryable: boolean
  readonly details: unknown
  readonly requestId: string
  readonly status: number

  constructor(status: number, body: ApiErrorBody) {
    super(body.message)
    this.name = 'ApiError'
    this.status = status
    this.code = body.code
    this.retryable = body.retryable
    this.details = body.details
    this.requestId = body.request_id
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError
}

/** 将任意异常规范化为 ApiError 供界面展示 */
export function toApiError(value: unknown): ApiError {
  if (isApiError(value)) return value
  const message = value instanceof Error ? value.message : String(value)
  return new ApiError(0, {
    code: 'UNEXPECTED_ERROR',
    message: `发生未预期错误：${message}`,
    retryable: false,
    request_id: 'req_local',
  })
}

/** 错误文案：优先展示 details.fields[].reason（后端校验把具体原因放在这里） */
export function apiErrorText(err: unknown): string {
  const apiError = toApiError(err)
  const fields = (apiError.details as { fields?: Array<{ field: string; reason: string }> } | undefined)?.fields
  if (fields && fields.length > 0) {
    return `${apiError.message}：${fields.map((f) => `${f.field}（${f.reason}）`).join('；')}`
  }
  return `${apiError.message}（${apiError.code}）`
}

export function newRequestId(): string {
  const rand =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID().slice(0, 8)
      : Math.random().toString(16).slice(2, 10)
  return `req_${rand}`
}

export const API_MODE: 'mock' | 'real' =
  import.meta.env.VITE_API_MODE === 'mock' ? 'mock' : 'real'

export function apiBaseUrl(): string {
  return (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? ''
}

export interface ApiRequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  query?: Record<string, string | number | boolean | undefined>
  /** JSON 请求体；multipart 请求中作为 mock 侧的结构化载荷 */
  body?: unknown
  /** 真实模式上传用 multipart 表单（mock 模式忽略，改读 body） */
  formData?: FormData
  /** 写入/导入幂等键；同一键重复请求返回同一结果 */
  idempotencyKey?: string
  /** 期望的对象 revision（如 "3"）；不匹配返回 412 REVISION_MISMATCH */
  ifMatch?: string
  signal?: AbortSignal
}

export async function apiRequest<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const { method = 'GET', query, body, formData, idempotencyKey, ifMatch, signal } = options

  if (API_MODE === 'mock') {
    return mockRequest<T>({ method, path, query, body, idempotencyKey, ifMatch, signal })
  }

  const base = apiBaseUrl()
  const params = new URLSearchParams()
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined) params.set(k, String(v))
    }
  }
  const qs = params.size > 0 ? `?${params.toString()}` : ''
  const url = `${base}/api/v1${path}${qs}`

  let res: Response
  try {
    res = await fetch(url, {
      method,
      headers: {
        ...(formData === undefined && body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...(idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : {}),
        ...(ifMatch ? { 'If-Match': ifMatch } : {}),
      },
      body: formData ?? (body !== undefined ? JSON.stringify(body) : undefined),
      signal,
      credentials: 'include',
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err
    throw new ApiError(0, {
      code: 'NETWORK_ERROR',
      message: '无法连接后端服务，请检查后端地址与网络状态',
      retryable: true,
      request_id: newRequestId(),
    })
  }

  if (!res.ok) await throwResponseError(res)

  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

/** 文本响应（GET /review-papers/{id}/export 等直返文本的端点） */
export interface TextResponse {
  text: string
  /** Content-Disposition 中的文件名（RFC 5987 filename* 优先，中文按 UTF-8 解码） */
  filename: string | null
  content_type: string | null
}

/** 请求文本响应；错误信封解析与 apiRequest 一致 */
export async function apiRequestText(
  path: string,
  options: ApiRequestOptions = {},
): Promise<TextResponse> {
  const { method = 'GET', query, signal } = options

  if (API_MODE === 'mock') {
    return mockRequest<TextResponse>({ method, path, query, signal })
  }

  const base = apiBaseUrl()
  const params = new URLSearchParams()
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined) params.set(k, String(v))
    }
  }
  const qs = params.size > 0 ? `?${params.toString()}` : ''
  const url = `${base}/api/v1${path}${qs}`

  let res: Response
  try {
    res = await fetch(url, { method, signal, credentials: 'include' })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err
    throw new ApiError(0, {
      code: 'NETWORK_ERROR',
      message: '无法连接后端服务，请检查后端地址与网络状态',
      retryable: true,
      request_id: newRequestId(),
    })
  }

  if (!res.ok) await throwResponseError(res)

  return {
    text: await res.text(),
    filename: parseContentDisposition(res.headers.get('Content-Disposition')),
    content_type: res.headers.get('Content-Type'),
  }
}

async function throwResponseError(res: Response): Promise<never> {
  let bodyJson: unknown = null
  try {
    bodyJson = await res.json()
  } catch {
    bodyJson = null
  }
  // 错误体为契约信封 {error:{code,message,retryable,details,request_id}}
  const envelope = (bodyJson as { error?: Partial<ApiErrorBody> } | null)?.error
  const maybe = envelope ?? (bodyJson as Partial<ApiErrorBody> | null)
  throw new ApiError(res.status, {
    code: maybe?.code ?? 'HTTP_ERROR',
    message: maybe?.message ?? `请求失败（HTTP ${res.status}）`,
    retryable: maybe?.retryable ?? res.status >= 500,
    details: maybe?.details,
    request_id: maybe?.request_id ?? newRequestId(),
  })
}

/** 解析 Content-Disposition；中文文件名按 RFC 5987 filename*=UTF-8''… 解码 */
function parseContentDisposition(header: string | null): string | null {
  if (!header) return null
  const extended = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(header)
  if (extended) {
    try {
      return decodeURIComponent(extended[1].trim())
    } catch {
      return extended[1].trim()
    }
  }
  const plain = /filename\s*=\s*"([^"]+)"/i.exec(header)
  if (plain) return plain[1]
  return null
}
