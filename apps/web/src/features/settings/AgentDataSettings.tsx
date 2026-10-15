import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, InputNumber, Modal, Space, Typography } from 'antd'
import { apiErrorText, apiRequest, isApiError } from '../../lib/api/http'

export interface AgentDataPolicy {
  history_retention_days: number
  history_count: number
  pending_cleanup_count: number
  description: string
  erasure_limits: string
}
interface DeletionReceipt { deleted_conversation_ids: string[]; deleted_count: number; cleanup_complete: boolean }
interface HistoryItem { id: string; title: string | null; updated_at: string }
interface HistoryPage { items: HistoryItem[]; next_cursor: string | null; purge: DeletionReceipt }

/** Clear chat drafts/scroll only; never clear origin storage or credential settings. */
export function clearAgentHistoryStorage(ids?: string[]): boolean {
  let complete = true
  for (const kind of ['localStorage', 'sessionStorage'] as const) {
    try {
      const storage = window[kind]
      const keys = Array.from({ length: storage.length }, (_, index) => storage.key(index)).filter((key): key is string => !!key)
      for (const key of keys) {
        const prefix = ['campus.agent.draft.', 'campus.agent.scroll.'].find(value => key.startsWith(value))
        if (prefix && (!ids || ids.some(id => key === prefix + id || key.startsWith(prefix + id + '.')))) storage.removeItem(key)
      }
    } catch { complete = false }
  }
  return complete
}

export function AgentDataSettings() {
  const queryClient = useQueryClient()
  const policy = useQuery({ queryKey: ['agent', 'data', 'settings'], queryFn: () => apiRequest<AgentDataPolicy>('/agent/data/settings'), refetchOnWindowFocus: false })
  const [days, setDays] = useState(30)
  const [items, setItems] = useState<HistoryItem[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [pending, setPending] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [confirmation, setConfirmation] = useState<{ kind: 'all' | 'one' | 'expired'; id?: string; title: string } | null>(null)
  useEffect(() => { if (policy.data) setDays(policy.data.history_retention_days) }, [policy.data])

  async function refreshHistory(next?: string) {
    const page = await apiRequest<HistoryPage>('/agent/data/history', { query: { limit: 20, ...(next ? { cursor: next } : {}) } })
    if (page.purge.deleted_count) {
      const cleared = clearAgentHistoryStorage(page.purge.deleted_conversation_ids)
      setNotice(cleared ? `已按保留期限清理 ${page.purge.deleted_count} 个过期聊天。` : '过期聊天已删除，但浏览器存储受限，草稿/滚动缓存未完全清理。')
      for (const key of ['conversation', 'actions', 'files', 'browser']) queryClient.removeQueries({ queryKey: ['agent', key] })
      await queryClient.invalidateQueries({ queryKey: ['agent', 'list'] })
    }
    setItems(previous => next ? [...previous, ...page.items] : page.items)
    setCursor(page.next_cursor)
    await policy.refetch()
  }

  async function load(next?: string) {
    setError(''); setPending('load')
    try { await refreshHistory(next) } catch (e) { setError(apiErrorText(e)) }
    finally { setPending('') }
  }

  async function save() {
    setError(''); setNotice(''); setPending('save')
    try {
      await apiRequest('/agent/data/settings', { method: 'PUT', body: { history_retention_days: days } })
      await policy.refetch()
      setNotice('聊天历史保留期限已保存；读取数据管理历史或手动清理时执行，不是后台定时任务。')
    } catch (e) { setError(apiErrorText(e)) }
    finally { setPending('') }
  }

  async function erase() {
    if (!confirmation) return
    const selected = confirmation
    setConfirmation(null); setError(''); setNotice(''); setPending('delete')
    try {
      const result = await apiRequest<DeletionReceipt>(selected.kind === 'expired' ? '/agent/data/history/purge-expired'
        : selected.kind === 'one' ? `/agent/data/history/${encodeURIComponent(selected.id!)}` : '/agent/data/history',
      selected.kind === 'expired' ? { method: 'POST', body: {} } : { method: 'DELETE' })
      if (!result.cleanup_complete) throw new Error('历史副本未完全清理，请重试删除。')
      const cleared = clearAgentHistoryStorage(selected.kind === 'all' ? undefined : result.deleted_conversation_ids)
      queryClient.removeQueries({ queryKey: ['agent', 'conversation'] })
      for (const key of ['actions', 'files', 'browser']) queryClient.removeQueries({ queryKey: ['agent', key] })
      await queryClient.invalidateQueries({ queryKey: ['agent', 'list'] })
      setItems(previous => previous.filter(item => !result.deleted_conversation_ids.includes(item.id)))
      await policy.refetch()
      setNotice(cleared ? `已删除 ${result.deleted_count} 个聊天及应用可控历史副本；校园登录、API 密钥和学习数据均保留。`
        : '服务端聊天历史已删除，但浏览器存储受限，草稿/滚动缓存未完全清理；校园登录和 API 密钥保留。')
    } catch (e) {
      setError(apiErrorText(e))
      // A server cleanup error can follow a committed journal deletion. Never
      // claim success, but don't retain private chat drafts for known erased IDs.
      if (isApiError(e)) {
        const details = e.details as { deleted_conversation_ids?: string[] } | undefined
        if (details?.deleted_conversation_ids) {
          clearAgentHistoryStorage(details.deleted_conversation_ids)
          for (const key of ['conversation', 'actions', 'files', 'browser']) queryClient.removeQueries({ queryKey: ['agent', key] })
          await queryClient.invalidateQueries({ queryKey: ['agent', 'list'] })
          await policy.refetch()
        }
      }
    }
    finally { setPending('') }
  }

  async function exitCampus() {
    setError(''); setNotice(''); setPending('campus')
    try {
      // Shared-profile clearing is implemented by the browser-session owner route.
      await apiRequest('/agent/data/browser-session/clear', { method: 'POST', body: {} })
      await queryClient.invalidateQueries({ queryKey: ['campus'] })
      setNotice('校园浏览器会话已清除；聊天历史和 API 密钥未删除。')
    } catch (e) { setError(apiErrorText(e)) }
    finally { setPending('') }
  }

  if (policy.isLoading) return <Typography.Text>正在读取聊天数据设置…</Typography.Text>
  return <Space orientation="vertical" size={16} style={{ width: '100%' }}>
    {(error || policy.isError) && <Alert type="error" title={error || apiErrorText(policy.error)} />}
    {notice && <Alert type="info" title={notice} />}
    <Card title="聊天历史保留期限" size="small"><Space orientation="vertical" style={{ width: '100%' }}>
      <Typography.Paragraph>{policy.data?.description}</Typography.Paragraph>
      <Space wrap><InputNumber aria-label="聊天历史保留天数" min={0} max={36500} precision={0} value={days} onChange={value => setDays(value ?? 30)} />
        <Typography.Text>天（0 = 长期保留）</Typography.Text>
        <Button disabled={Boolean(pending) || !policy.data} loading={pending === 'save'} onClick={() => void save()}>保存保留期限</Button></Space>
      <Typography.Text type="secondary">按会话最后更新时间计算。读取下方历史列表会执行已保存的过期期限；原聊天侧栏不承诺后台定时清理。</Typography.Text>
    </Space></Card>
    <Card title={`仅删除聊天历史 · ${policy.data?.history_count ?? 0} 个会话`} size="small"><Space orientation="vertical" style={{ width: '100%' }}>
      <Alert type="warning" title="删除聊天 ≠ 退出校园 ≠ 清除全部本地数据"
        description="这里仅删除当前浏览器所有者的聊天、工具记录、恢复/定时任务记录、运行目录和附件/交付副本，并清理相应草稿与滚动缓存。不删除校园登录、共享浏览器档案、API 密钥、学习数据库、用户任务目录或已下载文件。运行中的聊天须先停止并等待收尾完成。" />
      {!!policy.data?.pending_cleanup_count && <Alert type="warning" title={`有 ${policy.data.pending_cleanup_count} 个会话的文件副本待重试清理，请再次删除聊天历史。`} />}
      <Typography.Paragraph type="secondary">{policy.data?.erasure_limits}</Typography.Paragraph>
      <Space wrap>
        <Button disabled={Boolean(pending) || !policy.data} loading={pending === 'load'} onClick={() => void load()}>读取历史（并按期限清理）</Button>
        <Button danger disabled={Boolean(pending) || !policy.data || policy.data.history_retention_days === 0} onClick={() => setConfirmation({ kind: 'expired', title: '删除过期聊天历史？' })}>清理过期聊天</Button>
        <Button danger disabled={Boolean(pending) || !policy.data} loading={pending === 'delete'} onClick={() => setConfirmation({ kind: 'all', title: '删除当前所有聊天历史？' })}>删除当前所有聊天历史</Button>
      </Space>
      {items.map(item => <Space key={item.id} wrap style={{ justifyContent: 'space-between', width: '100%' }}>
        <Typography.Text>{item.title || '空白会话'} · {item.updated_at}</Typography.Text>
        <Button danger size="small" aria-label={`删除聊天 ${item.title || item.id}`} disabled={Boolean(pending)} onClick={() => setConfirmation({ kind: 'one', id: item.id, title: '删除这个聊天？' })}>删除聊天</Button>
      </Space>)}
      {cursor && <Button disabled={Boolean(pending)} onClick={() => void load(cursor)}>读取更多历史</Button>}
    </Space></Card>
    <Card title="校园会话与其他数据（独立操作）" size="small"><Space orientation="vertical">
      <Typography.Text>退出校园只清除应用拥有的共享校园浏览器会话，可能影响其他聊天；不会删除聊天或 API 密钥。</Typography.Text>
      <Button disabled={Boolean(pending)} loading={pending === 'campus'} onClick={() => void exitCampus()}>退出校园浏览器会话</Button>
      <Typography.Text type="secondary">API 密钥在“模型服务”或“工具与搜索”页使用现有密钥管理操作；本页不提供“全部本地数据”一键清除。</Typography.Text>
    </Space></Card>
    {confirmation && <Modal open title={confirmation.title} okText="确认不可逆删除" cancelText="取消" okButtonProps={{ danger: true }}
      onCancel={() => setConfirmation(null)} onOk={() => void erase()}>
      <Typography.Paragraph>此操作不可恢复，删除范围仅限当前所有者的所选聊天历史及应用可控副本。校园登录、共享浏览器档案、API 密钥和业务学习数据保留。正在运行或停止中的聊天会拒绝删除，请先停止并等待完成。</Typography.Paragraph>
    </Modal>}
  </Space>
}
