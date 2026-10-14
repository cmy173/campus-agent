import { Alert, Button, Checkbox, Drawer, Empty, Space, Tabs, Tag, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiErrorText } from '../../lib/api/http'
import { agentApi, fileContentUrl } from './api'
import type { ConversationFile } from './api'
import { PdfPreview } from './PdfPreview'

export function FilePreviewPane({ file }: { file: ConversationFile }) {
  const preview = useQuery({ queryKey: ['agent', 'file-preview', file.conversation_id, file.id],
    queryFn: () => agentApi.preview(file.conversation_id, file.id) })
  if (preview.isError) return <Alert type="error" title="文件预览失败" description={apiErrorText(preview.error)} />
  if (!preview.data) return <Typography.Text>正在读取文件…</Typography.Text>
  const content = preview.data
  return <section aria-label={`${file.name} 预览`}>
    <Typography.Title level={5}>{file.name}</Typography.Title>
    {content.truncated && <Alert type="info" title={content.kind === 'spreadsheet' ? '表格预览最多显示8张表、每表100行24列，部分内容已截断；下载可查看完整文件' : '预览显示前512 KiB，下载可查看完整文件'} />}
    {content.kind === 'text' && <pre className="agent-file-text">{content.text}</pre>}
    {content.kind === 'image' && <img className="agent-file-image" src={fileContentUrl(file.conversation_id, file.id, true)} alt={file.name} />}
    {content.kind === 'pdf' && <PdfPreview name={file.name} url={fileContentUrl(file.conversation_id, file.id, true)} />}
    {content.kind === 'spreadsheet' && <>
      {content.has_formulas && <Typography.Paragraph type="secondary">公式显示为原式，此预览不重新计算结果。</Typography.Paragraph>}
      <Tabs items={content.sheets?.map(sheet => ({ key: sheet.name, label: sheet.name, children: <div className="agent-sheet-preview">
        <table aria-label={`${sheet.name} 表格`}><tbody>{sheet.rows.map((row, index) => <tr key={index}>
          <th scope="row">{index + 1}</th>{row.map((cell, col) => <td key={col}>{cell == null ? '' : String(cell)}</td>)}
        </tr>)}</tbody></table>
      </div> }))} />
    </>}
    {content.kind === 'download' && <Typography.Paragraph>{content.message ?? '此格式暂不支持内嵌预览，请下载后打开。'}</Typography.Paragraph>}
    <Button href={fileContentUrl(file.conversation_id, file.id)}>下载 {file.name}</Button>
  </section>
}

export function FileWorkspace({ open, onClose, items, selected, onSelect, disabled, loading, error, onRefresh, focusedFile }:
  { open: boolean; onClose: () => void; items: ConversationFile[]; selected: string[]; onSelect: (id: string) => void;
    disabled: boolean; loading: boolean; error?: unknown; onRefresh: () => void; focusedFile?: ConversationFile | null }) {
  const [preview, setPreview] = useState<ConversationFile | null>(null)
  const previewPane = useRef<HTMLDivElement>(null)
  useEffect(() => { if (focusedFile) setPreview(focusedFile) }, [focusedFile])
  useEffect(() => { if (open && preview) previewPane.current?.scrollIntoView?.({ block: 'start' }) }, [open, preview])
  return <Drawer className="agent-workspace" title="文件与成果" open={open} onClose={onClose} size="min(720px, 100vw)">
    <Space orientation="vertical" style={{ width: '100%' }} size={16}>
      <Space wrap><Typography.Text type="secondary">选择文件后随下一条消息发送，移除选择会保留原文件。</Typography.Text>
        <Button size="small" loading={loading} onClick={onRefresh}>刷新文件</Button></Space>
      {error != null && <Alert type="error" title="文件列表读取失败" description={apiErrorText(error)} />}
      {!loading && !error && !items.length && <Empty description="还没有上传文件或生成成果" />}
      {preview && <><div ref={previewPane}><FilePreviewPane key={'preview:' + preview.id} file={preview} /></div><Typography.Title level={5}>会话文件</Typography.Title></>}
      {items.map(file => <div className="agent-file-row" key={file.id}>
        <Checkbox aria-label={`选择附件 ${file.name}`} checked={selected.includes(file.id)} disabled={disabled} onChange={() => onSelect(file.id)} />
        <div className="agent-file-description"><Typography.Text strong>{file.name}</Typography.Text>
          <div><Tag>{file.kind === 'input' ? '上传资料' : '生成成果'}</Tag><Typography.Text type="secondary">{(file.size / 1024).toFixed(1)} KiB</Typography.Text></div></div>
        <Space wrap><Button size="small" onClick={() => setPreview(file)}>预览 {file.name}</Button>
          <Button size="small" href={fileContentUrl(file.conversation_id, file.id)}>下载</Button></Space>
      </div>)}
    </Space>
  </Drawer>
}
