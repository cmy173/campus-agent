import { useEffect, useRef } from 'react'
import { Alert, Button, Typography } from 'antd'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { agentApi } from './api'

const toolNames: Record<string, string> = {
  'campus.query': '校园信息查询',
  'campus.academics': '课表与安排',
  'campus.status': '校园连接状态',
  'campus.notice.read': '学院通知',
}

export function recoverySummary(checkpoint: { pending_tool: string | null }) {
  return toolNames[checkpoint.pending_tool ?? ''] ?? '校园只读查询'
}

/**
 * Inline, non-blocking recovery state. A session expiry mid-task is normal, so the
 * user gets one line and a login entry point — never a dialog and never a re-ask.
 */
export function AuthRecoveryNotice({ id, onAccounts }: { id?: string; onAccounts: () => void }) {
  const queryClient = useQueryClient()
  const previous = useRef<string>('idle')
  const state = useQuery({
    queryKey: ['agent', 'recovery', id],
    queryFn: ({ signal }) => agentApi.recovery(id!, signal),
    enabled: Boolean(id),
    // Only a pending checkpoint is worth polling; idle conversations cost nothing.
    refetchInterval: query => (query.state.data && query.state.data.status !== 'idle' ? 1500 : false),
  })
  const status = state.data?.status ?? 'idle'
  useEffect(() => {
    // A completed continuation is a normal new turn in this conversation: refresh it
    // so the user sees the continued result without sending anything again.
    if (previous.current !== 'idle' && status === 'idle' && id) {
      void queryClient.invalidateQueries({ queryKey: ['agent', 'conversation', id] })
      void queryClient.invalidateQueries({ queryKey: ['agent', 'list'] })
    }
    previous.current = status
  }, [status, id, queryClient])
  if (state.isError || status === 'idle') return null
  const pending = state.data?.items.find(item => item.status === 'waiting_auth') ?? state.data?.items[0]
  const waiting = status === 'waiting_auth'
  return <Alert
    type={waiting ? 'info' : 'success'}
    showIcon
    title={waiting ? '任务在等待校园登录，已完成的资料都保留着' : '校园登录已确认，正在从原任务继续'}
    description={<>
      <Typography.Text>
        {waiting
          ? `需要在助手浏览器里完成一次学校登录，之后会从原目标继续（${recoverySummary(pending ?? { pending_tool: null })}）。不需要重新提问，也不用把任何密码或验证码贴进对话。`
          : '无需重新提问，正在接着原来的步骤完成。'}
      </Typography.Text>
      {waiting && <div style={{ marginTop: 8 }}><Button size="small" onClick={onAccounts}>打开校园账号</Button></div>}
    </>} />
}
