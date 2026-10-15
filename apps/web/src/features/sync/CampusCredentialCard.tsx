import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Card, Collapse, Input, Popconfirm, Space, Switch, Tag, Typography } from 'antd'
import { agentApi } from '../agent/api'
import type { CampusCredentialItem, CampusCredentialStatus } from '../agent/api'
import { apiErrorText } from '../../lib/api/http'

const outcomeText: Record<string, string> = {
  AUTO_LOGIN_OK: '自动登录成功，校园会话已恢复。',
  CAPTCHA_REQUIRED: '校方要求验证码，需要你手动登录一次；任务进度已保留。',
  INTERACTION_REQUIRED: '校方要求二次认证，需要你手动登录一次；任务进度已保留。',
  CREDENTIAL_REJECTED: '校方提示账号或密码不正确，请核对后重新保存。',
  CAS_REJECTED: '校方未确认本次认证（不等同于密码错误）；可能是账号或认证策略变化。',
  RATE_LIMITED: '校方认证请求过于频繁，已暂停自动重试并进入冷却。',
  ACCESS_REJECTED: '校方认证服务拒绝了本次访问，请稍后手动登录确认。',
  NO_CREDENTIAL: '尚未保存凭据。',
  AUTO_RELOGIN_DISABLED: '自动重新登录当前是关闭的。',
  AUTO_LOGIN_UNAVAILABLE: '暂时无法开始校园登录流程，请稍后重试。',
  AUTO_LOGIN_FAILED: '自动登录未能完成，请稍后重试。',
  WEBVPN_LOGIN_OK: 'WebVPN 自动认证成功，离校通道已就绪。',
  WEBVPN_CREDENTIAL_REJECTED: 'WebVPN 账号或密码不正确，请核对后重新保存。',
  WEBVPN_CAPTCHA_REQUIRED: 'WebVPN 要求验证码，需要在官方页面手动登录一次。',
  WEBVPN_LOCKED: 'WebVPN 账号已锁定，请稍后重试或联系信息技术中心。',
  WEBVPN_RATE_LIMITED: 'WebVPN 登录过于频繁，已进入冷却。',
  WEBVPN_UNAVAILABLE: 'WebVPN 服务暂时不可达，请稍后重试。',
  WEBVPN_COOLDOWN: '上一次自动认证失败，冷却中；避免频繁请求校方。',
  WEBVPN_NOT_CONFIGURED: '尚未保存 WebVPN 凭据。',
  WEBVPN_PROTOCOL_CHANGED: 'WebVPN 返回内容不符合约定，请在官方页面确认后手动登录。',
}

const scopeMeta = {
  passport: {
    label: '校园通行证',
    action: '加密保存并启用',
    placeholderUser: '学号/账号',
    help: '会话过期时由本机后端自动重新认证并继续原任务，不需要打开或操作浏览器窗口。',
  },
  webvpn: {
    label: 'WebVPN（离校访问，独立账号）',
    action: '保存 WebVPN 凭据',
    placeholderUser: 'WebVPN 账号',
    help: '不在校园网时，教务读取会尝试经学校 WebVPN 自动认证；账号与校园通行证分开保存。同一账号只能有一个 WebVPN 会话，后台认证可能会顶掉浏览器里的 WebVPN 登录；门户要求验证码或二次认证时会退回一次人工登录。',
  },
} as const

function ScopeBlock({ scope, item, status, busy, run }: {
  scope: 'passport' | 'webvpn'
  item?: CampusCredentialItem
  status?: CampusCredentialStatus
  busy: string
  run: (kind: string, work: () => Promise<unknown>, message: string) => Promise<void>
}) {
  const meta = scopeMeta[scope]
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [authcode, setAuthcode] = useState('')
  const channel = status?.webvpn_channel
  const blocked = Boolean(busy)
  return <Space orientation="vertical" size={8} style={{ width: '100%' }}>
    <Space wrap size={6}>
      <Typography.Text strong style={{ fontSize: 13 }}>{meta.label}</Typography.Text>
      <Tag color={item?.configured ? 'success' : 'default'}>{item?.configured ? `已保存 ${item.username_hint}` : '未保存'}</Tag>
      {item?.configured && <Tag color={item.auto_relogin ? 'processing' : 'default'}>{item.auto_relogin ? '自动认证已开启' : '自动认证已关闭'}</Tag>}
      {scope === 'webvpn' && channel?.configured && <Tag color={channel.authenticated ? 'success' : 'default'}>{channel.authenticated ? '通道已认证' : '通道待认证'}</Tag>}
    </Space>
    <Typography.Text type="secondary" style={{ fontSize: 12 }}>{meta.help}</Typography.Text>
    <Space wrap size={6}>
      <Input size="small" aria-label={scope === 'webvpn' ? 'WebVPN 账号' : '校园账号'} placeholder={meta.placeholderUser}
        value={username} onChange={e => setUsername(e.target.value)} style={{ width: 160 }} />
      <Input.Password size="small" aria-label={scope === 'webvpn' ? 'WebVPN 密码' : '校园密码'} placeholder="密码（保存后不回显）"
        value={password} autoComplete="new-password" onChange={e => setPassword(e.target.value)} style={{ width: 190 }} />
      {scope === 'passport' && <Input size="small" aria-label="验证码（如校方固定要求）" placeholder="验证码（可选）"
        value={authcode} onChange={e => setAuthcode(e.target.value)} style={{ width: 120 }} />}
      <Button size="small" type="primary" loading={busy === `save-${scope}`} disabled={blocked || !username || !password}
        onClick={() => void run(`save-${scope}`, async () => {
          await agentApi.saveCampusCredential({ scope, username, password, authcode, auto_relogin: true })
          setUsername(''); setPassword(''); setAuthcode('')
        }, `${meta.label}凭据已加密保存。`)}>{meta.action}</Button>
    </Space>
    {item?.configured && <Space wrap size={8}>
      <Switch size="small" aria-label={`${meta.label}自动认证`} checked={item.auto_relogin} disabled={blocked}
        onChange={value => void run(`toggle-${scope}`, () => agentApi.toggleCampusAutoRelogin(scope, value),
          value ? '已开启自动认证。' : '已关闭自动认证（凭据仍保留，可随时再开启）。')} />
      <Button size="small" loading={busy === `test-${scope}`} disabled={blocked || !item.auto_relogin}
        onClick={() => void run(`test-${scope}`, async () => {
          const result = await agentApi.testCampusAutoLogin(scope)
          return (result as { message?: string }).message ?? outcomeText[result.code] ?? `结果：${result.code}`
        }, '')}>立即验证</Button>
      <Popconfirm title={`清除本机保存的${meta.label}凭据？`} description="清除后自动认证立即失效；已缓存资料与浏览器登录不受影响。"
        okText="清除凭据" cancelText="取消"
        onConfirm={() => void run(`clear-${scope}`, () => agentApi.clearCampusCredential(scope), '凭据已从本机清除。')}>
        <Button size="small" danger disabled={blocked}>清除</Button>
      </Popconfirm>
    </Space>}
  </Space>
}

/** Opt-in credential vault: automatic re-login and the off-campus WebVPN channel. */
export function CampusCredentialCard() {
  const credentials = useQuery({ queryKey: ['agent', 'campus', 'credentials'], queryFn: agentApi.campusCredentials })
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const items = credentials.data?.items ?? []
  const itemFor = (scope: 'passport' | 'webvpn') => items.find(entry => entry.scope === scope)
  const channel = credentials.data?.webvpn_channel

  async function run(kind: string, work: () => Promise<unknown>, message: string) {
    setBusy(kind); setError(''); setNotice('')
    try {
      const result = await work()
      await credentials.refetch()
      if (typeof result === 'string') setNotice(result)
      else if (message) setNotice(message)
    } catch (e) { setError(apiErrorText(e)) }
    finally { setBusy('') }
  }

  return <Card title="本机加密保存的校园账号" size="small">
    <Space orientation="vertical" size={10} style={{ width: '100%' }}>
      {credentials.isLoading && <Typography.Text role="status">正在读取凭据状态…</Typography.Text>}
      {!credentials.isLoading && credentials.isError && <Alert type="error" showIcon
        title="凭据状态读取失败" description={apiErrorText(credentials.error)} />}
      {!credentials.isLoading && !credentials.isError && <>
        <ScopeBlock scope="passport" item={itemFor('passport')} status={credentials.data} busy={busy} run={run} />
        <ScopeBlock scope="webvpn" item={itemFor('webvpn')} status={credentials.data} busy={busy} run={run} />
        <Space wrap size={6}>
          {credentials.data?.cooldown_active && <Tag color="warning">校验冷却中</Tag>}
          {credentials.data?.last_login && <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            最近一次通行证自动登录：{credentials.data.last_login.ok ? '成功' : '未成功'}（{outcomeText[credentials.data.last_login.code] ?? credentials.data.last_login.code}）
          </Typography.Text>}
          {channel?.last_error && <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            WebVPN 最近结果：{outcomeText[channel.last_error.code] ?? channel.last_error.code}
          </Typography.Text>}
        </Space>
        <Collapse size="small" ghost items={[{
          key: 'detail', label: <Typography.Text type="secondary" style={{ fontSize: 12 }}>存储与安全说明</Typography.Text>,
          children: <Space orientation="vertical" size={4}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              凭据以本机账号加密（Windows DPAPI）保存，<strong>不会进入对话、日志或模型上下文</strong>，也不会被任何接口回显。
              自动登录无法绕过校方验证码或多因素认证；校方要求额外交互时会退回一次人工登录，任务进度保留。
            </Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {channel ? `离校通道：${channel.mode === 'direct' ? '仅直连' : channel.last_error ? `上次自动认证未成功（${outcomeText[channel.last_error.code] ?? channel.last_error.code}）` : channel.authenticated ? '已认证' : channel.configured ? '已配置，按需自动认证' : '未启用'}；已登记主机 ${channel.mapped_hosts.join('、') || '无'}${channel.unmapped_hosts?.length ? `；未登记 ${channel.unmapped_hosts.join('、')}` : ''}。` : ''}
            </Typography.Text>
          </Space>,
        }]} />
        {error && <Alert type="error" showIcon title={error} />}
        {notice && <Alert type="info" showIcon title={notice} />}
      </>}
    </Space>
  </Card>
}
