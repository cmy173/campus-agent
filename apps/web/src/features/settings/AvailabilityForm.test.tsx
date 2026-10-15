import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App as AntApp } from 'antd'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { getDb, resetMockDb } from '../../lib/api/mock/db'
import { AvailabilityForm } from './AvailabilityForm'

describe('可用时间编辑保护', () => {
  it('服务端数据刷新不会丢弃尚未保存的作息修改', async () => {
    resetMockDb()
    const initial = structuredClone(getDb().availability)
    const queryClient = new QueryClient()
    const wrap = (value: typeof initial) => <QueryClientProvider client={queryClient}><AntApp><AvailabilityForm initial={value} /></AntApp></QueryClientProvider>
    const view = render(wrap(initial))
    const user = userEvent.setup()
    const limit = screen.getByRole('spinbutton', { name: /每日学习上限/ })
    await user.clear(limit)
    await user.type(limit, '150')
    view.rerender(wrap({ ...initial, updated_at: '2026-10-11T01:00:00Z', daily_limit_minutes: 45 }))
    expect(screen.getByRole('spinbutton', { name: /每日学习上限/ })).toHaveValue('150')
    expect(screen.getByText('有未保存修改')).toBeInTheDocument()
  })
  it('日期覆盖的最后一个时段不能通过删除变成整天不可用', () => {
    resetMockDb()
    const initial = { ...structuredClone(getDb().availability), weekly_windows: [], date_exceptions: [{ date: '2026-10-12', windows: [{ start: '19:00', end: '20:00' }], note: null }] }
    render(<QueryClientProvider client={new QueryClient()}><AntApp><AvailabilityForm initial={initial} /></AntApp></QueryClientProvider>)
    expect(screen.getByRole('button', { name: '删除该时段' })).toBeDisabled()
    expect(screen.getByText('整天不可用')).toBeInTheDocument()
  })
})
