import { Alert, App, Button, Card, Checkbox, Form, Input, InputNumber, Select, Space, Switch, Tag, Typography, Upload } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { api } from '../../lib/api/client'
import { apiRequest, apiErrorText } from '../../lib/api/http'
import { useCourseResources } from '../../lib/api/hooks'
import { checkUploadFile } from '../../lib/upload'
import { DocumentPreview } from '../../components/DocumentPreview'
import type { Resource } from '../../types/contract'

type RightsReport = { state: string; evidence: string[]; message: string }
type Book = { id: string; course_id: string; title: string; author: string; edition: string; isbn: string; source_url: string; resource_id: string | null; confirmed: boolean; rights_check?: RightsReport }
type Exercise = { chapter: number; number: string; text: string; page: number | null; resource_version?: number | null; confirmed?: boolean }
const blank = { title: '', author: '', edition: '', isbn: '', source_url: '', resource_id: null, confirmed: false }
const rightsLabels: Record<string, string> = { disabled: '检查已关闭', not_checked: '尚未检查', stale: '检查已过期', unknown: '无法判断', declaration_found: '发现许可声明', restriction_found: '发现限制声明', conflicting: '声明存在冲突' }

function BookEditor({ book, courseId, onSaved, resources }: { book: Book | null; courseId: string; onSaved: () => void; resources: Resource[] }) {
  const { message } = App.useApp()
  const [form] = Form.useForm()
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState<string | null>(null)
  const [job, setJob] = useState<string | null>(null)
  const [preview, setPreview] = useState<Resource | null>(null)
  useEffect(() => { form.setFieldsValue(book ?? blank) }, [book, form])
  useEffect(() => {
    if (!job || !book) return
    let active = true
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const result = await api.getJob(job)
        if (!active) return
        if (result.status === 'queued' || result.status === 'running') { timer = setTimeout(poll, 2000); return }
        const id = result.result_refs.find(ref => ref.type === 'resource')?.id
        if (!id) throw new Error(result.errors[0]?.message ?? '教材导入失败')
        const resource = await api.getResource(id)
        if (!active) return
        if (resource.parse_status === 'pending' || resource.parse_status === 'processing') {
          setStatus('教材已关联，正在解析并自动寻找习题区…'); timer = setTimeout(poll, 2000); return
        }
        if (active) { setJob(null); setStatus(resource.parse_status === 'ready' ? '教材正文已就绪，习题候选已自动建立。' : '文件已导入，但正文解析未完成；扫描件可能需要 OCR。'); onSaved() }
      } catch (err) { if (active) { setStatus(apiErrorText(err)); setJob(null) } }
    }
    void poll()
    return () => { active = false; clearTimeout(timer) }
  }, [job, book, onSaved])
  const save = async () => {
    const values = await form.validateFields()
    setBusy(true)
    try {
      await apiRequest(book ? `/textbooks/${book.id}` : `/courses/${courseId}/textbooks`, { method: book ? 'PUT' : 'POST', body: values })
      message.success('教材版本已保存'); onSaved()
    } catch (err) { setStatus(apiErrorText(err)) } finally { setBusy(false) }
  }
  return <Card title={book ? `教材 · ${book.title}` : '添加教材'} size="small">
    <Form form={form} layout="vertical" initialValues={blank}>
      <Space wrap align="start">
        <Form.Item name="title" label="教材名称" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="author" label="作者"><Input /></Form.Item>
        <Form.Item name="edition" label="版次 / 出版年份"><Input placeholder="例如第 5 版 / 2024" /></Form.Item>
        <Form.Item name="isbn" label="ISBN"><Input /></Form.Item>
      </Space>
      <Form.Item name="source_url" label="教材信息出处（选填）"><Input /></Form.Item>
      <Form.Item name="resource_id" label="关联已导入的教材文件">
        <Select allowClear options={resources.map(resource => ({ value: resource.id, label: `${resource.title} · ${resource.parse_status}` }))} />
      </Form.Item>
      <Form.Item name="confirmed" valuePropName="checked"><Checkbox>已核对教材版本（ISBN 或作者与版次）；允许自动匹配作业题号</Checkbox></Form.Item>
      <Space wrap>
        <Button type="primary" loading={busy} onClick={() => void save()}>保存教材版本</Button>
        {book && <Upload accept=".pdf,.docx" showUploadList={false} beforeUpload={file => {
          const check = checkUploadFile(file.name, file.size)
          if (!check.ok) { message.error('教材支持 PDF、DOCX；大小上限可在设置中调整'); return Upload.LIST_IGNORE }
          setStatus('正在导入教材…')
          void api.importFiles({ textbook_id: book.id, course_id: courseId, kind: 'document', title: `教材：${book.title} ${book.edition}` }, [file], `textbook:${crypto.randomUUID()}`)
            .then(result => setJob(result.job.id)).catch(err => setStatus(apiErrorText(err)))
          return Upload.LIST_IGNORE
        }}><Button loading={!!job}>上传教材文件</Button></Upload>}
        {book?.resource_id && <Button onClick={() => setPreview(resources.find(resource => resource.id === book.resource_id) ?? null)}>离线查看教材</Button>}
      </Space>
      {status && <Alert style={{ marginTop: 12 }} type="info" title={status} />}
    </Form>
    {book && <Space orientation="vertical" style={{ marginTop: 12 }}>
      <Space><Tag>{rightsLabels[book.rights_check?.state ?? 'not_checked']}</Tag>
        <Button size="small" onClick={async () => {
          try { await apiRequest(`/textbooks/${book.id}/rights-check`, { method: 'POST' }); onSaved() }
          catch (err) { setStatus(apiErrorText(err)) }
        }}>更新本书检查</Button></Space>
      <Typography.Text type="secondary">{book.rights_check?.message}</Typography.Text>
      {book.rights_check?.evidence.map((item, index) => <Typography.Text key={index}>{item}</Typography.Text>)}
    </Space>}
    {book && <ExercisesEditor book={book} />}
    <DocumentPreview resource={preview} onClose={() => setPreview(null)} />
  </Card>
}

function ExercisesEditor({ book }: { book: Book }) {
  const [chapter, setChapter] = useState(1)
  const [start, setStart] = useState(1)
  const [end, setEnd] = useState(1)
  const [items, setItems] = useState<Exercise[]>([])
  const [status, setStatus] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [verified, setVerified] = useState(false)
  const load = useCallback(() => apiRequest<{ items: Exercise[] }>(`/textbooks/${book.id}/exercises`).then(result => setItems(result.items)), [book.id])
  useEffect(() => { void load().catch(err => setStatus(apiErrorText(err))) }, [load])
  const change = (index: number, changes: Partial<Exercise>) => { setVerified(false); setItems(current => current.map((item, i) => i === index ? { ...item, ...changes } : item)) }
  return <Space orientation="vertical" style={{ width: '100%', marginTop: 16 }}>
    <Typography.Text strong>习题正文索引</Typography.Text>
    <Button disabled={!book.resource_id} loading={busy} onClick={async () => {
      setBusy(true); setVerified(false)
      try {
        const result = await apiRequest<{ message: string; ambiguous: string[] }>(`/textbooks/${book.id}/auto-index`, { method: 'POST' })
        await load(); setStatus([result.message, ...result.ambiguous].join('；'))
      } catch (err) { setStatus(apiErrorText(err)) } finally { setBusy(false) }
    }}>自动定位全书章节与习题（无需页码）</Button>
    <Typography.Text type="secondary">指定习题所在 PDF 文件页码，提取候选后核对题号与公式。保存一次后，出卷会自动引用所选作业对应原题。扫描件或图形公式不保证能提取，可手动补充原文。</Typography.Text>
    <Space wrap>
      第 <InputNumber min={1} max={999} value={chapter} onChange={value => setChapter(value ?? 1)} /> 章，
      文件页 <InputNumber min={1} max={2000} value={start} onChange={value => setStart(value ?? 1)} /> 至
      <InputNumber min={1} max={2000} value={end} onChange={value => setEnd(value ?? 1)} />
      <Button loading={busy} disabled={!book.resource_id} onClick={async () => {
        setBusy(true); setVerified(false)
        try {
          const result = await apiRequest<{ items: Exercise[]; resource_version: number; message: string }>(`/textbooks/${book.id}/extract`, { method: 'POST', body: { chapter, start_page: start, end_page: end } })
          setItems(result.items.map(item => ({ ...item, resource_version: result.resource_version }))); setStatus(result.message)
        } catch (err) { setStatus(apiErrorText(err)) } finally { setBusy(false) }
      }}>提取这些页面的习题</Button>
      <Button onClick={() => { setVerified(false); setItems(current => [...current, { chapter, number: '1', text: '', page: null, resource_version: null }]) }}>手动补充原题</Button>
      <Button onClick={() => void load().catch(err => setStatus(apiErrorText(err)))}>查看已保存习题</Button>
    </Space>
    {items.map((item, index) => <Card size="small" key={index}>
      <Space wrap>第 <InputNumber min={1} value={item.chapter} onChange={value => change(index, { chapter: value ?? 1 })} /> 章，第
        <Input style={{ width: 80 }} value={item.number} onChange={event => change(index, { number: event.target.value })} /> 题，文件页
        <InputNumber min={1} value={item.page} onChange={value => change(index, { page: value })} />
        <Button onClick={() => { setVerified(false); setItems(current => current.filter((_item, i) => i !== index)) }}>从本次核对列表移除</Button></Space>
      <Input.TextArea style={{ marginTop: 8 }} rows={4} value={item.text} onChange={event => change(index, { text: event.target.value })} />
    </Card>)}
    {!!items.length && <><Checkbox checked={verified} onChange={event => setVerified(event.target.checked)}>已核对以上题干、章节、题号与公式，允许作为实际原题发送给出卷 AI</Checkbox>
      <Button type="primary" disabled={!verified} loading={busy} onClick={async () => {
        setBusy(true)
        try {
          if (new Set(items.map(item => `${item.chapter}:${Number(item.number)}`)).size !== items.length) throw new Error('存在重复章节与题号，请先核对并移除重复候选，再保存。')
          for (const item of items) await apiRequest(`/textbooks/${book.id}/exercises`, { method: 'PUT', body: { ...item, confirmed: true } })
          setStatus('习题已保存。生成复习卷时会按所选作业章节和题号自动加入正文。')
        } catch (err) { setStatus(apiErrorText(err)) } finally { setBusy(false) }
      }}>保存已核对的习题</Button></>}
    {status && <Alert type="info" title={status} />}
  </Space>
}

export function TextbooksTab({ courseId }: { courseId: string }) {
  const [books, setBooks] = useState<Book[]>([])
  const [error, setError] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  const resources = useCourseResources(courseId)
  const refetchResources = resources.refetch
  useEffect(() => {
    let active = true
    void apiRequest<{ items: Book[] }>(`/courses/${courseId}/textbooks`).then(result => { if (active) { setBooks(result.items); setError(null) } }).catch(err => { if (active) setError(apiErrorText(err)) })
    return () => { active = false }
  }, [courseId, revision])
  const refresh = useCallback(() => { setRevision(value => value + 1); void refetchResources() }, [refetchResources])
  return <Space orientation="vertical" style={{ width: '100%' }} size={16}>
    <TextbookSettings />
    <Alert type="info" title="教材库：保存准确版本和实际题干" description="可导入你有权使用的教材 PDF / DOCX。教材名称不能代替正文；整本书不会自动发送，只有所选作业匹配的已核对习题会加入出卷材料。备注可指定教材名称或 ISBN，多本匹配时会提示缺口。" />
    {error && <Alert type="error" title={error} />}
    {books.map(book => <BookEditor key={book.id} book={book} courseId={courseId} resources={resources.data?.items ?? []} onSaved={refresh} />)}
    <BookEditor book={null} courseId={courseId} resources={resources.data?.items ?? []} onSaved={refresh} />
  </Space>
}

function TextbookSettings() {
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let active = true
    void apiRequest<{ rights_check_enabled: boolean }>('/preferences/textbooks')
      .then(value => { if (active) setEnabled(value.rights_check_enabled) })
      .catch(err => { if (active) setError(apiErrorText(err)) })
    return () => { active = false }
  }, [])
  return <Space orientation="vertical">
    <Space><Switch aria-label="自动检查教材许可信息" checked={enabled ?? false} disabled={enabled === null || busy} loading={busy}
      onChange={async value => {
        setBusy(true)
        try {
          const saved = await apiRequest<{ rights_check_enabled: boolean }>('/preferences/textbooks', { method: 'PUT', body: { rights_check_enabled: value } })
          setEnabled(saved.rights_check_enabled); setError(null)
        } catch (err) { setError(apiErrorText(err)) } finally { setBusy(false) }
      }} />检查本地教材中的许可声明</Space>
    {error && <Alert type="error" title={error} />}
  </Space>
}
