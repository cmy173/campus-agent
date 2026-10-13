import { Alert, Button, Checkbox, Drawer, Empty, Space, Tag, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiErrorText } from '../../lib/api/http'
import { agentApi, fileContentUrl } from './api'
import type { ConversationFile } from './api'

export function FilePreviewPane({ file }: { file: ConversationFile }) {
  const preview = useQuery({ queryKey: ['agent', 'file-preview', file.conversation_id, file.id],
    queryFn: () => agentApi.preview(file.conversation_id, file.id) })
  if (preview.isError) return <Alert type="error" title="文件预览失败" description={apiErrorText(preview.error)} />
  if (!preview.data) return <Typography.Text>正在读取文件…</Typography.Text>
  const content = preview.data
  return <section aria-label={`${file.name} 预览`}>
    <Typography.Title level={5}>{file.name}</Typography.Title>
    {content.truncated && <Alert type="info" title="预览显示前512 KiB，下载可查看完整文件" />}
    {content.kind === 'text' && <pre className="agent-file-text">{content.text}</pre>}
    {content.kind === 'image' && <img className="agent-file-image" src={fileContentUrl(file.conversation_id, file.id, true)} alt={file.name} />}
    {content.kind === 'pdf' && <iframe className="agent-file-pdf" title={file.name} src={fileContentUrl(file.conversation_id, file.id, true)} />}
    {content.kind === 'download' && <Typography.Paragraph>此格式暂不支持内嵌预览，请下载后打开。</Typography.Paragraph>}
    <Button href={fileContentUrl(file.conversation_id, file.id)}>下载 {file.name}</Button>
  </section>
}

export function FileWorkspace({ open, onClose, items, selected, onSelect, disabled, loading, error, onRefresh, focusedFile }:
  { open: boolean; onClose: () => void; items: ConversationFile[]; selected: string[]; onSelect: (id: string) => void;
    disabled: boolean; loading: boolean; error?: unknown; onRefresh: () => void; focusedFile?: ConversationFile | null }) {
  const [preview, setPreview] = useState<ConversationFile | null>(null)
  useEffect(() => { if (focusedFile) setPreview(focusedFile) }, [focusedFile])
  return <Drawer className="agent-workspace" title="文件与成果" open={open} onClose={onClose} size="min(720px, 100vw)">
    <Space orientation="vertical" style={{ width: '100%' }} size={16}>
      <Space wrap><Typography.Text type="secondary">选择文件后随下一条消息发送，移除选择会保留原文件。</Typography.Text>
        <Button size="small" loading={loading} onClick={onRefresh}>刷新文件</Button></Space>
      {error != null && <Alert type="error" title="文件列表读取失败" description={apiErrorText(error)} />}
      {!loading && !error && !items.length && <Empty description="还没有上传文件或生成成果" />}
      {items.map(file => <div className="agent-file-row" key={file.id}>
        <Checkbox aria-label={`选择附件 ${file.name}`} checked={selected.includes(file.id)} disabled={disabled} onChange={() => onSelect(file.id)} />
        <div className="agent-file-description"><Typography.Text strong>{file.name}</Typography.Text>
          <div><Tag>{file.kind === 'input' ? '上传资料' : '生成成果'}</Tag><Typography.Text type="secondary">{(file.size / 1024).toFixed(1)} KiB</Typography.Text></div></div>
        <Space wrap><Button size="small" onClick={() => setPreview(file)}>预览 {file.name}</Button>
          <Button size="small" href={fileContentUrl(file.conversation_id, file.id)}>下载</Button></Space>
      </div>)}
      {preview && <FilePreviewPane key={preview.id} file={preview} />}
    </Space>
  </Drawer>
}
