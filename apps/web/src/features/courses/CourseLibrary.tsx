import { BookOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Col, Row, Space, Tag, Typography } from 'antd'
import { WorkspaceLink as Link } from '../agent/WorkspaceLink'
import { StateView } from '../../components/StateView'
import { useCourses, useConnections } from '../../lib/api/hooks'
import { platformLabel } from '../../lib/platforms'
import { AttachSourceRefButton } from './AttachSourceRef'
import { PlatformCourseImport } from './PlatformCourseImport'
import { API_MODE } from '../../lib/api/http'

export function CourseLibrary() {
  const coursesQuery = useCourses()
  const connections = useConnections()
  const courses = coursesQuery.data?.items ?? []
  const unattached = courses.filter((c) => c.source_refs.length === 0)

  return (
    <StateView
      state={coursesQuery.isLoading ? 'loading' : coursesQuery.isError ? 'error' : 'ready'}
      error={coursesQuery.error}
      onRetry={() => coursesQuery.refetch()}
    >
      <Space orientation="vertical" size={16} style={{ width: '100%' }}>
        {API_MODE === 'real' && <Space wrap>{connections.data?.items.map(connection =>
          <PlatformCourseImport key={connection.id} connection={connection} />)}</Space>}
        {unattached.length > 0 && (
          <Alert
            type="info"
            showIcon
            title={`有 ${unattached.length} 门课程未挂接平台来源`}
            description="挂接平台的课程来源后，平台同步的资料、作业与回放会自动归并到对应课程；映射由你手动确认，不自动猜测。"
          />
        )}
        {courses.length === 0 ? (
          <StateView state="empty" emptyText="还没有课程，先连接平台或手动导入资料">
            <Link to="/onboarding">
              <Button type="primary">打开账号与连接设置</Button>
            </Link>
          </StateView>
        ) : (
          <Row gutter={[16, 16]}>
            {courses.map((course) => (
              <Col key={course.id} xs={24} sm={12} lg={8}>
                <Card
                  title={course.title}
                  extra={
                    <Link to={`/courses/${course.id}`}>
                      <Button size="small" type="primary">
                        查看详情
                      </Button>
                    </Link>
                  }
                >
                  <Space orientation="vertical" size={4}>
                    {course.term && <Typography.Text type="secondary">{course.term}</Typography.Text>}
                    {course.teacher && (
                      <Typography.Text type="secondary">授课：{course.teacher}</Typography.Text>
                    )}
                    <Space wrap size={4}>
                      {course.source_refs.length === 0 ? (
                        <>
                          <Tag>未挂接平台来源</Tag>
                          <AttachSourceRefButton
                            courseId={course.id}
                            courseTitle={course.title}
                            size="small"
                            type="link"
                          >
                            挂接
                          </AttachSourceRefButton>
                        </>
                      ) : (
                        course.source_refs.map((ref) => (
                          <Tag key={`${ref.platform}-${ref.external_id}`} icon={<BookOutlined />}>
                            {platformLabel(ref.platform)}
                          </Tag>
                        ))
                      )}
                    </Space>
                  </Space>
                </Card>
              </Col>
            ))}
          </Row>
        )}
      </Space>
    </StateView>
  )
}
