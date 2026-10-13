import { useState } from 'react'
import { Alert, Button, Checkbox, Drawer, Input, Select, Space, Typography } from 'antd'
import { CalendarOutlined, PlusOutlined, SendOutlined, SettingOutlined } from '@ant-design/icons'
import { useParams } from 'react-router-dom'
import { API_MODE, apiErrorText } from '../../lib/api/http'
import { AssistantSettings } from '../settings/AssistantSettings'
import type { ToolEnvelope } from './api'
import { PlanActions } from './PlanActions'
import { CalendarWorkspace, SourceWorkspace } from './Workspace'
import { TurnView } from './TurnView'
import { useConversationController } from './useConversationController'
import './agent.css'

const examples = ['这周有哪些作业需要留意？', '这份资料里要求提交哪些材料？', '读一下我当前的学习安排']

function ConversationPage({ id }: { id?: string }) {
  const { navigate, draft, consent, setConsent, sending, error, providers, courses,
    conversations, conversation, turns, active, actions, providerItems, selectedProvider,
    provider, courseId, transcript, messages, changeText, create, send, cancel, options,
    selectCourse, selectProvider, reconnect, onScroll } = useConversationController(id)
  const [calendar, setCalendar] = useState(false)
  const [settings, setSettings] = useState(false)
  const [source, setSource] = useState<ToolEnvelope | null>(null)
  return <div className="agent-page">
    <div className="agent-toolbar">
      <div><Typography.Title level={3} style={{ margin: 0 }}>校园助手</Typography.Title><Typography.Text type="secondary">说出目标，信息和安排一起核对。</Typography.Text></div>
      <Space wrap size={8}>
        <Button aria-label="新对话" icon={<PlusOutlined />} onClick={() => navigate('/chat')}>新对话</Button>
        <Button aria-label="日历" icon={<CalendarOutlined />} onClick={() => setCalendar(true)}>日历</Button>
        <Button aria-label="模型设置" icon={<SettingOutlined />} onClick={() => setSettings(true)}>模型设置</Button>
      </Space>
    </div>
    <div className="agent-context-row">
      <Select aria-label="选择历史对话" placeholder="历史对话" value={id} allowClear style={{ minWidth: 170, maxWidth: '100%' }}
        onChange={value => navigate(value ? '/chat/' + value : '/chat')}
        options={conversations.data?.items.map(c => ({ value: c.id, label: `${new Date(c.created_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })} · ${c.id.slice(-6)}` }))} />
      <Select aria-label="当前课程" placeholder="所有课程" allowClear showSearch optionFilterProp="label" value={courseId}
        disabled={active || sending} style={{ minWidth: 170, maxWidth: '100%' }} options={options}
        onChange={selectCourse} />
      {provider && id && <Typography.Text type="secondary">{provider.name} · {provider.model}</Typography.Text>}
    </div>
    {API_MODE === 'mock' && <Alert type="warning" title="当前为模拟数据模式，对话功能未启用" description="启动本地 API 后使用真实模式，不会用示例回答冒充智能问答。" />}
    {import.meta.env.VITE_AGENT_TEST_DATA === '1' && <Alert type="info" title="合成数据验收环境" description="当前使用受控测试模型，未连接真实校园账户。" />}
    {(error || conversation.isError) && <Alert type="error" title={error || apiErrorText(conversation.error)}
      action={<Button size="small" onClick={reconnect}>重新连接</Button>} />}
    {(providers.isError || courses.isError || conversations.isError) && <Alert type="warning" title="部分上下文读取失败" description={apiErrorText(providers.error ?? courses.error ?? conversations.error)} />}
    <div className="agent-transcript" ref={transcript} aria-label="对话记录" onScroll={onScroll}>
      <div ref={messages}>
      {!turns.length && <div className="agent-welcome">
        <Typography.Title level={2}>今天有什么要紧事？</Typography.Title>
        <Typography.Paragraph type="secondary">可以查作业、理解资料要求，或看看学习安排。回答会保留来源和未确认的信息。</Typography.Paragraph>
        <Space wrap>{examples.map(text => <Button key={text} onClick={() => changeText(text)}>{text}</Button>)}</Space>
      </div>}
      {conversation.isLoading && id && <Typography.Text>正在恢复对话…</Typography.Text>}
      {turns.map(turn => <TurnView key={turn.id} turn={turn} onSource={setSource} onCancel={cancel} />)}
      {actions.isError && <Alert type="warning" title="安排方案读取失败" description={apiErrorText(actions.error)} />}
      <PlanActions actions={actions.data?.items ?? []} disabled={active || sending} onAccept={(action, operation) => {
        void send({ text: operation === 'apply' ? '按这个来' : '撤销刚才安排', action, operation })
      }} />
      </div>
    </div>
    {!id && API_MODE === 'real' && <section className="agent-start" aria-label="模型发送授权">
      {!providerItems.length && !providers.isLoading ? <Space orientation="vertical">
        <Typography.Text>先选择一个模型服务，即可开始问答。已有资料和日历仍可查看。</Typography.Text>
        <Button onClick={() => setSettings(true)}>配置模型服务</Button>
      </Space> : <>
        <Select aria-label="选择对话模型" value={selectedProvider} onChange={selectProvider}
          options={providerItems.map(p => ({ value: p.id, label: `${p.name} · ${p.model}` }))} style={{ width: '100%' }} />
        <Checkbox checked={consent} onChange={event => setConsent(event.target.checked)}>
          同意将这次对话及相关本地资料发送到 {provider?.name ?? '所选模型'}{provider ? `（${new URL(provider.base_url).host}）` : ''}
        </Checkbox>
        <Button type="primary" loading={sending} disabled={!consent || !selectedProvider} onClick={create}>开始对话</Button>
      </>}
    </section>}
    <div className="agent-composer">
      <Input.TextArea aria-label="向校园助手提问" placeholder="例如：这周三晚上没空，看看剩下的任务怎么安排。"
        autoSize={{ minRows: 2, maxRows: 6 }} maxLength={8000} value={draft.text} onChange={e => changeText(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send() } }} />
      <div className="agent-composer-footer"><Typography.Text type="secondary">Enter 发送 · Shift+Enter 换行</Typography.Text>
        <Button aria-label="发送" type="primary" icon={<SendOutlined />} onClick={() => void send()} loading={sending} disabled={!id || active || !draft.text.trim() || API_MODE === 'mock'}>发送</Button>
      </div>
    </div>
    <SourceWorkspace result={source} onClose={() => setSource(null)} />
    <CalendarWorkspace open={calendar} onClose={() => setCalendar(false)} />
    <Drawer title="助手设置" open={settings} onClose={() => setSettings(false)} size="min(660px, 100vw)">{settings && <AssistantSettings />}</Drawer>
  </div>
}

export function AgentPage() {
  const { conversationId } = useParams()
  return <ConversationPage key={conversationId ?? 'new'} id={conversationId} />
}
