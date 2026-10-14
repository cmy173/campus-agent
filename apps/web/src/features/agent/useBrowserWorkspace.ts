import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { agentApi } from './api'
import { API_MODE, apiErrorText } from '../../lib/api/http'

/** Browser transport and action receipts remain independent of visual layout. */
export function useBrowserWorkspace(id: string, open: boolean, active: boolean) {
  const client = useQueryClient()
  const status = useQuery({ queryKey: ['agent', 'browser', id], queryFn: () => agentApi.browser(id),
    enabled: API_MODE === 'real', refetchInterval: open || active ? 1000 : false })
  const previouslyActive = useRef(active)
  useEffect(() => {
    if (previouslyActive.current && !active && API_MODE === 'real') void status.refetch()
    previouslyActive.current = active
  }, [active, status.refetch])
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [receipt, setReceipt] = useState('')
  const executing = useRef(false)
  async function run(key: string, action: () => Promise<{ message?: string } | unknown>) {
    if (executing.current) return
    executing.current = true; setBusy(key); setError(''); setReceipt('')
    try {
      const result = await action() as { message?: string }
      if (result?.message) setReceipt(result.message)
    } catch (e) { setError(apiErrorText(e)) }
    finally {
      await status.refetch()
      await client.invalidateQueries({ queryKey: ['agent', 'files', id] })
      executing.current = false; setBusy('')
    }
  }
  return { status, busy, error, receipt,
    openBrowser: (url?: string) => run('open', () => agentApi.openBrowser(id, url)),
    stop: () => run('stop', () => agentApi.stopBrowser(id)),
    act: (aid: string, operation: 'confirm' | 'reject') => run(aid, () => agentApi.browserAction(id, aid, operation)) }
}
