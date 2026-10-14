import { useState } from 'react'
import { Alert, Button, Input, Segmented, Space, Tag, Typography } from 'antd'
import { ChatLayout } from './ChatLayout'
import { ToolActivities } from './ToolActivities'
import { projectTurnEvents } from './turnProjection'
import type { AgentEvent, AgentTurn } from './api'
import './agent.css'

const turn: AgentTurn = { id: 'preview-turn', conversation_id: 'preview', text: '查一下实验报告要求，并整理附件。',
  status: 'succeeded', response: '实验报告需要附原始数据。已整理成报告清单.md；截止时间尚未给出。',
  error: null, created_at: '2026-10-14T08:00:00Z', updated_at: '2026-10-14T08:01:00Z' }
const events: AgentEvent[] = [
  { seq: 1, kind: 'harness_event', created_at: turn.created_at, data: { session_id: 'preview', event: {
    type: 'tool/call', data: { callId: 'read-1', name: 'read', arguments: { path: '实验说明.md' } } } } },
  { seq: 2, kind: 'harness_event', created_at: turn.updated_at, data: { session_id: 'preview', event: {
    type: 'tool/result', data: { message: { toolCallId: 'read-1', content: [{ type: 'text', text: '实验报告附原始数据；截止时间未给出。' }] } } } } },
]
const projection = projectTurnEvents(events, turn)

/** Development-only presentation example; never opens a model/API session. */
export function ChatLayoutPreview() {
  const [variant, setVariant] = useState<'conversation' | 'studio'>('conversation')
  const [theme, setTheme] = useState<'calm' | 'contrast'>('calm')
  const [text, setText] = useState('请补充一个阅读计划')
  return <div className="agent-preview">
    <Space wrap className="agent-preview-controls">
      <Tag color="gold">仅开发示例 · 固定合成数据 · 不调用模型</Tag>
      <Segmented aria-label="示例布局" value={variant} onChange={value => setVariant(value as typeof variant)}
        options={[{ value: 'conversation', label: '对话布局' }, { value: 'studio', label: '侧栏布局' }]} />
      <Segmented aria-label="示例主题" value={theme} onChange={value => setTheme(value as typeof theme)}
        options={[{ value: 'calm', label: '柔和主题' }, { value: 'contrast', label: '高对比主题' }]} />
    </Space>
    <ChatLayout variant={variant} theme={theme}
      toolbar={<div className="agent-toolbar"><Typography.Title level={3}>校园助手</Typography.Title><Button>新对话</Button></div>}
      context={<div className="agent-context-row"><Tag>实验课程</Tag><Tag>文件与成果 · 1</Tag><Button>日历</Button></div>}
      notices={<Alert type="info" title="相同会话、工具事件和草稿；只切换布局与 tokens" />}
      transcript={<div className="agent-transcript" aria-label="示例对话记录"><article className="agent-turn">
        <div className="agent-user-message">{turn.text}</div>
        <div className="agent-assistant-message"><div className="agent-answer">{turn.response}</div>
          <ToolActivities tools={projection.tools}>{projection.children}</ToolActivities>
          <div className="agent-fact">来源：实验说明.md · 截止时间未知</div>
          <div className="agent-file-results"><Tag>报告清单.md · Markdown 成果</Tag></div>
        </div>
      </article></div>}
      setup={null}
      composer={<div className="agent-composer"><Input.TextArea aria-label="示例草稿" value={text} onChange={event => setText(event.target.value)} />
        <Typography.Text type="secondary">切换布局后草稿保持，示例不发送请求。</Typography.Text></div>} />
  </div>
}
