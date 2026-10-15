import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AgentDataSettings, clearAgentHistoryStorage } from './AgentDataSettings'
import { ApiError } from '../../lib/api/http'

const request = vi.hoisted(() => vi.fn())
vi.mock('../../lib/api/http', async original => ({ ...await original<typeof import('../../lib/api/http')>(), apiRequest: request }))

beforeEach(() => {
  request.mockReset()
  localStorage.clear(); sessionStorage.clear()
  const policy = { history_retention_days: 30, history_count: 2, pending_cleanup_count: 0,
    description: '默认保留30天，0表示长期保留；读取数据管理历史或手动清理执行。', erasure_limits: '不保证备份、外部服务日志或文件系统物理擦除。' }
  request.mockImplementation(async (path, options) => {
    if (path === '/agent/data/settings') {
      if (options?.method === 'PUT') policy.history_retention_days = options.body.history_retention_days
      return { ...policy }
    }
    if (path === '/agent/data/history' && options?.method === 'DELETE') return { deleted_conversation_ids: ['conv_one', 'conv_two'], deleted_count: 2, cleanup_complete: true }
    if (path === '/agent/data/history') return { items: [{ id: 'conv_one', title: '合成聊天', updated_at: '2026-10-15T00:00:00Z' }], next_cursor: null,
      purge: { deleted_conversation_ids: [], deleted_count: 0, cleanup_complete: true } }
    if (path === '/agent/data/history/conv_one') return { deleted_conversation_ids: ['conv_one'], deleted_count: 1, cleanup_complete: true }
    if (path === '/agent/data/history/purge-expired') return { deleted_conversation_ids: [], deleted_count: 0, cleanup_complete: true }
    if (path === '/agent/data/browser-session/clear') return { cleared: true }
    throw new Error('Unexpected path: ' + path)
  })
})

function show() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <AgentDataSettings /></QueryClientProvider>)
}

function seed() {
  for (const storage of [localStorage, sessionStorage]) {
    storage.setItem('campus.agent.draft.conv_one', '{"text":"合成草稿"}')
    storage.setItem('campus.agent.scroll.conv_one', '100')
    storage.setItem('campus.agent.scroll.conv_one.follow', 'false')
    storage.setItem('campus.agent.draft.conv_two', 'keep-other')
    storage.setItem('campus.agent.draft.new', 'new-unsent')
    storage.setItem('campus.agent.provider', 'provider-keep')
    storage.setItem('campus.session', 'campus-keep')
    storage.setItem('api.keys', 'keys-keep')
  }
}

describe('Chat-only data settings', () => {
  it('persists 0 as long-term retention without deleting data or runtime keys', async () => {
    const user = userEvent.setup()
    show()
    const input = await screen.findByRole('spinbutton', { name: '聊天历史保留天数' })
    await user.clear(input); await user.type(input, '0')
    await user.click(screen.getByRole('button', { name: '保存保留期限' }))
    await screen.findByText(/聊天历史保留期限已保存/)
    expect(request).toHaveBeenCalledWith('/agent/data/settings', { method: 'PUT', body: { history_retention_days: 0 } })
    expect(request.mock.calls.some(([, options]) => options?.method === 'DELETE')).toBe(false)
    expect(screen.getByRole('button', { name: '清理过期聊天' })).toBeDisabled()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByText(/本页不提供“全部本地数据”/)).toBeInTheDocument()
  })

  it('requires exactly one central irreversible confirmation, then clears only chat caches', async () => {
    const user = userEvent.setup()
    seed(); show()
    await user.click(await screen.findByRole('button', { name: '删除当前所有聊天历史' }))
    expect(request.mock.calls.some(([, options]) => options?.method === 'DELETE')).toBe(false)
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText(/此操作不可恢复/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: '确认不可逆删除' }))
    await screen.findByText(/已删除 2 个聊天/)
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(request.mock.calls.filter(([path, options]) => path === '/agent/data/history' && options?.method === 'DELETE')).toHaveLength(1)
    for (const storage of [localStorage, sessionStorage]) {
      expect(storage.getItem('campus.agent.draft.conv_one')).toBeNull()
      expect(storage.getItem('campus.agent.scroll.conv_one.follow')).toBeNull()
      expect(storage.getItem('campus.agent.draft.new')).toBeNull()
      expect(storage.getItem('campus.agent.provider')).toBe('provider-keep')
      expect(storage.getItem('campus.session')).toBe('campus-keep')
      expect(storage.getItem('api.keys')).toBe('keys-keep')
    }
  })

  it('single deletion clears selected conversation only and cancelling sends no request', async () => {
    const user = userEvent.setup()
    seed(); show()
    await user.click(await screen.findByRole('button', { name: '读取历史（并按期限清理）' }))
    await user.click(await screen.findByRole('button', { name: '删除聊天 合成聊天' }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: /取\s*消/ }))
    expect(request).not.toHaveBeenCalledWith('/agent/data/history/conv_one', expect.anything())
    await user.click(screen.getByRole('button', { name: '删除聊天 合成聊天' }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: '确认不可逆删除' }))
    await screen.findByText(/已删除 1 个聊天/)
    expect(request).toHaveBeenCalledWith('/agent/data/history/conv_one', { method: 'DELETE' })
    expect(sessionStorage.getItem('campus.agent.draft.conv_one')).toBeNull()
    expect(sessionStorage.getItem('campus.agent.draft.conv_two')).toBe('keep-other')
    expect(sessionStorage.getItem('campus.agent.draft.new')).toBe('new-unsent')
  })

  it('active refusal reports failure without success or clearing drafts', async () => {
    const user = userEvent.setup()
    seed()
    request.mockImplementation(async (_path, options) => {
      if (options?.method === 'DELETE') throw new Error('聊天仍在停止收尾中，请先停止并等待完成')
      return { history_retention_days: 30, history_count: 1, pending_cleanup_count: 0, description: 'synthetic', erasure_limits: 'synthetic' }
    })
    show()
    await user.click(await screen.findByRole('button', { name: '删除当前所有聊天历史' }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: '确认不可逆删除' }))
    await screen.findByText(/聊天仍在停止收尾中/)
    expect(screen.queryByText(/已删除 \d+ 个聊天/)).not.toBeInTheDocument()
    expect(sessionStorage.getItem('campus.agent.draft.conv_one')).not.toBeNull()
  })

  it('partial committed deletion clears known drafts but reports incomplete cleanup without success', async () => {
    const user = userEvent.setup()
    seed()
    const original = request.getMockImplementation()!
    request.mockImplementation(async (path, options) => {
      if (options?.method === 'DELETE') throw new ApiError(503, { code: 'HISTORY_CLEANUP_INCOMPLETE', message: '历史文件副本尚未完全清理，请重试',
        retryable: true, request_id: 'synthetic', details: { deleted_conversation_ids: ['conv_one'], cleanup_complete: false } })
      return original(path, options)
    })
    show()
    await user.click(await screen.findByRole('button', { name: '删除当前所有聊天历史' }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: '确认不可逆删除' }))
    await screen.findByText(/历史文件副本尚未完全清理/)
    await waitFor(() => expect(sessionStorage.getItem('campus.agent.draft.conv_one')).toBeNull())
    expect(sessionStorage.getItem('campus.agent.draft.conv_two')).toBe('keep-other')
    expect(localStorage.getItem('api.keys')).toBe('keys-keep')
    expect(screen.queryByText(/已删除 \d+ 个聊天/)).not.toBeInTheDocument()
  })

  it('campus exit uses the existing Lead route without deleting chat or keys', async () => {
    const user = userEvent.setup()
    seed(); show()
    await user.click(await screen.findByRole('button', { name: '退出校园浏览器会话' }))
    await screen.findByText('校园浏览器会话已清除；聊天历史和 API 密钥未删除。')
    expect(request).toHaveBeenCalledWith('/agent/data/browser-session/clear', { method: 'POST', body: {} })
    expect(request.mock.calls.some(([, options]) => options?.method === 'DELETE')).toBe(false)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(sessionStorage.getItem('campus.agent.draft.conv_one')).not.toBeNull()
    expect(localStorage.getItem('api.keys')).toBe('keys-keep')
  })

  it('clears lazy-expired conversation caches but preserves remaining drafts', async () => {
    const user = userEvent.setup()
    seed()
    const original = request.getMockImplementation()!
    request.mockImplementation(async (path, options) => path === '/agent/data/history' ? { items: [], next_cursor: null,
      purge: { deleted_conversation_ids: ['conv_one'], deleted_count: 1, cleanup_complete: true } } : original(path, options))
    show()
    await user.click(await screen.findByRole('button', { name: '读取历史（并按期限清理）' }))
    await screen.findByText('已按保留期限清理 1 个过期聊天。')
    expect(sessionStorage.getItem('campus.agent.draft.conv_one')).toBeNull()
    expect(sessionStorage.getItem('campus.agent.draft.conv_two')).toBe('keep-other')
  })

  it('does not confuse similarly prefixed conversation ids and tolerates unavailable storage', async () => {
    seed()
    sessionStorage.setItem('campus.agent.draft.conv_one_extra', 'unrelated')
    expect(clearAgentHistoryStorage(['conv_one'])).toBe(true)
    expect(sessionStorage.getItem('campus.agent.draft.conv_one_extra')).toBe('unrelated')
    const spy = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new Error('storage blocked') })
    expect(clearAgentHistoryStorage()).toBe(false)
    spy.mockRestore()
    await waitFor(() => expect(sessionStorage.getItem('campus.agent.draft.conv_two')).toBe('keep-other'))
  })
})
