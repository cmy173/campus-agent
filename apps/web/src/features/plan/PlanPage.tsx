import {
  CalendarOutlined,
  ClockCircleOutlined,
  LockOutlined,
  ReloadOutlined,
  RollbackOutlined,
  UnlockOutlined,
} from '@ant-design/icons'
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Col,
  Listy,
  Popconfirm,
  Row,
  Segmented,
  Space,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { StateView } from '../../components/StateView'
import { PlanDiffTag } from '../../components/tags'
import {
  useAllTasks,
  useApplyPlan,
  useCourses,
  useCurrentPlan,
  useLockTask,
  usePreviewPlan,
  useRevokePlan,
} from '../../lib/api/hooks'
import { toApiError } from '../../lib/api/http'
import type { ApiError } from '../../lib/api/http'
import { dayKey, fmtDateTime, fmtDayLabel, fmtMinutes, fmtTimeRange, weekDays } from '../../lib/time'
import type {
  Plan,
  PlanBlock,
  PlanDiffKind,
  PlanStatus,
  UnassignedItem,
  UnassignedReasonCode,
} from '../../types/contract'

const PLAN_STATUS_LABELS: Record<PlanStatus, string> = {
  candidate: '候选（未应用）',
  active: '生效中',
  superseded: '已被替代',
  revoked: '已撤销',
}

const GAP_LABELS: Record<UnassignedReasonCode, string> = {
  DEADLINE_PASSED: '已过截止时间',
  INSUFFICIENT_WINDOW_BEFORE_DUE: '截止前可用时间不足',
  INSUFFICIENT_TOTAL_TIME: '可用时间总量不足',
  TASK_TOO_LONG_INDIVISIBLE: '任务不可拆分且超出单日窗口',
  FRAGMENTED_WINDOW: '可用时间碎片化',
  DEPENDENCY_CONFLICT: '依赖冲突',
  INVALID_TASK: '任务数据无效',
}

const GAP_HINTS: Record<UnassignedReasonCode, string> = {
  DEADLINE_PASSED: '截止时间已过，重新生成预览时暂不安排；可调整任务截止时间或标记完成后重试。',
  INSUFFICIENT_WINDOW_BEFORE_DUE: '可以增加可用时间，或将任务截止时间调整为更现实的日期后重新生成预览。',
  INSUFFICIENT_TOTAL_TIME: '可以增加每周可用时段或调整每日上限后重新生成预览。',
  TASK_TOO_LONG_INDIVISIBLE: '该任务要求连续完成且超过单日窗口；可调整可用时间，或让任务拆分为多次完成。',
  FRAGMENTED_WINDOW: '可用时段过短，放不下该任务；可合并或延长每日可用时段后重试。',
  DEPENDENCY_CONFLICT: '请先完成前置任务，然后重新生成预览。',
  INVALID_TASK: '任务缺少必要信息（如估算时长）；请先补全任务信息再重新生成预览。',
}

/** Q1 裁决：预览与当前生效计划按 task_id 对比，得出 新增/移动/保留/移出 四态（纯前端派生，非契约字段） */
function buildDiff(current: Plan | null, preview: Plan): Map<string, PlanDiffKind> {
  const result = new Map<string, PlanDiffKind>()
  const fingerprint = (blocks: PlanBlock[]) =>
    blocks
      .map((b) => `${b.starts_at}|${b.ends_at}`)
      .sort()
      .join(',')
  const prevByTask = new Map<string, PlanBlock[]>()
  for (const b of current?.blocks ?? []) {
    const list = prevByTask.get(b.task_id) ?? []
    list.push(b)
    prevByTask.set(b.task_id, list)
  }
  const previewByTask = new Map<string, PlanBlock[]>()
  for (const b of preview.blocks) {
    const list = previewByTask.get(b.task_id) ?? []
    list.push(b)
    previewByTask.set(b.task_id, list)
  }
  for (const [taskId, blocks] of previewByTask) {
    const prev = prevByTask.get(taskId)
    if (!prev) result.set(taskId, 'added')
    else if (fingerprint(prev) === fingerprint(blocks)) result.set(taskId, 'kept')
    else result.set(taskId, 'moved')
  }
  for (const taskId of prevByTask.keys()) {
    if (!previewByTask.has(taskId)) result.set(taskId, 'removed')
  }
  return result
}

function WeekView({
  plan,
  diff,
  taskTitle,
}: {
  plan: Plan
  diff: Map<string, PlanDiffKind> | null
  taskTitle: (taskId: string) => string
}) {
  const days = weekDays(plan.horizon_start)
  return (
    <Space orientation="vertical" size={8} style={{ width: '100%' }}>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        周视图仅用于快速浏览；锁定在列表视图中操作。块拖动与微调时间未接通（v0.1）。
      </Typography.Text>
      <Row gutter={[8, 8]}>
        {days.map((day) => {
          const dayBlocks = plan.blocks
            .filter((b) => dayKey(b.starts_at) === day.key)
            .sort((a, b) => Date.parse(a.starts_at) - Date.parse(b.starts_at))
          return (
            <Col key={day.key} xs={12} md={8} xl={6}>
              <Card
                size="small"
                title={`${day.label}${day.isToday ? '（今天）' : ''}`}
                styles={{ body: { minHeight: 96 } }}
              >
                {dayBlocks.length === 0 ? (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    无安排
                  </Typography.Text>
                ) : (
                  <Space orientation="vertical" size={8} style={{ width: '100%' }}>
                    {dayBlocks.map((b) => (
                      <div key={b.id}>
                        <Space size={4} wrap>
                          <Typography.Text strong style={{ fontSize: 12 }}>
                            {fmtTimeRange(b.starts_at, b.ends_at)}
                          </Typography.Text>
                          {diff?.get(b.task_id) && <PlanDiffTag kind={diff.get(b.task_id)!} />}
                          {b.locked && <LockOutlined style={{ fontSize: 12 }} />}
                        </Space>
                        <div>
                          <Typography.Text style={{ fontSize: 12 }} ellipsis>
                            {taskTitle(b.task_id)}
                          </Typography.Text>
                        </div>
                      </div>
                    ))}
                  </Space>
                )}
              </Card>
            </Col>
          )
        })}
      </Row>
    </Space>
  )
}

export function PlanPage() {
  const { message } = AntApp.useApp()
  const planQuery = useCurrentPlan()
  const tasksQuery = useAllTasks(true)
  const coursesQuery = useCourses()
  const previewPlan = usePreviewPlan()
  const applyPlan = useApplyPlan()
  const revokePlan = useRevokePlan()
  const lockTask = useLockTask()

  const [preview, setPreview] = useState<Plan | null>(null)
  const [previewRevision, setPreviewRevision] = useState<string | null>(null)
  const [staleConflict, setStaleConflict] = useState<ApiError | null>(null)
  const [viewMode, setViewMode] = useState<'list' | 'week'>('list')

  const taskById = new Map((tasksQuery.data?.items ?? []).map((t) => [t.id, t]))
  const courseTitleById = new Map((coursesQuery.data?.items ?? []).map((c) => [c.id, c.title]))
  const taskTitle = (taskId: string) => taskById.get(taskId)?.title ?? (tasksQuery.isPending ? '正在读取任务名称…' : '任务资料暂不可用，请刷新重试')
  const taskCourseTitle = (taskId: string) => {
    const task = taskById.get(taskId)
    return task?.course_id ? courseTitleById.get(task.course_id) : undefined
  }

  const handlePreview = () => {
    previewPlan.mutate(undefined, {
      onSuccess: (data) => {
        setPreview(data.plan)
        setPreviewRevision(data.current_input_revision)
        setStaleConflict(null)
        message.success('已生成预览：绿色=新增，蓝色=移动，灰色=保留，橙色=移出')
      },
      onError: (err) => message.error(toApiError(err).message),
    })
  }

  const handleApply = () => {
    if (!preview) return
    applyPlan.mutate(preview.id, {
      onSuccess: () => {
        setPreview(null)
        setPreviewRevision(null)
        setStaleConflict(null)
        message.success('计划已应用；今日任务已同步更新')
      },
      onError: (err) => {
        const apiError = toApiError(err)
        if (apiError.code === 'PLAN_STALE') {
          setStaleConflict(apiError)
        } else {
          message.error(apiError.message)
        }
      },
    })
  }

  const handleRevoke = (planId: string) => {
    revokePlan.mutate(planId, {
      onSuccess: () => message.success('已撤销该计划（平台事实未回滚）'),
      onError: (err) => message.error(toApiError(err).message),
    })
  }

  const toggleLock = (block: PlanBlock) => {
    lockTask.mutate(
      { taskId: block.task_id, locked: !block.locked },
      {
        onSuccess: () =>
          message.success(block.locked ? '已解锁' : '已锁定：重新生成预览时保持此安排'),
        onError: (err) => message.error(toApiError(err).message),
      },
    )
  }

  if (planQuery.isLoading || planQuery.isError || !planQuery.data) {
    return (
      <StateView
        state={planQuery.isLoading ? 'loading' : planQuery.isError ? 'error' : 'empty'}
        error={planQuery.error}
        onRetry={() => planQuery.refetch()}
        emptyText="还没有生效计划，点击生成预览开始安排"
      >
        <Button type="primary" onClick={handlePreview} loading={previewPlan.isPending}>
          生成预览
        </Button>
      </StateView>
    )
  }

  const currentPlan = planQuery.data.plan
  if (!currentPlan && preview === null) {
    return (
      <StateView
        state="empty"
        emptyText="还没有生效计划，点击生成预览开始安排"
        extra={
          <Button type="primary" onClick={handlePreview} loading={previewPlan.isPending}>
            生成预览
          </Button>
        }
      />
    )
  }

  const displayed: Plan = preview ?? currentPlan!
  const isPreview = preview !== null
  const diff = preview !== null ? buildDiff(currentPlan, preview) : null
  const previewStale =
    isPreview &&
    previewRevision !== null &&
    planQuery.data.current_input_revision !== previewRevision
  const removedEntries: { taskId: string; previous: PlanBlock }[] = []
  if (diff) {
    for (const block of currentPlan?.blocks ?? []) {
      if (
        diff.get(block.task_id) === 'removed' &&
        !removedEntries.some((e) => e.taskId === block.task_id)
      ) {
        removedEntries.push({ taskId: block.task_id, previous: block })
      }
    }
  }

  const blocks = [...displayed.blocks].sort(
    (a, b) => Date.parse(a.starts_at) - Date.parse(b.starts_at),
  )
  const groups = new Map<string, PlanBlock[]>()
  for (const block of blocks) {
    const key = dayKey(block.starts_at)
    const list = groups.get(key) ?? []
    list.push(block)
    groups.set(key, list)
  }

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <Card>
        <Space orientation="vertical" size={12} style={{ width: '100%' }}>
          <Space wrap size={8}>
            <Typography.Text strong>{isPreview ? '计划预览' : '当前生效计划'}</Typography.Text>
            <Tag>v{displayed.version}</Tag>
            <Tag color={displayed.status === 'active' ? 'success' : 'default'}>
              {PLAN_STATUS_LABELS[displayed.status]}
            </Tag>
            <Typography.Text type="secondary">
              生成于 {fmtDateTime(displayed.created_at)} · 输入修订 {displayed.input_revision} · 覆盖至{' '}
              {fmtDateTime(displayed.horizon_end, undefined, 'M月D日')}
            </Typography.Text>
          </Space>
          <Space wrap size={16}>
            <Typography.Text type="secondary">
              可用容量 {fmtMinutes(displayed.summary.capacity_minutes)}
            </Typography.Text>
            <Typography.Text type="secondary">
              已安排 {fmtMinutes(displayed.summary.scheduled_minutes)}
              {displayed.summary.locked_minutes > 0 &&
                `（含锁定 ${fmtMinutes(displayed.summary.locked_minutes)}）`}
            </Typography.Text>
            <Typography.Text type="secondary">
              任务 {displayed.summary.scheduled_task_count} 项
            </Typography.Text>
            {displayed.summary.unassigned_task_count > 0 && (
              <Typography.Text type="warning">
                未安排 {displayed.summary.unassigned_task_count} 项
              </Typography.Text>
            )}
          </Space>
          <Space wrap>
            <Button
              type="primary"
              icon={<ReloadOutlined />}
              loading={previewPlan.isPending}
              onClick={handlePreview}
            >
              {isPreview ? '重新生成预览' : '生成预览'}
            </Button>
            {isPreview && (
              <>
                <Tooltip title={previewStale ? '输入已变化，请先重新生成预览再应用' : undefined}>
                  <Button
                    type="primary"
                    ghost
                    loading={applyPlan.isPending}
                    disabled={previewStale}
                    onClick={handleApply}
                  >
                    应用此计划（{preview?.blocks.length ?? 0} 个安排）
                  </Button>
                </Tooltip>
                <Button
                  onClick={() => {
                    setPreview(null)
                    setPreviewRevision(null)
                  }}
                >
                  放弃预览
                </Button>
              </>
            )}
            {!isPreview && displayed.status === 'active' && (
              <Popconfirm
                title="撤销该计划？"
                description="撤销只恢复计划安排；平台事实（作业提交状态、同步数据）不会被回滚。"
                okText="撤销"
                cancelText="取消"
                onConfirm={() => handleRevoke(displayed.id)}
              >
                <Button icon={<RollbackOutlined />} loading={revokePlan.isPending}>
                  撤销该计划
                </Button>
              </Popconfirm>
            )}
            <Segmented
              value={viewMode}
              onChange={(v) => setViewMode(v as 'list' | 'week')}
              options={[
                { label: '列表', value: 'list' },
                { label: '周视图', value: 'week' },
              ]}
            />
          </Space>
        </Space>
      </Card>

      {isPreview && (
        <Alert
          type="info"
          showIcon
          title="这是预览，尚未应用"
          description="标签表示相对当前生效计划的变化：新增 / 移动 / 保留 / 移出（按任务对比）。可用时间或学习任务发生变化后，预览会过期，需要重新生成。"
        />
      )}

      {previewStale && (
        <Alert
          type="warning"
          showIcon
          title="可用时间或任务已变化，预览已过期"
          description="当前输入修订与生成预览时不一致；请重新生成预览后再应用，避免把过期安排写入计划。"
          action={
            <Button size="small" type="primary" onClick={handlePreview} loading={previewPlan.isPending}>
              重新生成
            </Button>
          }
        />
      )}

      {staleConflict && (
        <Alert
          type="error"
          showIcon
          title="预览已过期，不能应用"
          description="生成预览后，可用时间或学习任务发生了变化。请重新生成预览，确认内容后再应用。"
          action={
            <Button size="small" type="primary" onClick={handlePreview} loading={previewPlan.isPending}>
              重新生成
            </Button>
          }
        />
      )}

      {displayed.warnings.length > 0 && (
        <Alert
          type="warning"
          showIcon
          title="计划提示"
          description={
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {displayed.warnings.map((w, i) => (
                <li key={`${w.code}-${i}`}>
                  {w.message}
                  {w.task_ids.length > 0 && `（涉及：${w.task_ids.map(taskTitle).join('、')}）`}
                </li>
              ))}
            </ul>
          }
        />
      )}

      {isPreview && removedEntries.length > 0 && (
        <Card size="small" title={`本次预览移出安排（${removedEntries.length}）`}>
          <Space orientation="vertical" size={4} style={{ width: '100%' }}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              这些任务在当前生效计划中有安排，但本次预览不再安排（如已完成、已取消或不再满足条件）。
            </Typography.Text>
            {removedEntries.map(({ taskId, previous }) => (
              <Space key={taskId} wrap size={8}>
                <PlanDiffTag kind="removed" />
                <Typography.Text>{taskTitle(taskId)}</Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  原时间 {fmtTimeRange(previous.starts_at, previous.ends_at)}
                </Typography.Text>
              </Space>
            ))}
          </Space>
        </Card>
      )}

      {displayed.unassigned.length > 0 && (
        <Card title={`无法安排的任务（${displayed.unassigned.length}）`} size="small">
          <Space orientation="vertical" size={8} style={{ width: '100%' }}>
            {displayed.unassigned.map((gap: UnassignedItem) => {
              const task = taskById.get(gap.task_id)
              return (
                <Card key={gap.task_id} size="small" type="inner">
                  <Space orientation="vertical" size={4} style={{ width: '100%' }}>
                    <Space wrap size={8}>
                      <Typography.Text strong>{taskTitle(gap.task_id)}</Typography.Text>
                      {taskCourseTitle(gap.task_id) && <Tag>{taskCourseTitle(gap.task_id)}</Tag>}
                      {task && (
                        <Typography.Text type="secondary">
                          需要 {fmtMinutes(task.estimated_minutes)}
                        </Typography.Text>
                      )}
                      {task?.due_at && (
                        <Typography.Text type="secondary">
                          截止 {fmtDateTime(task.due_at, undefined, 'M月D日 HH:mm')}
                        </Typography.Text>
                      )}
                    </Space>
                    <Alert
                      type="warning"
                      showIcon
                      title={GAP_LABELS[gap.reason_code]}
                      description={
                        <Space orientation="vertical" size={2}>
                          {gap.message && <span>{gap.message}</span>}
                          {gap.shortage_minutes !== null && (
                            <span>缺口约 {fmtMinutes(gap.shortage_minutes)}</span>
                          )}
                          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                            {gap.suggestion ?? GAP_HINTS[gap.reason_code]}
                          </Typography.Text>
                        </Space>
                      }
                    />
                    <Space>
                      <Link to="/settings">
                        <Button size="small" icon={<CalendarOutlined />}>
                          调整可用时间
                        </Button>
                      </Link>
                    </Space>
                  </Space>
                </Card>
              )
            })}
          </Space>
        </Card>
      )}

      {viewMode === 'week' ? (
        <WeekView plan={displayed} diff={diff} taskTitle={taskTitle} />
      ) : blocks.length === 0 ? (
        <StateView state="empty" emptyText="计划还没有时间块" />
      ) : (
        <Space orientation="vertical" size={12} style={{ width: '100%' }}>
          {[...groups.entries()].map(([key, dayBlocks]) => (
            <Card key={key} size="small" title={fmtDayLabel(dayBlocks[0].starts_at)}>
              <Listy
                items={dayBlocks}
                rowKey={(b) => b.id}
                itemRender={(block) => {
                  const minutes = (Date.parse(block.ends_at) - Date.parse(block.starts_at)) / 60000
                  const kind = diff?.get(block.task_id)
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
                          <Typography.Text>{taskTitle(block.task_id)}</Typography.Text>
                          {kind && <PlanDiffTag kind={kind} />}
                          {block.locked && <Tag icon={<LockOutlined />}>已锁定</Tag>}
                          <Tag icon={<ClockCircleOutlined />}>
                            {fmtTimeRange(block.starts_at, block.ends_at)}
                          </Tag>
                        </Space>
                        <Space wrap size={8}>
                          <Typography.Text type="secondary">
                            {taskCourseTitle(block.task_id) ?? ''} · 时长 {fmtMinutes(minutes)}
                          </Typography.Text>
                        </Space>
                      </Space>
                      {!isPreview && (
                        <Space wrap size={4}>
                          <Tooltip
                            title={
                              block.locked
                                ? '解锁后，重新生成预览时可以被移动'
                                : '锁定后，重新生成预览时保持此安排'
                            }
                          >
                            <Button
                              size="small"
                              icon={block.locked ? <UnlockOutlined /> : <LockOutlined />}
                              loading={
                                lockTask.isPending && lockTask.variables?.taskId === block.task_id
                              }
                              onClick={() => toggleLock(block)}
                            >
                              {block.locked ? '解锁' : '锁定'}
                            </Button>
                          </Tooltip>
                          <Tooltip title="v0.1 未接通：块拖动与微调时间暂不可用；如需改时间，请解锁后调整可用时间并重新生成预览">
                            <Button size="small" disabled>
                              调整时间（未接通）
                            </Button>
                          </Tooltip>
                        </Space>
                      )}
                    </div>
                  )
                }}
              />
            </Card>
          ))}
        </Space>
      )}
    </Space>
  )
}
