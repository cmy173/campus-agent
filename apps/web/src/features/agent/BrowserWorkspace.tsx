import { useState } from 'react'
import { Alert, Button, Card, Drawer, Input, Space, Tag, Typography } from 'antd'
import { apiErrorText } from '../../lib/api/http'
import { useBrowserWorkspace } from './useBrowserWorkspace'

export function BrowserWorkspace({ id, open, active, onOpen, onClose }: {
  id: string; open: boolean; active: boolean; onOpen: () => void; onClose: () => void
}) {
  const state = useBrowserWorkspace(id, open, active)
  const [url, setUrl] = useState('')
  const pending = state.status.data?.reviews.filter(r => r.status === 'pending') ?? []
  return <>
    {!open && pending.length > 0 && <Alert type="info" title={`有 ${pending.length} 项浏览器操作等待确认`}
      action={<Button onClick={onOpen}>查看具体操作</Button>} />}
    <Drawer title="浏览器工作区" open={open} onClose={onClose} size="min(660px, 100vw)">
      <Space orientation="vertical" size={16} style={{ width: '100%' }}>
        <Typography.Paragraph>在助手自己的浏览器窗口完成登录或验证码，之后可继续读取、下载和填写。登录状态会保留。下载附件出现在“文件与成果”。</Typography.Paragraph>
        <Input aria-label="浏览器地址" placeholder="网站地址，可留空打开浏览器" value={url} onChange={e => setUrl(e.target.value)} />
        <Space wrap><Button loading={state.busy === 'open'} disabled={Boolean(state.busy)} onClick={() => void state.openBrowser(url.trim() || undefined)}>打开浏览器 / 登录</Button>
          <Button loading={state.busy === 'stop'} disabled={Boolean(state.busy)} onClick={() => void state.stop()}>停止浏览器</Button>
          <Button onClick={() => void state.status.refetch()} loading={state.status.isFetching}>刷新状态</Button></Space>
        {state.error && <Alert type="error" title={state.error} />}
        {state.receipt && <Alert type="info" title={state.receipt} />}
        {state.status.isError && <Alert type="error" title={apiErrorText(state.status.error)} />}
        <Tag>{state.status.data?.running ? (state.status.data.headed ? '浏览器运行中 · 可在窗口操作' : '浏览器运行中 · 后台') : '浏览器未打开'}</Tag>
        {state.status.data?.pages.map(p => <div key={p.index} style={{ overflowWrap: 'anywhere' }}>
          <Typography.Text strong>{p.title || `页面 ${p.index + 1}`}</Typography.Text><br /><Typography.Text type="secondary">{p.url}</Typography.Text>
        </div>)}
        {state.status.data?.reviews.slice().reverse().map(action => <Card key={action.id} size="small" title={action.preview.description || action.preview.button || '浏览器提交'}>
          <Space orientation="vertical" style={{ width: '100%', overflowWrap: 'anywhere' }}>
            <Typography.Text>对象：{action.preview.recipient || action.preview.action_url || action.preview.page_url}</Typography.Text>
            {action.preview.content && <Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>{action.preview.content}</Typography.Paragraph>}
            {action.preview.fields?.map((f, i) => <Typography.Text key={i}>{f.name}：{f.value || '（空）'}</Typography.Text>)}
            {action.preview.attachments?.map((name, i) => <Tag key={i}>附件：{name}</Tag>)}
            {action.status === 'pending' ? <Space wrap>
              <Button type="primary" disabled={Boolean(state.busy)} loading={state.busy === action.id} onClick={() => void state.act(action.id, 'confirm')}>确认并执行</Button>
              <Button disabled={Boolean(state.busy)} onClick={() => void state.act(action.id, 'reject')}>取消此操作</Button>
            </Space> : <Typography.Text type={action.status === 'uncertain' || action.status === 'stale' ? 'warning' : 'secondary'}>
              {{ executing: '执行中', executed: '已执行操作，请核对网站回执', rejected: '已取消', stale: '内容或目标已改变，需要新预览', uncertain: '结果不确定，请核对网站回执后再操作' }[action.status]}</Typography.Text>}
          </Space>
        </Card>)}
      </Space>
    </Drawer>
  </>
}
