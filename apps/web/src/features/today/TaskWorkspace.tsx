import { ClockCircleOutlined, ExportOutlined, ThunderboltOutlined } from '@ant-design/icons'
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Listy,
  Modal,
  Radio,
  Select,
  Space,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { useMemo, useState } from 'react'
import { WorkspaceLink as Link } from '../agent/WorkspaceLink'
import { StateView } from '../../components/StateView'
import {
  DuePrecisionHint,
  SourceTag,
  SubmissionStatusTag,
  TaskStatusTag,
  taskTypeLabel,
} from '../../components/tags'
import { BuildTasksDrawer } from '../tasks/BuildTasksDrawer'
import {
  useConnections,
  useCourses,
  useCurrentPlan,
  useGlobalAssignments,
  useSubmitFeedback,
  useTasks,
} from '../../lib/api/hooks'
import { toApiError } from '../../lib/api/http'
import { linkedToSubmittedAssignment, sourceRefKey, submittedAssignmentRefKeys } from '../../lib/matching'
import { filterTodayTasks } from '../../lib/tasks'
import {
  dayKey,
  fmtDateTime,
  fmtMinutes,
  fmtRelativeToNow,
  fmtTimeRange,
  isStale,
  todayKey,
} from '../../lib/time'
import type { Assignment, FeedbackOutcome, LearningTask, PlanBlock } from '../../types/contract'

const OUTCOME_LABELS: Record<FeedbackOutcome, string> = {
  completed: '已完成',
  partial: '部分完成',
  skipped: '本次未做',
}

interface FeedbackFormValues {
  outcome: FeedbackOutcome
  actual_minutes?: number | null
  difficulty?: number | null
  note?: string | null
}

export function TaskWorkspace() {
  const { message } = AntApp.useApp()
  const tasksQuery = useTasks()
  const planQuery = useCurrentPlan()
  const coursesQuery = useCourses()
  const connectionsQuery = useConnections()
  const submitFeedback = useSubmitFeedback()

  const [feedbackTask, setFeedbackTask] = useState<LearningTask | null>(null)
  const [feedbackForm] = Form.useForm<FeedbackFormValues>()
  const [buildOpen, setBuildOpen] = useState(false)

  const today = todayKey()
  const blocksByTask = new Map<string, PlanBlock>()
  for (const block of planQuery.data?.plan?.blocks ?? []) {
    if (dayKey(block.starts_at) === today) blocksByTask.set(block.task_id, block)
  }

  // 窗口为天级语义，按天量化：毫秒级重算会改变 queryKey 引发无限重复请求
  const upcomingWindow = useMemo(() => {
    const nowMs = Date.now()
    return {
      due_before: new Date(nowMs + 7 * 24 * 3600 * 1000).toISOString(),
      due_after: new Date(nowMs - 14 * 24 * 3600 * 1000).toISOString(),
    }
  }, [today])
  const upcomingQuery = useGlobalAssignments(upcomingWindow)

  const courses = coursesQuery.data?.items ?? []
  const courseTitle = (courseId: string | null) =>
    courseId ? (courses.find((c) => c.id === courseId)?.title ?? '未知课程') : '通用'

  const assignments = upcomingQuery.data?.items ?? []
  const assignmentBySource = new Map<string, Assignment>()
  for (const a of assignments) {
    if (a.source_ref) assignmentBySource.set(sourceRefKey(a.source_ref), a)
  }
  /** Q3：按 (platform, external_id) 匹配已加载对象；匹配不到禁用跳转（不猜测） */
  const jumpTargetFor = (task: LearningTask): Assignment | undefined => {
    for (const ref of task.source_refs) {
      const hit = assignmentBySource.get(sourceRefKey(ref))
      if (hit) return hit
    }
    return undefined
  }

  // PD-3：平台已提交/已评分的作业对应任务保留但弱化展示、不排程（与后端排程排除同一关联依据）
  const submittedRefs = submittedAssignmentRefKeys(assignments)
  const isPlatformSubmitted = (task: LearningTask) =>
    linkedToSubmittedAssignment(task.source_refs, submittedRefs)

  const tasks = filterTodayTasks(tasksQuery.data?.items ?? [], planQuery.data?.plan)
  const activeTasks = tasks.filter((t) => t.status !== 'completed' && t.status !== 'cancelled')
  const workloadTasks = activeTasks.filter((t) => !isPlatformSubmitted(t))
  const platformSubmittedCount = activeTasks.length - workloadTasks.length
  const totalEstimated = workloadTasks.reduce((sum, t) => sum + t.estimated_minutes, 0)
  const todayLoading = tasksQuery.isLoading || (tasksQuery.isSuccess && planQuery.isLoading)

  const syncIssues = (connectionsQuery.data?.items ?? []).filter(
    (c) =>
      c.status === 'auth_required' ||
      c.status === 'network_error' ||
      c.status === 'blocked' ||
      isStale(c.last_sync_at),
  )

  const handleFeedback = async () => {
    if (!feedbackTask) return
    const values = await feedbackForm.validateFields()
    submitFeedback.mutate(
      {
        taskId: feedbackTask.id,
        feedback: {
          outcome: values.outcome,
          actual_minutes: values.actual_minutes ?? null,
          difficulty: values.difficulty ?? null,
          note: values.note ?? null,
        },
      },
      {
        onSuccess: (data) => {
          if (data.feedback.outcome === 'completed') {
            message.success('已记录学习完成（学习状态，不等同于学校平台已提交）')
          } else if (data.feedback.outcome === 'partial') {
            message.success('已记录部分完成；任务保持「进行中」')
          } else {
            message.info('已记录本次未做；重新生成计划时会重新安排该任务')
          }
          if (data.replan_suggested) {
            message.info('建议到计划页重新生成预览，让未完成任务重排到剩余时间')
          }
          setFeedbackTask(null)
          feedbackForm.resetFields()
        },
        onError: (err) => message.error(toApiError(err).message),
      },
    )
  }

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      {syncIssues.length > 0 && (
        <Alert
          type="warning"
          showIcon
          title="同步异常，今日任务可能不完整"
          description={
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {syncIssues.map((c) => (
                <li key={c.id}>
                  「{c.display_name}」
                  {c.status === 'auth_required'
                    ? '等待登录（同步已暂停，不是失败）'
                    : c.status === 'network_error'
                      ? '网络异常'
                      : c.status === 'blocked'
                        ? '被平台阻断'
                        : '数据可能过期'}
                  {c.last_sync_at ? `（最近成功同步：${fmtDateTime(c.last_sync_at)}）` : '（尚无成功同步记录）'}
                </li>
              ))}
            </ul>
          }
          action={
            <Link to="/sync">
              <Button size="small">去同步中心</Button>
            </Link>
          }
        />
      )}

      {planQuery.isError && (
        <Alert
          type="warning"
          showIcon
          title="当前计划加载失败，今日任务列表可能不完整"
          description="今日任务依赖计划中的时间块；计划加载成功前仅能展示截止时间在今天的任务。"
          action={
            <Button size="small" onClick={() => planQuery.refetch()}>
              重试
            </Button>
          }
        />
      )}

      <StateView
        state={
          todayLoading
            ? 'loading'
            : tasksQuery.isError
              ? 'error'
              : tasks.length === 0
                ? 'empty'
                : 'ready'
        }
        error={tasksQuery.error}
        onRetry={() => tasksQuery.refetch()}
        emptyText="今天没有安排任务，可以到计划页生成计划或复习错题"
        extra={
          <Link to="/plan">
            <Button type="primary">去计划页</Button>
          </Link>
        }
      >
        <Card
          title="今日任务"
          extra={
            <Space wrap>
              <Typography.Text type="secondary">
                {workloadTasks.length} 项待完成 · 预计 {fmtMinutes(totalEstimated)}
                {platformSubmittedCount > 0 && `（平台已提交 ${platformSubmittedCount} 项不参与排程）`}
              </Typography.Text>
              <Button size="small" icon={<ThunderboltOutlined />} onClick={() => setBuildOpen(true)}>
                由资料生成任务
              </Button>
            </Space>
          }
        >
          <Listy
            items={tasks}
            rowKey={(t) => t.id}
            itemRender={(task) => {
              const block = blocksByTask.get(task.id)
              const done = task.status === 'completed'
              const platformSubmitted = isPlatformSubmitted(task)
              const overdue = !done && task.due_at !== null && Date.parse(task.due_at) < Date.now()
              const jumpTarget = jumpTargetFor(task)
              const courseId = task.course_id
              return (
                <div
                  style={{
                    display: 'flex',
                    flexWrap: 'wrap',
                    justifyContent: 'space-between',
                    columnGap: 16,
                    rowGap: 8,
                    opacity: done || platformSubmitted ? 0.6 : 1,
                  }}
                >
                  <Space orientation="vertical" size={4} style={{ flex: '1 1 320px', minWidth: 0 }}>
                    <Space wrap size={8}>
                      <Typography.Text delete={done}>{task.title}</Typography.Text>
                      <TaskStatusTag status={task.status} />
                      {platformSubmitted && (
                        <Tooltip title="平台已提交，不参与排程（平台事实）。该任务关联的作业已在平台提交/评分，保留供复习；与本地「学习完成」状态无关。">
                          <Tag color="success">平台已提交</Tag>
                        </Tooltip>
                      )}
                      {block && (
                        <Tag icon={<ClockCircleOutlined />}>
                          {fmtTimeRange(block.starts_at, block.ends_at)}
                        </Tag>
                      )}
                      {overdue && <Tag color="error">已超期</Tag>}
                    </Space>
                    <Space wrap size={8}>
                      <Typography.Text type="secondary">
                        {courseTitle(task.course_id)} · {taskTypeLabel(task.type)} · 预计{' '}
                        {fmtMinutes(task.estimated_minutes)}
                      </Typography.Text>
                      {task.due_at && (
                        <Typography.Text type="secondary">
                          截止 {fmtDateTime(task.due_at, undefined, 'M月D日 HH:mm')}
                        </Typography.Text>
                      )}
                      <DuePrecisionHint precision={task.due_precision} />
                      <SourceTag source={task.fact_source} />
                    </Space>
                  </Space>
                  <Space wrap size={4}>
                    <Button
                      size="small"
                      type="primary"
                      disabled={done}
                      onClick={() => {
                        // 每次打开重置并预填，避免 Form 实例残留上一任务的输入
                        feedbackForm.resetFields()
                        feedbackForm.setFieldsValue({
                          outcome: 'completed',
                          actual_minutes: task.estimated_minutes ?? null,
                        })
                        setFeedbackTask(task)
                      }}
                    >
                      记录反馈
                    </Button>
                    {jumpTarget?.source_ref?.canonical_url ? (
                      <Button
                        size="small"
                        type="link"
                        icon={<ExportOutlined />}
                        href={jumpTarget.source_ref.canonical_url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        去平台
                      </Button>
                    ) : (
                      <Tooltip title="未匹配到对应的平台对象，不猜测跳转地址">
                        <Button size="small" type="link" icon={<ExportOutlined />} disabled>
                          去平台
                        </Button>
                      </Tooltip>
                    )}
                    {courseId && (
                      <Link to={`/courses/${courseId}`}>
                        <Button size="small" type="link">
                          查看资料
                        </Button>
                      </Link>
                    )}
                  </Space>
                </div>
              )
            }}
          />
        </Card>
      </StateView>

      <Card title="临近截止作业" size="small">
        {upcomingQuery.isLoading && <Typography.Text type="secondary">加载中…</Typography.Text>}
        {upcomingQuery.isError && (
          <Alert type="error" showIcon title={toApiError(upcomingQuery.error).message} />
        )}
        {upcomingQuery.data &&
          (assignments.length === 0 ? (
            <Typography.Text type="secondary">未来 7 天没有临近截止的未提交作业。</Typography.Text>
          ) : (
            <Listy
              items={assignments}
              rowKey={(a) => a.id}
              itemRender={(a) => {
                const overdue = a.due_at !== null && Date.parse(a.due_at) < Date.now()
                return (
                  <div
                    style={{
                      display: 'flex',
                      flexWrap: 'wrap',
                      justifyContent: 'space-between',
                      columnGap: 16,
                      rowGap: 8,
                    }}
                  >
                    <Space orientation="vertical" size={4} style={{ flex: '1 1 320px', minWidth: 0 }}>
                      <Space wrap size={8}>
                        <Typography.Text>{a.title}</Typography.Text>
                        {overdue ? <Tag color="error">已逾期</Tag> : <Tag color="warning">临近截止</Tag>}
                        <SubmissionStatusTag status={a.submission_status} />
                        <SourceTag source={a.fact_source} confidence={a.confidence} />
                      </Space>
                      <Space wrap size={8}>
                        <Typography.Text type="secondary">{courseTitle(a.course_id)}</Typography.Text>
                        <Typography.Text type="secondary">
                          截止：{a.due_at ? fmtDateTime(a.due_at, undefined, 'M月D日 HH:mm') : '未提供'}
                        </Typography.Text>
                        <DuePrecisionHint precision={a.due_precision} />
                        {a.due_at &&
                          (() => {
                            const absolute = fmtDateTime(a.due_at, undefined, 'M月D日 HH:mm')
                            const relative = fmtRelativeToNow(a.due_at)
                            return relative !== absolute ? (
                              <Typography.Text type="secondary">（{relative}）</Typography.Text>
                            ) : null
                          })()}
                      </Space>
                    </Space>
                    <Space wrap size={4}>
                      {a.source_ref?.canonical_url ? (
                        <Button
                          size="small"
                          type="link"
                          href={a.source_ref.canonical_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          去平台提交
                        </Button>
                      ) : (
                        <Tooltip title="平台未提供跳转链接">
                          <Button size="small" type="link" disabled>
                            去平台提交
                          </Button>
                        </Tooltip>
                      )}
                      <Link to={`/courses/${a.course_id}`}>
                        <Button size="small" type="link">
                          查看作业
                        </Button>
                      </Link>
                    </Space>
                  </div>
                )
              }}
            />
          ))}
      </Card>

      <Modal
        title={`记录反馈 · ${feedbackTask?.title ?? ''}`}
        open={feedbackTask !== null}
        onCancel={() => setFeedbackTask(null)}
        onOk={handleFeedback}
        confirmLoading={submitFeedback.isPending}
        okText="提交反馈"
        destroyOnHidden
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          title="学习完成 ≠ 学校平台已提交"
          description="这里只记录你的学习状态；如关联了平台作业，提交仍需在原平台完成。"
        />
        <Form
          form={feedbackForm}
          layout="vertical"
          initialValues={{
            outcome: 'completed',
            actual_minutes: feedbackTask?.estimated_minutes ?? null,
          }}
        >
          <Form.Item
            name="outcome"
            label="本次结果"
            rules={[{ required: true, message: '请选择本次结果' }]}
          >
            <Radio.Group>
              {(Object.keys(OUTCOME_LABELS) as FeedbackOutcome[]).map((outcome) => (
                <Radio key={outcome} value={outcome}>
                  {OUTCOME_LABELS[outcome]}
                </Radio>
              ))}
            </Radio.Group>
          </Form.Item>
          <Form.Item name="actual_minutes" label="实际投入（分钟）">
            <InputNumber min={0} max={1440} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="difficulty" label="难度感受（可选）">
            <Select
              aria-label="难度感受（可选）"
              allowClear
              placeholder="1 轻松 … 5 困难"
              options={[
                { value: 1, label: '1 · 很轻松' },
                { value: 2, label: '2 · 轻松' },
                { value: 3, label: '3 · 一般' },
                { value: 4, label: '4 · 偏难' },
                { value: 5, label: '5 · 困难' },
              ]}
            />
          </Form.Item>
          <Form.Item name="note" label="备注（可选）">
            <Input.TextArea rows={2} maxLength={2000} showCount placeholder="如：卡在哪一步、需要补充什么资料" />
          </Form.Item>
        </Form>
      </Modal>

      <BuildTasksDrawer open={buildOpen} onClose={() => setBuildOpen(false)} />
    </Space>
  )
}
