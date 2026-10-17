import { FileSearchOutlined, SendOutlined } from '@ant-design/icons'
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Checkbox,
  Input,
  InputNumber,
  Modal,
  Radio,
  Select,
  Space,
  Steps,
  Tag,
  Typography,
} from 'antd'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { JobStatusTag, ParseStatusTag } from '../../components/tags'
import {
  useCancelJob,
  useCreateReviewRevision,
  useGenerateReview,
  usePreflightReview,
  useProviders,
  useQuestionbankCapabilities,
  useResourceChunks,
  useReviewJob,
  useReviewMaterials,
  useReviewPaper,
  useReviewPapers,
  useRetryJob,
} from '../../lib/api/hooks'
import { toApiError, apiErrorText } from '../../lib/api/http'
import {
  REVIEW_DIFFICULTY_PREFERENCE_LABELS,
  REVIEW_MATERIAL_REASON_ADVICE,
  REVIEW_MODE_LABELS,
  REVIEW_NOTES_LIMIT,
  REVIEW_QUESTION_TYPE_OPTIONS,
  REVIEW_REVISION_REASON_LABELS,
  REVIEW_SNAPSHOT_KIND_COLORS,
  REVIEW_SNAPSHOT_KIND_LABELS,
  reviewMaterialReasonLabel,
  reviewModeLabel,
  reviewPhasesForMode,
} from '../../lib/review'
import { fmtDateTime } from '../../lib/time'
import { reviewGenerationKey } from '../../lib/reviewRequest'
import { ReferenceExamUpload } from './ReferenceExamUpload'
import type {
  MaterialUnavailableReason,
  ReviewAssignmentSelection,
  ReviewMaterialItem,
  ReviewPreflight,
  ReviewQuestionType,
  ReviewRequest,
  ReviewResourceSelection,
  ReviewRevisionRequest,
} from '../../types/review'
import {
  clearActiveReviewJob,
  clearReviewDraft,
  defaultDraft,
  draftFromPaper,
  loadActiveReviewJob,
  loadGenNonce,
  loadReviewDraft,
  saveActiveReviewJob,
  saveGenNonce,
  saveReviewDraft,
  type ActiveReviewJob,
  type ReviewDraftState,
} from './draft'

function buildRequest(draft: ReviewDraftState, courseId: string): ReviewRequest {
  return {
    course_id: courseId,
    mode: draft.mode,
    provider_config_id: draft.mode === 'bank' ? null : draft.provider_config_id,
    title: draft.title.trim() || null,
    materials: { resources: draft.resources, assignments: draft.assignments },
    notes: draft.notes,
    constraints: {
      question_types: draft.question_types,
      difficulty: draft.difficulty,
      total_points: draft.total_points,
      duration_minutes: draft.duration_minutes,
      answer_display: draft.answer_display,
    },
  }
}

function unusableReasonText(reason: MaterialUnavailableReason | null): string {
  if (!reason) return '无可用正文'
  return `${reviewMaterialReasonLabel(reason)}——${REVIEW_MATERIAL_REASON_ADVICE[reason]}`
}

// ---------- 片段选择（chunk_ids） ----------

function ChunkSelectModal({
  item,
  current,
  onSave,
  onCancel,
}: {
  item: ReviewMaterialItem
  current: string[] | null
  onSave: (chunkIds: string[] | null) => void
  onCancel: () => void
}) {
  const chunksQuery = useResourceChunks(item.id)
  const chunks = useMemo(() => {
    const list = [...(chunksQuery.data?.items ?? [])]
    list.sort((a, b) => a.ordinal - b.ordinal)
    return list
  }, [chunksQuery.data])
  const [selected, setSelected] = useState<Set<string> | 'all'>(current === null ? 'all' : new Set(current))

  const allIds = useMemo(() => chunks.map((c) => c.id), [chunks])
  const effectiveSelected: Set<string> = selected === 'all' ? new Set(allIds) : selected
  const isAll = selected === 'all' || (allIds.length > 0 && effectiveSelected.size === allIds.length)
  const selectedList = isAll ? allIds : allIds.filter((id) => effectiveSelected.has(id))

  return (
    <Modal
      title={`片段范围：《${item.title}》${item.version !== null ? ` v${item.version}` : ''}`}
      open
      onCancel={onCancel}
      onOk={() => onSave(isAll ? null : selectedList)}
      okText={isAll ? '保存（整份）' : `保存（已选 ${selectedList.length} 个片段）`}
      okButtonProps={{ disabled: selectedList.length === 0 }}
      cancelText="取消"
      width={640}
    >
      <Space orientation="vertical" size={8} style={{ width: '100%' }}>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          只有勾选的片段（chunk）会随请求发送；默认整份。片段定位（页码/幻灯片）来自解析结果，扫描图片内容不在其中。
        </Typography.Text>
        {chunksQuery.isLoading && <Typography.Text type="secondary">片段加载中…</Typography.Text>}
        {chunksQuery.isError && (
          <Alert type="error" showIcon title="片段加载失败" description={toApiError(chunksQuery.error).message} />
        )}
        {chunksQuery.isSuccess && chunks.length === 0 && (
          <Typography.Text type="warning">该资源当前版本没有可读片段。</Typography.Text>
        )}
        <Checkbox checked={isAll} onChange={(e) => setSelected(e.target.checked ? 'all' : new Set())}>
          整份（全选）
        </Checkbox>
        <div style={{ maxHeight: 320, overflowY: 'auto' }}>
          <Space orientation="vertical" size={4} style={{ width: '100%' }}>
            {chunks.map((chunk) => {
              const checked = selected === 'all' || selected.has(chunk.id)
              const locator =
                chunk.locator.kind === 'page'
                  ? `第 ${chunk.locator.page} 页`
                  : chunk.locator.kind === 'slide'
                    ? `第 ${chunk.locator.slide} 张幻灯片`
                    : chunk.locator.kind === 'timestamp'
                      ? `回放 ${chunk.locator.start_seconds}s 起`
                      : '正文片段'
              return (
                <Checkbox
                  key={chunk.id}
                  checked={checked}
                  onChange={(e) => {
                    const next = new Set(selected === 'all' ? allIds : selected)
                    if (e.target.checked) next.add(chunk.id)
                    else next.delete(chunk.id)
                    setSelected(next)
                  }}
                >
                  <Typography.Text>
                    片段 {chunk.ordinal + 1}（{locator}）：
                    <Typography.Text type="secondary">
                      {chunk.text.length > 60 ? `${chunk.text.slice(0, 60)}…` : chunk.text}
                    </Typography.Text>
                  </Typography.Text>
                </Checkbox>
              )
            })}
          </Space>
        </div>
      </Space>
    </Modal>
  )
}

// ---------- 材料行 ----------

function ResourceRow({
  item,
  selection,
  checked,
  onToggle,
  onSelectScope,
  onUseLatest,
  courseId,
}: {
  item: ReviewMaterialItem
  selection: ReviewResourceSelection | undefined
  checked: boolean
  onToggle: (checked: boolean) => void
  onSelectScope: () => void
  onUseLatest: () => void
  courseId: string
}) {
  const versionChanged =
    selection !== undefined &&
    selection.version !== null &&
    item.version !== null &&
    selection.version !== item.version
  return (
    <Space align="start" size={8} style={{ width: '100%' }}>
      <Checkbox checked={checked} disabled={!item.usable} onChange={(e) => onToggle(e.target.checked)}>
        <Space wrap size={6}>
          {item.parse_status !== null && <ParseStatusTag status={item.parse_status} />}
          <Typography.Text strong>{item.title}</Typography.Text>
          {item.version !== null && <Tag>{`v${item.version}`}</Tag>}
        </Space>
      </Checkbox>
      <div style={{ marginLeft: 24 }}>
        <Space orientation="vertical" size={2}>
          {item.usable ? (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              可发送正文 {item.chars ?? 0} 字符
              {item.chunk_count !== null ? ` · ${item.chunk_count} 个片段` : ''}
            </Typography.Text>
          ) : (
            <Typography.Text type="warning" style={{ fontSize: 12 }}>
              不可用：{unusableReasonText(item.unavailable_reason)}
            </Typography.Text>
          )}
          <Space wrap size={8}>
            {item.usable && checked && (
              <Button size="small" type="link" style={{ padding: 0 }} onClick={onSelectScope}>
                {selection && selection.chunk_ids !== null
                  ? `片段范围（已选 ${selection.chunk_ids.length} 个）`
                  : '片段范围（整份）'}
              </Button>
            )}
            {!item.usable && (
              <Link to={`/courses/${courseId}?tab=resources`}>
                <Typography.Text style={{ fontSize: 12 }}>去资料页处理解析</Typography.Text>
              </Link>
            )}
            {versionChanged && (
              <Space size={4}>
                <Tag color="orange">
                  资料已更新到 v{item.version}，当前选择 v{selection?.version ?? '—'}
                </Tag>
                <Button size="small" type="link" style={{ padding: 0 }} onClick={onUseLatest}>
                  改用最新版本
                </Button>
              </Space>
            )}
          </Space>
        </Space>
      </div>
    </Space>
  )
}

function AssignmentRow({
  item,
  selection,
  checked,
  onToggle,
  onIncludeAttachments,
}: {
  item: ReviewMaterialItem
  selection: ReviewAssignmentSelection | undefined
  checked: boolean
  onToggle: (checked: boolean) => void
  onIncludeAttachments: (include: boolean) => void
}) {
  const usableAttachments = item.attachments.filter((att) => att.usable)
  const anyUsable = item.usable || usableAttachments.length > 0
  return (
    <Space align="start" size={8} style={{ width: '100%' }}>
      <Checkbox checked={checked} disabled={!anyUsable} onChange={(e) => onToggle(e.target.checked)}>
        <Space wrap size={6}>
          <Tag color="gold">作业</Tag>
          <Typography.Text strong>{item.title}</Typography.Text>
          {item.due_at && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              截止 {fmtDateTime(item.due_at)}
            </Typography.Text>
          )}
        </Space>
      </Checkbox>
      <div style={{ marginLeft: 24 }}>
        <Space orientation="vertical" size={2}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {item.chars !== null && item.chars > 0 ? `作业正文 ${item.chars} 字符` : (usableAttachments.length > 0 ? '题目正文位于附件中' : '尚无可读的作业正文')}
            {item.attachments.length > 0 ? `；附件 ${item.attachments.length} 个` : ''}
            {!anyUsable ? '；该作业没有可发送的正文（仅有标题时不出题）' : ''}
          </Typography.Text>
          {item.attachments.length > 0 && (
            <Space orientation="vertical" size={2}>
              {item.attachments.map((att) => (
                <Typography.Text key={att.resource_id} type="secondary" style={{ fontSize: 12 }}>
                  {att.usable
                    ? `附件：${att.title}（${att.chars ?? 0} 字符${att.chunk_count !== null ? ` · ${att.chunk_count} 个片段` : ''}）`
                    : `附件不可用：${att.title}——${unusableReasonText(att.unavailable_reason)}`}
                </Typography.Text>
              ))}
              <Checkbox
                checked={selection?.include_attachments ?? false}
                disabled={!checked || usableAttachments.length === 0}
                onChange={(e) => onIncludeAttachments(e.target.checked)}
              >
                <Typography.Text style={{ fontSize: 12 }}>包含附件正文（随请求一起发送）</Typography.Text>
              </Checkbox>
            </Space>
          )}
        </Space>
      </div>
    </Space>
  )
}

// ---------- 生成任务跟踪（阶段 + 取消/重试 + 恢复） ----------

export function ReviewJobTracker({
  courseId,
  jobId,
  mode,
  onTerminal,
  onActiveChange,
  onForget,
}: {
  courseId: string
  jobId: string
  mode: ReviewDraftState['mode']
  onTerminal?: (status: string, paperId: string | null) => void
  onActiveChange?: (active: boolean) => void
  onForget?: () => void
}) {
  const { message } = AntApp.useApp()
  const [trackedId, setTrackedId] = useState(jobId)
  useEffect(() => setTrackedId(jobId), [jobId])
  const jobQuery = useReviewJob(trackedId)
  const cancelJob = useCancelJob()
  const retryJob = useRetryJob()
  const notifiedRef = useRef<string | null>(null)

  const job = jobQuery.data
  const phases = reviewPhasesForMode(mode)
  const phase = job?.phase ?? null
  const phaseIndex = phase ? phases.findIndex((p) => p.key === phase) : -1
  const active = job ? job.status === 'queued' || job.status === 'running' : false

  useEffect(() => {
    onActiveChange?.(active)
  }, [active, onActiveChange])

  useEffect(() => {
    if (!job) return
    const terminal = job.status !== 'queued' && job.status !== 'running'
    const key = `${job.id}:${job.status}`
    if (terminal && notifiedRef.current !== key) {
      notifiedRef.current = key
      // 任务到达终态后不再需要恢复锚点；页面刷新后从历史列表查看结果
      clearActiveReviewJob(courseId)
      const paperId =
        job.status === 'succeeded'
          ? (job.result_refs.find((r) => r.type === 'review_paper')?.id ?? null)
          : null
      onTerminal?.(job.status, paperId)
    }
  }, [job, courseId, onTerminal])

  if (jobQuery.isLoading) return <Typography.Text type="secondary">生成任务状态加载中…</Typography.Text>
  if (jobQuery.isError) {
    const apiError = toApiError(jobQuery.error)
    const notFound = apiError.status === 404
    return (
      <Alert
        type="error"
        showIcon
        title={notFound ? '任务记录不存在（可能已被清理）' : `任务状态获取失败（${apiError.code}）`}
        description={
          notFound
            ? '本地记录的生成任务在后端已不存在；可清除本地任务记录后重新生成。'
            : apiError.message
        }
        action={
          notFound && onForget ? (
            <Button size="small" onClick={onForget}>
              清除本地任务记录
            </Button>
          ) : undefined
        }
      />
    )
  }
  if (!job) return null

  const isTerminal = !active
  const current =
    isTerminal && job.status === 'succeeded' ? phases.length : phaseIndex >= 0 ? phaseIndex : 0
  const stepsStatus =
    job.status === 'cancelled' || job.status === 'failed' || job.status === 'partial_failed'
      ? 'error'
      : active
        ? 'process'
        : 'finish'
  const paperId =
    job.status === 'succeeded' ? (job.result_refs.find((r) => r.type === 'review_paper')?.id ?? null) : null

  return (
    <Card size="small" title="生成任务">
      <Space orientation="vertical" size={8} style={{ width: '100%' }}>
        <Space wrap>
          <JobStatusTag status={job.status} />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {job.id}
          </Typography.Text>
          {job.status === 'succeeded' && <Typography.Text type="success">复习卷已保存</Typography.Text>}
        </Space>
        <Steps
          size="small"
          current={current}
          status={stepsStatus}
          items={phases.map((p) => ({ title: p.label }))}
        />
        {job.progress !== null && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            进度 {Math.round(job.progress * 100)}%
            {phase ? ` · 当前阶段：${phases.find((p) => p.key === phase)?.label ?? phase}` : ''}
            （阶段与进度以服务端任务为准，不伪造）
          </Typography.Text>
        )}
        {job.warnings.length > 0 && (
          <Alert
            type="warning"
            showIcon
            title="生成完成但有提示（请核对）"
            description={
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {job.warnings.map((w, i) => (
                  <li key={`${w.code}-${i}`}>
                    [{w.code}] {w.message}
                  </li>
                ))}
              </ul>
            }
          />
        )}
        {job.errors.length > 0 && (
          <Alert
            type="error"
            showIcon
            title="生成失败明细"
            description={
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {job.errors.map((err) => (
                  <li key={`${err.code}-${err.message}`}>
                    {err.message}（{err.code}）{err.retryable ? ' · 可重试' : ''}
                  </li>
                ))}
              </ul>
            }
          />
        )}
        {job.status === 'cancelled' && (
          <Alert type="warning" showIcon title="已取消：未生成新的复习卷（旧卷保留在历史列表）" />
        )}
        <Space wrap>
          {job.status === 'succeeded' && paperId && (
            <Link to={`/reviews/${paperId}`}>
              <Button size="small" type="primary">
                查看复习卷
              </Button>
            </Link>
          )}
          {active && !job.cancel_requested && (
            <Button
              size="small"
              loading={cancelJob.isPending}
              onClick={() =>
                cancelJob.mutate(job.id, {
                  onError: (err) => message.error(toApiError(err).message),
                  onSuccess: (updated) =>
                    message.info(updated.status === 'cancelled' ? '任务已取消' : '已请求取消任务'),
                })
              }
            >
              取消生成
            </Button>
          )}
          {(job.status === 'failed' || job.status === 'partial_failed') && (
            <Button
              size="small"
              type="primary"
              loading={retryJob.isPending}
              onClick={() =>
                retryJob.mutate(job.id, {
                  onError: (err) => message.error(toApiError(err).message),
                  onSuccess: (data) => {
                    message.success('已提交重试（按原请求重跑，旧卷保留）')
                    setTrackedId(data.job.id)
                    saveActiveReviewJob(courseId, { jobId: data.job.id, mode })
                  },
                })
              }
            >
              重试
            </Button>
          )}
          {isTerminal && onForget && (
            <Button size="small" onClick={onForget}>
              清除任务记录
            </Button>
          )}
        </Space>
        {active && job.cancel_requested && (
          <Typography.Text type="warning">已请求取消，当前步骤结束后停止</Typography.Text>
        )}
      </Space>
    </Card>
  )
}

// ---------- 发送预览 ----------

function PreflightPanel({
  preflight,
  stale,
  requestMode,
  generating,
  activeJobRunning,
  onGenerate,
}: {
  preflight: ReviewPreflight
  stale: boolean
  requestMode: ReviewRequest['mode']
  generating: boolean
  activeJobRunning: boolean
  onGenerate: () => void
}) {
  const { materials, provider, limits, bank } = preflight
  return (
    <Space orientation="vertical" size={8} style={{ width: '100%' }}>
      {stale && (
        <Alert type="warning" showIcon title="参数已修改：请重新预览，确认发送内容后再生成" />
      )}
      <Typography.Text strong>将发送的材料</Typography.Text>
      <ul style={{ margin: 0, paddingLeft: 18 }}>
        {materials.items.map((item, i) => (
          <li key={`${item.kind}-${item.resource_id ?? item.assignment_id ?? i}`}>
            <Tag color={REVIEW_SNAPSHOT_KIND_COLORS[item.kind]}>{REVIEW_SNAPSHOT_KIND_LABELS[item.kind]}</Tag>
            {item.title}
            {item.version !== null ? ` v${item.version}` : ''}：
            <Typography.Text type="success">将发送 {item.chars} 字符正文</Typography.Text>
            {item.chunk_ids.length > 0 ? `（${item.chunk_ids.length} 个片段）` : ''}
          </li>
        ))}
        {materials.items.length === 0 && <li>（未选择有正文的材料）</li>}
      </ul>
      {materials.missing.length > 0 && (
        <Alert
          type="warning"
          showIcon
          title="以下所选材料无可发送正文（不以标题冒充正文）"
          description={
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {materials.missing.map((miss, i) => (
                <li key={`${miss.kind}-${miss.resource_id ?? miss.assignment_id ?? miss.title}-${i}`}>
                  {miss.title}（{reviewMaterialReasonLabel(miss.reason)}）：{miss.message}
                </li>
              ))}
            </ul>
          }
        />
      )}
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        材料合计 {materials.total_chars} / {limits.material_max_chars} 字符；题数合计{' '}
        {limits.question_count_total} / 上限 {limits.question_count_max}（超限将被后端拒绝，不静默截断）。
      </Typography.Text>
      <Space orientation="vertical" size={4}>
        <Typography.Text strong>本次出卷备注（原文，与资料正文一起发送）</Typography.Text>
        <pre
          style={{
            margin: 0,
            padding: 8,
            background: '#fafafa',
            border: '1px solid #f0f0f0',
            borderRadius: 4,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            fontFamily: 'inherit',
            fontSize: 13,
          }}
        >
          {materials.notes.chars > 0 ? materials.notes.text : '（未填写备注）'}
        </pre>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          备注 {limits.notes_chars} / {limits.notes_max_chars} 字符 · 指纹 {materials.notes.sha256.slice(0, 18)}…
          ；换行与中文原样发送，不截断、不摘要。
        </Typography.Text>
      </Space>
      <Space orientation="vertical" size={4}>
        <Typography.Text strong>目标模型服务</Typography.Text>
        <Typography.Text>
          {requestMode === 'bank'
            ? '纯网络题库模式：不调用模型服务'
            : provider.config_id
              ? `将调用「${provider.name ?? provider.config_id}」 · ${provider.host ?? '—'} · 模型 ${provider.model ?? '—'}${provider.has_api_key ? '' : ' · 尚未设置密钥（生成会被拦截）'}`
              : '未选择模型设置（AI／混合模式需要先到设置页配置）'}
        </Typography.Text>
        {(requestMode === 'bank' || requestMode === 'hybrid') && (
          <Typography.Text type={bank.usable ? 'secondary' : 'warning'} style={{ fontSize: 12 }}>
            网络题库（{requestMode}）：{bank.usable ? '可用' : '不可用'}；已配置来源 {bank.configured_sources} 个
            {bank.message ? `——${bank.message}` : ''}
          </Typography.Text>
        )}
      </Space>
      {preflight.warnings.length > 0 && (
        <Alert
          type="warning"
          showIcon
          title="提醒（不阻止生成）"
          description={
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {preflight.warnings.map((w, i) => (
                <li key={`${w.code}-${i}`}>
                  [{w.code}] {w.message}
                </li>
              ))}
            </ul>
          }
        />
      )}
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        请求指纹 <Typography.Text style={{ fontFamily: 'monospace' }}>{preflight.fingerprint.slice(0, 26)}…</Typography.Text>
        （与生成请求一致；重复点击不会产生两份卷）
      </Typography.Text>
      <Space wrap>
        <Button
          type="primary"
          icon={<SendOutlined />}
          disabled={stale || generating || activeJobRunning}
          loading={generating}
          onClick={onGenerate}
        >
          生成复习卷
        </Button>
        {activeJobRunning && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            已有进行中的生成任务，完成后可再次生成
          </Typography.Text>
        )}
      </Space>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        生成复习卷不会自动向学习计划追加任务，也不会向学校平台提交任何内容。
      </Typography.Text>
    </Space>
  )
}

// ---------- 主组件 ----------

export function ReviewTab({ courseId, courseTitle }: { courseId: string; courseTitle: string }) {
  const { message } = AntApp.useApp()
  const [searchParams] = useSearchParams()
  const basePaperId = searchParams.get('base')

  const materialsQuery = useReviewMaterials(courseId)
  const providersQuery = useProviders()
  const papersQuery = useReviewPapers({ course_id: courseId })
  const basePaperQuery = useReviewPaper(basePaperId ?? undefined)
  const bankCapsQuery = useQuestionbankCapabilities()
  const preflightMutation = usePreflightReview()
  const generateMutation = useGenerateReview()
  const revisionMutation = useCreateReviewRevision()

  const [draft, setDraft] = useState<ReviewDraftState>(() => loadReviewDraft(courseId) ?? defaultDraft())
  const [draftSavedAt, setDraftSavedAt] = useState<string | null>(null)
  const [preflightState, setPreflightState] = useState<{ preflight: ReviewPreflight; snapshot: string } | null>(null)
  const [preflightError, setPreflightError] = useState<string | null>(null)
  const [activeJob, setActiveJob] = useState<ActiveReviewJob | null>(() => loadActiveReviewJob(courseId))
  const [jobActive, setJobActive] = useState(false)
  // 幂等键代数：同一代内同指纹防并发重复提交；取消/失败（非成功）终态后代数+1（并持久化，
  // 刷新后仍生效），使「取消后再生成同内容」不再重放已取消的旧任务；成功终态不换键，
  // 重复点击重放同结果，维持「不会产生两份卷」
  const [genNonce, setGenNonce] = useState(() => loadGenNonce(courseId))
  const [chunkModalFor, setChunkModalFor] = useState<ReviewMaterialItem | null>(null)
  const appliedBaseRef = useRef<string | null>(null)

  const request = buildRequest(draft, courseId)
  const requestSnapshot = JSON.stringify(request)
  const stale = preflightState !== null && preflightState.snapshot !== requestSnapshot

  const materials = materialsQuery.data ?? []
  const resources = materials.filter((m) => m.kind === 'resource')
  const assignments = materials.filter((m) => m.kind === 'assignment')
  const providers = providersQuery.data?.items ?? []
  const papers = papersQuery.data?.pages.flatMap((p) => p.items) ?? []

  const totalQuestions = draft.question_types.reduce((sum, q) => sum + q.count, 0)
  const notesChars = [...draft.notes].length
  const generating = generateMutation.isPending || revisionMutation.isPending
  const reviseBase = basePaperId !== null ? basePaperQuery.data : undefined

  // 自动保存草稿：刷新/切页返回后可恢复所选材料、参数与备注（密钥不在草稿内）
  useEffect(() => {
    saveReviewDraft(courseId, draft)
  }, [courseId, draft])

  // 未选模型时默认选择第一个已设密钥的服务（可修改）
  useEffect(() => {
    if (draft.mode === 'bank' || draft.provider_config_id !== null) return
    const list = providersQuery.data?.items ?? []
    if (list.length === 0) return
    const preferred = list.find((p) => p.has_api_key) ?? list[0]
    setDraft((d) => ({ ...d, provider_config_id: preferred.id }))
  }, [draft.mode, draft.provider_config_id, providersQuery.data])

  // 重做：按旧卷回填（生成新修订，旧版保留）
  useEffect(() => {
    if (!basePaperId || appliedBaseRef.current === basePaperId) return
    const paper = basePaperQuery.data
    if (!paper) return
    appliedBaseRef.current = basePaperId
    const fromPaper = draftFromPaper(paper)
    setDraft((d) => ({ ...fromPaper, provider_config_id: d.provider_config_id ?? fromPaper.provider_config_id }))
    setPreflightState(null)
    message.info(`已按旧卷回填参数；生成将保存为 v${paper.revision + 1} 修订（旧版保留）`)
  }, [basePaperId, basePaperQuery.data, message])

  const update = (patch: Partial<ReviewDraftState>) => {
    setDraft((d) => ({ ...d, ...patch }))
  }

  const toggleResource = (item: ReviewMaterialItem, checked: boolean) => {
    if (checked) {
      update({
        resources: [...draft.resources, { resource_id: item.id, version: item.version, chunk_ids: null }],
      })
    } else {
      update({ resources: draft.resources.filter((s) => s.resource_id !== item.id) })
    }
  }

  const toggleAssignment = (item: ReviewMaterialItem, checked: boolean) => {
    if (checked) {
      update({
        assignments: [...draft.assignments, { assignment_id: item.id, include_attachments: true }],
      })
    } else {
      update({ assignments: draft.assignments.filter((a) => a.assignment_id !== item.id) })
    }
  }

  const setIncludeAttachments = (assignmentId: string, include: boolean) => {
    update({
      assignments: draft.assignments.map((a) =>
        a.assignment_id === assignmentId ? { ...a, include_attachments: include } : a,
      ),
    })
  }

  const countOf = (type: ReviewQuestionType) =>
    draft.question_types.find((q) => q.type === type)?.count ?? 0
  const pointsEachOf = (type: ReviewQuestionType) =>
    draft.question_types.find((q) => q.type === type)?.points_each ?? null

  const setCount = (type: ReviewQuestionType, count: number) => {
    const rest = draft.question_types.filter((q) => q.type !== type)
    if (count <= 0) {
      update({ question_types: rest })
    } else {
      const existing = draft.question_types.find((q) => q.type === type)
      update({ question_types: [...rest, { type, count, points_each: existing?.points_each ?? null }] })
    }
  }

  const setPointsEach = (type: ReviewQuestionType, value: number | null) => {
    update({
      question_types: draft.question_types.map((q) => (q.type === type ? { ...q, points_each: value } : q)),
    })
  }

  const handlePreview = () => {
    preflightMutation.mutate(request, {
      onSuccess: (preflight) => {
        setPreflightError(null)
        setPreflightState({ preflight, snapshot: requestSnapshot })
      },
      onError: (err) => {
        setPreflightError(apiErrorText(err))
        setPreflightState(null)
      },
    })
  }

  const startTracking = (jobId: string) => {
    message.success('已提交生成任务')
    const active: ActiveReviewJob = { jobId, mode: draft.mode }
    setActiveJob(active)
    saveActiveReviewJob(courseId, active)
  }

  const handleGenerateError = (err: unknown) => {
    const apiError = toApiError(err)
    if (apiError.code === 'PREFLIGHT_FAILED') {
      setPreflightState(null)
      message.error('预检未通过：请重新预览查看阻止项')
    } else {
      message.error(`生成失败（${apiError.code}）：${apiError.message}`)
    }
  }

  const handleGenerate = () => {
    if (!preflightState) return
    const fingerprint = preflightState.preflight.fingerprint
    if (reviseBase) {
      const patch: ReviewRevisionRequest = {
        provider_config_id: request.provider_config_id,
        title: request.title,
        materials: request.materials,
        notes: request.notes,
        constraints: request.constraints,
      }
      revisionMutation.mutate(
        {
          paperId: reviseBase.id,
          input: patch,
          idempotencyKey: reviewGenerationKey(patch, fingerprint, genNonce, reviseBase.id),
          expectedFingerprint: fingerprint,
        },
        { onSuccess: (data) => startTracking(data.job.id), onError: handleGenerateError },
      )
    } else {
      generateMutation.mutate(
        { input: request, idempotencyKey: reviewGenerationKey(request, fingerprint, genNonce), expectedFingerprint: fingerprint },
        { onSuccess: (data) => startTracking(data.job.id), onError: handleGenerateError },
      )
    }
  }

  const selectedProvider = providers.find((p) => p.id === draft.provider_config_id) ?? null

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      {basePaperId && basePaperQuery.isError && (
        <Alert
          type="info"
          showIcon
          title="无法读取重做基准卷"
          description={`${toApiError(basePaperQuery.error).message}——将按新卷生成（不产生新修订）。`}
        />
      )}

      <Card size="small" title="1. 选择材料（课件 / 作业附件）">
        <Space orientation="vertical" size={8} style={{ width: '100%' }}>
          {materialsQuery.isLoading && (
            <Typography.Text type="secondary">材料加载中（自动翻页取全量）…</Typography.Text>
          )}
          {materialsQuery.isError && (
            <Alert
              type="error"
              showIcon
              title="材料加载失败"
              description={toApiError(materialsQuery.error).message}
            />
          )}
          {materialsQuery.isSuccess && materials.length === 0 && (
            <Typography.Text type="secondary">本课程暂无可选材料。</Typography.Text>
          )}
          {assignments.length > 0 && (
            <Space orientation="vertical" size={10} style={{ width: '100%' }}>
              <Typography.Text strong>作业（正文与附件正文）</Typography.Text>
              {assignments.map((item) => (
                <AssignmentRow
                  key={item.id}
                  item={item}
                  selection={draft.assignments.find((a) => a.assignment_id === item.id)}
                  checked={draft.assignments.some((a) => a.assignment_id === item.id)}
                  onToggle={(checked) => toggleAssignment(item, checked)}
                  onIncludeAttachments={(include) => setIncludeAttachments(item.id, include)}
                />
              ))}
            </Space>
          )}
          {resources.length > 0 && (
            <Space orientation="vertical" size={10} style={{ width: '100%' }}>
              <Typography.Text strong>课件与资料</Typography.Text>
              {resources.map((item) => (
                <ResourceRow
                  key={item.id}
                  item={item}
                  selection={draft.resources.find((s) => s.resource_id === item.id)}
                  checked={draft.resources.some((s) => s.resource_id === item.id)}
                  onToggle={(checked) => toggleResource(item, checked)}
                  onSelectScope={() => setChunkModalFor(item)}
                  onUseLatest={() =>
                    update({
                      resources: draft.resources.map((s) =>
                        s.resource_id === item.id ? { ...s, version: item.version, chunk_ids: null } : s,
                      ),
                    })
                  }
                  courseId={courseId}
                />
              ))}
            </Space>
          )}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            只有含可读正文的材料可勾选；不可用原因与处理建议如实标注（不以标题冒充正文）。
            资料更新后按所选版本发送，结果页可查看当时的材料快照（含实际发送正文）。
          </Typography.Text>
        </Space>
      </Card>

      <Card size="small" title="2. 出卷参数">
        <Space orientation="vertical" size={12} style={{ width: '100%' }}>
          <Space wrap size={8}>
            <Typography.Text>标题</Typography.Text>
            <Input
              style={{ width: 320 }}
              value={draft.title}
              placeholder={`${courseTitle} 复习卷（留空由服务端命名）`}
              onChange={(e) => update({ title: e.target.value })}
            />
            <Button size="small" onClick={() => update({ title: `${courseTitle} 复习卷` })}>
              使用建议标题
            </Button>
          </Space>
          <Space wrap size={16}>
            <Space size={8}>
              <Typography.Text>来源模式</Typography.Text>
              <Radio.Group
                value={draft.mode}
                onChange={(e) => update({ mode: e.target.value })}
                options={[
                  { value: 'ai', label: REVIEW_MODE_LABELS.ai },
                  { value: 'bank', label: REVIEW_MODE_LABELS.bank },
                  { value: 'hybrid', label: REVIEW_MODE_LABELS.hybrid },
                ]}
              />
            </Space>
          </Space>
          {draft.mode !== 'bank' && (
            <Space orientation="vertical" size={4}>
              <Space wrap size={8}>
                <Typography.Text>模型设置</Typography.Text>
                {providers.length > 0 ? (
                  <Select
                    style={{ minWidth: 300 }}
                    value={draft.provider_config_id ?? undefined}
                    placeholder="选择已保存的模型设置"
                    onChange={(value) => update({ provider_config_id: value })}
                    options={providers.map((p) => ({
                      value: p.id,
                      label: `${p.name}（${p.model}）${p.has_api_key ? '' : ' · 未设密钥'}`,
                    }))}
                  />
                ) : (
                  <Space size={8}>
                    <Typography.Text type="warning">尚未配置模型服务</Typography.Text>
                    <Link to="/settings">
                      <Button size="small">去设置页添加</Button>
                    </Link>
                  </Space>
                )}
              </Space>
              {draft.provider_config_id !== null && providers.length > 0 && selectedProvider === null && (
                <Typography.Text type="warning" style={{ fontSize: 12 }}>
                  当前选择的模型设置不存在（可能已被删除），请重新选择。
                </Typography.Text>
              )}
              {selectedProvider && !selectedProvider.has_api_key && (
                <Typography.Text type="warning" style={{ fontSize: 12 }}>
                  「{selectedProvider.name}」尚未设置密钥：可保存设置但无法测试连接与生成，请到设置页补填。
                </Typography.Text>
              )}
            </Space>
          )}
          {(draft.mode === 'bank' || draft.mode === 'hybrid') && (
            <Space orientation="vertical" size={4} style={{ width: '100%' }}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                网络题库来源（来自后端能力清单；未配置来源不得用于检索）：
              </Typography.Text>
              {bankCapsQuery.isLoading && (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  能力清单加载中…
                </Typography.Text>
              )}
              {bankCapsQuery.isError && (
                <Alert
                  type="warning"
                  showIcon
                  title="题库能力获取失败"
                  description={`${toApiError(bankCapsQuery.error).message}——bank 模式将被后端拒绝；hybrid 仅本地生成。`}
                />
              )}
              {bankCapsQuery.data && (
                <Space wrap size={6}>
                  {bankCapsQuery.data.sources.map((source) => (
                    <Tag key={source.id} color={source.configured ? 'green' : 'default'}>
                      {source.name}
                      {source.configured ? '（已配置）' : '（未配置）'}
                    </Tag>
                  ))}
                  {bankCapsQuery.data.sources.length === 0 && (
                    <Typography.Text type="warning" style={{ fontSize: 12 }}>
                      暂无题库来源：bank 模式将被拒绝；hybrid 仅本地生成并如实标注。
                    </Typography.Text>
                  )}
                </Space>
              )}
            </Space>
          )}
          <Space orientation="vertical" size={6} style={{ width: '100%' }}>
            <Typography.Text>
              题型与配分（合计 {totalQuestions} 题，上限 100；数量为 0 的题型不参与出卷）
            </Typography.Text>
            <Space wrap size={12}>
              {REVIEW_QUESTION_TYPE_OPTIONS.map((opt) => {
                const count = countOf(opt.value)
                return (
                  <Space key={opt.value} size={4}>
                    <Typography.Text>{opt.label}</Typography.Text>
                    <InputNumber
                      min={0}
                      max={100}
                      value={count}
                      onChange={(value) => setCount(opt.value, value ?? 0)}
                      style={{ width: 70 }}
                      aria-label={`${opt.label}数量`}
                    />
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      题
                    </Typography.Text>
                    <InputNumber
                      min={1}
                      max={200}
                      placeholder="分值自动"
                      disabled={count === 0}
                      value={pointsEachOf(opt.value)}
                      onChange={(value) => setPointsEach(opt.value, value ?? null)}
                      style={{ width: 110 }}
                      aria-label={`${opt.label}每题分值`}
                    />
                  </Space>
                )
              })}
            </Space>
            {totalQuestions > 100 && (
              <Typography.Text type="danger">合计 {totalQuestions} 题超过上限 100，请调整后再预览。</Typography.Text>
            )}
            {totalQuestions === 0 && <Typography.Text type="danger">至少需要 1 题。</Typography.Text>}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              每题分值留空=由引擎在总分约束下分配；填写则与总分合计校验（不一致时预览会报错）。
            </Typography.Text>
          </Space>
          <Space wrap size={16}>
            <Space size={8}>
              <Typography.Text>难度</Typography.Text>
              <Select
                style={{ width: 150 }}
                value={draft.difficulty ?? 'auto'}
                onChange={(value) =>
                  update({ difficulty: value === 'auto' ? null : (value as ReviewDraftState['difficulty']) })
                }
                options={[
                  { value: 'auto', label: '由引擎推断' },
                  ...Object.entries(REVIEW_DIFFICULTY_PREFERENCE_LABELS).map(([value, label]) => ({
                    value,
                    label,
                  })),
                ]}
              />
            </Space>
            <Space size={8}>
              <Typography.Text>总分（可空）</Typography.Text>
              <InputNumber
                min={1}
                max={500}
                placeholder="留空=引擎分配"
                value={draft.total_points}
                onChange={(value) => update({ total_points: value ?? null })}
                style={{ width: 150 }}
              />
            </Space>
            <Space size={8}>
              <Typography.Text>建议用时（分钟，可空）</Typography.Text>
              <InputNumber
                min={5}
                max={600}
                value={draft.duration_minutes}
                onChange={(value) => update({ duration_minutes: value ?? null })}
                style={{ width: 110 }}
              />
            </Space>
          </Space>
          <Space size={8}>
            <Typography.Text>答案显示方式</Typography.Text>
            <Radio.Group
              value={draft.answer_display}
              onChange={(e) => update({ answer_display: e.target.value })}
              options={[
                { value: 'separate', label: '题目与答案分开（默认）' },
                { value: 'inline', label: '答案紧随题目' },
              ]}
            />
          </Space>
        </Space>
      </Card>

      <Card size="small" title="3. 出卷备注（会与选定资料原文一起发送）">
        <Space orientation="vertical" size={8} style={{ width: '100%' }}>
          <Typography.Text type="secondary">若作业仅有教材上序号，请在备注中上传题目照片或教材电子版</Typography.Text>
          <ReferenceExamUpload courseId={courseId} onReady={ids => {
            setDraft(current => ({ ...current, resources: [...current.resources,
              ...ids.filter(id => !current.resources.some(resource => resource.resource_id === id))
                .map(resource_id => ({ resource_id, version: null, chunk_ids: null }))] }))
            setPreflightState(null)
            void materialsQuery.refetch()
          }} />
          <Input.TextArea
            aria-label="出卷备注（会与选定资料原文一起发送）"
            value={draft.notes}
            autoSize={{ minRows: 4, maxRows: 12 }}
            placeholder={
              '示例：重点复习第二章，增加综合计算题；难度接近期末，附分步评分点。\n可多行输入；中文与换行会原样保留。'
            }
            onChange={(e) => update({ notes: e.target.value })}
          />
          <Typography.Text type={notesChars > REVIEW_NOTES_LIMIT ? 'danger' : 'secondary'} style={{ fontSize: 12 }}>
            备注原文会与所选材料正文一起发送；不截断、不摘要替换。当前 {notesChars} / {REVIEW_NOTES_LIMIT}{' '}
            字符（超限将阻止预览与生成）。
          </Typography.Text>
          <Space wrap size={8}>
            <Button
              size="small"
              onClick={() => {
                saveReviewDraft(courseId, draft)
                const now = new Date()
                setDraftSavedAt(
                  `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`,
                )
                message.success('草稿已保存到本机浏览器（不含任何密钥）')
              }}
            >
              保存草稿
            </Button>
            <Button
              size="small"
              onClick={() => {
                clearReviewDraft(courseId)
                setDraft(defaultDraft())
                setPreflightState(null)
                setPreflightError(null)
                setDraftSavedAt(null)
                message.info('草稿已清空，表单已重置（不影响已生成的试卷）')
              }}
            >
              清空草稿
            </Button>
            {draftSavedAt && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                最近保存：{draftSavedAt}
              </Typography.Text>
            )}
          </Space>
        </Space>
      </Card>

      <Card size="small" title="4. 发送预览与生成">
        <Space orientation="vertical" size={12} style={{ width: '100%' }}>
          <Space wrap size={8}>
            <Button
              icon={<FileSearchOutlined />}
              loading={preflightMutation.isPending}
              disabled={totalQuestions === 0 || totalQuestions > 100 || notesChars > REVIEW_NOTES_LIMIT}
              onClick={handlePreview}
            >
              预览将发送的内容
            </Button>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              预检由后端计算实际可发送正文、缺失项与上限；只有点击生成才会实际发送。
            </Typography.Text>
          </Space>
          {preflightError && (
            <Alert type="error" showIcon title="预览失败" description={preflightError} />
          )}
          {preflightState && (
            <PreflightPanel
              preflight={preflightState.preflight}
              stale={stale}
              requestMode={request.mode}
              generating={generating}
              activeJobRunning={jobActive}
              onGenerate={handleGenerate}
            />
          )}
          {activeJob && (
            <ReviewJobTracker
              courseId={courseId}
              jobId={activeJob.jobId}
              mode={activeJob.mode}
              onTerminal={(status) => {
                // 取消/失败后换幂等键代数并落盘：再点生成提交新任务，而非重放旧任务（刷新后同效）
                if (status !== 'succeeded') {
                  const next = genNonce + 1
                  setGenNonce(next)
                  saveGenNonce(courseId, next)
                }
                if (status === 'succeeded') {
                  message.success('复习卷已生成并保存')
                  void papersQuery.refetch()
                } else if (status === 'cancelled') {
                  message.info('生成任务已取消')
                }
              }}
              onActiveChange={setJobActive}
              onForget={() => {
                clearActiveReviewJob(courseId)
                setActiveJob(null)
                setJobActive(false)
              }}
            />
          )}
        </Space>
      </Card>

      <Card
        size="small"
        title="本课程历史复习卷"
        extra={
          papers.length > 0 ? (
            <Link to={`/reviews?course_id=${courseId}`}>
              <Button size="small" type="link">
                查看全部
              </Button>
            </Link>
          ) : undefined
        }
      >
        <Space orientation="vertical" size={8} style={{ width: '100%' }}>
          {papersQuery.isLoading && <Typography.Text type="secondary">加载中…</Typography.Text>}
          {!papersQuery.isLoading && papers.length === 0 && (
            <Typography.Text type="secondary">本课程还没有复习卷；完成上面的步骤即可生成第一张。</Typography.Text>
          )}
          {papers.slice(0, 5).map((paper) => (
            <Space key={paper.id} wrap size={8}>
              <Link to={`/reviews/${paper.id}`}>{paper.title}</Link>
              <Tag>v{paper.revision}</Tag>
              <Tag>{REVIEW_REVISION_REASON_LABELS[paper.revision_reason]}</Tag>
              <Tag color="blue">{reviewModeLabel(paper.mode)}</Tag>
              {paper.warnings.length > 0 && <Tag color="orange">{paper.warnings.length} 条提示</Tag>}
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {paper.summary.question_count} 题 · {paper.summary.total_points} 分 ·{' '}
                {fmtDateTime(paper.created_at)}
              </Typography.Text>
            </Space>
          ))}
        </Space>
      </Card>

      {chunkModalFor && (
        <ChunkSelectModal
          item={chunkModalFor}
          current={draft.resources.find((s) => s.resource_id === chunkModalFor.id)?.chunk_ids ?? null}
          onSave={(chunkIds) => {
            update({
              resources: draft.resources.map((s) =>
                s.resource_id === chunkModalFor.id ? { ...s, chunk_ids: chunkIds } : s,
              ),
            })
            setChunkModalFor(null)
          }}
          onCancel={() => setChunkModalFor(null)}
        />
      )}
    </Space>
  )
}
