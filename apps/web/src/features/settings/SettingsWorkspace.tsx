import { Tabs } from 'antd'
import { useSearchParams } from 'react-router-dom'
import { AssistantSettings } from './AssistantSettings'
import { PersonalSettings } from './PersonalSettings'
import { ConnectionsSettings } from '../sync/ConnectionsSettings'

export function SettingsWorkspace() {
  const [params, setParams] = useSearchParams()
  const raw = params.get('settings_tab')
  const activeKey = ['assistant', 'accounts', 'personal'].includes(raw ?? '') ? raw! : 'assistant'
  return <Tabs activeKey={activeKey} onChange={key => setParams(previous => {
    const next = new URLSearchParams(previous); next.set('settings_tab', key); return next
  }, { replace: true })} items={[
    { key: 'assistant', label: '模型与工具', children: <AssistantSettings /> },
    { key: 'accounts', label: '校园账号与同步', children: <ConnectionsSettings /> },
    { key: 'personal', label: '时间与数据', children: <PersonalSettings /> },
  ]} />
}
