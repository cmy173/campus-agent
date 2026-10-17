import { Alert, Button, Card, Input, Radio, Select, Space, Typography } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../../lib/api/client'
import { apiRequest, toApiError } from '../../lib/api/http'
import { JobProgressInline } from '../../components/JobProgressInline'
import type { Job, Recording } from '../../types/contract'
import type { ProviderConfig } from '../../types/review'

type Lesson = Recording & { course_title: string; teacher: string | null }
type Summary = { job: Job; recording_id: string; title: string; course_title: string; detail: string; summary: string | null }
export function ClassroomPage() {
  const [lessons, setLessons] = useState<Lesson[]>([])
  const [providers, setProviders] = useState<ProviderConfig[]>([])
  const [recordingId, setRecordingId] = useState<string>()
  const [providerId, setProviderId] = useState<string>()
  const [detail, setDetail] = useState<'brief' | 'detailed'>('brief')
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [history, setHistory] = useState<Summary[]>([])
  const [jobId, setJobId] = useState<string>()
  const selection = useRef<string | undefined>(undefined)
  const loadHistory = useCallback(async () => {
    try { setHistory((await apiRequest<{ items: Summary[] }>('/classroom/summaries')).items) }
    catch (e) { setError(toApiError(e).message) }
  }, [])
  useEffect(() => {
    let active = true
    Promise.all([apiRequest<{ items: Lesson[] }>('/classroom/recordings'), api.listProviders()])
      .then(([recordings, configs]) => { if (active) { setLessons(recordings.items); setProviders(configs.items) } })
      .catch(e => { if (active) setError(toApiError(e).message) })
    void loadHistory()
    return () => { active = false }
  }, [loadHistory])
  async function readTranscript() {
    if (!recordingId) return
    const chosen = recordingId
    setBusy(true); setError('')
    try {
      const result = await api.getRecordingTranscript(chosen)
      if (selection.current !== chosen) return
      setText(result.segments.map(segment => `[${Math.floor(segment.start_seconds / 60)}:${String(Math.floor(segment.start_seconds % 60)).padStart(2, '0')}] ${segment.text}`).join('\n'))
      if (!result.segments.length) setError('该场次暂无可用转写，请稍后重试，或粘贴你已识别的文字。')
    } catch (e) { if (selection.current === chosen) setError(toApiError(e).message) }
    finally { setBusy(false) }
  }
  async function generate() {
    if (!recordingId || !providerId || !text.trim()) return
    setBusy(true); setError('')
    try {
      const result = await apiRequest<{ job: Job }>(`/recordings/${recordingId}/summary`, {
        method: 'POST', body: { provider_id: providerId, detail, transcript: text },
      })
      setJobId(result.job.id); await loadHistory()
    } catch (e) { setError(toApiError(e).message) }
    finally { setBusy(false) }
  }
  return <Space orientation="vertical" size="middle" style={{ width: '100%' }}>
    <Typography.Title level={3}>智云课堂 · 语音识别与课堂总结</Typography.Title>
    <Card title="选择自己的课堂">
      <Space orientation="vertical" style={{ width: '100%' }}>
        <Select aria-label="选择课堂" showSearch optionFilterProp="label" style={{ width: '100%' }} value={recordingId}
          placeholder="搜索课程、教师或上课日期" disabled={busy}
          options={lessons.map(lesson => ({ value: lesson.id, label: `${lesson.course_title} · ${lesson.teacher || '教师未标注'} · ${lesson.title} · ${lesson.started_at ? new Date(lesson.started_at).toLocaleString('zh-CN') : '日期未标注'}` }))}
          onChange={id => { selection.current = id; setRecordingId(id); setText(''); setError('') }} />
        {!lessons.length && <Typography.Text>暂无已同步的智云课堂场次，请先到<Link to="/sync">同步中心</Link>同步自己的课程回放。</Typography.Text>}
        <Button onClick={readTranscript} disabled={!recordingId || busy}>读取该节课的语音转写</Button>
        <Typography.Text type="secondary">读取的是平台已有语音识别结果，可在下方修正，也可粘贴自行识别的文字。不会把未识别的音频当作已读内容。</Typography.Text>
        <Input.TextArea aria-label="课堂转写" value={text} onChange={event => setText(event.target.value)} rows={12}
          maxLength={160000} showCount disabled={busy} placeholder="查看并修正课堂转写后生成总结；建议保留时间标记。" />
      </Space>
    </Card>
    <Card title="生成课堂总结">
      <Space orientation="vertical" style={{ width: '100%' }}>
        <Select aria-label="总结 API 服务" value={providerId} onChange={setProviderId} style={{ width: '100%' }}
          disabled={busy} placeholder="选择已配置的 API 服务" options={providers.map(provider => ({ value: provider.id, label: `${provider.name} · ${provider.model}`, disabled: !provider.has_api_key }))} />
        <Typography.Text>API 地址、模型和密钥在<Link to="/settings">设置</Link>中配置。</Typography.Text>
        <Radio.Group aria-label="总结详略" value={detail} onChange={event => setDetail(event.target.value)} disabled={busy}
          options={[{ label: '简略版：核心结论与重点', value: 'brief' }, { label: '详细版：知识脉络、推导与例题', value: 'detailed' }]} />
        <Typography.Text type="secondary">点击生成会把上方文字发送到选定 API；长课时会分段总结并合并，可能产生多次调用。</Typography.Text>
        <Button type="primary" onClick={generate} loading={busy} disabled={!recordingId || !providerId || !text.trim() || busy}>生成课堂总结</Button>
      </Space>
    </Card>
    {error && <Alert type="error" showIcon title={error} />}
    {jobId && <JobProgressInline jobId={jobId} onFinished={loadHistory} />}
    <Typography.Title level={4}>已保存的课堂总结</Typography.Title>
    <Button onClick={loadHistory}>刷新总结记录</Button>
    {history.map(item => <Card key={item.job.id} title={`${item.course_title} · ${item.title} · ${item.detail === 'detailed' ? '详细版' : '简略版'}`}>
      {item.summary ? <><Typography.Paragraph copyable={{ text: item.summary }} style={{ whiteSpace: 'pre-wrap' }}>{item.summary}</Typography.Paragraph>
        <Button onClick={() => { const url = URL.createObjectURL(new Blob([item.summary || ''], { type: 'text/markdown;charset=utf-8' })); const anchor = document.createElement('a'); anchor.href = url; anchor.download = `课堂总结-${item.job.id}.md`; anchor.click(); URL.revokeObjectURL(url) }}>下载 Markdown</Button></>
        : <JobProgressInline jobId={item.job.id} onFinished={loadHistory} compact />}
    </Card>)}
  </Space>
}
