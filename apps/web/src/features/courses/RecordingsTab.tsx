import { PlayCircleOutlined } from '@ant-design/icons'
import { Alert, Button, Modal, Space, Table, Tag, Typography } from 'antd'
import { useState } from 'react'
import { api } from '../../lib/api/client'
import { API_MODE, toApiError } from '../../lib/api/http'
import type { TableProps } from 'antd'
import { StateView } from '../../components/StateView'
import { TranscriptStatusTag } from '../../components/tags'
import { useCourseRecordings } from '../../lib/api/hooks'
import { platformLabel } from '../../lib/platforms'
import { fmtDateTime, fmtDurationSeconds } from '../../lib/time'
import type { Recording } from '../../types/contract'

/** 课程回放：字幕可用性如实展示；字幕文本片段关联待接入（v0.1），无字幕时明确告知只按观看时长安排 */
export function RecordingsTab({ courseId }: { courseId: string }) {
  const recordingsQuery = useCourseRecordings(courseId)
  const [transcriptOpen, setTranscriptOpen] = useState(false)
  const [transcriptBusy, setTranscriptBusy] = useState(false)
  const [transcriptError, setTranscriptError] = useState<string | null>(null)
  const [segments, setSegments] = useState<{ start_seconds: number; end_seconds: number; text: string; translated_text?: string }[]>([])
  async function loadTranscript(id: string) {
    setTranscriptOpen(true); setTranscriptBusy(true); setTranscriptError(null); setSegments([])
    try { setSegments((await api.getRecordingTranscript(id)).segments) }
    catch (e) { setTranscriptError(toApiError(e).message) }
    finally { setTranscriptBusy(false) }
  }

  const items = [...(recordingsQuery.data?.items ?? [])].sort(
    (a, b) => Date.parse(b.started_at ?? '') - Date.parse(a.started_at ?? ''),
  )

  const columns: TableProps<Recording>['columns'] = [
    {
      title: '回放',
      dataIndex: 'title',
      render: (_, r) => (
        <Space orientation="vertical" size={2}>
          <Typography.Text strong>{r.title}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {r.source_ref ? platformLabel(r.source_ref.platform) : '手动整理'}
            {r.started_at ? ` · ${fmtDateTime(r.started_at)}` : ''}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: '时长',
      dataIndex: 'duration_seconds',
      width: 100,
      render: (value: number | null) => (value !== null ? fmtDurationSeconds(value) : '未知'),
    },
    {
      title: '字幕',
      dataIndex: 'transcript_status',
      width: 280,
      render: (_, r) => (
        <Space orientation="vertical" size={2}>
          <Space size={4} wrap>
            <TranscriptStatusTag status={r.transcript_status} />
            {(r.subtitle_languages ?? []).map((lang) => (
              <Tag key={lang}>{lang}</Tag>
            ))}
          </Space>
          {r.transcript_status === 'unavailable' && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              暂无字幕：计划只能按观看时长安排，无法按内容定位
            </Typography.Text>
          )}
          {r.transcript_status === 'processing' && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              字幕处理中；完成后按时间片段定位
            </Typography.Text>
          )}
          {r.transcript_status === 'ready' && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              可在原平台回看；智云课堂支持按需读取已有字幕
            </Typography.Text>
          )}
        </Space>
      ),
    },
    {
      title: '操作',
      width: 140,
      render: (_, r) => <Space orientation="vertical">
        {API_MODE === 'real' && r.source_ref?.platform === 'zykt' && <Button size="small" onClick={() => void loadTranscript(r.id)}>读取已有字幕</Button>}
        {r.canonical_url ? (
          <Button
            size="small"
            type="link"
            icon={<PlayCircleOutlined />}
            href={r.canonical_url}
            target="_blank"
            rel="noreferrer"
          >
            原平台播放
          </Button>
        ) : (
          <Typography.Text type="secondary">无播放链接</Typography.Text>
        )}</Space>,
    },
  ]

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <Modal title="课堂字幕与时间定位" open={transcriptOpen} footer={null} onCancel={() => setTranscriptOpen(false)} width={800}>
        {transcriptBusy ? <Typography.Text>正在读取平台已有字幕…</Typography.Text> : transcriptError ?
          <Alert type="error" title={transcriptError} /> : segments.length === 0 ?
          <Alert type="info" title="暂未读取到字幕，可稍后再次尝试" /> :
          <div style={{ maxHeight: '60vh', overflowY: 'auto' }}>{segments.map((segment, index) =>
            <Typography.Paragraph key={index}><Typography.Text type="secondary">{fmtDurationSeconds(segment.start_seconds)}—{fmtDurationSeconds(segment.end_seconds)} </Typography.Text>{segment.text}
              {segment.translated_text && <><br />{segment.translated_text}</>}
            </Typography.Paragraph>)}</div>}
      </Modal>
      <Alert
        type="info"
        showIcon
        title="回放视频不存储在本工作台"
        description="这里只整理回放元数据与平台已有字幕；观看与播放请在原平台进行。"
      />
      <StateView
        state={
          recordingsQuery.isLoading
            ? 'loading'
            : recordingsQuery.isError
              ? 'error'
              : items.length === 0
                ? 'empty'
                : 'ready'
        }
        error={recordingsQuery.error}
        onRetry={() => recordingsQuery.refetch()}
        emptyText="该课程暂无回放记录"
      >
        <Table
          rowKey="id"
          size="small"
          scroll={{ x: 'max-content' }}
          columns={columns}
          dataSource={items}
          pagination={false}
        />
      </StateView>
    </Space>
  )
}
