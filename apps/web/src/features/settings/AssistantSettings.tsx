import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Card, Checkbox, Input, Select, Space, Tabs, Typography } from 'antd'
import { useProviders } from '../../lib/api/hooks'
import { apiErrorText } from '../../lib/api/http'
import { agentApi } from '../agent/api'
import type { DiagnosticResult, McpServerSettings } from '../agent/api'
import { AiProviderCard } from './AiProviderCard'
import { AgentDataSettings } from './AgentDataSettings'

/** Accept the common MCP configuration shape in addition to the editable list. */
export function parseMcpSettings(text: string): McpServerSettings[] {
  const value = JSON.parse(text)
  if (Array.isArray(value)) return value
  if (value && typeof value.mcpServers === 'object' && !Array.isArray(value.mcpServers)) {
    return Object.entries(value.mcpServers).map(([serverName, entry]) => {
      if (!entry || typeof entry !== 'object') throw new Error('MCP 服务配置需要是对象')
      const { type, disabled, ...config } = entry as Partial<McpServerSettings> & { type?: string; disabled?: boolean }
      if (type && !['stdio', 'http', 'streamable-http'].includes(type)) throw new Error('当前支持 stdio 或 Streamable HTTP MCP 服务')
      return { enabled: !disabled, args: [], cwd: '', env: {}, headers: {}, url: '', command: '', toolCallTimeoutMs: 60000,
        ...config, serverName, transport: config.transport ?? (type === 'stdio' ? 'stdio' : config.url ? 'streamable-http' : 'stdio') }
    })
  }
  throw new Error('请填写服务列表，或包含 mcpServers 的标准配置对象')
}

function DiagnosticFeedback({ result }: { result?: DiagnosticResult }) {
  if (!result) return null
  return <Alert type={result.ok ? 'success' : 'error'} title={result.ok
    ? `连接通过 · ${result.tool_count !== undefined ? result.tool_count + ' 个工具' : result.source_count + ' 个搜索来源'} · ${result.latency_ms ?? '?'}ms`
    : result.error?.message ?? '连接测试失败'}
    description={result.ok ? result.tool_names?.join('、') : result.error?.code} />
}

export function RuntimeSettingsPanel() {
  const settings = useQuery({ queryKey: ['agent', 'settings'], queryFn: agentApi.settings, refetchOnWindowFocus: false })
  const providers = useProviders()
  const [workspace, setWorkspace] = useState('')
  const [skillDirs, setSkillDirs] = useState('')
  const [mcpText, setMcpText] = useState('[]')
  const [baseUrl, setBaseUrl] = useState('')
  const [model, setModel] = useState('')
  const [keyMode, setKeyMode] = useState<'shared' | 'separate'>('shared')
  const [apiKey, setApiKey] = useState('')
  const [clearKey, setClearKey] = useState(false)
  const [providerId, setProviderId] = useState<string>()
  const [pending, setPending] = useState('')
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)
  const [results, setResults] = useState<Record<string, DiagnosticResult>>({})
  useEffect(() => {
    if (!settings.data) return
    const value = settings.data
    setWorkspace(value.workspace)
    setSkillDirs(value.skill_dirs.join('\n'))
    setMcpText(JSON.stringify(value.mcp_servers, null, 2))
    setBaseUrl(value.search.base_url)
    setModel(value.search.model)
    setKeyMode(value.search.key_mode)
  }, [settings.data])

  async function save() {
    setError(''); setSaved(false); setPending('save')
    try {
      await agentApi.saveSettings({ workspace, skill_dirs: skillDirs.split('\n').map(p => p.trim()).filter(Boolean),
        mcp_servers: parseMcpSettings(mcpText), search: { base_url: baseUrl, model, key_mode: keyMode,
          ...(apiKey ? { api_key: apiKey } : {}), clear_key: clearKey } })
      setApiKey(''); setClearKey(false); setResults({})
      await settings.refetch()
      setSaved(true)
    } catch (e) { setError(apiErrorText(e)) }
    finally { setPending('') }
  }

  async function test(name: string, kind: 'search' | 'mcp') {
    const key = kind + ':' + name
    setPending(key); setError('')
    try {
      const result = kind === 'search' ? await agentApi.testSearch(providerId ?? providers.data?.items[0]?.id) : await agentApi.testMcp(name)
      setResults(previous => ({ ...previous, [key]: result }))
    } catch (e) { setError(apiErrorText(e)) }
    finally { setPending('') }
  }

  if (settings.isLoading) return <Typography.Text>正在读取助手设置…</Typography.Text>
  return <Space orientation="vertical" size={16} style={{ width: '100%' }}>
    {(error || settings.isError) && <Alert type="error" title={error || apiErrorText(settings.error)} />}
    {saved && <Alert type="success" title="助手设置已保存，下一个对话轮次使用新设置。" />}
    <Card title="文件与技能" size="small"><Space orientation="vertical" style={{ width: '100%' }}>
      <Typography.Text>默认任务目录</Typography.Text>
      <Input aria-label="默认任务目录" placeholder="留空使用应用的任务目录" value={workspace} onChange={e => setWorkspace(e.target.value)} />
      <Typography.Text>额外技能目录（每行一个）</Typography.Text>
      <Input.TextArea aria-label="额外技能目录" value={skillDirs} onChange={e => setSkillDirs(e.target.value)} autoSize={{ minRows: 2, maxRows: 5 }} />
    </Space></Card>
    <Card title="网页搜索" size="small"><Space orientation="vertical" style={{ width: '100%' }}>
      <Typography.Text type="secondary">搜索与聊天分别配置和测试。地址与模型留空时使用当前 Harness 的 DeepSeek 搜索默认值。</Typography.Text>
      <Input aria-label="搜索服务地址" placeholder="搜索 Messages 服务地址（不含 /messages）" value={baseUrl} onChange={e => setBaseUrl(e.target.value)} />
      <Input aria-label="搜索模型" placeholder="搜索模型" value={model} onChange={e => setModel(e.target.value)} />
      <Select aria-label="搜索密钥来源" value={keyMode} onChange={setKeyMode} style={{ width: '100%' }} options={[
        { value: 'shared', label: '使用当前对话的模型密钥' }, { value: 'separate', label: '使用独立搜索密钥' },
      ]} />
      {keyMode === 'separate' && <>
        <Input.Password aria-label="独立搜索密钥" autoComplete="new-password" placeholder={settings.data?.search.has_api_key ? '已保存密钥；留空保留' : '填写搜索服务密钥'} value={apiKey} onChange={e => setApiKey(e.target.value)} />
        <Checkbox checked={clearKey} onChange={e => setClearKey(e.target.checked)}>清除已保存的搜索密钥</Checkbox>
      </>}
      {keyMode === 'shared' && <Select aria-label="测试搜索使用的模型" placeholder="选择测试时共享密钥的模型" value={providerId ?? providers.data?.items[0]?.id}
        style={{ width: '100%' }} onChange={setProviderId} options={providers.data?.items.map(p => ({ value: p.id, label: p.name }))} />}
      <Button disabled={Boolean(pending)} loading={pending === 'search:'} onClick={() => void test('', 'search')}>测试已保存的搜索设置</Button>
      <Typography.Text type="secondary">测试会发送一次公开文档搜索请求，可能产生服务费用；不发送本地资料。</Typography.Text>
      <DiagnosticFeedback result={results['search:']} />
    </Space></Card>
    <Card title="MCP 服务" size="small"><Space orientation="vertical" style={{ width: '100%' }}>
      <Alert type="warning" title="仅启用可信的 MCP 服务" description="启用 MCP 将运行本地命令或访问远程服务，并授予该服务工具权限。连接测试也会启动该服务；请勿配置不必要的凭据。" />
      <Typography.Text type="secondary">粘贴标准 mcpServers 配置或编辑服务列表。enabled 设为 false 可停用。凭据保存后以 __SAVED_CREDENTIAL__ 显示，保留该值即可沿用。</Typography.Text>
      <Input.TextArea aria-label="MCP 服务配置" value={mcpText} onChange={e => setMcpText(e.target.value)} autoSize={{ minRows: 6, maxRows: 14 }} />
      {settings.data?.mcp_servers.map(server => <div key={server.serverName}>
        <Space wrap><Typography.Text strong>{server.serverName}</Typography.Text><Typography.Text type="secondary">{server.enabled ? '已启用' : '已停用'}</Typography.Text>
          <Button size="small" disabled={Boolean(pending)} loading={pending === 'mcp:' + server.serverName} onClick={() => void test(server.serverName, 'mcp')}>测试连接</Button></Space>
        <DiagnosticFeedback result={results['mcp:' + server.serverName]} />
      </div>)}
    </Space></Card>
    <Button type="primary" disabled={Boolean(pending) || !settings.data} loading={pending === 'save'} onClick={() => void save()}>保存助手设置</Button>
  </Space>
}

export function AssistantSettings() {
  return <Tabs items={[{ key: 'model', label: '模型服务', children: <AiProviderCard /> },
    { key: 'tools', label: '工具与搜索', children: <RuntimeSettingsPanel /> },
    { key: 'data', label: '数据与历史', children: <AgentDataSettings /> }]} />
}
