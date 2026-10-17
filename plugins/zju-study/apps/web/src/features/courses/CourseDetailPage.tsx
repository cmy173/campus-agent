import { ArrowLeftOutlined } from '@ant-design/icons'
import { Button, Card, Space, Tabs, Tag, Typography } from 'antd'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { StateView } from '../../components/StateView'
import { useCourse } from '../../lib/api/hooks'
import { platformLabel } from '../../lib/platforms'
import { ReviewTab } from '../reviews/ReviewTab'
import { AssignmentsTab } from './AssignmentsTab'
import { AttachSourceRefButton } from './AttachSourceRef'
import { RecordingsTab } from './RecordingsTab'
import { ResourcesTab } from './ResourcesTab'
import { TextbooksTab } from './TextbooksTab'

export function CourseDetailPage() {
  const { courseId } = useParams<{ courseId: string }>()
  const courseQuery = useCourse(courseId)
  const course = courseQuery.data
  const [searchParams, setSearchParams] = useSearchParams()
  const activeTab = searchParams.get('tab') ?? 'resources'

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <Link to="/courses">
        <Button size="small" type="link" icon={<ArrowLeftOutlined />}>
          返回课程列表
        </Button>
      </Link>

      <StateView
        state={courseQuery.isLoading ? 'loading' : courseQuery.isError ? 'error' : 'ready'}
        error={courseQuery.error}
        onRetry={() => courseQuery.refetch()}
      >
        {course && (
          <>
            <Card
              title={course.title}
              extra={
                  <AttachSourceRefButton courseId={course.id} courseTitle={course.title} size="small">
                    挂接平台来源
                  </AttachSourceRefButton>
              }
            >
              <Space orientation="vertical" size={4}>
                <Typography.Text type="secondary">
                  授课：{course.teacher ?? '未提供'}
                </Typography.Text>
                <Space wrap size={8}>
                  <Typography.Text type="secondary">来源：</Typography.Text>
                  {course.source_refs.length === 0 ? (
                    <Typography.Text type="secondary">
                      未挂接平台来源（当前只有手动导入的资料）
                    </Typography.Text>
                  ) : (
                    course.source_refs.map((ref) => (
                      <span key={`${ref.platform}-${ref.external_id}`}>
                        {ref.canonical_url ? (
                          <Typography.Link href={ref.canonical_url} target="_blank" rel="noreferrer">
                            {platformLabel(ref.platform)} ↗
                          </Typography.Link>
                        ) : (
                          <Tag>{platformLabel(ref.platform)}</Tag>
                        )}
                      </span>
                    ))
                  )}
                </Space>
              </Space>
            </Card>

            <Tabs
              activeKey={activeTab}
              onChange={(key) =>
                setSearchParams(
                  (prev) => {
                    const next = new URLSearchParams(prev)
                    next.set('tab', key)
                    return next
                  },
                  { replace: true },
                )
              }
              items={[
                { key: 'resources', label: '资料', children: <ResourcesTab courseId={course.id} /> },
                { key: 'textbooks', label: '教材库', children: <TextbooksTab courseId={course.id} /> },
                { key: 'assignments', label: '作业', children: <AssignmentsTab courseId={course.id} /> },
                { key: 'recordings', label: '回放', children: <RecordingsTab courseId={course.id} /> },
                {
                  key: 'review',
                  label: '复习卷',
                  children: <ReviewTab key={course.id} courseId={course.id} courseTitle={course.title} />,
                },
              ]}
            />
          </>
        )}
      </StateView>
    </Space>
  )
}
