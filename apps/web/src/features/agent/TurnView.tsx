import { Alert, Button, Collapse, Space, Tag, Typography } from 'antd'
import { isActive, toolLabels } from './api'
import type { AgentTurn, ConversationFile, ToolEnvelope } from './api'
import { dueText } from './Workspace'
import { JobProgressInline } from '../../components/JobProgressInline'
import { useTurnEvents } from './useTurnEvents'
import { projectTurnEvents } from './turnProjection'
import { ToolActivities } from './ToolActivities'
import { FileActivities } from './FileActivities'
import { GoalActivity } from './GoalActivity'
import { AgentErrorNotice } from './AgentErrorNotice'

const submissionStatusLabels: Record<string, string> = {
  unknown: '提交状态未知',
  not_submitted: '平台未提交',
  submitted: '平台已提交',
  graded: '已批改',
}

const learningStatusLabels: Record<string, string> = {
  pending: '待开始',
  in_progress: '进行中',
  completed: '学习完成',
  cancelled: '已取消',
}

const parseStatusLabels: Record<string, string> = {
  pending: '待解析',
  processing: '解析中',
  ready: '已解析',
  failed: '解析失败',
  skipped: '未解析',
}

function ResultView({ name, result, onSource }: { name: string; result: ToolEnvelope; onSource: (value: ToolEnvelope) => void }) {
  const items = result.data.items ?? []
  return <Space orientation="vertical" size={8} style={{ width: '100%' }}>
    {result.error && <Alert type="warning" title={result.error.message} description={result.error.code} />}
    {name === 'materials.read' && result.data.resource && <Button onClick={() => onSource(result)}>
      打开原文 · {String(result.data.resource.title ?? '资料')}
    </Button>}
    {['assignments.list', 'tasks.list', 'courses.list', 'materials.list'].includes(name) && items.map(item =>
      <article className="agent-fact" key={String(item.id)}>
        <Typography.Text strong>{String(item.title ?? item.id)}</Typography.Text>
        {name === 'assignments.list' && <div className="agent-fact-meta">
          <span>{dueText(item)}</span>
          <Tag color={item.submission_status === 'submitted' || item.submission_status === 'graded' ? 'success' : item.submission_status === 'not_submitted' ? 'warning' : 'default'}>
            {submissionStatusLabels[String(item.submission_status ?? 'unknown')] ?? '提交状态待核对'}
          </Tag>
        </div>}
        {name === 'tasks.list' && <div className="agent-fact-meta">
          <span>本地学习进度</span>
          <Tag color={item.status === 'completed' ? 'success' : item.status === 'in_progress' ? 'processing' : 'default'}>
            {learningStatusLabels[String(item.status)] ?? '进度待核对'}
          </Tag>
          <Typography.Text type="secondary">不代表学校平台已提交</Typography.Text>
        </div>}
        {name === 'materials.list' && <div className="agent-fact-meta">
          <span>资料版本 v{String(item.version)}</span>
          <Tag color={item.parse_status === 'ready' ? 'success' : item.parse_status === 'failed' ? 'error' : 'default'}>
            {parseStatusLabels[String(item.parse_status)] ?? '解析状态待核对'}
          </Tag>
        </div>}
        <details className="agent-technical-meta">
          <summary>查看记录编号</summary>
          <code>{String(item.id)}</code>
        </details>
      </article>)}
    {'items' in result.data && !items.length && !result.error && <Typography.Text type="secondary">本次查询没有返回条目，不能据此判断学校平台没有对应信息。</Typography.Text>}
    {name === 'materials.search' && items.length > 0 && <Typography.Text type="secondary">找到 {items.length} 个正文片段；读取原文后核对要求。</Typography.Text>}
    {result.coverage?.missing?.map(message => <div className="agent-note" key={message}>{message}</div>)}
    {result.warnings?.map(message => <div className="agent-note" key={message}>{message}</div>)}
    {result.freshness?.stale && <Typography.Text type="secondary">{result.freshness.fetched_at ? '缓存可能过期，请核对原文或同步结果。' : '来源观测时间未知，不能保证这是最新信息。'}</Typography.Text>}
  </Space>
}

export function TurnView({ turn, onSource, onCancel, attachments = [], onFile }: { turn: AgentTurn; onSource: (value: ToolEnvelope) => void; onCancel: (id: string) => void;
  attachments?: ConversationFile[]; onFile?: (file: ConversationFile) => void }) {
  const { events, error, controlError, interruptChild, stoppingChildren, goalError, pauseGoal, pausingGoal, resumeGoal, resumingGoal } = useTurnEvents(turn)
  const activity = projectTurnEvents(events, turn)
  const runningTool = activity.tools.findLast(tool => tool.status === 'running')
  const lastStopEvent = events.findLast(event => ['stop_requested', 'stop_incomplete'].includes(event.kind))
  const incompleteStop = turn.status === 'stopping' && lastStopEvent?.kind === 'stop_incomplete'
  const results = events.filter(e => e.kind === 'tool_result' && e.data.result)
  const modelErrors = turn.status === 'failed' || turn.status === 'interrupted'
    ? events.filter(e => e.kind === 'model_error' && typeof e.data.message === 'string')
      .filter((e, index, all) => all.findIndex(other => other.data.code === e.data.code && other.data.message === e.data.message) === index)
    : []
  const jobIds = [...new Set(results.filter(e => ['tasks.build', 'jobs.read', 'jobs.cancel'].includes(e.data.name ?? '') && !e.data.result?.error)
    .map(e => e.data.result?.job_id).filter((id): id is string => Boolean(id)))]
  return <section className="agent-turn" aria-label="对话轮次">
    <div className="agent-user-message"><Typography.Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{turn.text}</Typography.Paragraph>
      {attachments.length > 0 && <div className="agent-attachments">{attachments.map(file => <Tag key={file.id}>{file.name}</Tag>)}</div>}
    </div>
    <div className="agent-assistant-message">
      <Typography.Text strong>校园助手</Typography.Text>
      {isActive(turn) && <div role="status" className="agent-running">{turn.status === 'stopping' ? '停止中，正在结束本轮工具…' : runningTool ? runningTool.label + '…' : '正在处理…'}
        <Button aria-label={incompleteStop ? '重试停止' : '停止本轮'} size="small" disabled={turn.status === 'stopping' && !incompleteStop}
          loading={turn.status === 'stopping' && !incompleteStop} onClick={() => onCancel(turn.id)}>{incompleteStop ? '重试停止' : '停止本轮'}</Button></div>}
      {incompleteStop && <Alert type="warning" title="停止收尾尚未完成" description="本轮仍处于停止中，可以重试停止。" />}
      {activity.progressText && <Typography.Paragraph className="agent-answer">{activity.progressText}</Typography.Paragraph>}
      {turn.response && <Typography.Paragraph className="agent-answer">{turn.response}</Typography.Paragraph>}
      <GoalActivity events={events} turn={turn} onPause={pauseGoal} pausing={pausingGoal} onResume={resumeGoal} resuming={resumingGoal} />
      {goalError && <Alert type="warning" title="持续任务控制未完成" description={goalError} />}
      <FileActivities events={events} conversationId={turn.conversation_id} onOpen={onFile} />
      {turn.status === 'cancelled' && <Typography.Paragraph type="secondary">本轮已停止，已获得的资料保留。</Typography.Paragraph>}
      {modelErrors.map(event => <AgentErrorNotice key={event.seq} error={event.data} />)}
      {turn.error && !modelErrors.length && <AgentErrorNotice error={turn.error} />}
      {error && <Alert type="warning" title="进度连接暂时中断，正在恢复" description={error} />}
      {controlError && <Alert type="warning" title="子任务停止请求未完成" description={controlError} />}
      {jobIds.map(id => <JobProgressInline key={id} jobId={id} compact allowRetry={false} />)}
      {activity.tools.length > 0 && <div className="agent-task-summary" role="status" aria-label="工具进度摘要">
        <span>工具进度：已完成 {activity.tools.filter(tool => tool.status === 'completed').length} / {activity.tools.length} 项</span>
        {activity.tools.some(tool => ['running', 'stopping'].includes(tool.status)) && <Tag color="processing">仍在执行</Tag>}
        {activity.tools.some(tool => tool.status === 'failed') && <Tag color="error">部分失败，请展开查看</Tag>}
        {activity.tools.some(tool => ['cancelled', 'interrupted'].includes(tool.status)) && <Tag>有操作未完成</Tag>}
      </div>}
      <ToolActivities tools={activity.tools} children={activity.children} onInterrupt={interruptChild} stoppingChildren={stoppingChildren} />
      {results.filter(e => e.data.name === 'materials.read' && e.data.result?.data.resource).map(e =>
        <div className="agent-source-action" key={e.seq}><Button onClick={() => onSource(e.data.result!)}>
          打开原文 · {String(e.data.result!.data.resource!.title ?? '资料')}
        </Button></div>)}
      {results.length > 0 && <Collapse size="small" items={[{ key: 'sources', label: <Space><Tag>{results.length} 项核对</Tag>来源与覆盖范围</Space>,
        children: <Space orientation="vertical" size={16} style={{ width: '100%' }}>{results.map(event => <div key={event.seq}>
          <Typography.Paragraph strong>{toolLabels[event.data.name!] ?? event.data.name}</Typography.Paragraph>
          <ResultView name={event.data.name!} result={event.data.result!} onSource={onSource} />
        </div>)}</Space> }]} />}
    </div>
  </section>
}
