import { useEffect, useRef, useState } from 'react'
import { apiErrorText } from '../../lib/api/http'
import { agentApi, isActive } from './api'
import type { AgentEvent, AgentTurn } from './api'

/** Transport, pagination and reconnect state are independent of result presentation. */
export function useTurnEvents(turn: AgentTurn) {
  const [events, setEvents] = useState<AgentEvent[]>([])
  const [error, setError] = useState('')
  const [controlError, setControlError] = useState('')
  const cursor = useRef({ id: turn.id, value: 0 })
  const stoppingRef = useRef(new Set<string>())
  const [stoppingChildren, setStoppingChildren] = useState<string[]>([])
  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const abort = new AbortController()
    if (cursor.current.id !== turn.id) {
      cursor.current = { id: turn.id, value: 0 }
      setEvents([])
    }
    async function poll() {
      try {
        const data = await agentApi.turn(turn.id, cursor.current.value, abort.signal)
        if (stopped) return
        cursor.current.value = data.events.next_cursor
        setEvents(previous => [...previous, ...data.events.items].filter((e, i, all) => all.findIndex(v => v.seq === e.seq) === i))
        setError('')
        if (isActive(data) || data.events.items.length === 100) timer = setTimeout(poll, 600)
      } catch (e) {
        if (stopped) return
        setError(apiErrorText(e))
        timer = setTimeout(poll, 2000)
      }
    }
    void poll()
    return () => { stopped = true; abort.abort(); if (timer) clearTimeout(timer) }
  }, [turn.id, turn.status])
  async function interruptChild(childId: string) {
    if (stoppingRef.current.has(childId)) return
    stoppingRef.current.add(childId)
    setStoppingChildren([...stoppingRef.current])
    setControlError('')
    try { await agentApi.interruptChild(turn.id, childId) }
    catch (e) { setControlError(apiErrorText(e)) }
    finally {
      stoppingRef.current.delete(childId)
      setStoppingChildren([...stoppingRef.current])
    }
  }
  return { events, error, controlError, interruptChild, stoppingChildren }
}
