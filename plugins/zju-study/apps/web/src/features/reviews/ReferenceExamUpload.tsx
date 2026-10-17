import { UploadOutlined } from '@ant-design/icons'
import { Alert, App, Button, Space, Typography, Upload } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { api } from '../../lib/api/client'
import { toApiError } from '../../lib/api/http'
import { checkUploadFile, UPLOAD_ACCEPT } from '../../lib/upload'

export function ReferenceExamUpload({ courseId, onReady }: { courseId: string; onReady: (ids: string[]) => void }) {
  const { message } = App.useApp()
  const [pending, setPending] = useState<string[]>([])
  const [status, setStatus] = useState<string | null>(null)
  const onReadyRef = useRef(onReady)
  onReadyRef.current = onReady
  useEffect(() => {
    if (!pending.length) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      for (const id of pending) {
        try {
          const job = await api.getJob(id)
          if (cancelled) return
          if (job.status === 'queued' || job.status === 'running') continue
          const ids = job.result_refs.filter(ref => ref.type === 'resource').map(ref => ref.id)
          if (!ids.length) {
            setStatus(job.errors[0]?.message ?? '历年卷导入未成功，请重新上传')
            setPending(current => current.filter(value => value !== id))
            continue
          }
          const resources = await Promise.all(ids.map(resourceId => api.getResource(resourceId)))
          if (cancelled) return
          if (resources.some(resource => resource.parse_status === 'pending' || resource.parse_status === 'processing')) continue
          const parsed = resources.filter(resource => resource.parse_status === 'ready')
          const bodies = await Promise.all(parsed.map(resource => api.getResourceChunks(resource.id)))
          if (cancelled) return
          const ready = parsed.filter((_resource, index) => bodies[index].items.some(chunk => chunk.text.trim())).map(resource => resource.id)
          if (ready.length) onReadyRef.current(ready)
          setStatus(ready.length === ids.length
            ? '历年卷已解析并勾选为参考材料；你可以在备注中说明参考题型、难度或范围。'
            : '部分历年卷无法提取正文；扫描件可能需要 OCR，不能仅凭文件名作为已读取资料。')
          setPending(current => current.filter(value => value !== id))
        } catch (error) {
          if (!cancelled) setStatus(toApiError(error).message)
        }
      }
      if (!cancelled) timer = setTimeout(poll, 2000)
    }
    void poll()
    return () => { cancelled = true; clearTimeout(timer) }
  }, [pending])

  return <Space orientation="vertical" size={6}>
    <Upload accept={UPLOAD_ACCEPT} showUploadList={false} multiple beforeUpload={file => {
      const check = checkUploadFile(file.name, file.size)
      if (!check.ok) {
        message.error(check.reason === 'type' ? '历年卷支持 PDF、DOCX、PPTX' : '文件超过当前上传大小设置，请在设置中调整')
        return Upload.LIST_IGNORE
      }
      setStatus(`正在导入历年卷：${file.name}`)
      void api.importFiles({ course_id: courseId, kind: 'document', title: `历年卷参考：${file.name}` }, [file],
        `past-exam:${courseId}:${crypto.randomUUID()}`).then(result => {
          setPending(current => [...current, result.job.id])
        }).catch(error => setStatus(toApiError(error).message))
      return Upload.LIST_IGNORE
    }}>
      <Button icon={<UploadOutlined />} loading={pending.length > 0}>导入历年卷作为参考</Button>
    </Upload>
    <Typography.Text type="secondary">
      PPT 与作业是主要命题依据；历年卷参考题型、难度和分值，不默认照搬原题。
      文件正文会与选定资料及备注一起发送，不是只发送文件名。扫描卷须先提取出正文。
    </Typography.Text>
    {status && <Alert type="info" showIcon title={status} />}
  </Space>
}
