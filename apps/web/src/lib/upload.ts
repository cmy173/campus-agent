/** 上传护栏（PD-1 裁定）：单文件上限 64MB；超过 30MB 视为超大文件，解析较慢需在界面明示 */
export const MAX_UPLOAD_BYTES = 64 * 1024 * 1024
export const LARGE_UPLOAD_BYTES = 30 * 1024 * 1024
export const UPLOAD_ACCEPT = '.pdf,.pptx,.docx'
export const LARGE_UPLOAD_HINT = '超大文件（超过 30MB）解析较慢：解析在后台进行，可稍后回来查看进度'

export type UploadCheck = { ok: true; large: boolean } | { ok: false; reason: 'type' | 'too_large' }

export function checkUploadFile(name: string, size: number): UploadCheck {
  if (!/\.(pdf|pptx|docx)$/i.test(name)) return { ok: false, reason: 'type' }
  if (size > MAX_UPLOAD_BYTES) return { ok: false, reason: 'too_large' }
  return { ok: true, large: size > LARGE_UPLOAD_BYTES }
}

export function fmtFileSize(bytes: number | null): string {
  if (bytes === null || !Number.isFinite(bytes) || bytes < 0) return '未知'
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${Math.max(1, Math.round(bytes / 1024))} KB`
}
