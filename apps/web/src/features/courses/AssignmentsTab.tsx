import { Alert, Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { TableProps } from 'antd'
import { StateView } from '../../components/StateView'
import { DuePrecisionHint, SourceTag, SubmissionStatusTag, TaskStatusTag } from '../../components/tags'
import { useAllTasks, useCourseAssignments } from '../../lib/api/hooks'
import { sourceRefKey } from '../../lib/matching'
import { platformLabel } from '../../lib/platforms'
import { fmtDateTime, fmtRelativeToNow } from '../../lib/time'
import type { Assignment, LearningTask } from '../../types/contract'

/** 课程作业：截止时间（含精度与「平台未提供」）、平台提交状态与本地学习状态分离展示 */
export function AssignmentsTab({ courseId }: { courseId: string }) {
  const assignmentsQuery = useCourseAssignments(courseId)
  const tasksQuery = useAllTasks()

  const tasksBySource = new Map<string, LearningTask>()
  for (const task of tasksQuery.data?.items ?? []) {
    for (const ref of task.source_refs) {
      tasksBySource.set(sourceRefKey(ref), task)
    }
  }

  const items = [...(assignmentsQuery.data?.items ?? [])].sort((a, b) => {
    if (a.due_at === null && b.due_at === null) return 0
    if (a.due_at === null) return 1
    if (b.due_at === null) return -1
    return Date.parse(a.due_at) - Date.parse(b.due_at)
  })

  const columns: TableProps<Assignment>['columns'] = [
    {
      title: '作业',
      dataIndex: 'title',
      render: (_, a) => {
        const overdue =
          a.due_at !== null && Date.parse(a.due_at) < Date.now() && a.submission_status === 'not_submitted'
        return (
          <Space orientation="vertical" size={2}>
            <Space wrap size={4}>
              <Typography.Text strong>{a.title}</Typography.Text>
              {overdue && <Tag color="error">已逾期</Tag>}
            </Space>
            {a.instructions && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {a.instructions}
              </Typography.Text>
            )}
          </Space>
        )
      },
    },
    {
      title: '截止时间',
      dataIndex: 'due_at',
      width: 230,
      render: (_, a) => {
        if (!a.due_at || a.due_precision === 'unknown') {
          return (
            <Space orientation="vertical" size={2}>
              <Tooltip title="平台未公布截止时间，不猜测；可在学习任务中按自己的节奏安排">
                <span>
                  <Typography.Text type="secondary">平台未提供截止时间</Typography.Text>
                </span>
              </Tooltip>
              <Space size={4}>
                <DuePrecisionHint precision={a.due_precision} />
                <SourceTag source={a.fact_source} confidence={a.confidence} />
              </Space>
            </Space>
          )
        }
        const absolute = fmtDateTime(a.due_at, undefined, 'M月D日 HH:mm')
        const relative = fmtRelativeToNow(a.due_at)
        return (
          <Space orientation="vertical" size={2}>
            <Typography.Text>{absolute}</Typography.Text>
            <Space size={4} wrap>
              {relative !== absolute && (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {relative}
                </Typography.Text>
              )}
              <DuePrecisionHint precision={a.due_precision} />
              <SourceTag source={a.fact_source} confidence={a.confidence} />
            </Space>
          </Space>
        )
      },
    },
    {
      title: '平台提交状态',
      dataIndex: 'submission_status',
      width: 130,
      render: (_, a) => <SubmissionStatusTag status={a.submission_status} />,
    },
    {
      title: '学习任务',
      width: 200,
      render: (_, a) => {
        const task = a.source_ref ? tasksBySource.get(sourceRefKey(a.source_ref)) : undefined
        if (!task) return <Typography.Text type="secondary">未生成学习任务</Typography.Text>
        return (
          <Space orientation="vertical" size={2}>
            <Typography.Text style={{ fontSize: 13 }}>{task.title}</Typography.Text>
            <TaskStatusTag status={task.status} />
          </Space>
        )
      },
    },
    {
      title: '来源',
      dataIndex: 'source_ref',
      width: 130,
      render: (_, a) => {
        if (!a.source_ref) return <Tag>手动整理</Tag>
        return a.source_ref.canonical_url ? (
          <Typography.Link href={a.source_ref.canonical_url} target="_blank" rel="noreferrer">
            去{platformLabel(a.source_ref.platform)}查看
          </Typography.Link>
        ) : (
          <Tag>{platformLabel(a.source_ref.platform)}</Tag>
        )
      },
    },
  ]

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        title="学习完成 ≠ 学校平台已提交"
        description="这一页的「平台提交状态」是平台事实；「学习任务」状态只是本地记录，两者独立。提交作业仍需在原平台完成。"
      />
      <StateView
        state={
          assignmentsQuery.isLoading
            ? 'loading'
            : assignmentsQuery.isError
              ? 'error'
              : items.length === 0
                ? 'empty'
                : 'ready'
        }
        error={assignmentsQuery.error}
        onRetry={() => assignmentsQuery.refetch()}
        emptyText="该课程暂无作业信息"
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
