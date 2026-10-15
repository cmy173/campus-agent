import { describe, expect, it } from 'vitest'
import type { AgentEvent, AgentTurn } from './api'
import { projectTurnEvents } from './turnProjection'

const turn: AgentTurn = { id: 't', conversation_id: 'root', text: '任务', status: 'running',
  response: '', error: null, created_at: '', updated_at: '' }
function event(seq: number, session: string, type: string, data: Record<string, unknown>): AgentEvent {
  return { seq, kind: 'harness_event', created_at: '', data: { session_id: session, event: { type, data } } }
}

describe('Tool result presentation contract', () => {
  it('reports upstream cancellation as stopped while preserving real failures', () => {
    const result = projectTurnEvents([
      event(1, 'root', 'tool/call', { callId: 'one', name: 'subagent', arguments: '{}' }),
      event(2, 'root', 'tool/result', { message: { toolCallId: 'one', isError: true, content: [{ type: 'text', text: 'Error: subagent run was cancelled' }] } }),
      event(3, 'child', 'tool/call', { callId: 'two', name: 'pwsh', arguments: '{}' }),
      event(4, 'child', 'tool/result', { error: { code: 'ABORTED' }, message: { toolCallId: 'two', isError: true, content: [] } }),
    ], turn)
    expect(result.tools.map(t => t.status)).toEqual(['cancelled', 'cancelled'])
  })
  it('keeps accepted stop requests pending and recognizes actual upstream aborted completion', () => {
    const started: AgentEvent = { seq: 1, kind: 'subagent.started', created_at: '', data: { childSessionId: 'child' } }
    const requested: AgentEvent = { seq: 2, kind: 'subagent.stop_requested', created_at: '', data: { childSessionId: 'child' } }
    expect(projectTurnEvents([started, requested], turn).children[0].status).toBe('stopping')
    const finished: AgentEvent = { seq: 3, kind: 'subagent.finished', created_at: '', data: { childSessionId: 'child', status: 'error', stopReason: 'aborted' } }
    expect(projectTurnEvents([started, requested, finished], turn).children[0].status).toBe('cancelled')
  })
  it('keeps identical child/root call IDs separate and exposes unknown MCP results', () => {
    const projected = projectTurnEvents([
      event(1, 'root', 'tool/call', { callId: 'one', name: 'mcp__new__report', arguments: '{}' }),
      event(2, 'child', 'tool/call', { callId: 'one', name: 'pwsh', arguments: '{}' }),
      event(3, 'child', 'tool/result', { message: { toolCallId: 'one', isError: true, content: [{ type: 'text', text: 'Command failed' }] } }),
      event(4, 'root', 'tool/result', { message: { toolCallId: 'one', content: [{ type: 'text', text: 'Report produced' }] } }),
    ], turn)
    expect(projected.tools).toHaveLength(2)
    expect(projected.tools.find(t => t.sessionId === 'child')).toMatchObject({ status: 'failed', output: 'Command failed' })
    expect(projected.tools.find(t => t.sessionId === 'root')).toMatchObject({ name: 'mcp__new__report', status: 'completed', output: 'Report produced' })
  })

  it('does not claim interrupted work completed or duplicate domain sources', () => {
    const projected = projectTurnEvents([
      event(1, 'child', 'tool/call', { callId: 'one', name: 'campus_materials_read', arguments: '{}' }),
      { seq: 2, kind: 'tool_result', created_at: '', data: { call_id: 'child:one' } },
      event(3, 'root', 'tool/call', { callId: 'two', name: 'web_fetch', arguments: '{}' }),
      { seq: 4, kind: 'subagent.started', created_at: '', data: { childSessionId: 'child' } },
    ], { ...turn, status: 'cancelled' })
    expect(projected.tools).toHaveLength(1)
    expect(projected.tools[0].status).toBe('cancelled')
    expect(projected.children[0].status).toBe('cancelled')
  })
})
