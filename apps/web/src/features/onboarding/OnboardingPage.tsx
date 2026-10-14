import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Divider,
  Select,
  Space,
  Steps,
  Tag,
  Typography,
  Upload,
} from 'antd'
import type { UploadProps } from 'antd'
import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ConnectionCapabilities } from '../../components/ConnectionCapabilities'
import { ConnectionWaitingNotice } from '../../components/ConnectionWaitingNotice'
import { JobProgressInline } from '../../components/JobProgressInline'
import { JumpLoginModal } from '../../components/JumpLoginModal'
import { StateView } from '../../components/StateView'
import { ConnectorSetupNotice } from '../../components/ConnectorSetupNotice'
import { PlatformCourseImport } from '../courses/PlatformCourseImport'
import { API_MODE } from '../../lib/api/http'
import { ConnectionStatusTag, FreshnessTag } from '../../components/tags'
import {
  useAvailability,
  useCheckConnection,
  useConnectionCapabilities,
  useConnectionDetail,
  useConnections,
  useCourses,
  useEnableConnection,
  useImportFiles,
  useSyncConnection,
} from '../../lib/api/hooks'
import { toApiError } from '../../lib/api/http'
import { checkUploadFile, LARGE_UPLOAD_HINT, UPLOAD_ACCEPT } from '../../lib/upload'
import { AttachSourceRefButton } from '../courses/AttachSourceRef'
import { AvailabilityForm } from '../settings/AvailabilityForm'

const { Dragger } = Upload

/** 引导第 1 步的连接卡片：以状态驱动动作；登录在连接器窗口内的官方页面完成，本组件无凭据输入 */
function OnboardingConnectionCard({ connectionId }: { connectionId: string }) {
  const { message } = AntApp.useApp()
  const queryClient = useQueryClient()
  const detailQuery = useConnectionDetail(connectionId)
  const capabilitiesQuery = useConnectionCapabilities(connectionId)
  const syncConnection = useSyncConnection()
  const enableConnection = useEnableConnection()
  const checkConnection = useCheckConnection()
  const [jobId, setJobId] = useState<string | null>(null)
  const [loginOpen, setLoginOpen] = useState(false)

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

  const handleCheck = () => {
    checkConnection.mutate(detail.id, {
      onSuccess: (conn) => {
        if (conn.status === 'ok') {
          message.success('连接检查通过：已检测到有效登录')
          setLoginOpen(false)
        } else if (conn.status === 'auth_required') {
          message.warning(
            '仍未检测到有效登录；请在连接器打开的浏览器窗口内完成平台登录后再检查（不会自动重试）',
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
        {detail.status === 'auth_required' ? (
          <ConnectionWaitingNotice detail={detail} />
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
            已停用：不再同步与抓取，已有数据保留；可重新启用。
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
            <Button
              size="small"
              type="primary"
              loading={syncConnection.isPending}
              onClick={() =>
                syncConnection.mutate(detail.id, {
                  onSuccess: (data) => {
                    setJobId(data.job.id)
                    message.info('同步已开始，可在下方查看进度')
                  },
                  onError: (err) => message.error(toApiError(err).message),
                })
              }
            >
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
          {detail.status === 'disabled' && (
            <Button
              size="small"
              loading={enableConnection.isPending}
              onClick={() =>
                enableConnection.mutate(detail.id, {
                  onSuccess: (conn) => {
                    if (conn.status === 'ok') message.success('已重新启用，连接正常')
                    else if (conn.status === 'auth_required')
                      message.warning('已启用：需要先完成平台登录')
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

export function OnboardingPage() {
  const navigate = useNavigate()
  const { message } = AntApp.useApp()
  const [current, setCurrent] = useState(0)

  const connectionsQuery = useConnections()
  const coursesQuery = useCourses()
  const availabilityQuery = useAvailability()
  const importFiles = useImportFiles()

  const [importCourseId, setImportCourseId] = useState<string>()
  const [importJobs, setImportJobs] = useState<{ id: string; label: string }[]>([])

  const connections = connectionsQuery.data?.items ?? []
  const syncable = connections.find((c) => c.status === 'ok' || c.status === 'network_error')
  const courses = coursesQuery.data?.items ?? []
  const unattached = courses.filter((c) => c.source_refs.length === 0)

  const handleImport = (file: File) => {
    if (!importCourseId) {
      message.warning('请先选择导入到哪门课程')
      return
    }
    const check = checkUploadFile(file.name, file.size)
    if (!check.ok) {
      message.error(
        check.reason === 'type'
          ? '仅支持 PDF、PPTX、DOCX 文件；其他格式暂不解析'
          : '文件超过 64MB 上限，请压缩或拆分后重试',
      )
      return
    }
    if (check.large) message.warning(LARGE_UPLOAD_HINT)
    importFiles.mutate(
      {
        input: { course_id: importCourseId },
        files: [file],
        idempotencyKey: `onboarding:${importCourseId}:${file.name}:${file.size}`,
      },
      {
        onSuccess: (data) => {
          message.success('导入已受理，解析在后台完成')
          setImportJobs((prev) => [
            {
              id: data.job.id,
              label: check.large ? `导入（超大文件，解析较慢）：${file.name}` : `导入：${file.name}`,
            },
            ...prev.filter((j) => j.id !== data.job.id),
          ])
        },
        onError: (err) => message.error(toApiError(err).message),
      },
    )
  }

  const beforeUpload: UploadProps['beforeUpload'] = (file) => {
    handleImport(file)
    return Upload.LIST_IGNORE
  }

  const steps = [
    { title: '平台连接', description: '状态与登录衔接' },
    { title: '导入或同步资料', description: '让课程先有内容' },
    { title: '挂接平台课程', description: '未挂接课程手动关联' },
    { title: '设置可用时间', description: '排程的输入' },
  ]

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <Card
        title="首次使用引导"
        extra={
          <Link to="/today">
            <Button size="small" type="link">
              跳过，直接使用
            </Button>
          </Link>
        }
      >
        <Steps
          current={current}
          onChange={setCurrent}
          size="small"
          items={steps.map((s) => ({ title: s.title, content: s.description }))}
        />
      </Card>

      {current === 0 && (
        <Card title="1 · 平台连接" size="small">
          <Space orientation="vertical" size={12} style={{ width: '100%' }}>
            {connectionsQuery.isLoading ? (
              <Typography.Text type="secondary">连接状态加载中…</Typography.Text>
            ) : connections.length === 0 ? (
              <Alert
                type="info"
                showIcon
                title="还没有平台连接"
                description="连接由本机连接器建立（手动启动的本地组件，首次配置见部署文档）；接入后登录在其打开的浏览器窗口内的平台官方页面完成。不连接也可继续：直接手动导入资料。"
              />
            ) : (
              connections.map((c) => (
                <OnboardingConnectionCard key={c.id} connectionId={c.id} />
              ))
            )}
            <Typography.Paragraph type="secondary" style={{ fontSize: 12, margin: 0 }}>
              登录在平台官方页面完成：连接器在弹出的浏览器窗口内打开官方登录页，请在该窗口内自行登录；
              凭据只输入到平台官方页面，本工作台不会收集、代填或保存任何凭据。登录后连接器自动检查并更新状态，
              也可用「检查连接」手动触发；不会自动循环重试。会话只保存在本机连接器，远程访问本网页时无法完成登录。
            </Typography.Paragraph>
          </Space>
        </Card>
      )}

      {current === 1 && (
        <Card title="2 · 导入或同步资料" size="small">
          <Space orientation="vertical" size={12} style={{ width: '100%' }}>
            {syncable && (
              <SyncFromConnection connectionId={syncable.id} name={syncable.display_name} />
            )}

            <Space wrap>
              <Typography.Text>手动导入到：</Typography.Text>
              <Select
                aria-label="选择导入课程"
                style={{ minWidth: 240 }}
                placeholder="选择课程"
                value={importCourseId}
                onChange={setImportCourseId}
                options={courses.map((c) => ({ value: c.id, label: c.title }))}
              />
            </Space>

            <Dragger beforeUpload={beforeUpload} multiple={false} accept={UPLOAD_ACCEPT} showUploadList={false}>
              <p className="ant-upload-text">点击或拖拽课件（PDF / PPTX / DOCX，单文件 64MB 以内）</p>
              <p className="ant-upload-hint">
                超过 30MB 的超大文件解析较慢；重复导入同一文件会自动识别去重；同名新文件保存为新版本并保留历史
              </p>
            </Dragger>

            {importJobs.map((job) => (
              <div key={job.id}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {job.label}
                </Typography.Text>
                <JobProgressInline jobId={job.id} />
              </div>
            ))}
          </Space>
        </Card>
      )}

      {current === 2 && (
        <Card title="3 · 挂接平台课程" size="small">
          <Space orientation="vertical" size={12} style={{ width: '100%' }}>
            <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
              平台课程与本地课程的关联统一为「手动挂接」（v0.1 无自动映射）：为未挂接课程填写平台课程标识；
              若该来源已属于其他课程，服务端会拒绝并明确提示。
            </Typography.Paragraph>
            {coursesQuery.isLoading ? (
              <Typography.Text type="secondary">课程加载中…</Typography.Text>
            ) : unattached.length === 0 ? (
              <Alert
                type="success"
                showIcon
                title="所有课程都已挂接平台来源"
                description="没有需要处理的未挂接课程。"
              />
            ) : (
              unattached.map((c) => (
                <Space key={c.id} wrap size={8}>
                  <Typography.Text>{c.title}</Typography.Text>
                  <Tag color="warning">未挂接</Tag>
                  <AttachSourceRefButton courseId={c.id} courseTitle={c.title} size="small" type="primary">
                    挂接平台来源
                  </AttachSourceRefButton>
                </Space>
              ))
            )}
            <Divider style={{ margin: '4px 0' }} />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              已挂接课程可在课程详情页查看平台来源，或继续补充其他平台的来源。
            </Typography.Text>
          </Space>
        </Card>
      )}

      {current === 3 && (
        <StateView
          state={
            availabilityQuery.isLoading ? 'loading' : availabilityQuery.isError ? 'error' : 'ready'
          }
          error={availabilityQuery.error}
          onRetry={() => availabilityQuery.refetch()}
        >
          {availabilityQuery.data && (
            <AvailabilityForm key={availabilityQuery.data.updated_at} initial={availabilityQuery.data} />
          )}
        </StateView>
      )}

      <Space>
        <Button disabled={current === 0} onClick={() => setCurrent((c) => c - 1)}>
          上一步
        </Button>
        {current < steps.length - 1 ? (
          <Button type="primary" onClick={() => setCurrent((c) => c + 1)}>
            下一步
          </Button>
        ) : (
          <Button type="primary" onClick={() => navigate('/today')}>
            完成引导，去今日页
          </Button>
        )}
      </Space>
    </Space>
  )
}

/** 引导第 2 步内的同步入口（独立组件以便使用 mutation 与 job 进度状态） */
function SyncFromConnection({ connectionId, name }: { connectionId: string; name: string }) {
  const { message } = AntApp.useApp()
  const syncConnection = useSyncConnection()
  const [jobId, setJobId] = useState<string | null>(null)
  return (
    <Space orientation="vertical" size={8} style={{ width: '100%' }}>
      <Space wrap>
        <Typography.Text>从「{name}」同步最新数据：</Typography.Text>
        <Button
          size="small"
          loading={syncConnection.isPending}
          onClick={() =>
            syncConnection.mutate(connectionId, {
              onSuccess: (data) => {
                setJobId(data.job.id)
                message.info('同步已开始')
              },
              onError: (err) => message.error(toApiError(err).message),
            })
          }
        >
          立即同步
        </Button>
      </Space>
      {jobId && <JobProgressInline jobId={jobId} />}
    </Space>
  )
}
