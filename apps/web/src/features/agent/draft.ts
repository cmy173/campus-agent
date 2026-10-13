import type { SendPayload } from './api'

export interface PendingSend { key: string; payload: SendPayload }
export interface Draft { text: string; course_id?: string | null; pending?: PendingSend; attachment_ids?: string[] }
const prefix = 'campus.agent.draft.'

export function readDraft(id: string): Draft {
  try {
    const value = JSON.parse(sessionStorage.getItem(prefix + id) ?? '{}')
    return typeof value.text === 'string' ? value : { text: '' }
  } catch { return { text: '' } }
}

export function saveDraft(id: string, draft: Draft) {
  try { sessionStorage.setItem(prefix + id, JSON.stringify(draft)) } catch { /* Storage may be disabled. */ }
}

/** Preserve the exact request key after an uncertain network outcome. */
export function prepareSend(draft: Draft, payload: SendPayload): PendingSend {
  if (draft.pending && JSON.stringify(draft.pending.payload) === JSON.stringify(payload)) return draft.pending
  return { key: typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`, payload }
}
