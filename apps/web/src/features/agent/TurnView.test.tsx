import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { TurnView } from './TurnView'
import { agentApi } from './api'
import type { AgentTurn } from './api'

afterEach(() => vi.restoreAllMocks())
it('shows stopping rather than stopped and keeps the stop button disabled', async () => {
  const turn: AgentTurn = { id: 'turn_stop', conversation_id: 'conv_stop', text: '执行脚本', status: 'stopping',
    response: '', error: null, created_at: '', updated_at: '' }
  vi.spyOn(agentApi, 'turn').mockResolvedValue({ ...turn, events: { items: [], next_cursor: 0 } })
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <TurnView turn={turn} onSource={() => {}} onCancel={() => {}} />
  </QueryClientProvider>)
  expect(await screen.findByText('停止中，正在结束本轮工具…')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '停止本轮' })).toBeDisabled()
  expect(screen.queryByText('本轮已停止，已获得的资料保留。')).toBeNull()
})
