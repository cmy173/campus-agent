import { DeleteOutlined, ThunderboltOutlined } from '@ant-design/icons'
import {
  Alert,
  App as AntApp,
  Button,
  Drawer,
  InputNumber,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { TableProps } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { JobProgressInline } from '../../components/JobProgressInline'
import { DuePrecisionHint, TaskStatusTag, taskTypeLabel } from '../../components/tags'
import {
  useBuildTasks,
  useCourses,
  useDeleteTask,
  useJob,
  usePatchTask,
  useTasks,
} from '../../lib/api/hooks'
import { toApiError } from '../../lib/api/http'
import { platformLabel } from '../../lib/platforms'
import { fmtDateTime, fmtMinutes } from '../../lib/time'
import type { LearningTask } from '../../types/contract'

const ALL_SCOPE = '__all__'

const WARNING_META: Record<string, { text: string; color: string }> = {
  DUPLICATE_CONTENT: { text: '内容重复，已合并', color: 'blue' },
  TASK_VERSION_UPDATED: { text: '资料有新版本，已对齐', color: 'blue' },
  UNSUPPORTED_EXTENT: { text: '无法按页数估算，用默认时长', color: 'orange' },
  MISSING_DURATION: { text: '缺少时长信息', color: 'orange' },
  EXISTING_TASK_UNMATCHED: { text: '已有任务与资料不再匹配', color: 'orange' },
  DUPLICATE_TASK_ID: { text: '重复任务记录', color: 'red' },
  HINT_UNRESOLVED: { text: '提示无法解析', color: 'default' },
}

const PROVENANCE_META: Record<LearningTask['fact_source'], { text: string; color: string; hint: string }> = {
  platform: { text: '事实 · 平台数据', color: 'blue', hint: '耗时取自平台提供的页数/时长等事实' },
  user: { text: '事实 · 人工设定', color: 'green', hint: '耗时由人工填写，重新生成时不会被覆盖' },
  inferred: { text: '推断 · 规则估算', color: 'orange', hint: '由页数/时长按规则推算，可能不准确，可修改' },
}

function shortenId(value: string, max = 34): string {
  return value.length > max ? `${value.slice(0, max - 1)}…` : value
}

interface BuildTasksDrawerProps {
  open: boolean
  onClose: () => void
  /** 打开时的默认范围：课程页传具体课程；缺省（undefined/null）= 全部课程，可在抽屉内切换 */
  defaultCourseId?: string | null
  /** 入口补充说明（如单资源入口需注明“按课程范围生成”） */
  notice?: string
}

/** 由资料/作业/回放生成候选学习任务：触发 → 后台任务 → 告警如实展示 → 调整/归档 */
export function BuildTasksDrawer({ open, onClose, defaultCourseId, notice }: BuildTasksDrawerProps) {
  const { message } = AntApp.useApp()
  const coursesQuery = useCourses()
  const buildTasks = useBuildTasks()
  const patchTask = usePatchTask()
  const deleteTask = useDeleteTask()

  const [scope, setScope] = useState<string | null>(defaultCourseId ?? null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [snapshotIds, setSnapshotIds] = useState<Set<string> | null>(null)
  const [draftMinutes, setDraftMinutes] = useState<Record<string, number>>({})
  const [savingTaskId, setSavingTaskId] = useState<string | null>(null)
  const [archivingTaskId, setArchivingTaskId] = useState<string | null>(null)
  const lastAppliedScope = useRef<string | null | undefined>(undefined)
  const knownJobIds = useRef<Set<string>>(new Set())

  const listQuery = useTasks(
    scope !== null ? { course_id: scope } : undefined,
    { enabled: open },
  )
  const jobQuery = useJob(jobId)
  const job = jobQuery.data

  const resetSession = () => {
    setJobId(null)
    setSnapshotIds(null)
    setDraftMinutes({})
  }

  useEffect(() => {
    if (!open) return
    const next = defaultCourseId ?? null
    if (lastAppliedScope.current !== next) {
      lastAppliedScope.current = next
      setScope(next)
      resetSession()
    }
  }, [open, defaultCourseId])

  const courses = coursesQuery.data?.items ?? []
  const scopeLabel =
    scope === null ? '全部课程' : (courses.find((c) => c.id === scope)?.title ?? scope)

  const handleBuild = () => {
    if (!listQuery.data) {
      message.warning('任务列表尚未加载完成，请稍候再触发')
      return
    }
    setSnapshotIds(new Set(listQuery.data.items.map((t) => t.id)))
    setDraftMinutes({})
    buildTasks.mutate(
      { course_id: scope },
      {
        onSuccess: (data) => {
          const nextJob = data.job
          const joining = knownJobIds.current.has(nextJob.id) || nextJob.status === 'running'
          knownJobIds.current.add(nextJob.id)
          setJobId(nextJob.id)
          if (joining) {
            message.info('已有同一范围的生成任务在进行中，已接入该任务（不会重复创建）')
          } else {
            message.success('已开始生成候选任务')
          }
        },
        onError: (err) => message.error(toApiError(err).message),
      },
    )
  }

  const terminal =
    job !== undefined &&
    (job.status === 'succeeded' ||
      job.status === 'partial_failed' ||
      job.status === 'failed' ||
      job.status === 'cancelled')
  const resultIds = terminal ? job.result_refs.filter((r) => r.type === 'task').map((r) => r.id) : []
  const tasksById = new Map((listQuery.data?.items ?? []).map((t) => [t.id, t]))
  const resultTasks = resultIds
    .map((id) => tasksById.get(id))
    .filter((t): t is LearningTask => t !== undefined)
  const missingCount = resultIds.length - resultTasks.length
  const createdCount = snapshotIds
    ? resultIds.filter((id) => !snapshotIds.has(id)).length
    : resultIds.length
  const updatedCount = resultIds.length - createdCount

  const saveMinutes = (task: LearningTask) => {
    const minutes = draftMinutes[task.id]
    if (minutes === undefined || minutes === task.estimated_minutes) return
    if (!Number.isInteger(minutes) || minutes < 1) {
      message.warning('预计耗时需为正整数（分钟）')
      return
    }
    setSavingTaskId(task.id)
    patchTask.mutate(
      { taskId: task.id, patch: { estimated_minutes: minutes }, revision: task.revision },
      {
        onSuccess: (updated) => {
          message.success(`已保存「${updated.title}」预计耗时：${fmtMinutes(updated.estimated_minutes)}`)
          setDraftMinutes((prev) => {
            const next = { ...prev }
            delete next[task.id]
            return next
          })
        },
        onError: (err) => {
          const apiError = toApiError(err)
          message.error(
            apiError.code === 'REVISION_MISMATCH'
              ? '任务已被其他操作更新，已刷新列表，请确认后重试'
              : apiError.message,
          )
          void listQuery.refetch()
        },
        onSettled: () => setSavingTaskId(null),
      },
    )
  }

  const archiveTask = (task: LearningTask) => {
    setArchivingTaskId(task.id)
    deleteTask.mutate(task.id, {
      onSuccess: () => message.success(`已归档「${task.title}」`),
      onError: (err) => message.error(toApiError(err).message),
      onSettled: () => setArchivingTaskId(null),
    })
  }

  const columns: TableProps<LearningTask>['columns'] = [
    {
      title: '任务',
      width: 260,
      render: (_, t) => (
        <Space orientation="vertical" size={2}>
          <Space wrap size={6}>
            <Typography.Text strong>{t.title}</Typography.Text>
            <TaskStatusTag status={t.status} />
          </Space>
          <Space wrap size={6}>
            {t.match_key && (
              <Tooltip title={`匹配键（重复生成据此对齐）：${t.match_key}`}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {shortenId(t.match_key)}
                </Typography.Text>
              </Tooltip>
            )}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {t.due_at ? `截止 ${fmtDateTime(t.due_at, undefined, 'M月D日 HH:mm')}` : '无截止时间'}
            </Typography.Text>
            <DuePrecisionHint precision={t.due_precision} />
          </Space>
        </Space>
      ),
    },
    {
      title: '本次',
      width: 70,
      render: (_, t) =>
        snapshotIds === null ? (
          <Typography.Text type="secondary">—</Typography.Text>
        ) : snapshotIds.has(t.id) ? (
          <Tag color="blue">更新</Tag>
        ) : (
          <Tag color="green">新建</Tag>
        ),
    },
    {
      title: '类型',
      width: 70,
      render: (_, t) => taskTypeLabel(t.type),
    },
    {
      title: '预计耗时',
      width: 180,
      render: (_, t) => {
        const draft = draftMinutes[t.id]
        const changed = draft !== undefined && draft !== t.estimated_minutes
        return (
          <Space size={4}>
            <InputNumber
              size="small"
              min={1}
              max={1440}
              value={draft ?? t.estimated_minutes}
              onChange={(v) =>
                setDraftMinutes((prev) => ({
                  ...prev,
                  [t.id]: typeof v === 'number' ? v : t.estimated_minutes,
                }))
              }
              disabled={t.archived_at !== null}
            />
            {changed && (
              <Button
                size="small"
                type="link"
                loading={patchTask.isPending && savingTaskId === t.id}
                onClick={() => saveMinutes(t)}
              >
                保存
              </Button>
            )}
          </Space>
        )
      },
    },
    {
      title: '时长依据',
      width: 130,
      render: (_, t) => {
        const meta = PROVENANCE_META[t.fact_source]
        return (
          <Tooltip title={meta.hint}>
            <Tag color={meta.color}>{meta.text}</Tag>
          </Tooltip>
        )
      },
    },
    {
      title: '来源',
      width: 130,
      render: (_, t) =>
        t.source_refs.length === 0 ? (
          <Tag>无来源标注</Tag>
        ) : (
          <Space orientation="vertical" size={2}>
            {t.source_refs.map((ref) =>
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
                <Tooltip key={`${ref.platform}:${ref.external_id}`} title={ref.external_id}>
                  <Tag>{platformLabel(ref.platform)}</Tag>
                </Tooltip>
              ),
            )}
          </Space>
        ),
    },
    {
      title: '操作',
      width: 90,
      render: (_, t) => (
        <Popconfirm
          title="归档该任务？"
          description="归档后不参与排程与默认列表，历史与反馈保留；后续重复生成不会复活它。"
          okText="确认归档"
          cancelText="取消"
          onConfirm={() => archiveTask(t)}
        >
          <Button
            size="small"
            type="link"
            danger
            icon={<DeleteOutlined />}
            loading={deleteTask.isPending && archivingTaskId === t.id}
          >
            归档
          </Button>
        </Popconfirm>
      ),
    },
  ]

  return (
    <Drawer
      title="由资料生成学习任务"
      open={open}
      onClose={onClose}
      size={860}
      destroyOnHidden
    >
      <Space orientation="vertical" size={12} style={{ width: '100%' }}>
        <Alert
          type="info"
          showIcon
          title="生成说明"
          description={
            <Space orientation="vertical" size={2}>
              <Typography.Text style={{ fontSize: 13 }}>
                系统枚举范围内（全部课程或指定课程）的资料、作业与回放，按稳定匹配键（match_key）生成或对齐学习任务；
                重复触发不会产生重复任务，你调整的耗时与归档的条目不会被后续生成覆盖。
              </Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                完成标准（completion_criteria）：拆解引擎已生成默认完成标准，但当前任务接口未持久化/返回该字段，
                界面暂无法展示（已列入待协作事项，契约补充后展示）。
              </Typography.Text>
            </Space>
          }
        />
        {notice && <Alert type="warning" showIcon title={notice} />}

        <Space wrap>
          <Typography.Text>生成范围</Typography.Text>
          <Select
            aria-label="生成范围（课程）"
            style={{ minWidth: 240 }}
            value={scope ?? ALL_SCOPE}
            loading={coursesQuery.isLoading}
            onChange={(value) => {
              const next = value === ALL_SCOPE ? null : value
              lastAppliedScope.current = next
              setScope(next)
              resetSession()
            }}
            options={[
              { value: ALL_SCOPE, label: '全部课程' },
              ...courses.map((c) => ({ value: c.id, label: c.title })),
            ]}
          />
          <Button
            type="primary"
            icon={<ThunderboltOutlined />}
            loading={buildTasks.isPending}
            disabled={!listQuery.isSuccess}
            onClick={handleBuild}
          >
            生成候选任务
          </Button>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {listQuery.isSuccess ? `该范围现有 ${listQuery.data.items.length} 项任务` : '任务列表加载中…'}
          </Typography.Text>
        </Space>

        {jobId && (
          <div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              生成范围：{scopeLabel}
            </Typography.Text>
            <JobProgressInline
              jobId={jobId}
              onFinished={(status) => {
                if (status === 'succeeded' || status === 'partial_failed') void listQuery.refetch()
              }}
            />
          </div>
        )}

        {!jobId && (
          <Typography.Text type="secondary">
            尚未触发生成。点击「生成候选任务」后，这里会展示候选任务、来源标注、时长依据与生成告警。
          </Typography.Text>
        )}

        {terminal && job.status === 'succeeded' && resultIds.length === 0 && (
          <Alert
            type="success"
            showIcon
            title="本次没有新建或更新任务"
            description="重复触发不会产生重复任务——系统已按 match_key 对齐既有任务；对耗时/归档的人工修改保持有效。"
          />
        )}

        {terminal &&
          (job.status === 'succeeded' || job.status === 'partial_failed') &&
          resultIds.length > 0 && (
            <Alert
              type="success"
              showIcon
              title={`本次生成：新建 ${createdCount} 项、更新 ${updatedCount} 项${
                missingCount > 0 ? `（另有 ${missingCount} 项超出当前列表加载范围）` : ''
              }`}
              description="任务已按匹配键落库；可在下方直接调整耗时或归档，改动不会被后续生成覆盖。生成后可到计划页预览并排入时间块。"
            />
          )}

        {job && job.warnings.length > 0 && (
          <Alert
            type="warning"
            showIcon
            title={`生成告警（${job.warnings.length} 项，如实展示）`}
            description={
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {job.warnings.map((w, i) => {
                  const meta = WARNING_META[w.code]
                  return (
                    <li key={`${w.code}-${i}`}>
                      <Space wrap size={6}>
                        <Tag color={meta?.color ?? 'default'}>{meta?.text ?? w.code}</Tag>
                        <Typography.Text>{w.message}</Typography.Text>
                        <Typography.Text code style={{ fontSize: 12 }}>
                          {w.code}
                        </Typography.Text>
                        {w.task_ids.length > 0 && (
                          <Tooltip title={w.task_ids.join('\n')}>
                            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                              关联 {w.task_ids.length} 项
                            </Typography.Text>
                          </Tooltip>
                        )}
                      </Space>
                    </li>
                  )
                })}
              </ul>
            }
          />
        )}

        {terminal && resultTasks.length > 0 && (
          <Table
            rowKey="id"
            size="small"
            scroll={{ x: 'max-content' }}
            columns={columns}
            dataSource={resultTasks}
            pagination={false}
            title={() => (
              <Space wrap>
                <Typography.Text strong>候选任务</Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  可改预计耗时、可归档；来源标注与时长依据来自落库字段
                </Typography.Text>
              </Space>
            )}
          />
        )}
      </Space>
    </Drawer>
  )
}
