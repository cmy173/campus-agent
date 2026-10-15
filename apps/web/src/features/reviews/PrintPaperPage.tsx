import { ArrowLeftOutlined, PrinterOutlined } from '@ant-design/icons'
import { Button, Space, Spin, Tag, Typography } from 'antd'
import { useEffect, useMemo } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { WorkspaceLink as Link } from '../agent/WorkspaceLink'
import { ReviewQuestionView } from './ReviewQuestionView'
import { useReviewPaper } from '../../lib/api/hooks'
import { toApiError } from '../../lib/api/http'
import { REVIEW_REVISION_REASON_LABELS, materialTitlesOf } from '../../lib/review'
import { fmtDateTime } from '../../lib/time'

/**
 * 打印页（独立路由，不套用应用布局）：
 * variant=student 为学生版（题目与选项，无答案）；variant=answers 为答案版（含答案/解析/评分点）。
 * ?revision=N 可打印历史版本；不渲染调试信息与密钥；“打印/返回”控件在打印时隐藏。
 */
export function PrintPaperPage() {
  const { paperId } = useParams<{ paperId: string }>()
  const [searchParams] = useSearchParams()
  const variant = searchParams.get('variant') === 'answers' ? 'answers' : 'student'
  const revisionParam = searchParams.get('revision')
  const revision = revisionParam && /^\d+$/.test(revisionParam) ? Number(revisionParam) : undefined
  const paperQuery = useReviewPaper(paperId, revision)
  const paper = paperQuery.data
  const titleForMaterial = useMemo(() => (paper ? materialTitlesOf(paper) : undefined), [paper])

  useEffect(() => {
    if (paper) {
      document.title = `${paper.title} · v${paper.revision} · ${variant === 'answers' ? '答案版' : '学生版'}`
    }
    return () => {
      document.title = 'Campus Agent · 校园助手'
    }
  }, [paper, variant])

  const keepQuery = (v: string) =>
    `?variant=${v}${revision !== undefined ? `&revision=${revision}` : ''}`

  return (
    <div className="print-page" style={{ maxWidth: 860, margin: '0 auto', padding: '24px 16px' }}>
      <style>{`
        @media print {
          .no-print { display: none !important; }
          .print-page { max-width: none; padding: 0; }
          .review-question { break-inside: avoid; }
          body { background: #fff; }
        }
      `}</style>
      <div className="no-print" style={{ marginBottom: 12 }}>
        <Space wrap>
          <Link to={`/reviews/${paperId ?? ''}${revision !== undefined ? `?revision=${revision}` : ''}`}>
            <Button size="small" icon={<ArrowLeftOutlined />}>
              返回试卷详情
            </Button>
          </Link>
          <Button size="small" type="primary" icon={<PrinterOutlined />} onClick={() => window.print()}>
            打印 / 另存为 PDF
          </Button>
          <Space size={4}>
            <Link to={`/print/reviews/${paperId}${keepQuery('student')}`}>
              <Button size="small" type={variant === 'student' ? 'primary' : 'default'} ghost={variant === 'student'}>
                学生版
              </Button>
            </Link>
            <Link to={`/print/reviews/${paperId}${keepQuery('answers')}`}>
              <Button size="small" type={variant === 'answers' ? 'primary' : 'default'} ghost={variant === 'answers'}>
                答案版
              </Button>
            </Link>
          </Space>
        </Space>
      </div>

      {paperQuery.isLoading && <Spin />}
      {paperQuery.isError && (
        <Typography.Text type="danger">
          加载失败：{toApiError(paperQuery.error).message}
        </Typography.Text>
      )}
      {paper && (
        <>
          <Typography.Title level={3} style={{ marginBottom: 4 }}>
            {paper.title}
          </Typography.Title>
          <Space wrap size={6}>
            <Tag>v{paper.revision}</Tag>
            <Tag>{REVIEW_REVISION_REASON_LABELS[paper.revision_reason]}</Tag>
          </Space>
          <Typography.Text type="secondary" style={{ fontSize: 13, display: 'block', marginTop: 4 }}>
            满分 {paper.summary.total_points} 分
            {paper.constraints.duration_minutes !== null
              ? ` · 建议用时 ${paper.constraints.duration_minutes} 分钟`
              : ` · 预计用时约 ${paper.summary.estimated_minutes} 分钟（引擎估算）`}
            {' · '}
            {variant === 'answers' ? '答案版（含解析与评分点）' : '学生版'}
            {' · '}
            生成于 {fmtDateTime(paper.created_at)}
          </Typography.Text>
          {variant === 'student' && (
            <Typography.Paragraph style={{ marginTop: 12 }}>
              姓名：＿＿＿＿＿＿　　学号：＿＿＿＿＿＿＿＿　　得分：＿＿＿＿＿
            </Typography.Paragraph>
          )}
          <div style={{ marginTop: 16 }}>
            {paper.questions.map((q) => (
              <div key={q.id} style={{ marginBottom: 18 }}>
                <ReviewQuestionView
                  question={q}
                  courseId={paper.course_id}
                  showAnswer={variant === 'answers'}
                  titleForMaterial={titleForMaterial}
                />
              </div>
            ))}
          </div>
          {paper.warnings.length > 0 && variant === 'answers' && (
            <div style={{ marginTop: 16 }}>
              <Typography.Text strong>生成提示（如实记录）：</Typography.Text>
              <ul>
                {paper.warnings.map((w, i) => (
                  <li key={`${w.code}-${i}`}>
                    [{w.code}] {w.message}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </div>
  )
}
