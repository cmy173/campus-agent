import { Button, Space, Tag, Typography } from 'antd'
import { WorkspaceLink as Link } from '../agent/WorkspaceLink'
import {
  REVIEW_DIFFICULTY_LABELS,
  REVIEW_ORIGIN_COLORS,
  REVIEW_ORIGIN_LABELS,
  REVIEW_QUESTION_TYPE_LABELS,
  REVIEW_VALIDATION_COLORS,
  REVIEW_VALIDATION_LABELS,
  type MaterialTitleLookup,
} from '../../lib/review'
import type { ReviewQuestion, ReviewQuestionSourceRef } from '../../types/review'

function refMaterialId(refItem: ReviewQuestionSourceRef): string | null {
  return refItem.resource_id ?? refItem.assignment_id
}

function SourceRefItem({
  refItem,
  courseId,
  titleForMaterial,
}: {
  refItem: ReviewQuestionSourceRef
  courseId: string
  titleForMaterial?: MaterialTitleLookup
}) {
  if (refItem.kind === 'note') {
    // CR009：出自用户备注原文（locator=备注第N行，其余 ID/URL 为 null）；纯文本，不产生任何链接
    return (
      <>
        <Tag color="purple">备注</Tag>
        <Typography.Text>{refItem.locator ?? '备注'}</Typography.Text>
      </>
    )
  }
  if (refItem.kind === 'web') {
    const label = `${refItem.site_name ?? '网络来源'}${refItem.locator ? ` · ${refItem.locator}` : ''}`
    if (refItem.url) {
      return (
        <>
          <Tag color="geekblue">网络题</Tag>
          <Typography.Link href={refItem.url} target="_blank" rel="noreferrer">
            {label} ↗
          </Typography.Link>
        </>
      )
    }
    return (
      <>
        <Tag color="geekblue">网络题</Tag>
        <Typography.Text type="secondary">{label}（原题链接缺失）</Typography.Text>
      </>
    )
  }
  const materialId = refMaterialId(refItem)
  const title = (materialId ? titleForMaterial?.(materialId) : null) ?? materialId ?? '未知材料'
  const locatorText = refItem.locator ? ` · ${refItem.locator}` : ''
  if (refItem.kind === 'assignment') {
    return (
      <>
        <Tag color="gold">作业</Tag>
        <Link to={`/courses/${courseId}?tab=assignments`}>
          《{title}》{locatorText}
        </Link>
      </>
    )
  }
  return (
    <>
      <Tag color="cyan">材料</Tag>
      <Link to={`/courses/${courseId}?tab=resources`}>
        《{title}》{locatorText}
      </Link>
    </>
  )
}

/**
 * 单题渲染：题干保留换行（word-break 防溢出），选项/答案/解析/评分点/来源。
 * 学生版打印（showAnswer=false 且无切换）与详情页共用。
 */
export function ReviewQuestionView({
  question,
  courseId,
  showAnswer,
  onToggleAnswer,
  titleForMaterial,
}: {
  question: ReviewQuestion
  courseId: string
  showAnswer: boolean
  onToggleAnswer?: () => void
  titleForMaterial?: MaterialTitleLookup
}) {
  const validation = question.validation
  return (
    <div className="review-question" style={{ breakInside: 'avoid' }}>
      <Space wrap size={6}>
        <Typography.Text strong>
          {question.ordinal}. （{REVIEW_QUESTION_TYPE_LABELS[question.question_type]}，{question.points} 分）
        </Typography.Text>
        <Tag color={REVIEW_ORIGIN_COLORS[question.origin.kind]}>{REVIEW_ORIGIN_LABELS[question.origin.kind]}</Tag>
        <Tag>{REVIEW_DIFFICULTY_LABELS[question.difficulty]}</Tag>
        {validation.status !== 'valid' && (
          <Tag color={REVIEW_VALIDATION_COLORS[validation.status]}>
            {REVIEW_VALIDATION_LABELS[validation.status]}
          </Tag>
        )}
      </Space>
      {question.origin.note && (
        <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 2 }}>
          {question.origin.note}
        </Typography.Text>
      )}
      <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: '8px 0' }}>
        {question.stem}
      </Typography.Paragraph>
      {question.options.length > 0 && (
        <ul style={{ margin: '0 0 8px', paddingLeft: 22 }}>
          {question.options.map((opt) => (
            <li key={opt.key} style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
              <Typography.Text strong>{opt.key}.</Typography.Text> {opt.text}
            </li>
          ))}
        </ul>
      )}
      {question.knowledge_points.length > 0 && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          知识点：{question.knowledge_points.join('；')}
        </Typography.Text>
      )}
      <div style={{ marginTop: 6 }}>
        {showAnswer ? (
          <div
            style={{
              background: '#fafafa',
              border: '1px solid #f0f0f0',
              borderRadius: 4,
              padding: '8px 10px',
              marginTop: 6,
            }}
          >
            <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', marginBottom: 6 }}>
              <Typography.Text strong>答案：</Typography.Text>
              {question.answer}
            </Typography.Paragraph>
            <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', marginBottom: 6 }}>
              <Typography.Text strong>解析：</Typography.Text>
              {question.explanation}
            </Typography.Paragraph>
            {question.grading_points.length > 0 && (
              <Typography.Paragraph style={{ marginBottom: 6 }}>
                <Typography.Text strong>评分要点：</Typography.Text>
                {question.grading_points.map((point, i) => (
                  <Typography.Text key={`${i}-${point}`} style={{ display: 'block' }}>
                    {i + 1}. {point}
                  </Typography.Text>
                ))}
              </Typography.Paragraph>
            )}
            {validation.status !== 'valid' && validation.messages.length > 0 && (
              <div>
                <Typography.Text type="warning" style={{ fontSize: 12, display: 'block' }}>
                  校验说明（{REVIEW_VALIDATION_LABELS[validation.status]}）：
                </Typography.Text>
                {validation.messages.map((msg, i) => (
                  <Typography.Text
                    key={`${i}-${msg}`}
                    type="warning"
                    style={{ fontSize: 12, display: 'block' }}
                  >
                    · {msg}
                  </Typography.Text>
                ))}
              </div>
            )}
          </div>
        ) : (
          onToggleAnswer && (
            <Button size="small" type="link" style={{ padding: 0 }} onClick={onToggleAnswer}>
              显示本题答案与解析
            </Button>
          )
        )}
      </div>
      <div style={{ marginTop: 6 }}>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          出处：
        </Typography.Text>
        <Space orientation="vertical" size={2} style={{ display: 'block', marginTop: 2 }}>
          {question.source_refs.map((refItem, i) => (
            <div key={`${refItem.kind}-${refItem.resource_id ?? refItem.assignment_id ?? refItem.url ?? i}`}>
              <Space size={4} wrap>
                <SourceRefItem refItem={refItem} courseId={courseId} titleForMaterial={titleForMaterial} />
              </Space>
            </div>
          ))}
          {question.source_refs.length === 0 && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              未提供出处（不编造引用）
            </Typography.Text>
          )}
        </Space>
      </div>
    </div>
  )
}
