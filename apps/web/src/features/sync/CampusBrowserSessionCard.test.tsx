import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { CampusBrowserSessionCard } from './CampusBrowserSessionCard'
import { agentApi } from '../agent/api'

vi.mock('../agent/api', () => ({ agentApi: { campusStatus: vi.fn(), connectCampusBrowser: vi.fn(), disconnectCampus: vi.fn() } }))
const disconnected = { connected: false, passport_authenticated: false, session_remaining_seconds: 0,
  browser_session_authorized: false, login_mode: 'persistent_browser' as const, session_persistence: 'process' }
const connected = { ...disconnected, connected: true, passport_authenticated: true, browser_session_authorized: true, session_remaining_seconds: 620 }
function show() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <CampusBrowserSessionCard />
  </QueryClientProvider>)
}
beforeEach(() => {
  vi.mocked(agentApi.campusStatus).mockReset().mockResolvedValue(disconnected)
  vi.mocked(agentApi.connectCampusBrowser).mockReset().mockResolvedValue(connected)
  vi.mocked(agentApi.disconnectCampus).mockReset().mockResolvedValue(disconnected)
})

describe('Campus browser session authorization', () => {
  it('waits for explicit consent and shows only verified connected state without credentials', async () => {
    const user = userEvent.setup()
    show()
    await screen.findByText('校园查询未连接')
    expect(agentApi.connectCampusBrowser).not.toHaveBeenCalled()
    expect(document.querySelectorAll('input')).toHaveLength(0)
    await user.click(screen.getByRole('button', { name: '复用助手浏览器校园登录' }))
    expect(await screen.findByText('校园查询已连接')).toBeInTheDocument()
    expect(agentApi.connectCampusBrowser).toHaveBeenCalledOnce()
    // Authorization is persisted as non-secret metadata; the card must say so
    // instead of the old "restart requires re-consent" claim.
    expect(screen.getByText(/授权在服务重启后仍然有效/)).toBeInTheDocument()
    expect(screen.getByText(/不保存 Cookie 或密码/)).toBeInTheDocument()
    expect(screen.getByText(/不移交 WebVPN 会话/)).toBeInTheDocument()
  })

  it('keeps previous session state on failed adoption and does not claim successful login', async () => {
    const user = userEvent.setup()
    vi.mocked(agentApi.campusStatus).mockResolvedValue(connected)
    vi.mocked(agentApi.connectCampusBrowser).mockRejectedValue(new Error('官方身份核验未通过'))
    show()
    await screen.findByText('校园查询已连接')
    await user.click(screen.getByRole('button', { name: '复用助手浏览器校园登录' }))
    expect(await screen.findByText('校园授权操作未完成')).toBeInTheDocument()
    expect(screen.getByText('校园查询已连接')).toBeInTheDocument()
    expect(screen.queryByText('校园身份已核验，已授权本次运行复用登录')).not.toBeInTheDocument()
  })

  it('requires confirmation to revoke and clearly distinguishes authorization from browser logout', async () => {
    const user = userEvent.setup()
    vi.mocked(agentApi.campusStatus).mockResolvedValue(connected)
    show()
    await screen.findByText('校园查询已连接')
    await user.click(screen.getByRole('button', { name: '解除校园查询授权' }))
    expect(agentApi.disconnectCampus).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '解除授权' }))
    await waitFor(() => expect(agentApi.disconnectCampus).toHaveBeenCalledOnce())
    expect(await screen.findByText('校园查询未连接')).toBeInTheDocument()
    expect(screen.getByText('校园查询授权已解除，已缓存资料保留')).toBeInTheDocument()
    expect(screen.getByText(/助手浏览器里的学校登录仍可能有效/)).toBeInTheDocument()
  })
})
