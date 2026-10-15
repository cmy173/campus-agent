import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Popconfirm, Space, Tag, Typography } from 'antd'
import { agentApi } from '../agent/api'
import { AgentErrorNotice } from '../agent/AgentErrorNotice'

/** Campus SDK authorization is separate from legacy sync-connector login. */
export function CampusBrowserSessionCard() {
  const client = useQueryClient()
  const status = useQuery({ queryKey: ['agent', 'campus', 'status'], queryFn: agentApi.campusStatus,
    refetchOnWindowFocus: true })
  const [pending, setPending] = useState<'connect' | 'disconnect' | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [updated, setUpdated] = useState<'connected' | 'disconnected' | null>(null)
  async function update(operation: 'connect' | 'disconnect') {
    if (pending) return
    setPending(operation); setError(null); setUpdated(null)
    try {
      const value = operation === 'connect' ? await agentApi.connectCampusBrowser() : await agentApi.disconnectCampus()
      client.setQueryData(['agent', 'campus', 'status'], value)
      setUpdated(operation === 'connect' && value.connected ? 'connected' : operation === 'disconnect' ? 'disconnected' : null)
    } catch (e) { setError(e) }
    finally { setPending(null) }
  }
  return <Card title="助手校园查询授权" size="small">
    <Space orientation="vertical" size={10} style={{ width: '100%' }}>
      <Typography.Paragraph style={{ margin: 0 }}>
        先在对话的助手浏览器工作区打开学校官方页面，手动完成校园登录及验证码或多因素验证，再同意复用登录。
        验证身份成功后，助手才可读取校园数据；这里不接收密码，也不移交 WebVPN 会话。
      </Typography.Paragraph>
      {status.isLoading ? <Typography.Text role="status">正在检查校园授权…</Typography.Text> : status.data && <Space wrap>
        <Tag color={status.data.connected ? 'success' : 'default'}>{status.data.connected ? '校园查询已连接' : '校园查询未连接'}</Tag>
        <Tag>{status.data.browser_session_authorized ? '已同意复用浏览器登录' : '尚未同意复用浏览器登录'}</Tag>
        {status.data.connected && status.data.session_remaining_seconds > 0 && <Typography.Text type="secondary">
          当前会话剩余约 {Math.ceil(status.data.session_remaining_seconds / 60)} 分钟（以服务端检查为准）
        </Typography.Text>}
      </Space>}
      {(Boolean(error) || status.isError) && <AgentErrorNotice error={error || status.error} title="校园授权操作未完成" />}
      {updated === 'connected' && <Alert type="success" title="校园身份已核验，已授权本次运行复用登录" />}
      {updated === 'disconnected' && <Alert type="info" title="校园查询授权已解除，已缓存资料保留" description="助手浏览器里的学校登录仍可能有效；如需退出，请在浏览器工作区访问官方页面退出。" />}
      <Space wrap>
        <Button type="primary" loading={pending === 'connect'} disabled={Boolean(pending)} onClick={() => void update('connect')}>
          复用助手浏览器校园登录
        </Button>
        <Button loading={status.isFetching} disabled={Boolean(pending)} onClick={() => { void status.refetch() }}>刷新校园授权</Button>
        {(status.data?.connected || status.data?.browser_session_authorized) && <Popconfirm title="解除助手校园查询授权？"
          description="停止复用登录并清除当前查询会话。保留缓存，不会退出助手浏览器的学校账号。" okText="解除授权" cancelText="取消"
          onConfirm={() => update('disconnect')}>
          <Button loading={pending === 'disconnect'} disabled={Boolean(pending)}>解除校园查询授权</Button>
        </Popconfirm>}
      </Space>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        授权在服务重启后仍然有效（只保存授权元数据，不保存 Cookie 或密码）；解除授权不等于退出所有浏览器账号。
        若通行证会话频繁过期，可在下方启用自动重新登录，避免每次都手动操作浏览器窗口。
      </Typography.Text>
    </Space>
  </Card>
}
