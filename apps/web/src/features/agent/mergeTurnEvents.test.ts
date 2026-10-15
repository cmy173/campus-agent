import { expect, it } from 'vitest'
import { mergeTurnEvents } from './mergeTurnEvents'
const event = (seq: number) => ({ seq, kind: 'tool_result', data: {}, created_at: '' })
it('deduplicates overlapping and repeated pages in sequence order', () => {
  const previous = [event(1), event(2)]
  const next = mergeTurnEvents(previous, [event(4), event(2), event(3), event(4)])
  expect(next.map(item => item.seq)).toEqual([1, 2, 3, 4])
  expect(next[1]).toBe(previous[1])
})
it('keeps array identity when a poll returns no new event', () => {
  const previous = [event(1), event(2)]
  expect(mergeTurnEvents(previous, [])).toBe(previous)
  expect(mergeTurnEvents(previous, [event(2), event(1)])).toBe(previous)
})