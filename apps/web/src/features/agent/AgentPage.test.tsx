import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App as AntApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AgentPage } from './AgentPage'
import { ApiError } from '../../lib/api/http'
import type { AgentConversation, AgentTurn, ToolEnvelope } from './api'
import { dueText } from './Workspace'
import { prepareSend, readDraft, saveDraft } from './draft'

const request = vi.hoisted(() => vi.fn())
vi.mock('../../lib/api/http', async importOriginal => {
  const original = await importOriginal<typeof import('../../lib/api/http')>()
  return { ...original, API_MODE: 'real', apiRequest: request }
})

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
    await user.click(screen.getByText('查看输入与结果'))
    expect(await screen.findByText('<script>literal tool output</script>')).toBeInTheDocument()
    await user.click(screen.getByText('查看子任务结果'))
    expect(await screen.findByText('独立计算结果：56')).toBeInTheDocument()
    expect(document.querySelector('script')).toBeNull()
  })
  it('requires explicit provider consent and carries an unsent draft into the new conversation', async () => {
    const user = userEvent.setup()
    show('/chat')
    const start = await screen.findByRole('button', { name: '开始对话' })
    expect(start).toBeDisabled()
    await user.type(screen.getByLabelText('向校园助手提问'), '保留这段草稿')
    await user.click(screen.getByRole('checkbox'))
    await user.click(start)
    await waitFor(() => expect(request).toHaveBeenCalledWith('/agent/conversations', expect.objectContaining({ method: 'POST', body: { provider_id: 'provider_a', send_to_provider: true, course_id: null } })))
    expect(screen.getByLabelText('向校园助手提问')).toHaveValue('保留这段草稿')
    expect(sends).toHaveLength(0)
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
    await screen.findByText(/网络中断/)
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
    await user.click(await screen.findByRole('button', { name: '取消任务' }))
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
