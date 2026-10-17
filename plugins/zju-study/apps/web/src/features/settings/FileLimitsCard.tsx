import { Alert, App, Button, Card, Form, InputNumber, Space, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { apiErrorText, apiRequest } from '../../lib/api/http'
import { configureUploadLimit } from '../../lib/upload'

type Limits = { download_mb: number; upload_mb: number; parse_mb: number }

export function FileLimitsCard() {
  const [form] = Form.useForm<Limits>()
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { message } = App.useApp()
  const load = () => {
    setError(null)
    void apiRequest<Limits>('/preferences/file-limits').then(value => {
      form.setFieldsValue(value); configureUploadLimit(value.upload_mb); setLoaded(true)
    }).catch(err => setError(apiErrorText(err)))
  }
  useEffect(() => { load() }, []) // eslint-disable-line react-hooks/exhaustive-deps
  return <Card title="文件大小" size="small">
    <Form form={form} layout="vertical" onFinish={async values => {
      setBusy(true)
      try {
        const saved = await apiRequest<Limits>('/preferences/file-limits', { method: 'PUT', body: values })
        form.setFieldsValue(saved); configureUploadLimit(saved.upload_mb); setError(null)
        message.success('文件大小设置已保存，将用于下一次下载、上传和解析。')
      } catch (err) { setError(apiErrorText(err)) } finally { setBusy(false) }
    }}>
      <Space wrap align="start">
        <Form.Item name="download_mb" label="平台文件下载上限（MiB）" rules={[{ required: true }]}><InputNumber min={1} max={4096} precision={0} disabled={!loaded} /></Form.Item>
        <Form.Item name="upload_mb" label="本地文件上传上限（MiB）" rules={[{ required: true }]}><InputNumber min={1} max={4096} precision={0} disabled={!loaded} /></Form.Item>
        <Form.Item name="parse_mb" label="单个文档文本解析上限（MiB）" rules={[{ required: true }]}><InputNumber min={1} max={1024} precision={0} disabled={!loaded} /></Form.Item>
      </Space>
      <Typography.Paragraph type="secondary">默认下载和上传上限为 1 GiB，文档解析为 256 MiB。文件可先下载保存，再按解析限制处理；超限会注明具体阶段。大文档解析仍会占用内存，请按电脑配置调整。视频不作为文档解析。</Typography.Paragraph>
      <Button htmlType="submit" loading={busy} disabled={!loaded}>保存文件大小设置</Button>
      {error && <Alert style={{ marginTop: 8 }} type="error" title={error} action={<Button size="small" onClick={load}>重新加载</Button>} />}
    </Form>
  </Card>
}
