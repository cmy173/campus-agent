import { useEffect, useState } from 'react'
import { Alert, Button, Drawer, Input, Spin, Typography } from 'antd'
import { CommentOutlined, MenuFoldOutlined, MenuOutlined, MenuUnfoldOutlined, PlusOutlined, SearchOutlined, SettingOutlined, UserOutlined } from '@ant-design/icons'
import type { AgentConversation } from './api'

const collapsedKey = 'campus.agent.sidebar.collapsed'
const mobileQuery = '(max-width: 767px)'

function conversationTitle(conversation: AgentConversation) {
  return conversation.title?.trim() || conversation.turns?.[0]?.text.trim().slice(0, 64) || `对话 ${conversation.id.slice(-6)}`
}

function conversationDate(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

/** Navigation owns no conversation data or model authorization. */
export function ConversationSidebar({ items, currentId, search, onSearch, onSelect, onNew, onSettings, onAccounts,
  loading, error, onRetry, hasMore, loadingMore, onLoadMore }: {
  items: AgentConversation[]; currentId?: string; search: string; onSearch: (value: string) => void;
  onSelect: (id: string) => void; onNew: () => void; onSettings: () => void; onAccounts: () => void;
  loading: boolean; error?: unknown; onRetry: () => void; hasMore: boolean; loadingMore: boolean; onLoadMore: () => void;
}) {
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem(collapsedKey) === 'true' } catch { return false }
  })
  const [mobile, setMobile] = useState(() => window.matchMedia(mobileQuery).matches)
  const [open, setOpen] = useState(false)
  useEffect(() => {
    const media = window.matchMedia(mobileQuery)
    const update = () => { setMobile(media.matches); setOpen(false) }
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [])
  const act = (action: () => void) => { setOpen(false); action() }
  const toggle = () => {
    const next = !collapsed
    setCollapsed(next)
    try { localStorage.setItem(collapsedKey, String(next)) } catch { /* Optional preference. */ }
  }
  const compact = !mobile && collapsed
  const contents = <>
    <div className="agent-sidebar-top">
      {!compact && <Typography.Text strong>助手工作台</Typography.Text>}
      {!mobile && <Button type="text" aria-label={compact ? '展开侧边栏' : '收起侧边栏'} aria-expanded={!compact}
        icon={compact ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />} onClick={toggle} />}
    </div>
    <Button className="agent-sidebar-new" type="primary" icon={<PlusOutlined />} aria-label="新对话" onClick={() => act(onNew)}>
      {!compact && '新对话'}
    </Button>
    {compact ? <Button type="text" icon={<SearchOutlined />} aria-label="搜索历史对话" onClick={toggle} /> : <>
      <Input className="agent-history-search" aria-label="搜索历史对话" placeholder="搜索对话" prefix={<SearchOutlined />} allowClear
        value={search} onChange={event => onSearch(event.target.value)} />
      <Typography.Text type="secondary" className="agent-history-search-hint">按问题与回答搜索</Typography.Text>
      <div className="agent-history-heading"><Typography.Text type="secondary">历史对话</Typography.Text>
        <Typography.Text type="secondary">{items.length}{hasMore ? '+' : ''}</Typography.Text></div>
      <div className="agent-history-list" aria-busy={loading}>
        {loading && <div role="status"><Spin size="small" /> 正在读取历史…</div>}
        {Boolean(error) && <Alert type="warning" title="历史读取未完成" description="当前对话和草稿仍可保留。"
          action={<Button size="small" onClick={onRetry}>重试历史</Button>} />}
        {!loading && !error && !items.length && <Typography.Paragraph type="secondary">
          {search ? '没有找到匹配的对话，试试其他关键词。' : '还没有历史对话。从一个新对话开始。'}
        </Typography.Paragraph>}
        <nav aria-label="历史对话">
          {items.map(item => <button type="button" key={item.id} className="agent-history-item"
            aria-current={item.id === currentId ? 'page' : undefined} title={conversationTitle(item)} onClick={() => act(() => onSelect(item.id))}>
            <CommentOutlined aria-hidden="true" /><span><strong>{conversationTitle(item)}</strong><small>{conversationDate(item.updated_at)}</small></span>
          </button>)}
        </nav>
        {hasMore && <Button block type="text" loading={loadingMore} onClick={onLoadMore}>加载更多历史</Button>}
      </div>
    </>}
    <div className="agent-sidebar-footer">
      <Button type="text" icon={<UserOutlined />} aria-label="校园账号" onClick={() => act(onAccounts)}>{!compact && '校园账号'}</Button>
      <Button type="text" icon={<SettingOutlined />} aria-label="助手设置" onClick={() => act(onSettings)}>{!compact && '设置与偏好'}</Button>
      {!compact && <Typography.Text type="secondary">模型与工具授权在设置中管理</Typography.Text>}
    </div>
  </>
  if (mobile) return <>
    <Button className="agent-mobile-menu" aria-label="打开导航" aria-expanded={open} icon={<MenuOutlined />} onClick={() => setOpen(true)} />
    <Drawer className="agent-mobile-drawer" title="助手导航" placement="left" size={300} open={open} onClose={() => setOpen(false)}>
      <aside className="agent-sidebar agent-sidebar-drawer" aria-label="助手侧边栏">{contents}</aside>
    </Drawer>
  </>
  return <aside className={`agent-sidebar${compact ? ' is-collapsed' : ''}`} aria-label="助手侧边栏">{contents}</aside>
}
