import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { BrowserWorkspace } from './BrowserWorkspace'
import { agentApi } from './api'
import type { BrowserAction } from './api'

vi.mock('../../lib/api/http', async importOriginal => ({ ...(await importOriginal<typeof import('../../lib/api/http')>()), API_MODE: 'real' }))

afterEach(() => vi.restoreAllMocks())
it('shows concrete form values and confirms once, then preserves the receipt', async () => {
  const action: BrowserAction = { id: 'action_one', status: 'pending', created_at: '',
    preview: { page_url: 'https://example.com/form', action_url: 'https://example.com/submit', button: '提交材料', fields: [{ name: '姓名', value: '学生-819' }] } }
  let executed = false
  vi.spyOn(agentApi, 'browser').mockImplementation(async () => ({ running: true, headed: false, pages: [],
    reviews: [{ ...action, status: executed ? 'executed' : 'pending' }] }))
  const confirm = vi.spyOn(agentApi, 'browserAction').mockImplementation(async () => {
    executed = true
    return { action: { ...action, status: 'executed' }, message: '已执行提交操作，请核对页面回执。' }
  })
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <BrowserWorkspace id="conv_one" open active={false} onOpen={() => {}} onClose={() => {}} />
  </QueryClientProvider>)
  expect(await screen.findByText('姓名：学生-819')).toBeInTheDocument()
  expect(screen.getByText('对象：https://example.com/submit')).toBeInTheDocument()
  expect(screen.getByText(/停止浏览器只关闭窗口，不会清除持久登录/)).toBeInTheDocument()
  await userEvent.setup().click(screen.getByRole('button', { name: '确认并执行' }))
  await waitFor(() => expect(screen.queryByRole('button', { name: '确认并执行' })).toBeNull())
  expect(confirm).toHaveBeenCalledTimes(1)
  expect(confirm).toHaveBeenCalledWith('conv_one', 'action_one', 'confirm')
  expect(screen.getByText('已执行提交操作，请核对页面回执。')).toBeInTheDocument()
})
