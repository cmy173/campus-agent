import type { ErrorInfo } from '../types/contract'

export function groupJobErrors(errors: ErrorInfo[]) {
  const groups = new Map<string, { error: ErrorInfo; count: number }>()
  for (const error of errors) {
    const key = JSON.stringify([error.code, error.message, error.retryable])
    const previous = groups.get(key)
    groups.set(key, { error, count: (previous?.count ?? 0) + 1 })
  }
  return Array.from(groups.values())
}
