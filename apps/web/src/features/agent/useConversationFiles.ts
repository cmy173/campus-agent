import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { agentApi } from './api'
import { apiErrorText } from '../../lib/api/http'

/** File transport and attachment selection do not depend on the workspace layout. */
export function useConversationFiles(id: string | undefined, active: boolean, selected: string[], onSelect: (ids: string[]) => void) {
  const client = useQueryClient()
  const files = useQuery({ queryKey: ['agent', 'files', id], queryFn: () => agentApi.files(id!),
    enabled: Boolean(id), refetchInterval: active ? 900 : false })
  useEffect(() => { if (id) void client.invalidateQueries({ queryKey: ['agent', 'files', id] }) }, [active, id, client])
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const busy = useRef(false)
  const selectedRef = useRef(selected)
  selectedRef.current = selected
  async function upload(values: FileList | File[]) {
    if (!id || busy.current || active) return
    busy.current = true
    setUploading(true)
    setError('')
    try {
      for (const file of Array.from(values)) {
        if (selectedRef.current.length >= 32) throw new Error('每条消息最多选择32个附件，请先发送或移除部分选择。')
        const item = await agentApi.upload(id, file, crypto.randomUUID())
        const next = [...new Set([...selectedRef.current, item.id])]
        selectedRef.current = next
        onSelect(next)
        await client.invalidateQueries({ queryKey: ['agent', 'files', id] })
      }
    } catch (e) { setError(apiErrorText(e)) }
    finally { busy.current = false; setUploading(false) }
  }
  function toggle(fid: string) {
    if (active || uploading) return
    const next = selected.includes(fid) ? selected.filter(id => id !== fid) : [...selected, fid]
    if (next.length > 32) { setError('每条消息最多选择32个附件。'); return }
    onSelect(next)
  }
  return { files, uploading, error, upload, toggle,
    refresh: () => files.refetch(), selected, disabled: active || uploading }
}
