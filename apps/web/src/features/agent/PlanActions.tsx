import { Alert, Button, Card, Collapse, Space, Tag, Typography } from 'antd'
import type { PlanAction } from './api'
import { fmtDateTime } from '../../lib/time'

export function PlanActions({ actions, disabled, onAccept }: {
  actions: PlanAction[]; disabled: boolean; onAccept: (action: PlanAction, operation: 'apply' | 'revoke') => void
}) {
  return <section aria-label="安排方案">
    {actions.slice(0, 5).map(action => <Card key={action.id} size="small" style={{ marginBlock: 12 }}
      title={`${action.kind === 'apply' ? '安排方案' : '撤销安排'} · ${action.preview.plan.horizon_start} 至 ${action.preview.plan.horizon_end}`}>
      <Space orientation="vertical" style={{ width: '100%' }}>
        <Tag>{({ pending: '待接受', approved: '已接受，待执行', executing: '执行中', completed: '操作已完成' })[action.status]}</Tag>
        <Typography.Text>新增 {action.preview.diff.added.length} 段 · 移除 {action.preview.diff.removed.length} 段 · 保留 {action.preview.diff.retained.length} 段</Typography.Text>
        {!!action.preview.plan.unassigned.length && <Alert type="warning" title="部分任务未能安排" description={action.preview.plan.unassigned.map(item => item.message).join('；')} />}
        <Collapse items={[{ key: 'details', label: '查看具体时间与差异', children: <>
          {(['added', 'removed', 'retained'] as const).map(kind => <div key={kind}>
            <Typography.Text strong>{({ added: '新增', removed: '移除', retained: '保留' })[kind]}</Typography.Text>
            {action.preview.diff[kind].map(block => <p key={block.id}>{action.preview.task_titles?.[block.task_id] ?? block.task_id} · {fmtDateTime(block.starts_at)} — {fmtDateTime(block.ends_at)}{block.locked ? ' · 已锁定' : ''}</p>)}
          </div>)}
          {action.preview.constraints?.blocked?.map((block, i) => <p key={i}>本次不可用：{fmtDateTime(block.starts_at)} — {fmtDateTime(block.ends_at)}</p>)}
        </> }]} />
        {action.status !== 'completed' && <Button type="primary" disabled={disabled} onClick={() => onAccept(action, action.kind)}>
          {action.status === 'approved' ? '继续执行已接受操作' : '按这个来'}
        </Button>}
        {action.kind === 'apply' && action.status === 'completed' && <Button disabled={disabled} onClick={() => onAccept(action, 'revoke')}>撤销这次安排</Button>}
        {action.status === 'completed' && <Typography.Text type="secondary">此处保留操作记录；当前生效安排请打开日历查看。</Typography.Text>}
      </Space>
    </Card>)}
  </section>
}
