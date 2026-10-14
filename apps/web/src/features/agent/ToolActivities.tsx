import { Button, Collapse, Space, Tag, Typography } from 'antd'
import type { ChildActivity, ToolActivity } from './turnProjection'

const statuses = {
  running: { label: '执行中', color: 'processing' },
  stopping: { label: '停止中', color: 'processing' },
  completed: { label: '已完成', color: 'success' },
  failed: { label: '失败', color: 'error' },
  cancelled: { label: '已停止', color: 'default' },
  interrupted: { label: '未完成', color: 'warning' },
}

export function ToolActivities({ tools, children, onInterrupt, stoppingChildren = [] }: {
  tools: ToolActivity[]; children: ChildActivity[]; onInterrupt?: (id: string) => void; stoppingChildren?: string[]
}) {
  return <>
    {children.length > 0 && <section className="agent-child-tasks" aria-label="子任务进度">
      <Typography.Paragraph strong>子任务 · {children.length}</Typography.Paragraph>
      {children.map((child, index) => <div key={child.id}>
        <Space><Typography.Text>{child.label ?? `子任务 ${index + 1}`}</Typography.Text><Tag color={statuses[child.status].color}>{statuses[child.status].label}</Tag>
          {onInterrupt && ['running', 'stopping'].includes(child.status) && <Button size="small" aria-label={child.label ? `停止 ${child.label}` : `停止子任务 ${index + 1}`}
            loading={stoppingChildren.includes(child.id)} disabled={child.status === 'stopping' || stoppingChildren.includes(child.id)}
            onClick={() => onInterrupt(child.id)}>停止子任务</Button>}
        </Space>
        {child.output && <Collapse size="small" items={[{ key: child.id, label: '查看子任务结果',
          children: <pre className="agent-tool-output">{child.output}</pre> }]} />}
      </div>)}
    </section>}
    {tools.length > 0 && <Collapse className="agent-tool-activities" size="small" items={[{
      key: 'tools', label: <Space><Tag>{tools.length} 项操作</Tag>工具执行记录
        {tools.some(t => t.status === 'running') && <Tag color="processing">执行中</Tag>}
        {tools.some(t => t.status === 'failed') && <Tag color="error">有操作失败</Tag>}
        {tools.some(t => t.status === 'cancelled') && <Tag>有操作已停止</Tag>}
      </Space>,
      children: <Space orientation="vertical" size={12} style={{ width: '100%' }}>{tools.map(tool => <article key={tool.id}>
        <Space wrap><Typography.Text strong>{tool.label}</Typography.Text><Tag color={statuses[tool.status].color}>{statuses[tool.status].label}</Tag></Space>
        <Collapse size="small" items={[{ key: tool.id, label: '查看输入与结果', children: <>
          {tool.arguments && <><Typography.Text type="secondary">输入</Typography.Text><pre className="agent-tool-output">{tool.arguments}</pre></>}
          <Typography.Text type="secondary">结果</Typography.Text>
          <pre className="agent-tool-output">{tool.output || (tool.status === 'running' ? '等待工具返回…' : '没有返回文本结果。')}</pre>
        </> }]} />
      </article>)}</Space>,
    }]} />}
  </>
}
