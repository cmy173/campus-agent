import { InboxOutlined, ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons'
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Drawer,
  Empty,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
} from 'antd'
import type { TableProps, UploadProps } from 'antd'
import { useState } from 'react'
import { ChunksDrawer } from '../../components/ChunksDrawer'
import { DocumentPreview } from '../../components/DocumentPreview'
import { JobProgressInline } from '../../components/JobProgressInline'
import { StateView } from '../../components/StateView'
import { ParseStatusTag } from '../../components/tags'
import { BuildTasksDrawer } from '../tasks/BuildTasksDrawer'
import {
  useCourseResources,
  useImportFiles,
  useReparseResource,
  useResourceVersions,
} from '../../lib/api/hooks'
import { toApiError } from '../../lib/api/http'
import { platformLabel } from '../../lib/platforms'
import { fmtDateTime } from '../../lib/time'
import {
  checkUploadFile,
  fmtFileSize,
  LARGE_UPLOAD_BYTES,
  LARGE_UPLOAD_HINT,
  UPLOAD_ACCEPT,
} from '../../lib/upload'
import type { Resource } from '../../types/contract'

const { Dragger } = Upload

interface TrackedJob {
  id: string
  label: string
}

function VersionHistoryDrawer({
  resource,
  onClose,
}: {
  resource: Resource | null
  onClose: () => void
}) {
  const versionsQuery = useResourceVersions(resource?.id)
  const versions = [...(versionsQuery.data?.items ?? [])].sort((a, b) => b.version - a.version)
  return (
    <Drawer
      title={`版本历史 · ${resource?.title ?? ''}`}
      open={resource !== null}
      onClose={onClose}
      size={420}
    >
      {versionsQuery.isLoading && <Typography.Text type="secondary">加载中…</Typography.Text>}
      {versionsQuery.isError && (
        <Typography.Text type="danger">
          版本加载失败（{toApiError(versionsQuery.error).code}）：{toApiError(versionsQuery.error).message}
        </Typography.Text>
      )}
      {versionsQuery.data &&
        (versions.length === 0 ? (
          <Empty description="暂无版本记录" />
        ) : (
          <Space orientation="vertical" size={12} style={{ width: '100%' }}>
            {versions.map((v) => (
              <Card key={v.version} size="small">
                <Space orientation="vertical" size={4} style={{ width: '100%' }}>
                  <Space wrap size={8}>
                    <Typography.Text strong>v{v.version}</Typography.Text>
                    {resource && v.version === resource.version && <Tag color="blue">当前版本</Tag>}
                    <ParseStatusTag status={v.parse_status} />
                  </Space>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    抓取时间：{v.fetched_at ? fmtDateTime(v.fetched_at) : '未记录'} · 大小：
                    {fmtFileSize(v.size_bytes)}
                  </Typography.Text>
                </Space>
              </Card>
            ))}
          </Space>
        ))}
    </Drawer>
  )
}

/** 课程资料：导入/上传、解析状态与失败恢复、版本与来源、chunk_count 徽标与片段抽屉 */
export function ResourcesTab({ courseId }: { courseId: string }) {
  const { message } = AntApp.useApp()
  const resourcesQuery = useCourseResources(courseId)
  const importFiles = useImportFiles()
  const reparse = useReparseResource()

  const [chunksFor, setChunksFor] = useState<Resource | null>(null)
  const [previewFor, setPreviewFor] = useState<Resource | null>(null)
  const [versionsFor, setVersionsFor] = useState<Resource | null>(null)
  const [jobs, setJobs] = useState<TrackedJob[]>([])
  const [buildOpen, setBuildOpen] = useState(false)
  const [buildNotice, setBuildNotice] = useState<string | undefined>(undefined)

  const openBuildFromResource = (resource: Resource) => {
    setBuildNotice(
      `单资源入口说明：当前生成接口仅支持课程范围（BuildTasksRequest 仅 course_id），无法只针对「${resource.title}」；` +
        '本次将以整门课程为单位生成，可在确认界面查看候选任务。',
    )
    setBuildOpen(true)
  }

  const openBuildFromCourse = () => {
    setBuildNotice(undefined)
    setBuildOpen(true)
  }

  const items = (resourcesQuery.data?.items ?? [])
    .filter((r) => r.archived_at === null)
    .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))

  const pushJob = (id: string, label: string) => {
    setJobs((prev) => [{ id, label }, ...prev.filter((j) => j.id !== id)])
  }

  const handleFile = (file: File) => {
    const check = checkUploadFile(file.name, file.size)
    if (!check.ok) {
      message.error(
        check.reason === 'type'
          ? '仅支持 PDF、PPTX、DOCX 文件；其他格式暂不解析'
          : '文件超过当前上传上限，请在设置 → 文件大小中调整',
      )
      return
    }
    if (check.large) message.warning(LARGE_UPLOAD_HINT)
    importFiles.mutate(
      {
        input: { course_id: courseId },
        files: [file],
        idempotencyKey: `${courseId}:${file.name}:${file.size}`,
      },
      {
        onSuccess: (data) => {
          message.success('导入已受理，解析在后台完成')
          pushJob(
            data.job.id,
            check.large ? `导入（超大文件，解析较慢）：${file.name}` : `导入：${file.name}`,
          )
        },
        onError: (err) => message.error(toApiError(err).message),
      },
    )
  }

  const beforeUpload: UploadProps['beforeUpload'] = (file) => {
    handleFile(file)
    return Upload.LIST_IGNORE
  }

  const retryParse = (resource: Resource) => {
    reparse.mutate(resource.id, {
      onSuccess: (data) => {
        pushJob(data.job.id, `重新解析：${resource.title}`)
        message.info('已开始重新解析')
      },
      onError: (err) => message.error(toApiError(err).message),
    })
  }

  const columns: TableProps<Resource>['columns'] = [
    { title: '离线阅读', width: 110, render: (_, resource) => <Button size="small" disabled={!resource.file_id}
      onClick={() => setPreviewFor(resource)}>查看原文</Button> },
    {
      title: '标题',
      dataIndex: 'title',
      render: (_, r) => (
        <Space orientation="vertical" size={2}>
          <Space wrap size={4}>
            <Typography.Text strong>{r.title}</Typography.Text>
            <Tag color="blue">v{r.version}</Tag>
            {r.source_refs.length > 1 && (
              <Tooltip title={r.source_refs.map((s) => `${platformLabel(s.platform)} ${s.external_id}`).join('\n')}>
                <Tag color="cyan">已合并 {r.source_refs.length} 个来源</Tag>
              </Tooltip>
            )}
            {r.version > 1 && (
              <Button size="small" type="link" onClick={() => setVersionsFor(r)}>
                版本历史
              </Button>
            )}
          </Space>
          {r.parse_status === 'failed' && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              解析失败；错误明细见对应任务的批量失败项
            </Typography.Text>
          )}
        </Space>
      ),
    },
    {
      title: '类型',
      dataIndex: 'kind',
      width: 80,
      render: (kind: Resource['kind']) => kind.toUpperCase(),
    },
    {
      title: '大小',
      dataIndex: 'size_bytes',
      width: 130,
      render: (_, r) => (
        <Space size={4} wrap>
          <Typography.Text>{fmtFileSize(r.size_bytes)}</Typography.Text>
          {r.size_bytes !== null && r.size_bytes > LARGE_UPLOAD_BYTES && (
            <Tooltip title="超大文件（超过 30MB）解析较慢；解析在后台进行，失败可在解析状态列重试解析">
              <Tag color="orange">超大文件</Tag>
            </Tooltip>
          )}
        </Space>
      ),
    },
    {
      title: '解析状态',
      dataIndex: 'parse_status',
      width: 170,
      render: (_, r) => (
        <Space orientation="vertical" size={2}>
          <ParseStatusTag status={r.parse_status} />
          {(r.parse_status === 'failed' || r.parse_status === 'ready') && (
            <Button
              size="small"
              icon={<ReloadOutlined />}
              loading={reparse.isPending}
              onClick={() => retryParse(r)}
            >
              {r.parse_status === 'failed' ? '重试解析' : '重新解析'}
            </Button>
          )}
        </Space>
      ),
    },
    {
      title: '来源',
      width: 150,
      render: (_, r) =>
        r.source_refs.length === 0 ? (
          <Tag>手动导入</Tag>
        ) : (
          <Space orientation="vertical" size={2}>
            {r.source_refs.map((ref) =>
              ref.canonical_url ? (
                <Typography.Link
                  key={`${ref.platform}:${ref.external_id}`}
                  href={ref.canonical_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  {platformLabel(ref.platform)}
                </Typography.Link>
              ) : (
                <Tag key={`${ref.platform}:${ref.external_id}`}>{platformLabel(ref.platform)}</Tag>
              ),
            )}
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
      title: '文本片段',
      width: 130,
      render: (_, r) =>
        r.chunk_count !== null ? (
          <Button size="small" type="link" onClick={() => setChunksFor(r)}>
            查看片段（{r.chunk_count}）
          </Button>
        ) : (
          <Typography.Text type="secondary">—</Typography.Text>
        ),
    },
    {
      title: '任务',
      width: 110,
      render: (_, r) => (
        <Tooltip title="生成候选学习任务；当前接口仅支持课程范围，不支持仅针对单条资料">
          <Button
            size="small"
            type="link"
            icon={<ThunderboltOutlined />}
            onClick={() => openBuildFromResource(r)}
          >
            生成任务
          </Button>
        </Tooltip>
      ),
    },
  ]

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        title="导入仅用于本地学习整理，不会修改平台内容"
        description="支持 PDF / PPTX / DOCX（大小上限可在设置中调整），解析在后台完成；超过 30MB 的超大文件解析较慢。同一文件重复导入会被识别并跳过，同名新文件保存为新版本并保留历史。"
      />

      <Dragger beforeUpload={beforeUpload} multiple={false} accept={UPLOAD_ACCEPT} showUploadList={false}>
        <p className="ant-upload-drag-icon">
          <InboxOutlined />
        </p>
        <p className="ant-upload-text">点击或拖拽文件到这里导入</p>
        <p className="ant-upload-hint">导入后可在下方查看解析状态与文本片段</p>
      </Dragger>

      {jobs.map((job) => (
        <div key={job.id}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {job.label}
          </Typography.Text>
          <JobProgressInline
            jobId={job.id}
            onFinished={(status) => {
              if (status === 'succeeded' || status === 'partial_failed') void resourcesQuery.refetch()
            }}
          />
        </div>
      ))}

      <Card
        title="资料列表"
        extra={
          <Button
            size="small"
            type="primary"
            icon={<ThunderboltOutlined />}
            onClick={openBuildFromCourse}
          >
            由资料生成学习任务
          </Button>
        }
      >
        <StateView
          state={
            resourcesQuery.isLoading
              ? 'loading'
              : resourcesQuery.isError
                ? 'error'
                : items.length === 0
                  ? 'empty'
                  : 'ready'
          }
          error={resourcesQuery.error}
          onRetry={() => resourcesQuery.refetch()}
          emptyText="还没有资料，可在上方导入文件，或到同步中心同步平台资料"
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
      </Card>

      <ChunksDrawer
        resourceId={chunksFor?.id}
        title={chunksFor?.title ?? ''}
        open={chunksFor !== null}
        onClose={() => setChunksFor(null)}
        url={chunksFor?.source_refs[0]?.canonical_url ?? null}
      />

      <VersionHistoryDrawer resource={versionsFor} onClose={() => setVersionsFor(null)} />
      <DocumentPreview resource={previewFor} onClose={() => setPreviewFor(null)} />

      <BuildTasksDrawer
        open={buildOpen}
        onClose={() => setBuildOpen(false)}
        defaultCourseId={courseId}
        notice={buildNotice}
      />
    </Space>
  )
}
