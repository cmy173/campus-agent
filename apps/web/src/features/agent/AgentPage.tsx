import { useRef, useState } from 'react'
import { Alert, Button, Dropdown, Input, Select, Space, Tag, Typography } from 'antd'
import { BookOutlined, CalendarOutlined, CheckSquareOutlined, FileTextOutlined, FolderOpenOutlined, GlobalOutlined, PaperClipOutlined, SendOutlined } from '@ant-design/icons'
import { useParams, useSearchParams } from 'react-router-dom'
import { API_MODE, apiErrorText } from '../../lib/api/http'
import type { ConversationFile, ToolEnvelope } from './api'
import { PlanActions } from './PlanActions'
import { CalendarWorkspace, SourceWorkspace } from './Workspace'
import { TurnView } from './TurnView'
import { useConversationController } from './useConversationController'
import { useConversationFiles } from './useConversationFiles'
import { FileWorkspace } from './FileWorkspace'
import { BrowserWorkspace } from './BrowserWorkspace'
import { ChatLayout } from './ChatLayout'
import { ConversationSidebar } from './ConversationSidebar'
import { AgentErrorNotice } from './AgentErrorNotice'
import { AuthRecoveryNotice } from './AuthRecoveryNotice'
import { TaskWorkspace } from './TaskWorkspace'
import { useTaskWorkspace } from './useTaskWorkspace'
import './agent.css'

const examples = ['这周有哪些作业需要留意？', '这份资料里要求提交哪些材料？', '读一下我当前的学习安排']

function ConversationPage({ id }: { id?: string }) {
  const { navigate, draft, sending, error, providers, courses,
    conversations, historyItems, historySearch, setHistorySearch, conversation, turns, active, actions, providerItems, selectedProvider,
    provider, courseId, transcript, messages, changeText, send, cancel, options,
    selectCourse, selectProvider, selectAttachments, reconnect, onScroll } = useConversationController(id)
  const workspace = useTaskWorkspace()
  const [, setParams] = useSearchParams()
  const openSettings = (tab: 'assistant' | 'accounts') => setParams(previous => {
    const next = new URLSearchParams(previous)
    for (const key of ['course', 'paper', 'tab', 'revision', 'base', 'view', 'course_id', 'mode']) next.delete(key)
    next.set('workspace', 'settings'); next.set('settings_tab', tab)
    return next
  })
  const [source, setSource] = useState<ToolEnvelope | null>(null)
  const [fileWorkspace, setFileWorkspace] = useState(false)
  const [browserWorkspace, setBrowserWorkspace] = useState(false)
  const [focusedFile, setFocusedFile] = useState<ConversationFile | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const fileState = useConversationFiles(id, active || sending, draft.attachment_ids ?? [], selectAttachments)
  const files = fileState.files.data?.items ?? []
  const conversationTitle = (id ? historyItems.find(item => item.id === id)?.title : null) || '校园助手'
  return <>
    <div className="agent-workbench">
    <ConversationSidebar items={historyItems} currentId={id} search={historySearch} onSearch={setHistorySearch}
      onNew={() => navigate('/chat')} onSelect={value => navigate('/chat/' + encodeURIComponent(value))}
      onSettings={() => openSettings('assistant')} onAccounts={() => openSettings('accounts')}
      loading={conversations.isLoading} error={conversations.error} onRetry={() => { void conversations.refetch() }}
      hasMore={conversations.hasNextPage} loadingMore={conversations.isFetchingNextPage} onLoadMore={() => { void conversations.fetchNextPage() }} />
    <ChatLayout toolbar={<div className="agent-toolbar">
      <Typography.Title level={5} className="agent-heading-title" title={conversationTitle}>{conversationTitle}</Typography.Title>
      <Space size={6} className="agent-toolbar-actions">
        <Button size="small" aria-label="日历" icon={<CalendarOutlined />} onClick={() => workspace.open('calendar')}><span className="agent-action-label">日历</span></Button>
        {id && <Button size="small" aria-label={`文件与成果${files.length ? `，${files.length} 个文件` : ''}`} icon={<FolderOpenOutlined />} onClick={() => setFileWorkspace(true)}><span className="agent-action-label">文件与成果{files.length ? ` (${files.length})` : ''}</span></Button>}
        {id && <Button size="small" aria-label="浏览器工作区" icon={<GlobalOutlined />} onClick={() => setBrowserWorkspace(true)}><span className="agent-action-label">浏览器</span></Button>}
        <Dropdown trigger={['click']} menu={{ items: [
          { key: 'courses', icon: <BookOutlined />, label: '课程与资料', onClick: () => workspace.open('courses') },
          { key: 'tasks', icon: <CheckSquareOutlined />, label: '学习任务与反馈', onClick: () => workspace.open('tasks') },
          { key: 'reviews', icon: <FileTextOutlined />, label: '复习产物', onClick: () => workspace.open('reviews') },
        ] }}><Button size="small" aria-label="打开资料与任务" icon={<BookOutlined />}><span className="agent-action-label">资料与任务</span></Button></Dropdown>
      </Space>
    </div>} context={<div className="agent-context-row">
      <Select size="small" aria-label="当前课程" placeholder="所有课程" allowClear showSearch optionFilterProp="label" value={courseId}
        disabled={active || sending} style={{ maxWidth: '100%' }} options={options}
        onChange={selectCourse} />
      {providers.isLoading && <Typography.Text type="secondary" role="status" className="agent-provider-label">正在读取可用的模型服务…</Typography.Text>}
      {!providers.isLoading && (providerItems.length > 1
        ? <Select size="small" aria-label="选择对话模型" className="agent-provider-select" value={selectedProvider} onChange={selectProvider}
          options={providerItems.map(p => ({ value: p.id, label: `${p.name} · ${p.model}` }))} style={{ maxWidth: '100%' }} />
        : provider && <Typography.Text type="secondary" className="agent-provider-label">{provider.name} · {provider.model}</Typography.Text>)}
    </div>} notices={<>
    {API_MODE === 'mock' && <Alert type="warning" title="当前为模拟数据模式，对话功能未启用" description="启动本地 API 后使用真实模式，不会用示例回答冒充智能问答。" />}
    {import.meta.env.VITE_AGENT_TEST_DATA === '1' && <Alert type="info" title="合成数据验收环境" description="当前使用受控测试模型，未连接真实校园账户。" />}
    {(Boolean(error) || conversation.isError) && <AgentErrorNotice error={error || conversation.error}
      action={<Button size="small" onClick={reconnect}>重新连接</Button>} />}
    {(providers.isError || courses.isError || conversations.isError) && <Alert type="warning" title="部分上下文读取失败" description={apiErrorText(providers.error ?? courses.error ?? conversations.error)} />}
    <AuthRecoveryNotice id={id} onAccounts={() => openSettings('accounts')} />
    </>} transcript={<div className="agent-transcript" ref={transcript} aria-label="对话记录" onScroll={onScroll}>
      <div ref={messages}>
      {!turns.length && !conversation.isLoading && !conversation.isError && <div className="agent-welcome">
        <Typography.Text className="agent-welcome-kicker">先从一件事开始</Typography.Text>
        <Typography.Title level={2}>今天有什么要紧事？</Typography.Title>
        <Typography.Paragraph type="secondary">可以查作业、理解资料要求，或看看学习安排。回答会保留来源和未确认的信息。</Typography.Paragraph>
        <Space wrap className="agent-example-prompts">{examples.map(text => <Button key={text} onClick={() => changeText(text)}>{text}</Button>)}</Space>
      </div>}
      {conversation.isLoading && id && <Typography.Text>正在恢复对话…</Typography.Text>}
      {turns.map(turn => <TurnView key={turn.id} turn={turn} onSource={setSource} onCancel={cancel}
        attachments={files.filter(file => turn.attachment_ids?.includes(file.id))}
        onFile={file => { setFocusedFile(file); setFileWorkspace(true) }} />)}
      {actions.isError && <Alert type="warning" title="安排方案读取失败" description={apiErrorText(actions.error)} />}
      <PlanActions actions={actions.data?.items ?? []} disabled={active || sending || fileState.uploading} onAccept={(action, operation) => {
        void send({ text: operation === 'apply' ? '按这个来' : '撤销刚才安排', action, operation })
      }} />
      </div>
    </div>} composer={<div className="agent-composer">
      {fileState.error && <AgentErrorNotice error={fileState.error} />}
      {fileState.selected.length > 0 && <div className="agent-attachments" aria-label="待发送附件">
        {fileState.selected.map(fid => <Tag key={fid} closable={!fileState.disabled} onClose={() => fileState.toggle(fid)}>
          {files.find(file => file.id === fid)?.name ?? '附件'}
        </Tag>)}
      </div>}
      <Input.TextArea aria-label="向校园助手提问" aria-describedby="agent-composer-hint" placeholder="例如：这周三晚上没空，看看剩下的任务怎么安排。"
        autoSize={{ minRows: 2, maxRows: 6 }} maxLength={8000} value={draft.text} onChange={e => changeText(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); if ((id || selectedProvider) && !active && !sending && !fileState.uploading && API_MODE === 'real') void send() } }} />
      <input ref={fileInput} type="file" multiple hidden aria-label="上传对话附件" onChange={event => {
        const chosen = event.target.files
        if (chosen) void fileState.upload(chosen)
        event.target.value = ''
      }} />
      <div className="agent-composer-footer"><div className="agent-composer-hint">
        <Typography.Text type="secondary" id="agent-composer-hint">{active ? '正在处理本轮，可先写下追问'
          : !providerItems.length ? '先在设置中启用模型服务' : 'Enter 发送 · Shift+Enter 换行'}</Typography.Text>
        {!providerItems.length && <Button type="link" size="small" aria-label="配置模型服务" onClick={() => workspace.open('settings')}>配置模型服务</Button>}
      </div>
        <Space><Button size="small" icon={<PaperClipOutlined />} onClick={() => fileInput.current?.click()} loading={fileState.uploading}
          disabled={!id || fileState.disabled || API_MODE === 'mock'}>附件</Button>
        <Button size="small" aria-label="发送" type="primary" icon={<SendOutlined />} onClick={() => void send()} loading={sending} disabled={(!id && !selectedProvider) || active || sending || fileState.uploading || !draft.text.trim() || API_MODE === 'mock'}>发送</Button></Space>
      </div>
    </div>} />
    </div>
    <SourceWorkspace result={source} onClose={() => setSource(null)} />
    {id && <BrowserWorkspace id={id} open={browserWorkspace} active={active || sending}
      onOpen={() => setBrowserWorkspace(true)} onClose={() => setBrowserWorkspace(false)} />}
    <CalendarWorkspace open={workspace.workspace === 'calendar' && !workspace.calendarEditor} onClose={workspace.close} onEdit={workspace.editCalendar} />
    <FileWorkspace open={fileWorkspace} onClose={() => setFileWorkspace(false)} items={files} selected={fileState.selected}
      focusedFile={focusedFile}
      onSelect={fileState.toggle} disabled={fileState.disabled} loading={fileState.files.isFetching} error={fileState.files.error}
      onRefresh={() => { void fileState.refresh() }} />
    <TaskWorkspace state={workspace} />
  </>
}

export function AgentPage() {
  const { conversationId } = useParams()
  return <ConversationPage key={conversationId ?? 'new'} id={conversationId} />
}
