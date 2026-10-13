import type { AgentEvent, AgentTurn } from './api'

export interface ToolActivity {
  id: string
  sessionId: string
  callId: string
  name: string
  label: string
  status: 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted'
  arguments: string
  output: string
}

export interface ChildActivity {
  id: string
  status: ToolActivity['status']
  output: string
}

const labels: Record<string, string> = {
  read: '读取文件', write: '写入文件', edit: '修改文件', glob: '查找文件', grep: '检索文件',
  read_image: '读取图片', pwsh: '运行 PowerShell', bash: '运行命令',
  web_search: '搜索网页', web_fetch: '读取网页', skill: '加载技能', workflow: '执行工作流',
  subagent: '分配子任务', subagent_fork: '分配子任务', list_agents: '查看子任务',
  interrupt_agent: '停止子任务', send_message: '继续子任务',
  create_goal: '创建持续任务', get_goal: '查看持续任务', update_goal: '更新持续任务',
  todo_write: '更新步骤', job_list: '查看后台命令', job_output: '读取命令结果', job_kill: '停止命令',
}

const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const string = (value: unknown) => typeof value === 'string' ? value : ''

/** Text stays data: neither tool output nor MCP content is interpreted as HTML. */
export function contentText(value: unknown): string {
  if (!Array.isArray(value)) return ''
  return value.map(block => {
    const data = record(block)
    if (data.type === 'text') return string(data.text)
    if (data.type === 'image') return '[图片结果]'
    if (data.type === 'resource_link') return `${string(data.name)} ${string(data.uri)}`.trim()
    return ''
  }).filter(Boolean).join('\n')
}

function stoppedStatus(status: AgentTurn['status']): ToolActivity['status'] {
  return status === 'cancelled' ? 'cancelled' : 'interrupted'
}

/** One stable UI projection for upstream, MCP and future tools; session-scoped call IDs. */
export function projectTurnEvents(events: AgentEvent[], turn: AgentTurn) {
  const tools = new Map<string, ToolActivity>()
  const children = new Map<string, ChildActivity>()
  const domainCalls = new Set(events.filter(e => e.kind === 'tool_result').map(e => string(e.data.call_id)))
  let progressText = ''
  for (const item of events) {
    if (item.kind === 'harness_event') {
      const event = record(item.data.event)
      const data = record(event.data)
      const sessionId = string(item.data.session_id) || turn.conversation_id
      if (event.type === 'tool/call') {
        const callId = string(data.callId)
        if (!callId) continue
        const name = string(data.name) || 'unknown'
        const id = sessionId + ':' + callId
        tools.set(id, { id, sessionId, callId, name, label: labels[name] ?? name,
          status: 'running', arguments: string(data.arguments), output: '' })
      } else if (event.type === 'tool/result') {
        const message = record(data.message)
        const callId = string(message.toolCallId)
        if (!callId) continue
        const id = sessionId + ':' + callId
        const activity = tools.get(id) ?? { id, sessionId, callId, name: 'unknown',
          label: '工具结果', arguments: '', output: '', status: 'running' as const }
        tools.set(id, { ...activity, status: message.isError || data.error ? 'failed' : 'completed',
          output: contentText(message.content) || string(record(data.error).reason) })
      } else if (event.type === 'assistant/message' && sessionId === turn.conversation_id) {
        progressText = contentText(record(data.message).content)
      }
    } else if (item.kind === 'subagent.started' || item.kind === 'subagent.finished') {
      const id = string(item.data.childSessionId)
      if (!id) continue
      const status = item.kind === 'subagent.started' ? 'running' :
        item.data.status === 'ok' ? 'completed' : item.data.status === 'cancelled' || item.data.stopReason === 'interrupted' ? 'cancelled' : 'failed'
      children.set(id, { id, status, output: contentText(item.data.lastAssistantMessage) })
    }
  }
  const terminal = !['queued', 'running'].includes(turn.status)
  const settle = <T extends { status: ToolActivity['status'] }>(activity: T): T =>
    terminal && activity.status === 'running' ? { ...activity, status: stoppedStatus(turn.status) } : activity
  return {
    tools: [...tools.values()].filter(t => !(t.name.startsWith('campus_') &&
      (domainCalls.has(t.callId) || domainCalls.has(t.id)))).map(settle),
    children: [...children.values()].map(settle),
    progressText: turn.response ? '' : progressText,
  }
}
