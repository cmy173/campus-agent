import { apiRequest, apiBaseUrl } from '../../lib/api/http'
import type { ErrorInfo, Plan, PlanBlock } from '../../types/contract'

export interface AgentConversation {
  id: string
  provider_id: string
  course_ids: string[]
  current_course_id: string | null
  send_to_provider: boolean
  created_at: string
  updated_at: string
  turns?: AgentTurn[]
}

export interface AgentTurn {
  id: string
  conversation_id: string
  text: string
  status: 'queued' | 'running' | 'stopping' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'
  response: string
  error: ErrorInfo | null
  created_at: string
  updated_at: string
  attachment_ids?: string[]
}

export interface PersistentGoal {
  id: string
  revision: number
  objective: string
  phase: 'active' | 'paused' | 'blocked' | 'complete'
  activation: 'armed' | 'disarmed'
  roundsStarted: number
  maxGoalRounds: number
  blockedReason?: { code: string; message: string }
}

export interface ToolEnvelope {
  job_id?: string | null
  status: 'ok' | 'partial' | 'queued' | 'needs_user' | 'error'
  data: { items?: Record<string, unknown>[]; resource?: Record<string, unknown>; [key: string]: unknown }
  evidence: { source_id: string; entity_id: string; version: number | null; locator: unknown; observed_at: string | null }[]
  coverage: { scope: string; complete: boolean; missing: string[] }
  freshness: { fetched_at: string | null; stale: boolean }
  warnings: string[]
  error: ErrorInfo | null
}

export interface AgentEvent {
  seq: number
  kind: string
  data: { name?: string; call_id?: string; result?: ToolEnvelope; [key: string]: unknown }
  created_at: string
}

export interface TurnRead extends AgentTurn {
  events: { items: AgentEvent[]; next_cursor: number }
}

export interface SendPayload { text: string; course_id?: string; clear_course_context?: boolean; action_id?: string; attachment_ids?: string[] }

export interface ConversationFile {
  id: string
  conversation_id: string
  kind: 'input' | 'output'
  name: string
  size: number
  sha256: string
  mime_type: string
  created_at: string
}
export interface FilePreview { kind: 'text' | 'image' | 'pdf' | 'spreadsheet' | 'download'; text?: string; truncated: boolean;
  sheets?: { name: string; rows: (string | number | boolean | null)[][] }[]; has_formulas?: boolean; message?: string | null }
const filePath = (cid: string, fid?: string) => `/agent/conversations/${encodeURIComponent(cid)}/files${fid ? '/' + encodeURIComponent(fid) : ''}`
export const fileContentUrl = (cid: string, fid: string, inline = false) => `${apiBaseUrl()}/api/v1${filePath(cid, fid)}/content${inline ? '?inline=true' : ''}`

export interface McpServerSettings {
  serverName: string
  enabled: boolean
  transport: 'stdio' | 'streamable-http'
  command: string
  args: string[]
  cwd: string
  env: Record<string, string>
  url: string
  headers: Record<string, string>
  toolCallTimeoutMs: number
}

export interface RuntimeSettings {
  workspace: string
  skill_dirs: string[]
  search: { base_url: string; model: string; key_mode: 'shared' | 'separate'; has_api_key: boolean }
  mcp_servers: McpServerSettings[]
}

export interface DiagnosticResult {
  ok: boolean
  latency_ms?: number
  tool_names?: string[]
  tool_count?: number
  source_count?: number
  error?: ErrorInfo
}

export interface BrowserAction {
  id: string
  status: 'pending' | 'executing' | 'executed' | 'rejected' | 'stale' | 'uncertain'
  preview: { kind?: 'explicit' | null; page_url: string; action_url?: string | null; method?: string | null; button?: string | null;
    fields?: { name: string; value: string }[]; description?: string | null; recipient?: string | null; content?: string | null;
    attachments?: string[]; target_text?: string | null }
  created_at: string
}
export interface BrowserStatus { running: boolean; headed: boolean;
  pages: { index: number; url: string; title: string }[]; reviews: BrowserAction[] }
const browserPath = (cid: string) => `/agent/conversations/${encodeURIComponent(cid)}/browser`

export interface PlanAction {
  id: string
  kind: 'apply' | 'revoke'
  status: 'pending' | 'approved' | 'executing' | 'completed'
  plan_id: string
  expires_at: string
  preview: { plan: Plan; task_titles?: Record<string, string>; diff: { added: PlanBlock[]; removed: PlanBlock[]; retained: PlanBlock[] }; constraints?: { blocked?: { starts_at: string; ends_at: string }[] } }
  result: { plan: Plan } | null
}

export const agentApi = {
  resumeGoal: (cid: string, goal: Pick<PersistentGoal, 'id' | 'revision'> & { maxGoalRounds?: number }, key: string) => apiRequest<AgentTurn>(
    `/agent/conversations/${encodeURIComponent(cid)}/goal/resume`, { method: 'POST', body: goal, idempotencyKey: key }),
  pauseGoal: (tid: string, goal: Pick<PersistentGoal, 'id' | 'revision'>) => apiRequest<{ goal: PersistentGoal; agent_status: string }>(
    `/agent/turns/${encodeURIComponent(tid)}/goal/pause`, { method: 'POST', body: goal }),
  browser: (cid: string) => apiRequest<BrowserStatus>(browserPath(cid)),
  openBrowser: (cid: string, url?: string) => apiRequest<{ running: boolean; headed: boolean }>(browserPath(cid) + '/open', { method: 'POST', body: { url } }),
  stopBrowser: (cid: string) => apiRequest<{ running: boolean }>(browserPath(cid) + '/stop', { method: 'POST', body: {} }),
  browserAction: (cid: string, aid: string, operation: 'confirm' | 'reject') => apiRequest<{ action: BrowserAction; message?: string | null }>(
    browserPath(cid) + `/actions/${encodeURIComponent(aid)}/${operation}`, { method: 'POST', body: {} }),
  files: (cid: string) => apiRequest<{ items: ConversationFile[] }>(filePath(cid)),
  upload: (cid: string, file: File, key: string) => {
    const formData = new FormData()
    formData.append('file', file)
    return apiRequest<ConversationFile>(filePath(cid), { method: 'POST', formData, idempotencyKey: key })
  },
  preview: (cid: string, fid: string) => apiRequest<FilePreview>(filePath(cid, fid) + '/preview'),
  settings: () => apiRequest<RuntimeSettings>('/agent/settings'),
  saveSettings: (body: Omit<RuntimeSettings, 'search'> & { search: Omit<RuntimeSettings['search'], 'has_api_key'> & { api_key?: string; clear_key?: boolean } }) =>
    apiRequest<RuntimeSettings>('/agent/settings', { method: 'PUT', body }),
  testMcp: (name: string) => apiRequest<DiagnosticResult>(`/agent/settings/mcp/${encodeURIComponent(name)}/test`, { method: 'POST', body: {} }),
  testSearch: (provider_id?: string) => apiRequest<DiagnosticResult>('/agent/settings/search/test', { method: 'POST', body: { provider_id } }),
  list: () => apiRequest<{ items: AgentConversation[]; next_cursor: null }>('/agent/conversations'),
  read: (id: string, signal?: AbortSignal) => apiRequest<AgentConversation>(`/agent/conversations/${encodeURIComponent(id)}`, { signal }),
  create: (provider_id: string, course_id?: string) => apiRequest<AgentConversation>('/agent/conversations', {
    method: 'POST', body: { provider_id, send_to_provider: true, course_id: course_id ?? null },
  }),
  send: (id: string, body: SendPayload, idempotencyKey: string) => apiRequest<AgentTurn>(
    `/agent/conversations/${encodeURIComponent(id)}/turns`, { method: 'POST', body, idempotencyKey }),
  turn: (id: string, after = 0, signal?: AbortSignal) => apiRequest<TurnRead>(
    `/agent/turns/${encodeURIComponent(id)}`, { query: { after }, signal }),
  cancel: (id: string) => apiRequest<AgentTurn>(`/agent/turns/${encodeURIComponent(id)}/cancel`, { method: 'POST', body: {} }),
  interruptChild: (turnId: string, childId: string) => apiRequest<{ accepted: boolean; child_id: string }>(
    `/agent/turns/${encodeURIComponent(turnId)}/subagents/${encodeURIComponent(childId)}/interrupt`, { method: 'POST', body: {} }),
  actions: (id: string) => apiRequest<{ items: PlanAction[] }>(`/agent/conversations/${encodeURIComponent(id)}/actions`),
  accept: (id: string, actionId: string, operation: 'apply' | 'revoke', key: string) => apiRequest<PlanAction>(
    `/agent/conversations/${encodeURIComponent(id)}/actions/${encodeURIComponent(actionId)}/accept`,
    { method: 'POST', body: { operation }, idempotencyKey: key }),
}

export const isActive = (turn: AgentTurn) => turn.status === 'queued' || turn.status === 'running' || turn.status === 'stopping'

export const toolLabels: Record<string, string> = {
  'connections.status': '连接状态', 'courses.list': '查找课程', 'assignments.list': '核对作业',
  'materials.list': '查找资料', 'materials.search': '检索正文', 'materials.read': '读取原文',
  'tasks.list': '学习任务', 'plans.read': '当前安排', 'jobs.read': '后台进度',
  'availability.read': '可用时间', 'plans.preview': '安排预览', 'plans.apply': '应用安排', 'plans.revoke': '撤销安排',
  'tasks.build': '生成学习任务', 'jobs.cancel': '停止后台任务',
  'tasks.feedback': '记录学习反馈',
}
