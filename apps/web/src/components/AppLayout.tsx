import { Tag, Typography } from 'antd'
import { Link, Outlet } from 'react-router-dom'
import { API_MODE } from '../lib/api/http'

/** The shell only frames the conversation. Task tools belong to its workspace. */
export function AppLayout() {
  return <div className="app-shell">
    <header className="app-header">
      <Link to="/chat" className="app-brand">
        <span className="app-brand-mark" aria-hidden="true">C</span>
        <span>Campus Agent</span>
      </Link>
      <Typography.Text type="secondary" className="app-tagline">一个对话，完成校园与日常事务</Typography.Text>
      {API_MODE === 'mock' && <Tag color="orange">开发模式 · 模拟数据</Tag>}
    </header>
    <main className="page-content"><Outlet /></main>
  </div>
}
