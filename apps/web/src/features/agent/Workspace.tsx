import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Drawer, Empty, Space, Tag, Typography } from 'antd'
import { apiRequest, apiErrorText } from '../../lib/api/http'
import { api } from '../../lib/api/client'
import { fmtDateTime, fmtTimeRange } from '../../lib/time'
import type { CurrentPlanResponse, Resource } from '../../types/contract'
import type { ToolEnvelope } from './api'

export function dueText(item: Record<string, unknown>): string {
  if (!item.due_at || item.due_precision === 'unknown') return '截止时间未知，请核对原文'
  const iso = String(item.due_at)
  if (item.due_precision === 'date') return `${fmtDateTime(iso, undefined, 'YYYY-MM-DD')}（仅日期，未给出具体时刻）`
  return fmtDateTime(iso)
}

export function SourceWorkspace({ result, onClose }: { result: ToolEnvelope | null; onClose: () => void }) {
  const resource = result?.data.resource
  const id = resource?.id ? String(resource.id) : undefined
  const current = useQuery({ queryKey: ['agent-source-current', id],
    queryFn: () => apiRequest<Resource>(`/resources/${encodeURIComponent(id!)}`), enabled: Boolean(id && result), staleTime: 0 })
  const changed = current.data && current.data.version !== resource?.version
  return <Drawer title={`原文 · ${String(resource?.title ?? '资料')}`} open={Boolean(result)} onClose={onClose}
    destroyOnHidden size="min(560px, 100vw)" className="agent-workspace">
    {result && <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <Typography.Text type="secondary">回答依据的版本：v{String(resource?.version ?? '未知')}；以下是当时读取的原文快照。</Typography.Text>
      {current.isError && <Alert type="warning" title="暂时无法核对当前资料版本" description={apiErrorText(current.error)} />}
      {changed && <Alert type="warning" title="资料已更新，回答依据的是旧版本" description="请在对话中要求重新读取，旧快照保留供核对。" />}
      {(result.data.items ?? []).map(item => <article key={String(item.id)} className="agent-source-chunk">
        <Typography.Text type="secondary">来源 {String(item.id)} · 位置 {JSON.stringify(item.locator ?? null)}</Typography.Text>
        <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginTop: 8 }}>{String(item.text ?? '')}</Typography.Paragraph>
      </article>)}
      {!result.data.items?.length && <Empty description="本次没有读取到正文片段" />}
      <Typography.Text type="secondary">{result.coverage.complete ? '当前读取范围已完整返回' : result.coverage.missing.join('；')}</Typography.Text>
    </Space>}
  </Drawer>
}

export function CalendarWorkspace({ open, onClose, onEdit }: { open: boolean; onClose: () => void; onEdit?: () => void }) {
  const current = useQuery({ queryKey: ['plans', 'current'], queryFn: () => apiRequest<CurrentPlanResponse>('/plans/current'), enabled: open, staleTime: 0 })
  const plan = current.data?.plan
  const tasks = useQuery({ queryKey: ['agent-calendar-tasks'], queryFn: () => api.getAllTasks({ include_archived: true }),
    enabled: open && Boolean(plan), staleTime: 0 })
  const titles = new Map(tasks.data?.items.map(task => [task.id, task.title]) ?? [])
  return <Drawer title="我的日历" open={open} onClose={onClose} destroyOnHidden size="min(560px, 100vw)" className="agent-workspace">
    {onEdit && <Button onClick={onEdit} style={{ marginBottom: 16 }}>编辑安排与锁定任务</Button>}
    {current.isLoading && <Typography.Text>正在读取安排…</Typography.Text>}
    {current.isError && <Alert type="error" title="日历读取失败" description={apiErrorText(current.error)} />}
    {tasks.isError && <Alert type="warning" title="任务名称暂时无法读取" description={apiErrorText(tasks.error)} />}
    {current.data && !plan && <Empty description="还没有生效的安排，可以在对话中提出安排目标" />}
    {plan && <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <Typography.Text>{plan.horizon_start} 至 {plan.horizon_end} · 计划 v{plan.version}</Typography.Text>
      {plan.input_revision !== current.data?.current_input_revision && <Alert type="warning" title="任务或可用时间已更新，现有安排需要重新核对" />}
      {plan.blocks.map(block => <article key={block.id} className="agent-calendar-block">
        <Typography.Text strong>{fmtDateTime(block.starts_at, undefined, 'M月D日')} · {fmtTimeRange(block.starts_at, block.ends_at)}</Typography.Text>
        <div>{titles.get(block.task_id) ?? `任务 ${block.task_id}`} {block.locked && <Tag>已锁定</Tag>}</div>
      </article>)}
      {!plan.blocks.length && <Empty description="当前计划没有排入时间块" />}
      {plan.unassigned.map(item => <Alert key={item.task_id} type="warning" title={`未排入：${titles.get(item.task_id) ?? item.task_id}`}
        description={item.message ?? item.reason_code} />)}
      <Button onClick={onClose}>回到对话</Button>
    </Space>}
  </Drawer>
}
