import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthRecoveryNotice, recoverySummary } from './AuthRecoveryNotice'

const request = vi.hoisted(() => vi.fn())
vi.mock('../../lib/api/http', async original => ({ ...await original<typeof import('../../lib/api/http')>(), apiRequest: request }))

function show(id: string | undefined = 'conv_one', onAccounts = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(<QueryClientProvider client={client}><AuthRecoveryNotice id={id} onAccounts={onAccounts} /></QueryClientProvider>)
  return { view, client, onAccounts }
}

beforeEach(() => { request.mockReset() })

describe('Auth recovery inline notice', () => {
  it('stays silent while nothing is pending so normal chat is never interrupted', async () => {
    request.mockResolvedValue({ status: 'idle', items: [] })
    show()
    await waitFor(() => expect(request).toHaveBeenCalledWith('/agent/conversations/conv_one/recovery', expect.anything()))
    expect(screen.queryByText(/等待校园登录/)).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('explains that the task is preserved and offers the login entry without re-asking', async () => {
    const user = userEvent.setup()
    request.mockResolvedValue({ status: 'waiting_auth', items: [{
      id: 'ck_1', conversation_id: 'conv_one', source_turn_id: 'turn_one', status: 'waiting_auth',
      continuation_turn_id: null, pending_tool: 'campus.query', error: null,
      created_at: '2026-10-15T00:00:00Z', updated_at: '2026-10-15T00:00:00Z' }] })
    const { onAccounts } = show()
    await screen.findByText(/任务在等待校园登录，已完成的资料都保留着/)
    expect(screen.getByText(/校园信息查询/)).toBeInTheDocument()
    expect(screen.getByText(/不需要重新提问/)).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '打开校园账号' }))
    expect(onAccounts).toHaveBeenCalled()
  })

  it('refreshes the conversation once the continuation completes, without resending', async () => {
    let status = 'waiting_auth'
    request.mockImplementation(async () => ({ status, items: [{
      id: 'ck_1', conversation_id: 'conv_one', source_turn_id: 'turn_one', status,
      continuation_turn_id: status === 'idle' ? 'turn_resumed' : null, pending_tool: 'campus.query', error: null,
      created_at: '2026-10-15T00:00:00Z', updated_at: '2026-10-15T00:00:00Z' }] }))
    const { client } = show()
    await screen.findByText(/任务在等待校园登录/)
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    status = 'idle'
    await client.refetchQueries({ queryKey: ['agent', 'recovery', 'conv_one'] })
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['agent', 'conversation', 'conv_one'] }))
    expect(request.mock.calls.every(([path]) => path === '/agent/conversations/conv_one/recovery')).toBe(true)
  })

  it('renders nothing for a brand new chat with no conversation id', () => {
    show('')
    expect(request).not.toHaveBeenCalled()
    expect(recoverySummary({ pending_tool: 'campus.academics' })).toBe('课表与安排')
  })
})
