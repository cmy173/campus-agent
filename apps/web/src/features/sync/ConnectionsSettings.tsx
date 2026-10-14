import { ReloadOutlined } from '@ant-design/icons'
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Descriptions,
  Popconfirm,
  Progress,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import type { TableProps } from 'antd'
import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { ConnectionCapabilities } from '../../components/ConnectionCapabilities'
import { ConnectionWaitingNotice } from '../../components/ConnectionWaitingNotice'
import { JobProgressInline } from '../../components/JobProgressInline'
import { JumpLoginModal } from '../../components/JumpLoginModal'
import { StateView } from '../../components/StateView'
import { ConnectorSetupNotice } from '../../components/ConnectorSetupNotice'
import { PlatformCourseImport } from '../courses/PlatformCourseImport'
import { API_MODE } from '../../lib/api/http'
import {
  ConnectionStatusTag,
  FreshnessTag,
  JobStatusTag,
  jobTypeLabel,
  platformErrorAdvice,
} from '../../components/tags'
import {
  useCancelJob,
  useCheckConnection,
  useConnectionCapabilities,
  useConnectionDetail,
  useConnections,
  useDisableConnection,
  useEnableConnection,
  useJobs,
  useRetryJob,
  useSyncConnection,
} from '../../lib/api/hooks'
import { toApiError } from '../../lib/api/http'
import { fmtDateTime } from '../../lib/time'
import type { Connection, ConnectionStatus, Job } from '../../types/contract'

function ConnectionPanel({ connectionId }: { connectionId: string }) {
  const { message } = AntApp.useApp()
  const queryClient = useQueryClient()
  const detailQuery = useConnectionDetail(connectionId)
  const capabilitiesQuery = useConnectionCapabilities(connectionId)
  const syncConnection = useSyncConnection()
  const disableConnection = useDisableConnection()
  const enableConnection = useEnableConnection()
  const checkConnection = useCheckConnection()
  const [jobId, setJobId] = useState<string | null>(null)
  const [loginOpen, setLoginOpen] = useState(false)
  const [lastCheck, setLastCheck] = useState<{ status: ConnectionStatus; at: string } | null>(null)

  const detail = detailQuery.data

  if (detailQuery.isLoading) {
    return <Typography.Text type="secondary">连接信息加载中…</Typography.Text>
  }
  if (detailQuery.isError || !detail) {
    return (
      <Alert
        type="error"
        showIcon
        title="连接信息加载失败"
        description={toApiError(detailQuery.error).message}
      />
    )
  }

  const canSync = detail.status === 'ok' || detail.status === 'network_error'
  const jumpAction =
    detail.status === 'auth_required' && detail.recovery_action?.type === 'user_login'
      ? detail.recovery_action
      : null

  const handleSync = () => {
    syncConnection.mutate(detail.id, {
      onSuccess: (data) => {
        setJobId(data.job.id)
        message.info('同步已开始，可在下方查看进度')
      },
      onError: (err) => {
        const apiError = toApiError(err)
        if (apiError.code === 'AUTH_EXPIRED') {
          // 会话在点击后失效：显式进入等待登录，不静默、不重试
          message.warning('同步已暂停：需要先完成平台登录（不是失败）')
          void queryClient.invalidateQueries({ queryKey: ['connections'] })
          setLoginOpen(true)
        } else {
          message.error(apiError.message)
        }
      },
    })
  }

  const handleCheck = () => {
    checkConnection.mutate(detail.id, {
      onSuccess: (conn) => {
        setLastCheck({ status: conn.status, at: new Date().toISOString() })
        if (conn.status === 'ok') {
          message.success('连接检查通过：已检测到有效登录')
          setLoginOpen(false)
        } else if (conn.status === 'auth_required') {
          message.warning(
            '复用现有会话后仍未检测到有效登录；请在连接器打开的浏览器窗口内完成平台登录后再检查。如果已直接进入课程页，无须重复登录，可再次检查。',
          )
        } else {
          message.info(`连接检查完成：当前状态「${conn.status}」`)
        }
      },
      onError: (err) => {
        const apiError = toApiError(err)
        if (apiError.code === 'DEPENDENCY_UNAVAILABLE') {
          message.warning('连接检查暂不可用（服务端检查适配器未就绪），请稍后再试')
        } else {
          message.error(apiError.message)
        }
      },
    })
  }

  return (
    <Card size="small">
      <Space orientation="vertical" size={8} style={{ width: '100%' }}>
        <Space wrap size={8}>
          <Typography.Text strong>{detail.display_name}</Typography.Text>
          <Tag>{detail.platform}</Tag>
          <ConnectionStatusTag status={detail.status} />
          <FreshnessTag lastSyncAt={detail.last_sync_at} />
        </Space>
        <Descriptions size="small" column={1}>
          <Descriptions.Item label="最近检查">
            {detail.last_checked_at ? fmtDateTime(detail.last_checked_at) : '未记录'}
          </Descriptions.Item>
        </Descriptions>
        {detail.status === 'auth_required' ? (
          <ConnectionWaitingNotice
            detail={detail}
            lastCheckPendingAt={lastCheck?.status === 'auth_required' ? lastCheck.at : null}
          />
        ) : detail.last_error ? (
          <Alert
            type="error"
            showIcon
            title={detail.last_error.message}
            description={
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                错误码：{detail.last_error.code}
                {detail.last_error.retryable ? '（可重试）' : '（重试无效，需人工处理）'}
              </Typography.Text>
            }
          />
        ) : null}
        {detail.status === 'disabled' && (
          <Typography.Text type="secondary">
            已停用：不再同步与抓取，已有数据保留。重新启用后服务端会重新检查登录状态。
          </Typography.Text>
        )}
        {capabilitiesQuery.data && <ConnectionCapabilities capabilities={capabilitiesQuery.data} />}
        <ConnectorSetupNotice connection={detail} />
        {API_MODE === 'real' && <PlatformCourseImport connection={detail} />}
        {capabilitiesQuery.isError && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            能力清单加载失败（{toApiError(capabilitiesQuery.error).code}）：
            {toApiError(capabilitiesQuery.error).message}
          </Typography.Text>
        )}
        <Space wrap>
          {canSync && (
            <Button size="small" type="primary" loading={syncConnection.isPending} onClick={handleSync}>
              立即同步
            </Button>
          )}
          {jumpAction && (
            <Button size="small" type="primary" onClick={() => setLoginOpen(true)}>
              重新登录
            </Button>
          )}
          {detail.status !== 'disabled' && (
            <Button size="small" loading={checkConnection.isPending} onClick={handleCheck}>
              检查连接
            </Button>
          )}
          {detail.status !== 'disabled' && (
            <Popconfirm
              title="停用该连接？"
              description="停用后停止同步与抓取，已有数据保留，随时可重新启用。不影响学校平台上的任何内容。"
              okText="停用"
              cancelText="取消"
              onConfirm={() =>
                disableConnection.mutate(detail.id, {
                  onSuccess: () => message.success('已停用连接'),
                  onError: (err) => message.error(toApiError(err).message),
                })
              }
            >
              <Button size="small" loading={disableConnection.isPending}>
                停用
              </Button>
            </Popconfirm>
          )}
          {detail.status === 'disabled' && (
            <Button
              size="small"
              loading={enableConnection.isPending}
              onClick={() =>
                enableConnection.mutate(detail.id, {
                  onSuccess: (conn) => {
                    if (conn.status === 'ok') message.success('已重新启用，连接正常')
                    else if (conn.status === 'auth_required') message.warning('已启用：需要先完成平台登录')
                    else message.info(`已启用：当前状态「${conn.status}」`)
                  },
                  onError: (err) => message.error(toApiError(err).message),
                })
              }
            >
              启用（重新检查）
            </Button>
          )}
        </Space>
        {jumpAction && (
          <JumpLoginModal
            action={jumpAction}
            open={loginOpen}
            onClose={() => setLoginOpen(false)}
            onCheck={handleCheck}
            checking={checkConnection.isPending}
          />
        )}
        {jobId && (
          <JobProgressInline
            jobId={jobId}
            compact
            onFinished={() => {
              void queryClient.invalidateQueries({ queryKey: ['connections'] })
              void queryClient.invalidateQueries({ queryKey: ['jobs'] })
              void queryClient.invalidateQueries({ queryKey: ['courses'] })
            }}
          />
        )}
      </Space>
    </Card>
  )
}

const JOB_STATUS_OPTIONS = [
  { value: 'queued', label: '排队中' },
  { value: 'running', label: '进行中' },
  { value: 'succeeded', label: '成功' },
  { value: 'partial_failed', label: '部分失败' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
]

const JOB_TYPE_OPTIONS = [
  { value: 'import', label: '导入' },
  { value: 'parse', label: '解析' },
  { value: 'sync', label: '平台同步' },
  { value: 'export', label: '导出' },
]

export function ConnectionsSettings() {
  const { message } = AntApp.useApp()
  const queryClient = useQueryClient()
  const connectionsQuery = useConnections()
  const [statusFilter, setStatusFilter] = useState<Job['status'] | undefined>()
  const [typeFilter, setTypeFilter] = useState<Job['type'] | undefined>()
  const jobsQuery = useJobs({ status: statusFilter, type: typeFilter })
  const cancelJob = useCancelJob()
  const retryJob = useRetryJob()
  const [trackedJobId, setTrackedJobId] = useState<string | null>(null)

  const jobs = jobsQuery.data?.items ?? []
  const hasActive = jobs.some((j) => j.status === 'queued' || j.status === 'running')
  const jobsRefetch = jobsQuery.refetch

  useEffect(() => {
    if (!hasActive) return
    const timer = setInterval(() => void jobsRefetch(), 2000)
    return () => clearInterval(timer)
  }, [hasActive, jobsRefetch])

  const columns: TableProps<Job>['columns'] = [
    {
      title: '任务',
      dataIndex: 'type',
      render: (_, job) => (
        <Space orientation="vertical" size={2}>
          <Space size={6} wrap>
            <Typography.Text>{jobTypeLabel(job.type)}</Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {job.id}
            </Typography.Text>
            {job.errors.length > 0 && <Tag color="warning">{job.errors.length} 项失败</Tag>}
            {job.cancel_requested && (job.status === 'queued' || job.status === 'running') && (
              <Tag>请求取消中（协作式）</Tag>
            )}
          </Space>
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 170,
      render: (_, job) => (
        <Space orientation="vertical" size={4} style={{ width: 130 }}>
          <JobStatusTag status={job.status} />
          {(job.status === 'running' || job.status === 'queued') &&
            (job.progress !== null ? (
              <Progress percent={Math.round(job.progress * 100)} size="small" status="active" />
            ) : (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                进行中（进度未知）
              </Typography.Text>
            ))}
        </Space>
      ),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 150,
      render: (value: string) => fmtDateTime(value),
    },
    {
      title: '完成时间',
      dataIndex: 'finished_at',
      width: 150,
      render: (value: string | null) => (value ? fmtDateTime(value) : '—'),
    },
    {
      title: '操作',
      width: 150,
      render: (_, job) => {
        const active = job.status === 'queued' || job.status === 'running'
        const cancellable = active && !job.cancel_requested
        const retriable = job.status === 'failed' || job.status === 'partial_failed'
        return (
          <Space>
            {cancellable && (
              <Button
                size="small"
                loading={cancelJob.isPending}
                onClick={() =>
                  cancelJob.mutate(job.id, {
                    onSuccess: (updated) =>
                      message.info(
                        updated.status === 'cancelled'
                          ? '任务已取消'
                          : '已请求取消（协作式）；执行中的步骤结束后停止',
                      ),
                    onError: (err) => message.error(toApiError(err).message),
                  })
                }
              >
                取消
              </Button>
            )}
            {retriable && (
              <Button
                size="small"
                type="primary"
                loading={retryJob.isPending}
                onClick={() =>
                  retryJob.mutate(job.id, {
                    onSuccess: (data) => {
                      message.success('已提交重试（重新发起一个新任务）')
                      setTrackedJobId(data.job.id)
                    },
                    onError: (err) => message.error(toApiError(err).message),
                  })
                }
              >
                重试
              </Button>
            )}
          </Space>
        )
      },
    },
  ]

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        title="同步只读取平台数据"
        description="这里的同步连接器读取课程与资料。需要填写或提交网页材料时，请在对话中提出目标，并通过浏览器工作区检查提交内容。"
      />

      <Card title="平台连接">
        <StateView
          state={
            connectionsQuery.isLoading
              ? 'loading'
              : connectionsQuery.isError
                ? 'error'
                : (connectionsQuery.data?.items.length ?? 0) === 0
                  ? 'empty'
                  : 'ready'
          }
          error={connectionsQuery.error}
          onRetry={() => connectionsQuery.refetch()}
          emptyText="还没有平台连接。连接由本机连接器建立（手动启动的本地组件）；如需接入新平台，请按部署文档配置采集连接器。"
          extra={
            <Typography.Text type="secondary">启动本机连接器后，在这里完成登录与课程导入；无需在对话中填写密码。</Typography.Text>
          }
        >
          <Space orientation="vertical" size={12} style={{ width: '100%' }}>
            {(connectionsQuery.data?.items ?? []).map((conn: Connection) => (
              <ConnectionPanel key={conn.id} connectionId={conn.id} />
            ))}
          </Space>
        </StateView>
      </Card>

      <Card
        title="后台任务"
        extra={
          <Space wrap>
            <Select
              aria-label="按状态筛选"
              size="small"
              style={{ width: 120 }}
              placeholder="状态"
              allowClear
              value={statusFilter}
              onChange={(v) => setStatusFilter(v)}
              options={JOB_STATUS_OPTIONS}
            />
            <Select
              aria-label="按类型筛选"
              size="small"
              style={{ width: 120 }}
              placeholder="类型"
              allowClear
              value={typeFilter}
              onChange={(v) => setTypeFilter(v)}
              options={JOB_TYPE_OPTIONS}
            />
            <Button size="small" icon={<ReloadOutlined />} onClick={() => jobsQuery.refetch()}>
              刷新
            </Button>
          </Space>
        }
      >
        {trackedJobId && (
          <div style={{ marginBottom: 12 }}>
            <JobProgressInline
              jobId={trackedJobId}
              onFinished={() => {
                void queryClient.invalidateQueries({ queryKey: ['jobs'] })
                void queryClient.invalidateQueries({ queryKey: ['connections'] })
              }}
            />
          </div>
        )}
        <StateView
          state={
            jobsQuery.isLoading
              ? 'loading'
              : jobsQuery.isError
                ? 'error'
                : jobs.length === 0
                  ? 'empty'
                  : 'ready'
          }
          error={jobsQuery.error}
          onRetry={() => jobsQuery.refetch()}
          emptyText="没有符合条件的任务"
        >
          <Table
            rowKey="id"
            size="small"
            scroll={{ x: 'max-content' }}
            columns={columns}
            dataSource={jobs}
            pagination={false}
            expandable={{
              rowExpandable: (job) => job.errors.length > 0,
              expandedRowRender: (job) => (
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {job.errors.map((err) => (
                    <li key={`${err.code}-${err.message}`}>
                      {err.message} <Typography.Text type="secondary">（{err.code}）</Typography.Text>
                      {err.retryable ? '（可重试）' : '（暂不可重试）'}
                      {platformErrorAdvice(err.code) && (
                        <Typography.Text
                          type="secondary"
                          style={{ fontSize: 12, display: 'block' }}
                        >
                          {platformErrorAdvice(err.code)}
                        </Typography.Text>
                      )}
                      {err.details ? (
                        <Typography.Text
                          type="secondary"
                          style={{ fontSize: 12, display: 'block' }}
                          copyable={{ text: JSON.stringify(err.details, null, 2) }}
                        >
                          明细：{JSON.stringify(err.details)}
                        </Typography.Text>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ),
            }}
          />
        </StateView>
      </Card>
    </Space>
  )
}
