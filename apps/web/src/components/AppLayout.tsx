import {
  BookOutlined,
  CalendarOutlined,
  ClockCircleOutlined,
  FileTextOutlined,
  MenuOutlined,
  MessageOutlined,
  RocketOutlined,
  SettingOutlined,
  SyncOutlined,
} from '@ant-design/icons'
import { Button, Grid, Layout, Menu, Tag, Typography } from 'antd'
import { useState } from 'react'
import { Link, Outlet, useLocation } from 'react-router-dom'
import { API_MODE } from '../lib/api/http'

const menuItems = [
  { key: '/chat', icon: <MessageOutlined />, label: <Link to="/chat">校园助手</Link>, title: '校园助手' },
  { key: '/today', icon: <ClockCircleOutlined />, label: <Link to="/today">今日</Link>, title: '今日' },
  { key: '/courses', icon: <BookOutlined />, label: <Link to="/courses">课程与资源</Link>, title: '课程与资源' },
  { key: '/plan', icon: <CalendarOutlined />, label: <Link to="/plan">学习计划</Link>, title: '学习计划' },
  { key: '/reviews', icon: <FileTextOutlined />, label: <Link to="/reviews">复习卷</Link>, title: '复习卷' },
  { key: '/sync', icon: <SyncOutlined />, label: <Link to="/sync">同步中心</Link>, title: '同步中心' },
  { key: '/settings', icon: <SettingOutlined />, label: <Link to="/settings">设置</Link>, title: '设置' },
  { key: '/onboarding', icon: <RocketOutlined />, label: <Link to="/onboarding">使用引导</Link>, title: '首次使用引导' },
]

export function AppLayout() {
  const location = useLocation()
  const screens = Grid.useBreakpoint()
  const isNarrow = !screens.lg
  const [collapsed, setCollapsed] = useState(false)

  const active = menuItems
    .filter((item) => location.pathname === item.key || location.pathname.startsWith(`${item.key}/`))
    .sort((a, b) => b.key.length - a.key.length)[0]
  const selectedKey = active?.key ?? '/chat'

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Layout.Sider
        theme="light"
        width={200}
        breakpoint="lg"
        collapsedWidth={0}
        trigger={null}
        collapsed={collapsed}
        onBreakpoint={(broken) => setCollapsed(broken)}
        className="app-sider"
        style={{ borderRight: '1px solid #f0f0f0' }}
      >
        <div style={{ padding: '18px 16px 8px', fontWeight: 600, fontSize: 16 }}>Campus Agent</div>
        <Menu
          mode="inline"
          selectedKeys={[selectedKey]}
          items={[
            { key: '/chat', icon: <MessageOutlined />, label: <Link to="/chat">校园助手</Link> },
            { key: 'workbench', icon: <BookOutlined />, label: '辅助工作台',
              children: menuItems.filter(item => item.key !== '/chat').map(({ key, icon, label }) => ({ key, icon, label })) },
          ]}
          defaultOpenKeys={location.pathname.startsWith('/chat') ? [] : ['workbench']}
          onClick={() => {
            if (isNarrow) setCollapsed(true)
          }}
          style={{ borderInlineEnd: 'none' }}
        />
      </Layout.Sider>
      {isNarrow && !collapsed && (
        <div className="app-sider-mask" aria-hidden="true" onClick={() => setCollapsed(true)} />
      )}
      <Layout>
        <Layout.Header
          style={{
            background: '#fff',
            padding: '0 16px',
            display: 'flex',
            alignItems: 'center',
            gap: 12,
            borderBottom: '1px solid #f0f0f0',
          }}
        >
          <Button
            type="text"
            icon={<MenuOutlined />}
            aria-label="切换导航"
            onClick={() => setCollapsed((c) => !c)}
          />
          <Typography.Text strong style={{ flex: 1, fontSize: 16 }}>
            {active?.title ?? '浙大学习工作台'}
          </Typography.Text>
          {API_MODE === 'mock' && (
            <Tag color="orange" title="当前使用本地模拟数据，未连接真实后端">
              开发模式 · 模拟数据
            </Tag>
          )}
          <Tag>Asia/Shanghai</Tag>
        </Layout.Header>
        <Layout.Content>
          <div className="page-content">
            <Outlet />
          </div>
        </Layout.Content>
      </Layout>
    </Layout>
  )
}
