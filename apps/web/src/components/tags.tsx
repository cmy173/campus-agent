import { Tag, Tooltip } from 'antd'
import type {
  ConnectionStatus,
  DuePrecision,
  FactSource,
  Job,
  LearningTask,
  ParseStatus,
  PlanDiffKind,
  SubmissionStatus,
  TaskStatus,
  TranscriptStatus,
} from '../types/contract'
import { fmtDateTime, isStale } from '../lib/time'

export function SourceTag({ source, confidence }: { source: FactSource; confidence?: number | null }) {
  if (source === 'platform') {
    return <Tag color="blue">平台事实</Tag>
  }
  if (source === 'user') {
    return <Tag color="green">我的记录</Tag>
  }
  const pct = confidence !== undefined && confidence !== null ? ` ${Math.round(confidence * 100)}%` : ''
  return (
    <Tooltip title="由系统推断得出，可能不准确，可以修改或忽略">
      <Tag color="orange">推断{pct}</Tag>
    </Tooltip>
  )
}

export function FreshnessTag({ lastSyncAt }: { lastSyncAt: string | null }) {
  const stale = isStale(lastSyncAt)
  const label = lastSyncAt ? `最近成功同步：${fmtDateTime(lastSyncAt)}` : '尚无成功同步记录'
  return (
    <Tooltip title={label}>
      <Tag color={stale ? 'warning' : 'success'}>{stale ? '数据可能过期' : '同步正常'}</Tag>
    </Tooltip>
  )
}

const CONNECTION_LABELS: Record<ConnectionStatus, { text: string; color: string; tip?: string }> = {
  ok: { text: '连接正常', color: 'success' },
  auth_required: {
    text: '等待登录',
    color: 'warning',
    tip: '同步已暂停（不是失败）：请在连接器打开的浏览器窗口内完成平台登录，然后回到本页点「检查连接」；不会自动循环重试',
  },
  network_error: { text: '网络异常', color: 'warning' },
  blocked: { text: '被平台阻断', color: 'error' },
  disabled: { text: '已停用', color: 'default' },
  unsupported: { text: '尚未接入', color: 'default' },
  service_unavailable: { text: '本地服务未启动', color: 'warning' },
}

export function ConnectionStatusTag({ status }: { status: ConnectionStatus }) {
  const item = CONNECTION_LABELS[status]
  const tag = <Tag color={item.color}>{item.text}</Tag>
  return item.tip ? <Tooltip title={item.tip}>{tag}</Tooltip> : tag
}

/** 平台错误码的界面口径（A5b 实测映射；未知码不给猜测性提示） */
const PLATFORM_ERROR_ADVICE: Record<string, string> = {
  AUTH_EXPIRED: '登录已失效：同步已暂停（不是失败），完成平台登录并「检查连接」后重试',
  PERMISSION_DENIED: '该内容受平台权限限制，已跳过；与登录状态无关，重试也不会通过',
  NOT_FOUND: '该内容在平台侧不存在或已被移除，不视为同步故障',
  FETCH_FAILED: '平台暂时异常，可稍后重试',
}

export function platformErrorAdvice(code: string): string | null {
  return PLATFORM_ERROR_ADVICE[code] ?? null
}

const PARSE_LABELS: Record<ParseStatus, { text: string; color: string }> = {
  pending: { text: '待解析', color: 'default' },
  processing: { text: '解析中', color: 'processing' },
  ready: { text: '已解析', color: 'success' },
  failed: { text: '解析失败', color: 'error' },
  skipped: { text: '不解析', color: 'default' },
}

export function ParseStatusTag({ status }: { status: ParseStatus }) {
  const item = PARSE_LABELS[status]
  return <Tag color={item.color}>{item.text}</Tag>
}

const SUBMISSION_LABELS: Record<SubmissionStatus, { text: string; color: string }> = {
  not_submitted: { text: '平台未提交', color: 'warning' },
  submitted: { text: '平台已提交', color: 'success' },
  graded: { text: '已批改', color: 'success' },
  unknown: { text: '提交状态未知', color: 'default' },
}

export function SubmissionStatusTag({ status }: { status: SubmissionStatus }) {
  const item = SUBMISSION_LABELS[status]
  return <Tag color={item.color}>{item.text}</Tag>
}

const TASK_STATUS_LABELS: Record<TaskStatus, { text: string; color: string }> = {
  pending: { text: '待开始', color: 'default' },
  in_progress: { text: '进行中', color: 'processing' },
  completed: { text: '学习完成', color: 'success' },
  cancelled: { text: '已取消', color: 'default' },
}

export function TaskStatusTag({ status }: { status: TaskStatus }) {
  const item = TASK_STATUS_LABELS[status]
  return <Tag color={item.color}>{item.text}</Tag>
}

const JOB_TYPE_LABELS: Record<Job['type'], string> = {
  import: '导入',
  parse: '解析',
  sync: '平台同步',
  export: '导出',
  build_tasks: '任务生成',
  review_generate: '复习卷生成',
}

export function jobTypeLabel(type: Job['type']): string {
  return JOB_TYPE_LABELS[type] ?? type
}

const TASK_TYPE_LABELS: Record<LearningTask['type'], string> = {
  study: '学习',
  reading: '阅读',
  watching: '观看',
  practice: '练习',
  assignment: '作业',
  review: '复习',
  other: '其他',
}

export function taskTypeLabel(type: LearningTask['type']): string {
  return TASK_TYPE_LABELS[type] ?? type
}

const JOB_STATUS_LABELS: Record<Job['status'], { text: string; color: string }> = {
  queued: { text: '排队中', color: 'default' },
  running: { text: '进行中', color: 'processing' },
  succeeded: { text: '成功', color: 'success' },
  partial_failed: { text: '部分失败', color: 'warning' },
  failed: { text: '失败', color: 'error' },
  cancelled: { text: '已取消', color: 'default' },
}

export function JobStatusTag({ status }: { status: Job['status'] }) {
  const item = JOB_STATUS_LABELS[status]
  return <Tag color={item.color}>{item.text}</Tag>
}

const TRANSCRIPT_LABELS: Record<TranscriptStatus, { text: string; color: string }> = {
  pending: { text: '字幕待获取', color: 'default' },
  processing: { text: '字幕处理中', color: 'processing' },
  ready: { text: '字幕可用', color: 'success' },
  unavailable: { text: '确认无字幕', color: 'warning' },
  unknown: { text: '字幕情况未知', color: 'default' },
  failed: { text: '字幕获取失败', color: 'error' },
}

export function TranscriptStatusTag({ status }: { status: TranscriptStatus }) {
  const item = TRANSCRIPT_LABELS[status]
  return <Tag color={item.color}>{item.text}</Tag>
}

const DIFF_LABELS: Record<PlanDiffKind, { text: string; color: string }> = {
  added: { text: '新增', color: 'green' },
  moved: { text: '移动', color: 'blue' },
  kept: { text: '保留', color: 'default' },
  removed: { text: '移出', color: 'warning' },
}

export function PlanDiffTag({ kind }: { kind: PlanDiffKind }) {
  const item = DIFF_LABELS[kind]
  return <Tag color={item.color}>{item.text}</Tag>
}

/** 截止时间的精度提示：date=仅日期，unknown=平台未提供 */
export function DuePrecisionHint({ precision }: { precision: DuePrecision }) {
  if (precision === 'date') {
    return (
      <Tooltip title="平台只提供了日期，未提供具体时刻">
        <Tag>仅日期</Tag>
      </Tooltip>
    )
  }
  if (precision === 'unknown') {
    return (
      <Tooltip title="平台未公布截止时间，本系统不做猜测">
        <Tag color="warning">截止时间未知</Tag>
      </Tooltip>
    )
  }
  return null
}
