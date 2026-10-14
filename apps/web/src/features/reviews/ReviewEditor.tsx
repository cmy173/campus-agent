import { ArrowLeftOutlined, DownloadOutlined, PrinterOutlined, RedoOutlined } from '@ant-design/icons'
import { Alert, App as AntApp, Button, Card, Descriptions, Dropdown, Space, Table, Tag, Typography } from 'antd'
import { useMemo, useState } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { WorkspaceLink as Link } from '../agent/WorkspaceLink'
import { StateView } from '../../components/StateView'
import { useCourses, useExportReviewPaper, useReviewPaper, useReviewRevisions } from '../../lib/api/hooks'
import { toApiError } from '../../lib/api/http'
import {
  REVIEW_DIFFICULTY_LABELS,
  REVIEW_EXPORT_FORMAT_LABELS,
  REVIEW_MATERIAL_REASON_LABELS,
  REVIEW_QUESTION_TYPE_LABELS,
  REVIEW_REVISION_REASON_LABELS,
  REVIEW_SNAPSHOT_KIND_COLORS,
  REVIEW_SNAPSHOT_KIND_LABELS,
  materialTitlesOf,
  reviewHostOf,
  reviewLocatorText,
  reviewModeLabel,
} from '../../lib/review'
import { fmtDateTime } from '../../lib/time'
import { ReviewQuestionView } from './ReviewQuestionView'
import type { ReviewExportFormat, ReviewTextExport, SnapshotItem } from '../../types/review'

function locatorsText(row: SnapshotItem): string {
  const texts = row.locators.map((l) => reviewLocatorText(l.locator)).filter((t) => t.length > 0)
  if (texts.length === 0) return '—'
  if (texts.length <= 2) return texts.join('、')
  return `${texts[0]} 等 ${texts.length} 处`
}

function downloadReviewExport(res: ReviewTextExport, fallbackName: string) {
  const blob = new Blob([res.text], { type: res.content_type ?? 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = res.filename ?? fallbackName
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

function fallbackExportName(paperId: string, format: ReviewExportFormat): string {
  const ext = format === 'markdown' ? 'md' : 'html'
  return `${paperId}-${format}.${ext}`
}

export function ReviewEditor({ id }: { id?: string }) {
  const params = useParams<{ paperId: string }>()
  const paperId = id ?? params.paperId
  const [searchParams] = useSearchParams()
  const revisionRaw = searchParams.get('revision')
  const viewedRevision =
    revisionRaw && /^\d+$/.test(revisionRaw) && Number(revisionRaw) >= 1 ? Number(revisionRaw) : undefined

  const paperQuery = useReviewPaper(paperId, viewedRevision)
  const revisionsQuery = useReviewRevisions(paperId)
  const coursesQuery = useCourses()
  const exportMutation = useExportReviewPaper()
  const { message } = AntApp.useApp()
  const [showAllAnswers, setShowAllAnswers] = useState(false)
  const [revealed, setRevealed] = useState<Set<string>>(new Set())

  const paper = paperQuery.data
  const titleForMaterial = useMemo(() => (paper ? materialTitlesOf(paper) : undefined), [paper])
  const courseTitles = new Map((coursesQuery.data?.items ?? []).map((c) => [c.id, c.title]))
  const latestRevision = revisionsQuery.data?.[0]?.revision

  const toggleOne = (questionId: string) => {
    setRevealed((prev) => {
      const next = new Set(prev)
      if (next.has(questionId)) next.delete(questionId)
      else next.add(questionId)
      return next
    })
  }

  const handleExport = (format: ReviewExportFormat) => {
    if (!paper) return
    exportMutation.mutate(
      { paperId: paper.id, format, revision: viewedRevision },
      {
        onSuccess: (res) => {
          downloadReviewExport(res, fallbackExportName(paper.id, format))
          message.success(`已导出${res.filename ? `：${res.filename}` : ''}`)
        },
        onError: (err) => message.error(toApiError(err).message),
      },
    )
  }

  const paperQuerySuffix = viewedRevision !== undefined ? `&revision=${viewedRevision}` : ''

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <Link to="/reviews">
        <Button size="small" type="link" icon={<ArrowLeftOutlined />}>
          返回复习卷列表
        </Button>
      </Link>

      <StateView
        state={paperQuery.isLoading ? 'loading' : paperQuery.isError ? 'error' : 'ready'}
        error={paperQuery.error}
        onRetry={() => paperQuery.refetch()}
      >
        {paper && (
          <>
            {latestRevision !== undefined && paper.revision < latestRevision && (
              <Alert
                type="info"
                showIcon
                title={`正在查看历史版本 v${paper.revision}（最新为 v${latestRevision}）`}
                description="历史版本为只读快照；导出与打印跟随当前查看的版本。"
                action={
                  <Link to={`/reviews/${paper.id}`}>
                    <Button size="small">回到最新版</Button>
                  </Link>
                }
              />
            )}

            <Card
              title={
                <Space wrap size={8}>
                  <Typography.Text strong style={{ fontSize: 18 }}>
                    {paper.title}
                  </Typography.Text>
                  <Tag>v{paper.revision}</Tag>
                  <Tag>{REVIEW_REVISION_REASON_LABELS[paper.revision_reason]}</Tag>
                  <Tag color="blue">{reviewModeLabel(paper.mode)}</Tag>
                  {paper.warnings.length > 0 && <Tag color="orange">{paper.warnings.length} 条生成提示</Tag>}
                </Space>
              }
              extra={
                <Space wrap size={8}>
                  <Dropdown
                    menu={{
                      items: (['markdown', 'student', 'answers'] as ReviewExportFormat[]).map((f) => ({
                        key: f,
                        label: REVIEW_EXPORT_FORMAT_LABELS[f],
                      })),
                      onClick: ({ key }) => handleExport(key as ReviewExportFormat),
                    }}
                  >
                    <Button size="small" icon={<DownloadOutlined />} loading={exportMutation.isPending}>
                      导出
                    </Button>
                  </Dropdown>
                  <a href={`/print/reviews/${paper.id}?variant=student${paperQuerySuffix}`} target="_blank" rel="noreferrer">
                    <Button size="small" icon={<PrinterOutlined />}>
                      打印学生版
                    </Button>
                  </a>
                  <a href={`/print/reviews/${paper.id}?variant=answers${paperQuerySuffix}`} target="_blank" rel="noreferrer">
                    <Button size="small" icon={<PrinterOutlined />}>
                      打印答案版
                    </Button>
                  </a>
                  <Link to={`/courses/${paper.course_id}?tab=review&base=${paper.id}`}>
                    <Button size="small" icon={<RedoOutlined />}>
                      以此卷为底重做
                    </Button>
                  </Link>
                </Space>
              }
            >
              <Descriptions
                column={2}
                size="small"
                items={[
                  {
                    key: 'course',
                    label: '课程',
                    children: (
                      <Link to={`/courses/${paper.course_id}`}>
                        {courseTitles.get(paper.course_id) ?? paper.course_id}
                      </Link>
                    ),
                  },
                  { key: 'created', label: '生成时间', children: fmtDateTime(paper.created_at) },
                  {
                    key: 'score',
                    label: '总分 / 用时',
                    children:
                      paper.constraints.duration_minutes !== null
                        ? `${paper.summary.total_points} 分 · 建议 ${paper.constraints.duration_minutes} 分钟`
                        : `${paper.summary.total_points} 分 · 预计约 ${paper.summary.estimated_minutes} 分钟（引擎估算）`,
                  },
                  {
                    key: 'answer_display',
                    label: '答案显示方式',
                    children: paper.constraints.answer_display === 'inline' ? '答案紧随题目' : '题目与答案分开',
                  },
                  {
                    key: 'provider',
                    label: '生成所用模型服务',
                    children: paper.provider.config_id
                      ? [
                          paper.provider.name,
                          paper.provider.base_url ? reviewHostOf(paper.provider.base_url) : null,
                          paper.provider.model,
                        ]
                          .filter(Boolean)
                          .join(' · ')
                      : paper.mode === 'bank'
                        ? '纯网络题库模式（未调用模型）'
                        : '未记录',
                  },
                  {
                    key: 'revision_source',
                    label: '修订来源',
                    children:
                      paper.based_on_revision !== null ? (
                        <Link to={`/reviews/${paper.id}?revision=${paper.based_on_revision}`}>
                          基于 v{paper.based_on_revision}（{REVIEW_REVISION_REASON_LABELS[paper.revision_reason]}）
                        </Link>
                      ) : (
                        '首次生成'
                      ),
                  },
                  { key: 'engine', label: '引擎版本', children: paper.engine_version },
                  {
                    key: 'fingerprint',
                    label: '快照指纹',
                    children: (
                      <Typography.Text
                        style={{ fontFamily: 'monospace', fontSize: 12 }}
                        title={paper.snapshot.fingerprint}
                      >
                        {paper.snapshot.fingerprint.slice(0, 26)}…
                      </Typography.Text>
                    ),
                  },
                ]}
              />
            </Card>

            <Card size="small" title="本次出卷备注（原文快照）">
              {paper.snapshot.notes.text.length > 0 ? (
                <>
                  <pre
                    style={{
                      margin: 0,
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word',
                      fontFamily: 'inherit',
                      fontSize: 13,
                    }}
                  >
                    {paper.snapshot.notes.text}
                  </pre>
                  <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 6 }}>
                    {paper.snapshot.notes.chars} 字符 · 指纹 {paper.snapshot.notes.sha256.slice(0, 18)}…
                  </Typography.Text>
                </>
              ) : (
                <Typography.Text type="secondary">本次未填写备注。</Typography.Text>
              )}
            </Card>

            <Card
              size="small"
              title={`材料快照（生成时实际发送；正文合计 ${paper.snapshot.total_chars} 字符）`}
            >
              <Space orientation="vertical" size={8} style={{ width: '100%' }}>
                <Table
                  size="small"
                  rowKey={(row) => `${row.kind}-${row.resource_id ?? row.assignment_id ?? row.title}`}
                  pagination={false}
                  dataSource={paper.snapshot.items}
                  columns={[
                    {
                      title: '材料',
                      dataIndex: 'title',
                      render: (_: unknown, row: SnapshotItem) => (
                        <Space size={6} wrap>
                          <Tag color={REVIEW_SNAPSHOT_KIND_COLORS[row.kind]}>{REVIEW_SNAPSHOT_KIND_LABELS[row.kind]}</Tag>
                          <span>{row.title}</span>
                          {row.version !== null && <Tag>{`v${row.version}`}</Tag>}
                        </Space>
                      ),
                    },
                    {
                      title: '发送正文',
                      width: 180,
                      render: (_: unknown, row: SnapshotItem) => (
                        <Typography.Text>
                          {row.chars} 字符
                          {row.chunk_ids.length > 0 ? ` · ${row.chunk_ids.length} 个片段` : ''}
                        </Typography.Text>
                      ),
                    },
                    {
                      title: '定位',
                      width: 200,
                      render: (_: unknown, row: SnapshotItem) =>
                        row.chunk_ids.length > 0 ? (
                          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                            {locatorsText(row)}
                          </Typography.Text>
                        ) : (
                          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                            全文
                          </Typography.Text>
                        ),
                    },
                  ]}
                />
                {paper.snapshot.missing.length > 0 && (
                  <Alert
                    type="warning"
                    showIcon
                    title={`${paper.snapshot.missing.length} 项所选材料未纳入正文（不以标题冒充正文）`}
                    description={
                      <ul style={{ margin: 0, paddingLeft: 18 }}>
                        {paper.snapshot.missing.map((miss, i) => (
                          <li key={`${miss.kind}-${miss.resource_id ?? miss.assignment_id ?? miss.title}-${i}`}>
                            {miss.title}（{REVIEW_MATERIAL_REASON_LABELS[miss.reason]}）：{miss.message}
                          </li>
                        ))}
                      </ul>
                    }
                  />
                )}
              </Space>
            </Card>

            {paper.warnings.length > 0 && (
              <Alert
                type="warning"
                showIcon
                title="生成提示（如实记录）"
                description={
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {paper.warnings.map((w, i) => (
                      <li key={`${w.code}-${i}`}>
                        [{w.code}] {w.message}
                      </li>
                    ))}
                  </ul>
                }
              />
            )}

            <Card
              size="small"
              title={`题目（共 ${paper.summary.question_count} 题 · ${paper.summary.total_points} 分）`}
              extra={
                <Space>
                  <Button size="small" onClick={() => setShowAllAnswers((v) => !v)}>
                    {showAllAnswers ? '隐藏全部答案' : '显示全部答案与解析'}
                  </Button>
                </Space>
              }
            >
              <Space orientation="vertical" size={20} style={{ width: '100%' }}>
                {paper.questions.map((q) => (
                  <ReviewQuestionView
                    key={q.id}
                    question={q}
                    courseId={paper.course_id}
                    showAnswer={showAllAnswers || revealed.has(q.id)}
                    onToggleAnswer={() => toggleOne(q.id)}
                    titleForMaterial={titleForMaterial}
                  />
                ))}
              </Space>
            </Card>

            <Card size="small" title="蓝图与配额（生成时的计划）">
              <Space orientation="vertical" size={8} style={{ width: '100%' }}>
                <Space wrap size={8}>
                  <Typography.Text strong>题型配额：</Typography.Text>
                  {paper.blueprint.type_quotas.map((q) => (
                    <Tag key={q.type}>
                      {REVIEW_QUESTION_TYPE_LABELS[q.type]} {q.count} 题 · {q.points_total} 分
                    </Tag>
                  ))}
                </Space>
                <Space wrap size={8}>
                  <Typography.Text strong>难度配额：</Typography.Text>
                  <Tag>简单 {paper.blueprint.difficulty_quota.easy}</Tag>
                  <Tag>中等 {paper.blueprint.difficulty_quota.medium}</Tag>
                  <Tag>较难 {paper.blueprint.difficulty_quota.hard}</Tag>
                </Space>
                <Typography.Text>
                  蓝图合计 {paper.blueprint.total_points} 分 · 预计约 {paper.blueprint.estimated_minutes} 分钟（引擎估算）
                </Typography.Text>
                {paper.blueprint.sections.map((section) => (
                  <div key={section.id}>
                    <Typography.Text strong>{section.title}</Typography.Text>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {' '}
                      · {section.question_slots.reduce((sum, slot) => sum + slot.count, 0)} 题 · 知识点{' '}
                      {section.knowledge_points.length} 项
                    </Typography.Text>
                    <div style={{ marginTop: 2 }}>
                      {section.question_slots.map((slot) => (
                        <Tag key={slot.slot_id}>
                          {REVIEW_QUESTION_TYPE_LABELS[slot.type]}×{slot.count}（{slot.points_each} 分/题
                          {slot.difficulty ? ` · ${REVIEW_DIFFICULTY_LABELS[slot.difficulty]}` : ''}）
                        </Tag>
                      ))}
                    </div>
                  </div>
                ))}
                {paper.blueprint.gaps.length > 0 && (
                  <Alert
                    type="warning"
                    showIcon
                    title="蓝图缺口（未隐瞒）"
                    description={
                      <ul style={{ margin: 0, paddingLeft: 18 }}>
                        {paper.blueprint.gaps.map((gap, i) => (
                          <li key={`${gap.kind}-${i}`}>
                            [{gap.kind}] {gap.message}
                            {gap.knowledge_point ? `（知识点：${gap.knowledge_point}）` : ''}
                          </li>
                        ))}
                      </ul>
                    }
                  />
                )}
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  复习卷不会自动加入学习计划；如需安排复习，请在计划页自行生成任务。
                </Typography.Text>
              </Space>
            </Card>

            <Card
              size="small"
              title="版本历史"
              extra={
                viewedRevision !== undefined &&
                latestRevision !== undefined &&
                viewedRevision < latestRevision ? (
                  <Link to={`/reviews/${paper.id}`}>
                    <Button size="small">回到最新版</Button>
                  </Link>
                ) : undefined
              }
            >
              {revisionsQuery.isLoading && <Typography.Text type="secondary">加载中…</Typography.Text>}
              {revisionsQuery.isError && (
                <Alert
                  type="error"
                  showIcon
                  title="版本历史加载失败"
                  description={toApiError(revisionsQuery.error).message}
                />
              )}
              {revisionsQuery.data && (
                <Space orientation="vertical" size={6} style={{ width: '100%' }}>
                  {revisionsQuery.data.map((rev) => {
                    const isCurrent = rev.revision === paper.revision
                    return (
                      <Space key={rev.revision} wrap size={8}>
                        <Tag color={isCurrent ? 'blue' : undefined}>v{rev.revision}</Tag>
                        <Tag>{REVIEW_REVISION_REASON_LABELS[rev.revision_reason]}</Tag>
                        {rev.revision === latestRevision && <Tag color="green">最新</Tag>}
                        <Typography.Text>
                          {rev.summary.question_count} 题 · {rev.summary.total_points} 分
                        </Typography.Text>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          {fmtDateTime(rev.created_at)}
                        </Typography.Text>
                        {isCurrent ? (
                          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                            当前查看
                          </Typography.Text>
                        ) : (
                          <Link to={`/reviews/${paper.id}?revision=${rev.revision}`}>查看该版</Link>
                        )}
                      </Space>
                    )
                  })}
                </Space>
              )}
            </Card>
          </>
        )}
      </StateView>
    </Space>
  )
}
