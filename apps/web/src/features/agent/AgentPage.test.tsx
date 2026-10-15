import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App as AntApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AgentPage } from './AgentPage'
import { AiProviderCard } from '../settings/AiProviderCard'
import { ApiError } from '../../lib/api/http'
import type { AgentConversation, AgentTurn, ToolEnvelope } from './api'
import { dueText } from './Workspace'
import { prepareSend, readDraft, saveDraft } from './draft'

const request = vi.hoisted(() => vi.fn())
vi.mock('../../lib/api/http', async importOriginal => {
  const original = await importOriginal<typeof import('../../lib/api/http')>()
  return { ...original, API_MODE: 'real', apiRequest: request }
})

/** A new chat navigates, remounts and refetches; the 1s default is too tight under load. */
const settle = { timeout: 5000 }

/** The provider label replaces the removed start gate as the "providers are loaded" anchor. */
const providerReady = () => screen.findByText('测试模型 · fixture')

const result: ToolEnvelope = {
  status: 'ok', data: { resource: { id: 'resource_a', title: '实验说明', version: 1 }, items: [
    { id: 'chunk_a', text: '报告需要原始数据附件。', locator: { page: 2 } },
  ] }, evidence: [{ source_id: 'chunk_a', entity_id: 'resource_a', version: 1, locator: { page: 2 }, observed_at: null }],
  coverage: { scope: 'course_a', complete: true, missing: [] }, freshness: { fetched_at: null, stale: true }, warnings: [], error: null,
}
let conversation: AgentConversation
let firstTurn: AgentTurn
let sends: { key: string; body: unknown }[]
let rejectNext: boolean

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  request.mockReset()
  sends = []
  rejectNext = false
  firstTurn = { id: 'turn_one', conversation_id: 'conv_one', text: '查资料要求', status: 'succeeded', response: '已从原文确认报告需要数据附件。', error: null, created_at: '2026-10-13T00:00:00Z', updated_at: '2026-10-13T00:00:00Z' }
  conversation = { id: 'conv_one', provider_id: 'provider_a', course_ids: ['course_a'], current_course_id: 'course_a', send_to_provider: true,
    created_at: '2026-10-13T00:00:00Z', updated_at: '2026-10-13T00:00:00Z', turns: [firstTurn] }
  request.mockImplementation(async (path: string, options?: { method?: string; idempotencyKey?: string; body?: unknown; query?: { after?: number } }) => {
    if (path === '/ai/providers') return { items: [{ id: 'provider_a', name: '测试模型', model: 'fixture', base_url: 'http://127.0.0.1:19191/v1', protocol: 'openai_chat_completions', has_api_key: false }], next_cursor: null }
    if (path === '/courses') return { items: [{ id: 'course_a', title: '实验课程' }], next_cursor: null }
    if (path === '/plans/current') return { plan: null, current_input_revision: 'test' }
    if (path === '/resources/resource_a') return { id: 'resource_a', version: 2 }
    if (path === '/agent/conversations' && options?.method === 'POST') return { ...conversation, turns: [] }
    if (path === '/agent/conversations') return { items: [conversation], next_cursor: null }
    if (path === '/agent/conversations/conv_one') return structuredClone(conversation)
    if (path === '/agent/conversations/conv_one/actions') return { items: [] }
    if (path === '/agent/conversations/conv_one/files') return { items: [] }
    if (path === '/agent/conversations/conv_one/turns') {
      sends.push({ key: options!.idempotencyKey!, body: options!.body })
      if (rejectNext) {
        rejectNext = false
        throw new ApiError(0, { code: 'NETWORK_ERROR', message: '网络中断', retryable: true, request_id: 'test' })
      }
      const turn = { ...firstTurn, id: 'turn_two', text: (options!.body as { text: string }).text, response: '第二轮已核对' }
      conversation = { ...conversation, turns: [...(conversation.turns ?? []), turn] }
      return turn
    }
    if (path === '/agent/turns/turn_one/cancel') {
      firstTurn = { ...firstTurn, status: 'cancelled' }
      conversation = { ...conversation, turns: [firstTurn] }
      return firstTurn
    }
    if (path.startsWith('/agent/turns/')) return {
      ...(path.endsWith('turn_two') ? conversation.turns!.at(-1)! : firstTurn),
      events: { items: options?.query?.after ? [] : [{ seq: 1, kind: 'tool_result', created_at: firstTurn.created_at, data: { name: 'materials.read', result } }], next_cursor: 1 },
    }
    throw new Error('Unexpected path: ' + path)
  })
})

function show(path = '/chat/conv_one') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><ConfigProvider locale={zhCN}><AntApp>
    <MemoryRouter initialEntries={[path]}><Routes><Route path="/chat/:conversationId?" element={<AgentPage />} /></Routes></MemoryRouter>
  </AntApp></ConfigProvider></QueryClientProvider>)
}

describe('Agent conversation product flow', () => {
  it('loads all cursor pages, deduplicates IDs and remotely searches conversation content', async () => {
    const user = userEvent.setup()
    const base = request.getMockImplementation()!
    const older = { ...conversation, id: 'conv_old', title: '数学复习', turns: undefined }
    request.mockImplementation(async (path, options) => {
      const query = options?.query as { cursor?: string; search?: string } | undefined
      if (path === '/agent/conversations') {
        if (query?.search === '数学') return { items: [older], next_cursor: null }
        return query?.cursor ? { items: [conversation, older], next_cursor: null } : { items: [conversation], next_cursor: 'opaque_cursor==' }
      }
      return base(path, options)
    })
    show()
    await user.click(await screen.findByRole('button', { name: '加载更多历史' }))
    const history = screen.getByRole('navigation', { name: '历史对话' })
    expect(await within(history).findByRole('button', { name: /数学复习/ })).toBeInTheDocument()
    expect(within(history).getAllByRole('button')).toHaveLength(2)
    await user.type(screen.getByRole('textbox', { name: '搜索历史对话' }), '数学')
    // useConversationController debounces the remote search by 200ms, and the new query key
    // renders an empty list before any request is issued, so waiting on the local list alone
    // can succeed before the request exists. Wait for the actual remote request first, then
    // for its response to replace the paginated list with exactly the one remote match.
    await waitFor(() => expect(request).toHaveBeenCalledWith('/agent/conversations', { query: { cursor: undefined, search: '数学' } }))
    await waitFor(() => {
      expect(within(history).getAllByRole('button')).toHaveLength(1)
      expect(within(history).queryByRole('button', { name: /查资料要求/ })).not.toBeInTheDocument()
    })
    expect(within(history).getByRole('button', { name: /数学复习/ })).toBeInTheDocument()
    expect(request).toHaveBeenCalledWith('/agent/conversations', { query: { cursor: undefined, search: '数学' } })
    expect(screen.getByLabelText('向校园助手提问')).toHaveValue('')
  })

  it('opens account and assistant settings tabs directly without losing the chat draft', async () => {
    const user = userEvent.setup()
    show()
    await screen.findByText(firstTurn.response)
    await user.type(screen.getByLabelText('向校园助手提问'), '设置期间保留草稿')
    await user.click(screen.getByRole('button', { name: '校园账号' }))
    expect(await screen.findByRole('tab', { name: '校园账号与同步' })).toHaveAttribute('aria-selected', 'true')
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: /Close|关闭/ }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: '助手设置' }))
    expect(await screen.findByRole('tab', { name: '模型与工具' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByLabelText('向校园助手提问')).toHaveValue('设置期间保留草稿')
  })

  it('keeps a conversation draft when starting new, then restores it from sidebar history', async () => {
    const user = userEvent.setup()
    show()
    await screen.findByText(firstTurn.response)
    await user.type(screen.getByLabelText('向校园助手提问'), '未发出的追问')
    await user.click(screen.getByRole('button', { name: '新对话' }))
    await providerReady()
    expect(screen.queryByRole('button', { name: '开始对话' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('向校园助手提问')).toHaveValue('')
    expect(request.mock.calls.filter(([path, options]) => path === '/agent/conversations' && options?.method === 'POST')).toHaveLength(0)
    await user.click(within(screen.getByRole('navigation', { name: '历史对话' })).getByRole('button', { name: /查资料要求/ }))
    expect(await screen.findByText(firstTurn.response)).toBeInTheDocument()
    expect(screen.getByLabelText('向校园助手提问')).toHaveValue('未发出的追问')
    expect(within(screen.getByRole('navigation', { name: '历史对话' })).getByRole('button', { name: /查资料要求/ })).toHaveAttribute('aria-current', 'page')
  })

  it.each(['malformed-success', 'server-error'] as const)('retains the exact send across reload after %s', async kind => {
    const user = userEvent.setup()
    const base = request.getMockImplementation()!
    let reject = true
    request.mockImplementation(async (path, options) => {
      if (path === '/agent/conversations/conv_one/turns' && reject) {
        reject = false
        sends.push({ key: options!.idempotencyKey!, body: options!.body })
        if (kind === 'malformed-success') throw new SyntaxError('Unexpected end of JSON input')
        throw new ApiError(503, { code: 'SERVER_ERROR', message: '后端暂时不可用', retryable: true, request_id: 'fixture' })
      }
      return base(path, options)
    })
    const view = show()
    await screen.findByText(firstTurn.response)
    await user.type(screen.getByLabelText('向校园助手提问'), '不确定结果的请求')
    await user.click(screen.getByRole('button', { name: '发送' }))
    await waitFor(() => expect(readDraft('conv_one').pending?.key).toBe(sends[0].key))
    view.unmount()
    show()
    await screen.findByText(firstTurn.response)
    await user.click(screen.getByRole('button', { name: '发送' }))
    await waitFor(() => expect(sends).toHaveLength(2))
    expect(sends[1]).toEqual(sends[0])
  })

  it('discards a definitively rejected key but preserves editable draft', async () => {
    const user = userEvent.setup()
    const base = request.getMockImplementation()!
    request.mockImplementation(async (path, options) => {
      if (path === '/agent/conversations/conv_one/turns') throw new ApiError(422, {
        code: 'INVALID_INPUT', message: '输入需要修改', retryable: false, request_id: 'fixture',
      })
      return base(path, options)
    })
    show()
    await screen.findByText(firstTurn.response)
    await user.type(screen.getByLabelText('向校园助手提问'), '需要修改的问题')
    await user.click(screen.getByRole('button', { name: '发送' }))
    await screen.findByText('输入需要修改')
    expect(readDraft('conv_one')).toMatchObject({ text: '需要修改的问题' })
    expect(readDraft('conv_one').pending).toBeUndefined()
  })

  it('opens one published-file card from restored events regardless of producing tool', async () => {
    const user = userEvent.setup()
    const base = request.getMockImplementation()!
    const output = { id: 'file_result', conversation_id: 'conv_one', kind: 'output', name: '分析结果.md', size: 42,
      sha256: 'fixture', mime_type: 'text/markdown', created_at: firstTurn.created_at }
    request.mockImplementation(async (path, options) => {
      if (path === '/agent/conversations/conv_one/files') return { items: [output] }
      if (path === '/agent/conversations/conv_one/files/file_result/preview') return { kind: 'text', text: 'actual-output-marker', truncated: false }
      if (path === '/agent/turns/turn_one') return { ...firstTurn, events: { items: options?.query?.after ? [] : [
        { seq: 1, kind: 'file.published', data: { file: output } },
        { seq: 2, kind: 'file.published', data: { file: output } },
        { seq: 3, kind: 'file.published', data: { file: { ...output, id: 'foreign_file', conversation_id: 'other_conversation', name: 'other.md' } } },
      ], next_cursor: 3 } }
      return base(path, options)
    })
    show()
    const card = await screen.findByRole('region', { name: '本轮生成成果' })
    expect(within(card).getAllByRole('button', { name: '打开成果 · 分析结果.md' })).toHaveLength(1)
    expect(within(card).queryByText('other.md')).not.toBeInTheDocument()
    await user.click(within(card).getByRole('button', { name: '打开成果 · 分析结果.md' }))
    expect(await screen.findByText('actual-output-marker')).toBeInTheDocument()
  })
  it('uploads a selected attachment, restores its draft and previews literal output text', async () => {
    const user = userEvent.setup()
    const base = request.getMockImplementation()!
    const item = { id: 'file_one', conversation_id: 'conv_one', kind: 'input', name: '资料.md', size: 20,
      sha256: 'fixture', mime_type: 'text/markdown', created_at: firstTurn.created_at }
    let uploaded = false
    request.mockImplementation(async (path, options) => {
      if (path === '/agent/conversations/conv_one/files' && options?.method === 'POST') { uploaded = true; return item }
      if (path === '/agent/conversations/conv_one/files') return { items: uploaded ? [item] : [] }
      if (path === '/agent/conversations/conv_one/files/file_one/preview') return { kind: 'text', text: '<script>literal-file-marker</script>', truncated: false }
      return base(path, options)
    })
    const view = show()
    await user.upload(screen.getByLabelText('上传对话附件'), new File(['source-marker'], '资料.md', { type: 'text/markdown' }))
    await waitFor(() => expect(within(screen.getByLabelText('待发送附件')).getByText('资料.md')).toBeInTheDocument())
    await user.type(screen.getByLabelText('向校园助手提问'), '整理这份资料')
    view.unmount()
    show()
    expect(await within(await screen.findByLabelText('待发送附件')).findByText('资料.md')).toBeInTheDocument()
    await user.click(await screen.findByRole('button', { name: '发送' }))
    await waitFor(() => expect(sends[0].body).toMatchObject({ text: '整理这份资料', attachment_ids: ['file_one'] }))
    await waitFor(() => expect(screen.queryByLabelText('待发送附件')).not.toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /文件与成果/ }))
    await user.click(await screen.findByRole('button', { name: '预览 资料.md' }))
    expect(await screen.findByText('<script>literal-file-marker</script>')).toBeInTheDocument()
    expect(document.querySelector('script')).toBeNull()
    expect(screen.getByRole('link', { name: '下载 资料.md' })).toHaveAttribute('href', '/api/v1/agent/conversations/conv_one/files/file_one/content')
  })
  it('stops only the selected child and waits for actual completion before displaying stopped', async () => {
    const user = userEvent.setup()
    const base = request.getMockImplementation()!
    firstTurn = { ...firstTurn, status: 'running', response: '' }
    conversation.turns = [firstTurn]
    let stopping = false
    request.mockImplementation(async (path, options) => {
      if (path === '/agent/turns/turn_one/subagents/child_a/interrupt') { stopping = true; return { accepted: true, child_id: 'child_a' } }
      if (path === '/agent/turns/turn_one') return { ...firstTurn, events: { items: options?.query?.after ? (stopping ? [
        { seq: 3, kind: 'subagent.stop_requested', data: { childSessionId: 'child_a' } },
      ] : []) : [
        { seq: 1, kind: 'subagent.started', data: { parentSessionId: 'conv_one', childSessionId: 'child_a' } },
        { seq: 2, kind: 'subagent.started', data: { parentSessionId: 'conv_one', childSessionId: 'child_b' } },
      ], next_cursor: stopping ? 3 : 2 } }
      return base(path, options)
    })
    show()
    await user.click(await screen.findByRole('button', { name: '停止子任务 1' }))
    expect(await screen.findByText('停止中')).toBeInTheDocument()
    expect(screen.queryByText('已停止')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '停止子任务 2' })).toBeEnabled()
    expect(request).toHaveBeenCalledWith('/agent/turns/turn_one/subagents/child_a/interrupt', { method: 'POST', body: {} })
    expect(request).not.toHaveBeenCalledWith('/agent/turns/turn_one/cancel', expect.anything())
  })
  it('shows unknown tools and child results without requiring a new chat renderer', async () => {
    const user = userEvent.setup()
    const base = request.getMockImplementation()!
    request.mockImplementation(async (path, options) => {
      if (path === '/agent/turns/turn_one') return { ...firstTurn, events: { items: options?.query?.after ? [] : [
        { seq: 1, kind: 'harness_event', data: { session_id: 'conv_one', event: { type: 'tool/call', data: { callId: 'm1', name: 'mcp__reports__generate', arguments: '{}' } } } },
        { seq: 2, kind: 'harness_event', data: { session_id: 'conv_one', event: { type: 'tool/result', data: { message: { toolCallId: 'm1', content: [{ type: 'text', text: '<script>literal tool output</script>' }], isError: true } } } } },
        { seq: 3, kind: 'subagent.finished', data: { childSessionId: 'child_one', status: 'ok', lastAssistantMessage: [{ type: 'text', text: '独立计算结果：56' }] } },
      ], next_cursor: 3 } }
      return base(path, options)
    })
    show()
    await user.click(await screen.findByText('工具执行记录'))
    expect(await screen.findByText('mcp__reports__generate')).toBeInTheDocument()
    expect(await screen.findByText('失败')).toBeInTheDocument()
    expect(screen.queryByText('<script>literal tool output</script>')).not.toBeInTheDocument()
    expect(screen.getByRole('status', { name: '工具进度摘要' })).toHaveTextContent('已完成 0 / 1 项')
    await user.click(screen.getByText('查看输入与结果'))
    expect(await screen.findByText('<script>literal tool output</script>')).toBeInTheDocument()
    await user.click(screen.getByText('查看子任务结果'))
    expect(await screen.findByText('独立计算结果：56')).toBeInTheDocument()
    expect(document.querySelector('script')).toBeNull()
  })
  it('sends the preserved new-chat draft on the first action, with no setup gate and no consent step', async () => {
    const user = userEvent.setup()
    const pending = prepareSend({ text: '保留这段草稿' }, { text: '保留这段草稿', clear_course_context: true })
    saveDraft('new', { text: '保留这段草稿', course_id: null, pending })
    show('/chat')
    expect(screen.queryByRole('button', { name: '开始对话' })).not.toBeInTheDocument()
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await providerReady()
    expect(screen.getByLabelText('向校园助手提问')).toHaveValue('保留这段草稿')
    expect(screen.getByRole('button', { name: '发送' })).toBeEnabled()
    await user.click(screen.getByRole('button', { name: '发送' }))
    await waitFor(() => expect(request).toHaveBeenCalledWith('/agent/conversations', expect.objectContaining({ method: 'POST', body: { provider_id: 'provider_a', send_to_provider: true, course_id: null } })))
    await screen.findByRole('button', { name: '浏览器工作区' }, settle)
    await waitFor(() => expect(sends).toHaveLength(1))
    expect(sends[0].key).toBe(pending.key)
    expect(sends[0].body).toMatchObject({ text: '保留这段草稿', clear_course_context: true })
    expect(readDraft('new').text).toBe('')
  })

  it.each(['button', 'enter'] as const)('creates and sends a new chat with one %s action and zero checkboxes', async mode => {
    const user = userEvent.setup()
    conversation.turns = []
    show('/chat')
    await providerReady()
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled()
    await user.type(screen.getByLabelText('向校园助手提问'), '直接问第一个问题')
    expect(screen.getByRole('button', { name: '发送' })).toBeEnabled()
    if (mode === 'button') await user.click(screen.getByRole('button', { name: '发送' }))
    else await user.keyboard('{Enter}')
    await waitFor(() => expect(sends).toHaveLength(1))
    await screen.findByRole('button', { name: '浏览器工作区' }, settle)
    expect(sends[0].body).toMatchObject({ text: '直接问第一个问题', clear_course_context: true })
    expect(request.mock.calls.filter(([path, options]) => path === '/agent/conversations' && options?.method === 'POST')).toHaveLength(1)
    expect(screen.getByLabelText('向校园助手提问')).toHaveValue('')
  })

  it('restores the failed first send under its new ID and retries once with the same key across reload', async () => {
    const user = userEvent.setup()
    conversation.turns = []
    rejectNext = true
    const view = show('/chat')
    await providerReady()
    await user.type(screen.getByLabelText('向校园助手提问'), '首次发送保留草稿')
    const send = screen.getByRole('button', { name: '发送' })
    fireEvent.click(send)
    fireEvent.click(send)
    await screen.findByText('连接暂时中断')
    await screen.findByRole('button', { name: '浏览器工作区' })
    expect(sends).toHaveLength(1)
    expect(screen.getByLabelText('向校园助手提问')).toHaveValue('首次发送保留草稿')
    expect(readDraft('conv_one')).toMatchObject({ text: '首次发送保留草稿', pending: { key: sends[0].key, payload: sends[0].body } })
    expect(readDraft('new').text).toBe('')
    view.unmount()
    show()
    await screen.findByRole('button', { name: '浏览器工作区' }, settle)
    await user.click(screen.getByRole('button', { name: '发送' }))
    await waitFor(() => expect(sends).toHaveLength(2))
    expect(sends[1]).toEqual(sends[0])
    expect(request.mock.calls.filter(([path, options]) => path === '/agent/conversations' && options?.method === 'POST')).toHaveLength(1)
    await waitFor(() => expect(screen.getByLabelText('向校园助手提问')).toHaveValue(''))
  })

  it('keeps a follow-up typed while the first send is pending through the new-ID remount', async () => {
    const user = userEvent.setup()
    const base = request.getMockImplementation()!
    conversation.turns = []
    let finishSend!: () => void
    const waiting = new Promise<void>(resolve => { finishSend = resolve })
    request.mockImplementation(async (path, options) => {
      if (path === '/agent/conversations/conv_one/turns') await waiting
      return base(path, options)
    })
    show('/chat')
    await providerReady()
    await user.type(screen.getByLabelText('向校园助手提问'), '第一问')
    await user.click(screen.getByRole('button', { name: '发送' }))
    await waitFor(() => expect(readDraft('conv_one').pending).toBeDefined())
    fireEvent.change(screen.getByLabelText('向校园助手提问'), { target: { value: '处理中写下的追问' } })
    finishSend()
    await screen.findByRole('button', { name: '浏览器工作区' }, settle)
    expect(screen.getByLabelText('向校园助手提问')).toHaveValue('处理中写下的追问')
    expect(readDraft('conv_one').text).toBe('处理中写下的追问')
    expect(readDraft('conv_one').pending).toBeUndefined()
    expect(readDraft('new').text).toBe('')
    expect(sends).toHaveLength(1)
  })

  it('preserves the new draft when creation itself fails without attempting a send', async () => {
    const user = userEvent.setup()
    const base = request.getMockImplementation()!
    request.mockImplementation(async (path, options) => {
      if (path === '/agent/conversations' && options?.method === 'POST') throw new ApiError(403, {
        code: 'PROVIDER_DISABLED', message: '模型服务已停用', retryable: false, request_id: 'fixture',
      })
      return base(path, options)
    })
    show('/chat')
    await providerReady()
    await user.type(screen.getByLabelText('向校园助手提问'), '创建失败也保留')
    await user.click(screen.getByRole('button', { name: '发送' }))
    await screen.findByText('模型服务已停用')
    expect(readDraft('new').text).toBe('创建失败也保留')
    expect(screen.getByLabelText('向校园助手提问')).toHaveValue('创建失败也保留')
    expect(sends).toHaveLength(0)
  })

  it('excludes disabled models and persists a switched public model ID for the next new chat', async () => {
    const user = userEvent.setup()
    const base = request.getMockImplementation()!
    const provider = { id: 'provider_a', name: '测试模型', model: 'fixture', base_url: 'http://127.0.0.1:19191/v1', protocol: 'openai_chat_completions', has_api_key: false }
    let secondEnabled = true
    localStorage.setItem('campus.agent.provider', 'provider_disabled')
    request.mockImplementation(async (path, options) => {
      if (path === '/ai/providers') return { items: [
        { ...provider, id: 'provider_disabled', name: '已停用模型', enabled: false },
        provider,
        { ...provider, id: 'provider_b', name: '第二模型', enabled: secondEnabled },
      ], next_cursor: null }
      return base(path, options)
    })
    const view = show('/chat')
    await screen.findByRole('combobox', { name: '选择对话模型' })
    expect(screen.getByText('测试模型 · fixture')).toBeInTheDocument()
    await user.click(screen.getByRole('combobox', { name: '选择对话模型' }))
    expect(screen.queryByText('已停用模型 · fixture')).not.toBeInTheDocument()
    await user.click(screen.getByText('第二模型 · fixture'))
    expect(localStorage.getItem('campus.agent.provider')).toBe('provider_b')
    view.unmount()
    const restored = show('/chat')
    await screen.findByRole('combobox', { name: '选择对话模型' })
    expect(screen.getByText('第二模型 · fixture')).toBeInTheDocument()
    await user.type(screen.getByLabelText('向校园助手提问'), '使用偏好的模型')
    await user.click(screen.getByRole('button', { name: '发送' }))
    await waitFor(() => expect(request).toHaveBeenCalledWith('/agent/conversations', expect.objectContaining({
      method: 'POST', body: expect.objectContaining({ provider_id: 'provider_b' }),
    })))
    await screen.findByRole('button', { name: '浏览器工作区' })
    restored.unmount()
    secondEnabled = false
    show('/chat')
    await providerReady()
    expect(screen.getByText('测试模型 · fixture')).toBeInTheDocument()
    expect(localStorage.getItem('campus.agent.provider')).toBe('provider_a')
    expect(screen.queryByText('第二模型 · fixture')).not.toBeInTheDocument()
  })

  it('keeps the draft and sending unavailable when every model is disabled', async () => {
    const base = request.getMockImplementation()!
    request.mockImplementation(async (path, options) => {
      if (path === '/ai/providers') return { items: [{ id: 'provider_a', name: '停用模型', enabled: false }], next_cursor: null }
      return base(path, options)
    })
    saveDraft('new', { text: '没有可用模型也保留' })
    show('/chat')
    expect(screen.queryByRole('button', { name: '开始对话' })).not.toBeInTheDocument()
    await screen.findByRole('button', { name: '配置模型服务' })
    expect(screen.getByText('先在设置中启用模型服务')).toBeInTheDocument()
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled()
    expect(screen.getByLabelText('向校园助手提问')).toHaveValue('没有可用模型也保留')
    expect(sends).toHaveLength(0)
  })

  it('toggles model availability with an enabled-only patch and preserves saved secrets', async () => {
    const user = userEvent.setup()
    const base = request.getMockImplementation()!
    let enabled = true
    const provider = { id: 'provider_a', name: '测试模型', model: 'fixture', base_url: 'http://127.0.0.1:19191/v1',
      protocol: 'openai_chat_completions', has_api_key: true, timeout_seconds: 120, last_tested_at: null, last_test_result: null }
    request.mockImplementation(async (path, options) => {
      if (path === '/ai/providers') return { items: [{ ...provider, enabled }], next_cursor: null }
      if (path === '/ai/providers/provider_a' && options?.method === 'PATCH') {
        expect(Object.keys(options.body)).toEqual(['enabled'])
        enabled = options.body.enabled
        return { ...provider, enabled }
      }
      return base(path, options)
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><ConfigProvider locale={zhCN}><AntApp><AiProviderCard /></AntApp></ConfigProvider></QueryClientProvider>)
    const toggle = await screen.findByRole('switch', { name: '启用 测试模型' })
    expect(toggle).toBeChecked()
    expect(screen.getByText(/正常对话上下文及工具结果由选中的模型服务处理/)).toBeInTheDocument()
    await user.click(toggle)
    await waitFor(() => expect(toggle).not.toBeChecked())
    expect(request).toHaveBeenCalledWith('/ai/providers/provider_a', { method: 'PATCH', body: { enabled: false } })
    expect(screen.getByText('已设置')).toBeInTheDocument()
    await user.click(toggle)
    await waitFor(() => expect(toggle).toBeChecked())
    expect(request).toHaveBeenCalledWith('/ai/providers/provider_a', { method: 'PATCH', body: { enabled: true } })
    expect(request.mock.calls.some(([path]) => path.endsWith('/key'))).toBe(false)
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
    expect(Object.keys(localStorage)).toHaveLength(0)
  })

  it('restores source evidence, warns of a newer version and keeps the draft across source/calendar workspaces', async () => {
    const user = userEvent.setup()
    show()
    expect(await screen.findByText(firstTurn.response)).toBeInTheDocument()
    await user.type(screen.getByLabelText('向校园助手提问'), '未发送的追问')
    await user.click(await screen.findByRole('button', { name: '打开原文 · 实验说明' }))
    expect(await screen.findByText('报告需要原始数据附件。')).toBeInTheDocument()
    expect(await screen.findByText('资料已更新，回答依据的是旧版本')).toBeInTheDocument()
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: /Close|关闭/ }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: '日历' }))
    expect(await screen.findByText('还没有生效的安排，可以在对话中提出安排目标')).toBeInTheDocument()
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: /Close|关闭/ }))
    expect(screen.getByLabelText('向校园助手提问')).toHaveValue('未发送的追问')
    expect(readDraft('conv_one').text).toBe('未发送的追问')
  })

  it('retries an uncertain send with the same key and prevents concurrent double submission', async () => {
    const user = userEvent.setup()
    show()
    await screen.findByText(firstTurn.response)
    await user.type(screen.getByLabelText('向校园助手提问'), '再核对一次')
    rejectNext = true
    await user.click(screen.getByRole('button', { name: /发\s*送/ }))
    await screen.findByText('连接暂时中断')
    expect(screen.getByText('查看错误详情').closest('details')).not.toHaveAttribute('open')
    expect(readDraft('conv_one').pending?.key).toBe(sends[0].key)
    const send = screen.getByRole('button', { name: /发\s*送/ })
    fireEvent.click(send)
    fireEvent.click(send)
    await waitFor(() => expect(sends).toHaveLength(2))
    expect(sends[0].key).toBe(sends[1].key)
    expect(sends[0].body).toEqual(sends[1].body)
    await waitFor(() => expect(screen.getByLabelText('向校园助手提问')).toHaveValue(''))
  })

  it('shows live processing and cancels the actual server turn instead of faking success', async () => {
    const user = userEvent.setup()
    firstTurn = { ...firstTurn, status: 'running', response: '' }
    conversation.turns = [firstTurn]
    show()
    await user.click(await screen.findByRole('button', { name: '停止本轮' }))
    expect(await screen.findByText('本轮已停止，已获得的资料保留。')).toBeInTheDocument()
    expect(request).toHaveBeenCalledWith('/agent/turns/turn_one/cancel', { method: 'POST', body: {} })
  })

  it('preserves unknown/date-only precision and an explicitly cleared context', () => {
    expect(dueText({ due_at: '2026-10-13T12:00:00Z', due_precision: 'unknown' })).toContain('未知')
    expect(dueText({ due_at: '2026-10-13T12:00:00Z', due_precision: 'date' })).not.toContain('20:00')
    saveDraft('conv_one', { text: 'draft', course_id: null })
    expect(readDraft('conv_one').course_id).toBeNull()
    const pending = prepareSend({ text: 'draft' }, { text: 'draft', clear_course_context: true })
    expect(prepareSend({ text: 'draft', pending }, pending.payload).key).toBe(pending.key)
    expect(prepareSend({ text: 'changed', pending }, { text: 'changed' }).key).not.toBe(pending.key)
  })

  it.each(['button', 'text'])('binds %s acceptance to the concrete preview before sending', async mode => {
    const user = userEvent.setup()
    const base = request.getMockImplementation()!
    let status = 'pending'
    const action = () => ({ id: 'action_one', kind: 'apply', status, plan_id: 'plan_one', expires_at: '2099-01-01T00:00:00Z', result: null,
      preview: { plan: { horizon_start: '2026-10-14', horizon_end: '2026-10-18', unassigned: [] }, diff: { added: [], removed: [], retained: [] } } })
    request.mockImplementation(async (path, options) => {
      if (path === '/agent/conversations/conv_one/actions') return { items: [action()] }
      if (path === '/agent/conversations/conv_one/actions/action_one/accept') {
        expect(options.body).toEqual({ operation: 'apply' })
        status = 'approved'
        return action()
      }
      return base(path, options)
    })
    show()
    await screen.findByRole('button', { name: '按这个来' })
    await user.type(screen.getByLabelText('向校园助手提问'), mode === 'button' ? '尚未发送的另一个问题' : '按这个来')
    await user.click(screen.getByRole('button', { name: mode === 'button' ? '按这个来' : '发送' }))
    await waitFor(() => expect(sends).toHaveLength(1))
    expect(sends[0].body).toMatchObject({ text: '按这个来', action_id: 'action_one' })
    await waitFor(() => expect(screen.getByLabelText('向校园助手提问')).toHaveValue(mode === 'button' ? '尚未发送的另一个问题' : ''))
  })

  it('shows real background state and keeps cancellation pending until the worker stops', async () => {
    const user = userEvent.setup()
    const base = request.getMockImplementation()!
    const job = { id: 'job_one', type: 'build_tasks', status: 'running', progress: 0.3,
      errors: [], warnings: [], result_refs: [], cancel_requested: false }
    request.mockImplementation(async (path, options) => {
      if (path === '/jobs/job_one/cancel') { job.cancel_requested = true; return { ...job } }
      if (path === '/jobs/job_one') return { ...job }
      if (path === '/agent/turns/turn_one') return { ...firstTurn, events: { items: options?.query?.after ? [] : [
        { seq: 1, kind: 'tool_result', created_at: firstTurn.created_at, data: { name: 'tasks.build', result: { ...result, job_id: 'job_one', data: { job } } } },
      ], next_cursor: 1 } }
      return base(path, options)
    })
    show()
    await user.click(await screen.findByRole('button', { name: '取消任务' }, settle))
    expect(await screen.findByText('已请求取消，等待当前步骤结束后停止')).toBeInTheDocument()
    expect(request).toHaveBeenCalledWith('/jobs/job_one/cancel', { method: 'POST' })
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument()
  })

  it('shows the persisted model failure cause instead of only the generic runner failure', async () => {
    const base = request.getMockImplementation()!
    firstTurn = { ...firstTurn, status: 'failed', response: '', error: { code: 'RUNTIME_FAILED', message: '运行时未完成', retryable: true } }
    conversation.turns = [firstTurn]
    request.mockImplementation(async (path, options) => {
      if (path === '/agent/turns/turn_one') return { ...firstTurn, events: { items: options?.query?.after ? [] : [
        { seq: 1, kind: 'model_error', created_at: firstTurn.created_at, data: { code: 'PROVIDER_CHANGED', message: '模型配置已变化，请新建对话', retryable: false } },
      ], next_cursor: 1 } }
      return base(path, options)
    })
    show()
    expect(await screen.findByText('模型配置已变化，请新建对话')).toBeInTheDocument()
    expect(screen.queryByText('运行时未完成')).not.toBeInTheDocument()
  })
})
