import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { TurnView } from './TurnView'
import { agentApi } from './api'
import type { AgentTurn } from './api'

const view = (turn: AgentTurn) => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
  <TurnView turn={turn} onSource={() => {}} onCancel={() => {}} />
</QueryClientProvider>)

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

it('pauses the exact native goal revision without treating it as completed', async () => {
  const turn: AgentTurn = { id: 'turn_goal', conversation_id: 'conv_goal', text: '持续整理资料', status: 'running',
    response: '', error: null, created_at: '', updated_at: '' }
  const goal = { id: 'goal_3', revision: 3, objective: '整理所有课程资料', phase: 'active' as const,
    activation: 'armed' as const, roundsStarted: 1, maxGoalRounds: 4 }
  vi.spyOn(agentApi, 'turn').mockResolvedValue({ ...turn, events: {
    items: [{ seq: 1, kind: 'goal.state', data: { goal }, created_at: '' }], next_cursor: 1,
  } })
  const pause = vi.spyOn(agentApi, 'pauseGoal').mockResolvedValue({ goal: { ...goal, phase: 'paused', revision: 4 }, agent_status: 'running' })
  view(turn)
  fireEvent.click(await screen.findByRole('button', { name: '暂停持续任务' }))
  await waitFor(() => expect(pause).toHaveBeenCalledWith('turn_goal', { id: 'goal_3', revision: 3 }))
  expect(screen.queryByText('已完成')).toBeNull()
})

it('shows a saved active goal at the round limit as unfinished after the turn ends', async () => {
  const turn: AgentTurn = { id: 'turn_cap', conversation_id: 'conv_goal', text: '持续整理资料', status: 'succeeded',
    response: '本轮进度已保存', error: null, created_at: '', updated_at: '' }
  vi.spyOn(agentApi, 'turn').mockResolvedValue({ ...turn, events: {
    items: [{ seq: 1, kind: 'goal.state', data: { goal: { id: 'g', revision: 1, objective: '整理资料',
      phase: 'blocked', blockedReason: { code: 'round-limit', message: 'Goal reached its configured limit of 2 rounds.' },
      activation: 'disarmed', roundsStarted: 2, maxGoalRounds: 2 } }, created_at: '' }], next_cursor: 1,
  } })
  view(turn)
  expect(await screen.findByText('已达到续行上限')).toBeInTheDocument()
  expect(screen.getByText(/目标尚未完成/)).toBeInTheDocument()
  expect(screen.queryByText('已完成')).toBeNull()
  expect(screen.queryByRole('button', { name: '暂停持续任务' })).toBeNull()
  const resume = vi.spyOn(agentApi, 'resumeGoal').mockResolvedValue({ ...turn, id: 'new_goal_turn', status: 'queued' })
  fireEvent.click(screen.getByRole('button', { name: '继续持续任务（新增64轮）' }))
  await waitFor(() => expect(resume).toHaveBeenCalledWith('conv_goal', { id: 'g', revision: 1, maxGoalRounds: 66 }, expect.any(String)))
})
