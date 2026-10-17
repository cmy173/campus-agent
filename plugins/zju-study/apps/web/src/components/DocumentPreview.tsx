import { Alert, Button, Drawer, Space, Spin, Typography } from 'antd'
import { lazy, Suspense, useEffect, useState } from 'react'
import { apiBaseUrl, apiRequest, apiErrorText } from '../lib/api/http'
import type { Resource } from '../types/contract'
const PdfReader = lazy(() => import('./PdfReader'))

export function DocumentPreview({ resource, onClose }: { resource: Resource | null; onClose: () => void }) {
  return <DocumentPreviewContent key={resource?.file_id ?? 'empty'} resource={resource} onClose={onClose} />
}

function DocumentPreviewContent({ resource, onClose }: { resource: Resource | null; onClose: () => void }) {
  const [url, setUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    if (!resource) return
    if (!resource.file_id) { setError('此资料尚未下载到本地，请重新同步或导入文件'); return }
    let active = true
    void apiRequest<{ url: string }>(`/files/${resource.file_id}/preview`, { method: 'POST' })
      .then(result => { if (active) setUrl(`${apiBaseUrl()}${result.url}`) })
      .catch(err => { if (active) setError(apiErrorText(err)) })
    return () => { active = false }
  }, [resource, attempt])
  return <Drawer title={`离线阅读 · ${resource?.title ?? ''}`} open={!!resource} onClose={onClose} size="90%">
    <Space orientation="vertical" style={{ width: '100%' }}>
      <Typography.Text type="secondary">文件在本地转换并保存，无需学校网站或在线预览服务。PPT 以静态 PDF 展示，动画和视频请下载原文件查看。</Typography.Text>
      {resource?.file_id && <Button href={`${apiBaseUrl()}/api/v1/files/${resource.file_id}`}>下载原文件</Button>}
      {error ? <Alert type="warning" title={error} action={<Button onClick={() => { setError(null); setUrl(null); setAttempt(value => value + 1) }}>重试</Button>} />
        : url ? <Suspense fallback={<Spin />}><PdfReader key={url} url={url} /></Suspense>
          : resource && <Spin tip="正在准备本地页面预览…"><div style={{ height: 160 }} /></Spin>}
    </Space>
  </Drawer>
}
