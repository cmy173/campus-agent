import { Alert, App as AntApp, Button, Card, Progress, Space, Spin, Tag, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { useCancelJob, useJob, useRetryJob } from '../lib/api/hooks'
import { api } from '../lib/api/client'
import { toApiError } from '../lib/api/http'
import { JobStatusTag, jobTypeLabel, platformErrorAdvice } from './tags'

interface JobProgressInlineProps {
  jobId: string
  onFinished?: (status: string) => void
  compact?: boolean
  allowRetry?: boolean
}

/** 后台任务进度：轮询状态、逐项失败展示，按契约字段判断是否可取消 / 重试 */
export function JobProgressInline({ jobId, onFinished, compact, allowRetry = true }: JobProgressInlineProps) {
  const { message } = AntApp.useApp()
  const [trackedId, setTrackedId] = useState(jobId)
  useEffect(() => setTrackedId(jobId), [jobId])

  const jobQuery = useJob(trackedId)
  const cancelJob = useCancelJob()
  const retryJob = useRetryJob()
  const notifiedRef = useRef<string | null>(null)

  const job = jobQuery.data

  useEffect(() => {
    if (!job) return
    const terminal = job.status !== 'queued' && job.status !== 'running'
    const key = `${job.id}:${job.status}`
    if (terminal && notifiedRef.current !== key) {
      notifiedRef.current = key
      onFinished?.(job.status)
    }
  }, [job, onFinished])

  if (jobQuery.isLoading) return <Typography.Text type="secondary">任务状态加载中…</Typography.Text>

  if (jobQuery.isError) {
    const apiError = toApiError(jobQuery.error)
    return (
      <Alert
        type="error"
        showIcon
        title={`任务状态获取失败（${apiError.code}）`}
        description={apiError.message}
      />
    )
  }

  if (!job) return null

  const active = job.status === 'queued' || job.status === 'running'
  const canRetry = job.status === 'failed' || job.status === 'partial_failed'
  const exportRefs = job.result_refs.filter((ref) => ref.type === 'export_file')

  return (
    <Card size="small" style={compact ? undefined : { marginTop: 8 }}>
      <Space orientation="vertical" style={{ width: '100%' }} size={8}>
        <Space wrap>
          <Typography.Text>{jobTypeLabel(job.type)}</Typography.Text>
          <JobStatusTag status={job.status} />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {job.id}
          </Typography.Text>
        </Space>
        {job.progress !== null ? (
          <Progress
            percent={Math.round(job.progress * 100)}
            status={active ? 'active' : job.status === 'failed' ? 'exception' : 'normal'}
          />
        ) : active ? (
          <Space size={8}>
            <Spin size="small" />
            <Typography.Text type="secondary">处理中（进度未知）</Typography.Text>
          </Space>
        ) : null}
        {job.errors.length > 0 && (
          <Alert
            type="warning"
            showIcon
            title="以下项目处理失败"
            description={
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {job.errors.map((err) => (
                  <li key={`${err.code}-${err.message}`}>
                    {err.message}{' '}
                    {err.retryable ? (
                      <Tag color="orange">可重试</Tag>
                    ) : (
                      <Tag>暂不可重试</Tag>
                    )}
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      （{err.code}）
                    </Typography.Text>
                    {platformErrorAdvice(err.code) && (
                      <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block' }}>
                        {platformErrorAdvice(err.code)}
                      </Typography.Text>
                    )}
                  </li>
                ))}
              </ul>
            }
          />
        )}
        {exportRefs.length > 0 && (
          <Space orientation="vertical" size={2}>
            {exportRefs.map((ref) => {
              const url = api.fileDownloadUrl(ref.id)
              return url ? (
                <Typography.Link key={ref.id} href={url} target="_blank" rel="noreferrer">
                  下载导出文件（{ref.id}）
                </Typography.Link>
              ) : (
                <Typography.Text key={ref.id} type="secondary" style={{ fontSize: 12 }}>
                  导出文件已生成（{ref.id}）。模拟模式不提供文件下载；真实模式由服务端受控文件流返回。
                </Typography.Text>
              )
            })}
          </Space>
        )}
        {active && job.cancel_requested && (
          <Typography.Text type="warning">已请求取消，等待当前步骤结束后停止</Typography.Text>
        )}
        <Space>
          {active && !job.cancel_requested && (
            <Button
              size="small"
              loading={cancelJob.isPending}
              onClick={() =>
                cancelJob.mutate(job.id, {
                  onError: (err) => message.error(toApiError(err).message),
                  onSuccess: (updated) =>
                    message.info(updated.status === 'cancelled' ? '任务已取消' : '已请求取消任务'),
                })
              }
            >
              取消任务
            </Button>
          )}
          {canRetry && allowRetry && (
            <Button
              size="small"
              type="primary"
              loading={retryJob.isPending}
              onClick={() =>
                retryJob.mutate(job.id, {
                  onError: (err) => message.error(toApiError(err).message),
                  onSuccess: (data) => {
                    message.success('已提交重试')
                    setTrackedId(data.job.id)
                  },
                })
              }
            >
              重试
            </Button>
          )}
        </Space>
      </Space>
    </Card>
  )
}
