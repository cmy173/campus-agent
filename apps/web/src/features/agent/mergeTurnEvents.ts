import type { AgentEvent } from './api'

/** Deduplicate in linear time, preserving identity for empty/replayed pages. */
export function mergeTurnEvents(previous: AgentEvent[], incoming: AgentEvent[]): AgentEvent[] {
  if (!incoming.length) return previous
  const seen = new Map(previous.map(event => [event.seq, event]))
  for (const event of incoming) if (!seen.has(event.seq)) seen.set(event.seq, event)
  return seen.size === previous.length ? previous : [...seen.values()].sort((a, b) => a.seq - b.seq)
}