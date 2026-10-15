import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App as AntApp } from 'antd'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { RuntimeSettingsPanel, parseMcpSettings } from './AssistantSettings'

const request = vi.hoisted(() => vi.fn())
vi.mock('../../lib/api/http', async original => ({ ...await original<typeof import('../../lib/api/http')>(), apiRequest: request }))

beforeEach(() => {
  request.mockReset()
  const settings = { workspace: '', skill_dirs: [], search: { base_url: '', model: '', key_mode: 'shared', has_api_key: false }, mcp_servers: [] }
  request.mockImplementation(async (path, options) => {
    if (path === '/ai/providers') return { items: [{ id: 'p', name: '测试服务' }] }
    if (path === '/agent/settings') {
      if (options?.method === 'PUT') Object.assign(settings, options.body)
      return structuredClone(settings)
    }
    if (path === '/agent/settings/mcp/calculator/test') return { ok: true, tool_count: 1, tool_names: ['multiply'], latency_ms: 4 }
    if (path === '/agent/settings/search/test') return { ok: false, error: { code: 'SEARCH_KEY_MISSING', message: '搜索密钥尚未配置', retryable: false } }
    throw new Error('Unexpected path: ' + path)
  })
})

function show() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <AntApp><RuntimeSettingsPanel /></AntApp></QueryClientProvider>)
}

describe('Personal assistant settings', () => {
  it('imports standard MCP configuration and tests the saved server without a conversation', async () => {
    const user = userEvent.setup()
    show()
    const editor = await screen.findByLabelText('MCP 服务配置')
    await user.clear(editor)
    await user.paste(JSON.stringify({ mcpServers: { calculator: { command: 'python', args: ['calculator.py'] } } }))
    await user.type(screen.getByLabelText('默认任务目录'), 'D:/Tasks')
    await user.click(screen.getByRole('button', { name: '保存助手设置' }))
    await screen.findByText('助手设置已保存，下一个对话轮次使用新设置。')
    expect(request).toHaveBeenCalledWith('/agent/settings', expect.objectContaining({ method: 'PUT', body: expect.objectContaining({
      workspace: 'D:/Tasks', mcp_servers: [expect.objectContaining({ serverName: 'calculator', transport: 'stdio', command: 'python' })],
    }) }))
    await user.click(screen.getByRole('button', { name: '测试连接' }))
    expect(await screen.findByText('multiply')).toBeInTheDocument()
    expect(request).toHaveBeenCalledWith('/agent/settings/mcp/calculator/test', { method: 'POST', body: {} })
  })

  it('shows a search configuration failure explicitly rather than reporting no results', async () => {
    const user = userEvent.setup()
    show()
    await user.click(await screen.findByRole('button', { name: '测试已保存的搜索设置' }))
    expect(await screen.findByText('搜索密钥尚未配置')).toBeInTheDocument()
    await waitFor(() => expect(request).toHaveBeenCalledWith('/agent/settings/search/test', expect.objectContaining({ body: { provider_id: 'p' } })))
  })

  it('preserves stored-credential markers and infers HTTP transport when importing', () => {
    const [server] = parseMcpSettings(JSON.stringify({ mcpServers: { remote: { type: 'http', disabled: true, url: 'https://example.com/mcp', headers: { Authorization: '__SAVED_CREDENTIAL__' } } } }))
    expect(server.transport).toBe('streamable-http')
    expect(server.headers.Authorization).toBe('__SAVED_CREDENTIAL__')
    expect(server.enabled).toBe(false)
  })
})
