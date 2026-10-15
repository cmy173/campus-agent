import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { CampusCredentialCard } from './CampusCredentialCard'
import type { CampusCredentialStatus } from '../agent/api'

const request = vi.hoisted(() => vi.fn())
vi.mock('../../lib/api/http', async original => ({ ...await original<typeof import('../../lib/api/http')>(), apiRequest: request }))

const empty: CampusCredentialStatus = { items: [{ scope: 'passport', configured: false, username_hint: null, auto_relogin: false, saved_at: null },
  { scope: 'webvpn', configured: false, username_hint: null, auto_relogin: false, saved_at: null }],
  dpapi: true, residual_risk: '验证码或二次认证时需人工登录一次。', last_login: null, cooldown_active: false,
  webvpn_channel: { configured: false, authenticated: false, cooldown_active: false, cooldown_seconds: 300, attempts: 0,
    last_error: null, authenticated_at: null, mapped_hosts: ['zdbk.zju.edu.cn', 'zjuam.zju.edu.cn'],
    unmapped_hosts: ['eta.zju.edu.cn'], mode: 'auto', channel_active: { 'zjuam.zju.edu.cn': false, 'zdbk.zju.edu.cn': false } } }
const saved: CampusCredentialStatus = { ...empty,
  items: [{ scope: 'passport', configured: true, username_hint: '32***00', auto_relogin: true, saved_at: '2026-10-15T00:00:00Z' },
    { scope: 'webvpn', configured: true, username_hint: 'wv***00', auto_relogin: true, saved_at: '2026-10-15T00:00:00Z' }],
  webvpn_channel: { ...empty.webvpn_channel!, configured: true, authenticated: true,
    channel_active: { 'zjuam.zju.edu.cn': true, 'zdbk.zju.edu.cn': true } } }

function show() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <CampusCredentialCard /></QueryClientProvider>)
}

beforeEach(() => { request.mockReset() })

describe('Campus credential vault card', () => {
  it('never echoes a saved password and keeps both accounts as separate slots', async () => {
    request.mockResolvedValue(saved)
    show()
    expect(await screen.findByLabelText('校园账号')).toBeInTheDocument()
    const webvpn = screen.getByLabelText('WebVPN 账号')
    expect(webvpn).toBeInTheDocument()
    expect(screen.getByText(/已保存 32\*\*\*00/)).toBeInTheDocument()
    expect(screen.getByText(/已保存 wv\*\*\*00/)).toBeInTheDocument()
    expect(screen.getByLabelText('校园密码')).toHaveAttribute('placeholder', '密码（保存后不回显）')
    expect(screen.getByLabelText('WebVPN 密码')).toHaveAttribute('placeholder', '密码（保存后不回显）')
    expect(document.body.textContent).not.toMatch(/synthetic-password/)
  })

  it('saves passport credentials, clears the fields and shows the masked hint', async () => {
    const user = userEvent.setup()
    let state = empty
    request.mockImplementation(async (path, options) => {
      if (path === '/agent/campus/credentials' && options?.method === 'PUT') { state = saved; return saved.items[0] }
      if (path === '/agent/campus/credentials') return state
      throw new Error('unexpected ' + path)
    })
    show()
    await user.type(await screen.findByLabelText('校园账号'), '3230100000')
    await user.type(screen.getByLabelText('校园密码'), 'synthetic-password')
    await user.click(screen.getByRole('button', { name: '加密保存并启用' }))
    await screen.findByText(/凭据已加密保存/)
    expect(request).toHaveBeenCalledWith('/agent/campus/credentials', expect.objectContaining({
      method: 'PUT', body: expect.objectContaining({ scope: 'passport', auto_relogin: true }) }))
    expect(screen.getByLabelText('校园密码')).toHaveValue('')
    expect(await screen.findByText(/已保存 32\*\*\*00/)).toBeInTheDocument()
  })

  it('stores the WebVPN account in its own slot with its own verification call', async () => {
    const user = userEvent.setup()
    let state = empty
    const sent: unknown[] = []
    request.mockImplementation(async (path, options) => {
      if (path === '/agent/campus/credentials' && options?.method === 'PUT') {
        sent.push(options.body); state = saved; return saved.items[1]
      }
      if (path === '/agent/campus/credentials/test-login') { sent.push(options.body); return { ok: true, code: 'WEBVPN_LOGIN_OK', needs_user: false } }
      if (path === '/agent/campus/credentials') return state
      throw new Error('unexpected ' + path)
    })
    show()
    await user.type(await screen.findByLabelText('WebVPN 账号'), 'wv-3230100000')
    await user.type(screen.getByLabelText('WebVPN 密码'), 'synthetic-webvpn-password')
    await user.click(screen.getByRole('button', { name: '保存 WebVPN 凭据' }))
    await waitFor(() => expect(sent[0]).toMatchObject({ scope: 'webvpn', auto_relogin: true }))
    expect(screen.getByLabelText('WebVPN 密码')).toHaveValue('')
    // Verification is requested for the WebVPN scope, never for the passport scope.
    const verify = await screen.findAllByRole('button', { name: '立即验证' })
    await user.click(verify[1])
    await screen.findByText(/WebVPN 自动认证成功/)
    await waitFor(() => expect(sent[1]).toMatchObject({ scope: 'webvpn' }))
  })

  it('reports a captcha challenge as a one-time manual step instead of failing silently', async () => {
    const user = userEvent.setup()
    request.mockImplementation(async (path) => {
      if (path === '/agent/campus/credentials/test-login') return { ok: false, code: 'CAPTCHA_REQUIRED', needs_user: true }
      if (path === '/agent/campus/credentials') return saved
      throw new Error('unexpected ' + path)
    })
    show()
    const verify = await screen.findAllByRole('button', { name: '立即验证' })
    await user.click(verify[0])
    await screen.findByText(/校方要求验证码，需要你手动登录一次；任务进度已保留。/)
  })

  it('clears stored credentials after one central confirmation', async () => {
    const user = userEvent.setup()
    let state = saved
    request.mockImplementation(async (path, options) => {
      if (path === '/agent/campus/credentials' && options?.method === 'DELETE') { state = empty; return empty.items[0] }
      if (path === '/agent/campus/credentials') return state
      throw new Error('unexpected ' + path)
    })
    show()
    // antd separates two CJK characters with a space in the accessible name.
    const clears = await screen.findAllByRole('button', { name: /^清\s*除$/ })
    await user.click(clears[0])
    // The confirmation dialog repeats the label; click the popconfirm action.
    const actions = await screen.findAllByRole('button', { name: '清除凭据' })
    await user.click(actions[actions.length - 1])
    await waitFor(() => expect(request).toHaveBeenCalledWith('/agent/campus/credentials', expect.objectContaining({ method: 'DELETE' })))
    await screen.findByText(/凭据已从本机清除/)
  })

  it('states the off-campus channel status instead of implying it always works', async () => {
    request.mockResolvedValue(empty)
    show()
    const detail = await screen.findByText(/存储与安全说明/)
    expect(detail).toBeInTheDocument()
    // The collapsed panel must not claim a live channel when nothing is stored.
    expect(screen.queryByText(/通道已认证/)).toBeNull()
  })

  it('reports a failed credentials read instead of unmounting the page', async () => {
    request.mockRejectedValue(new Error('synthetic-credentials-api-down'))
    show()
    expect(await screen.findByText('凭据状态读取失败')).toBeInTheDocument()
    // No credential form is rendered without a status payload to describe it.
    expect(screen.queryByLabelText('校园账号')).toBeNull()
    expect(screen.queryByRole('button', { name: '加密保存并启用' })).toBeNull()
  })
})
