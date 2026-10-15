import { Button, Card, Empty, Select, Space, Tag, Typography } from 'antd'
import { useSearchParams } from 'react-router-dom'
import { WorkspaceLink as Link } from '../agent/WorkspaceLink'
import { StateView } from '../../components/StateView'
import { useCourses, useReviewPapers } from '../../lib/api/hooks'
import { REVIEW_REVISION_REASON_LABELS, reviewModeLabel } from '../../lib/review'
import { fmtDateTime } from '../../lib/time'

export function ReviewLibrary() {
  const [searchParams, setSearchParams] = useSearchParams()
  const courseId = searchParams.get('course_id') ?? undefined
  const mode = searchParams.get('mode') ?? undefined

  const coursesQuery = useCourses()
  const papersQuery = useReviewPapers({ course_id: courseId, mode })

  const updateFilter = (key: 'course_id' | 'mode', value: string | undefined) => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        if (value) next.set(key, value)
        else next.delete(key)
        return next
      },
      { replace: true },
    )
  }

  const courseTitles = new Map((coursesQuery.data?.items ?? []).map((c) => [c.id, c.title]))
  const papers = papersQuery.data?.pages.flatMap((page) => page.items) ?? []

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <Card
        title="复习卷"
        extra={
          <Link to="/courses">
            <Button size="small" type="primary">
              打开课程资料生成复习卷
            </Button>
          </Link>
        }
      >
        <Space orientation="vertical" size={8} style={{ width: '100%' }}>
          <Space wrap size={8}>
            <Typography.Text>筛选：</Typography.Text>
            <Select
              allowClear
              placeholder="全部课程"
              style={{ minWidth: 220 }}
              value={courseId}
              loading={coursesQuery.isLoading}
              onChange={(value) => updateFilter('course_id', value)}
              options={(coursesQuery.data?.items ?? []).map((c) => ({ value: c.id, label: c.title }))}
            />
            <Select
              allowClear
              placeholder="全部模式"
              style={{ width: 160 }}
              value={mode}
              onChange={(value) => updateFilter('mode', value)}
              options={[
                { value: 'ai', label: reviewModeLabel('ai') },
                { value: 'bank', label: reviewModeLabel('bank') },
                { value: 'hybrid', label: reviewModeLabel('hybrid') },
              ]}
            />
          </Space>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            列表按时间倒序、游标分页；每行显示各卷最新版本（历史版本在详情页可切换查看）。
          </Typography.Text>
        </Space>
      </Card>

      <StateView
        state={papersQuery.isLoading ? 'loading' : papersQuery.isError ? 'error' : 'ready'}
        error={papersQuery.error}
        onRetry={() => papersQuery.refetch()}
      >
        {papers.length === 0 ? (
          <Card size="small">
            <Empty
              description={
                <Space orientation="vertical" size={4}>
                  <Typography.Text>还没有复习卷</Typography.Text>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    进入任一课程 → 「复习卷」标签 → 选择材料与参数即可生成。
                  </Typography.Text>
                </Space>
              }
            />
          </Card>
        ) : (
          <Space orientation="vertical" size={8} style={{ width: '100%' }}>
            {papers.map((paper) => (
              <Card key={paper.id} size="small">
                <Space wrap size={8}>
                  <Link to={`/reviews/${paper.id}`}>
                    <Typography.Text strong>{paper.title}</Typography.Text>
                  </Link>
                  <Tag>v{paper.revision}</Tag>
                  <Tag>{REVIEW_REVISION_REASON_LABELS[paper.revision_reason]}</Tag>
                  <Tag color="blue">{reviewModeLabel(paper.mode)}</Tag>
                  {paper.warnings.length > 0 && <Tag color="orange">{paper.warnings.length} 条生成提示</Tag>}
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {courseTitles.get(paper.course_id) ?? paper.course_id} · {paper.summary.question_count} 题 ·{' '}
                    {paper.summary.total_points} 分 · {fmtDateTime(paper.created_at)}
                  </Typography.Text>
                  {paper.based_on_revision !== null && (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      基于{' '}
                      <Link to={`/reviews/${paper.id}?revision=${paper.based_on_revision}`}>
                        v{paper.based_on_revision}
                      </Link>
                    </Typography.Text>
                  )}
                </Space>
              </Card>
            ))}
            {papersQuery.hasNextPage && (
              <Button
                block
                loading={papersQuery.isFetchingNextPage}
                onClick={() => papersQuery.fetchNextPage()}
              >
                加载更多
              </Button>
            )}
          </Space>
        )}
      </StateView>
    </Space>
  )
}
