import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ConversationSidebar } from './ConversationSidebar'

const item = { id: 'conv_one', title: '实验报告要求', provider_id: 'provider_a', course_ids: [], current_course_id: null,
  send_to_provider: true, created_at: '2026-10-13T00:00:00Z', updated_at: '2026-10-13T00:00:00Z' }
const props = () => ({ items: [item], currentId: item.id, search: '', onSearch: vi.fn(), onSelect: vi.fn(), onNew: vi.fn(),
  onSettings: vi.fn(), onAccounts: vi.fn(), loading: false, error: undefined, onRetry: vi.fn(), hasMore: false,
  loadingMore: false, onLoadMore: vi.fn() })

beforeEach(() => { localStorage.clear(); vi.mocked(window.matchMedia).mockReturnValue({ matches: false,
  addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as MediaQueryList) })

describe('Assistant sidebar navigation', () => {
  it('persists collapse without losing history or hiding account/settings actions', async () => {
    const user = userEvent.setup()
    const options = props()
    const view = render(<ConversationSidebar {...options} />)
    expect(screen.getByRole('button', { name: /实验报告要求/ })).toHaveAttribute('aria-current', 'page')
    await user.click(screen.getByRole('button', { name: '收起侧边栏' }))
    expect(screen.queryByRole('navigation', { name: '历史对话' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '展开侧边栏' })).toHaveAttribute('aria-expanded', 'false')
    await user.click(screen.getByRole('button', { name: '校园账号' }))
    expect(options.onAccounts).toHaveBeenCalledOnce()
    view.unmount()
    render(<ConversationSidebar {...options} />)
    expect(screen.getByRole('button', { name: '展开侧边栏' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '搜索历史对话' }))
    expect(screen.getByRole('textbox', { name: '搜索历史对话' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '助手设置' }))
    expect(options.onSettings).toHaveBeenCalledOnce()
  })

  it('opens a mobile drawer and closes after selecting history even with desktop collapse saved', async () => {
    localStorage.setItem('campus.agent.sidebar.collapsed', 'true')
    vi.mocked(window.matchMedia).mockReturnValue({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as MediaQueryList)
    const user = userEvent.setup()
    const options = props()
    render(<ConversationSidebar {...options} />)
    expect(screen.queryByRole('complementary', { name: '助手侧边栏' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '打开导航' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('textbox', { name: '搜索历史对话' })).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: /实验报告要求/ }))
    expect(options.onSelect).toHaveBeenCalledWith(item.id)
    expect(screen.getByRole('button', { name: '打开导航' })).toHaveAttribute('aria-expanded', 'false')
  })

  it('closes mobile navigation with Escape without selecting or creating anything', async () => {
    vi.mocked(window.matchMedia).mockReturnValue({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as MediaQueryList)
    const user = userEvent.setup()
    const options = props()
    render(<ConversationSidebar {...options} />)
    await user.click(screen.getByRole('button', { name: '打开导航' }))
    fireEvent.keyDown(await screen.findByRole('dialog'), { key: 'Escape', code: 'Escape', keyCode: 27 })
    expect(screen.getByRole('button', { name: '打开导航' })).toHaveAttribute('aria-expanded', 'false')
    expect(options.onSelect).not.toHaveBeenCalled()
    expect(options.onNew).not.toHaveBeenCalled()
  })

  it('keeps loading, empty search and failure distinguishable, with retry', async () => {
    const user = userEvent.setup()
    const options = { ...props(), items: [] }
    const view = render(<ConversationSidebar {...options} loading />)
    expect(screen.getByRole('status')).toHaveTextContent('正在读取历史')
    view.rerender(<ConversationSidebar {...options} search="不存在" />)
    expect(screen.getByText('没有找到匹配的对话，试试其他关键词。')).toBeInTheDocument()
    view.rerender(<ConversationSidebar {...options} error={new Error('offline')} />)
    expect(screen.getByText('历史读取未完成')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重试历史' }))
    expect(options.onRetry).toHaveBeenCalledOnce()
  })
})
