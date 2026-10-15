import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { apiErrorText } from '../../lib/api/http'
import { agentApi, isActive } from './api'
import { mergeTurnEvents } from './mergeTurnEvents'
import type { AgentEvent, AgentTurn, PersistentGoal } from './api'

/** Transport, pagination and reconnect state are independent of result presentation. */
export function useTurnEvents(turn: AgentTurn) {
  const queryClient = useQueryClient()
  const [events, setEvents] = useState<AgentEvent[]>([])
  const [error, setError] = useState('')
  const [controlError, setControlError] = useState('')
  const [goalError, setGoalError] = useState('')
  const [pausingGoal, setPausingGoal] = useState(false)
  const pauseRef = useRef(false)
  const [resumingGoal, setResumingGoal] = useState(false)
  const resumeRef = useRef<{ fingerprint: string; key: string } | null>(null)
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
        setEvents(previous => mergeTurnEvents(previous, data.events.items))
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
  async function pauseGoal(goal: PersistentGoal) {
    if (pauseRef.current) return
    pauseRef.current = true
    setPausingGoal(true)
    setGoalError('')
    try { await agentApi.pauseGoal(turn.id, { id: goal.id, revision: goal.revision }) }
    catch (e) { setGoalError(apiErrorText(e)) }
    finally { pauseRef.current = false; setPausingGoal(false) }
  }
  async function resumeGoal(goal: PersistentGoal) {
    if (resumingGoal) return
    const payload = { id: goal.id, revision: goal.revision,
      ...(goal.roundsStarted >= goal.maxGoalRounds ? { maxGoalRounds: goal.maxGoalRounds + 64 } : {}) }
    const fingerprint = JSON.stringify(payload)
    if (resumeRef.current?.fingerprint !== fingerprint) resumeRef.current = { fingerprint, key: crypto.randomUUID() }
    setResumingGoal(true)
    setGoalError('')
    try {
      await agentApi.resumeGoal(turn.conversation_id, payload, resumeRef.current.key)
      await queryClient.invalidateQueries({ queryKey: ['agent', 'conversation', turn.conversation_id] })
    } catch (e) { setGoalError(apiErrorText(e)) }
    finally { setResumingGoal(false) }
  }
  return { events, error, controlError, interruptChild, stoppingChildren, goalError, pauseGoal, pausingGoal, resumeGoal, resumingGoal }
}
