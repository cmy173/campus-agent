import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { useCourses, useProviders } from '../../lib/api/hooks'
import { API_MODE, apiErrorText, isApiError } from '../../lib/api/http'
import { agentApi, isActive } from './api'
import type { AgentConversation, PlanAction } from './api'
import { prepareSend, readDraft, saveDraft } from './draft'

/** Owns API state, retries and recovery independently of the chat layout. */
export function useConversationController(id?: string) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const draftKey = id ?? 'new'
  const [draft, setDraft] = useState(() => readDraft(draftKey))
  const [providerId, setProviderId] = useState<string>()
  const [consent, setConsent] = useState(false)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const sendingRef = useRef(false)
  const transcript = useRef<HTMLDivElement>(null)
  const messages = useRef<HTMLDivElement>(null)
  const followBottom = useRef(true)
  const scrollKey = 'campus.agent.scroll.' + draftKey
  const restoredScroll = useRef(false)
  const providers = useProviders()
  const courses = useCourses()
  const conversations = useQuery({ queryKey: ['agent', 'list'], queryFn: agentApi.list, enabled: API_MODE === 'real' })
  const conversation = useQuery({ queryKey: ['agent', 'conversation', id], queryFn: () => agentApi.read(id!), enabled: Boolean(id),
    refetchInterval: query => query.state.data?.turns?.some(isActive) ? 650 : false })
  const turns = conversation.data?.turns ?? []
  const active = turns.some(isActive)
  const actions = useQuery({ queryKey: ['agent', 'actions', id], queryFn: () => agentApi.actions(id!), enabled: Boolean(id),
    refetchInterval: active ? 650 : false })
  useEffect(() => { if (id) void queryClient.invalidateQueries({ queryKey: ['agent', 'actions', id] }) }, [active, id, queryClient])
  const providerItems = providers.data?.items ?? []
  const selectedProvider = providerId ?? providerItems[0]?.id
  const provider = providerItems.find(p => p.id === (conversation.data?.provider_id ?? selectedProvider))
  const courseId = draft.course_id ?? undefined

  useEffect(() => { saveDraft(draftKey, draft) }, [draftKey, draft])
  useEffect(() => {
    if (!conversation.data || restoredScroll.current) return
    restoredScroll.current = true
    if (readDraft(draftKey).course_id === undefined && conversation.data.current_course_id) {
      setDraft(previous => ({ ...previous, course_id: conversation.data!.current_course_id! }))
    }
    let previous = 0
    try {
      previous = Number(sessionStorage.getItem(scrollKey) ?? '0')
      followBottom.current = sessionStorage.getItem(scrollKey + '.follow') !== 'false'
    } catch { /* Optional storage. */ }
    if (transcript.current) transcript.current.scrollTop = previous
  }, [conversation.data, draftKey, scrollKey])
  useEffect(() => {
    const observer = new ResizeObserver(() => {
      if (followBottom.current && transcript.current) transcript.current.scrollTop = transcript.current.scrollHeight
    })
    if (messages.current) observer.observe(messages.current)
    return () => observer.disconnect()
  }, [])

  const changeText = (text: string) => setDraft(previous => ({ ...previous, text }))
  async function create() {
    if (!selectedProvider || !consent || sendingRef.current) return
    sendingRef.current = true
    setSending(true)
    setError('')
    try {
      const value = await agentApi.create(selectedProvider, courseId)
      saveDraft(value.id, { text: draft.text, course_id: courseId ?? null })
      queryClient.setQueryData(['agent', 'conversation', value.id], { ...value, turns: [] })
      await queryClient.invalidateQueries({ queryKey: ['agent', 'list'] })
      navigate('/chat/' + value.id)
    } catch (e) { setError(apiErrorText(e)) }
    finally { sendingRef.current = false; setSending(false) }
  }

  async function send(override?: { text: string; action: PlanAction; operation: 'apply' | 'revoke' }) {
    const text = override?.text ?? draft.text
    if (!id || !text.trim() || active || sendingRef.current) return
    sendingRef.current = true
    setSending(true)
    setError('')
    followBottom.current = true
    try {
      let actionId = draft.pending?.payload.text === text && (!override || override.action.id === draft.pending.payload.action_id)
        ? draft.pending.payload.action_id : undefined
      const operation = override?.operation ?? (text.trim() === '按这个来' ? 'apply' : text.trim() === '撤销刚才安排' ? 'revoke' : undefined)
      const target = override?.action ?? actions.data?.items.find(a => a.kind === 'apply' &&
        (operation === 'apply' ? a.status === 'pending' || a.status === 'approved' : a.status === 'completed'))
      if (operation && !actionId) {
        if (!target) throw new Error('当前没有可确认的具体方案，请先生成并查看预览。')
        const accepted = target.status === 'approved' && target.kind === operation ? target : await agentApi.accept(id, target.id, operation, crypto.randomUUID())
        actionId = accepted.id
      }
      const pending = prepareSend(draft, { text, ...(courseId ? { course_id: courseId } : { clear_course_context: true }), ...(actionId ? { action_id: actionId } : {}),
        ...(!override && draft.attachment_ids?.length ? { attachment_ids: draft.attachment_ids } : {}) })
      const retained = { ...draft, pending }
      saveDraft(draftKey, retained)
      setDraft(retained)
      const turn = await agentApi.send(id, pending.payload, pending.key)
      queryClient.setQueryData<AgentConversation>(['agent', 'conversation', id], old => old ? {
        ...old, current_course_id: courseId ?? null,
        turns: [...(old.turns ?? []).filter(t => t.id !== turn.id), turn],
      } : old)
      const nextDraft = { text: override ? draft.text : '', course_id: courseId ?? null,
        ...(override && draft.attachment_ids?.length ? { attachment_ids: draft.attachment_ids } : {}) }
      setDraft(nextDraft)
      saveDraft(draftKey, nextDraft)
      void queryClient.invalidateQueries({ queryKey: ['agent', 'conversation', id] })
      requestAnimationFrame(() => { if (transcript.current) transcript.current.scrollTop = transcript.current.scrollHeight })
    } catch (e) {
      setError(apiErrorText(e))
      // A transport error has an uncertain outcome; retry must keep the exact key.
      if (!isApiError(e) || e.code !== 'NETWORK_ERROR') setDraft({ ...draft, pending: undefined })
      void queryClient.invalidateQueries({ queryKey: ['agent', 'conversation', id] })
    } finally {
      void queryClient.invalidateQueries({ queryKey: ['agent', 'actions', id] })
      sendingRef.current = false; setSending(false)
    }
  }

  async function cancel(turnId: string) {
    try {
      const cancelled = await agentApi.cancel(turnId)
      queryClient.setQueryData<AgentConversation>(['agent', 'conversation', id], old => old ? {
        ...old, turns: old.turns?.map(t => t.id === turnId ? cancelled : t),
      } : old)
      void queryClient.invalidateQueries({ queryKey: ['agent', 'conversation', id] })
    } catch (e) { setError(apiErrorText(e)) }
  }

  const options = (courses.data?.items ?? []).filter(c => !id || conversation.data?.course_ids.includes(c.id)).map(c => ({ value: c.id, label: c.title }))
  if (courseId && !options.some(o => o.value === courseId)) options.push({ value: courseId, label: courseId })
  function selectCourse(value?: string) {
    setDraft(previous => ({ ...previous, course_id: value ?? null, pending: undefined }))
  }

  function selectProvider(value: string) {
    setProviderId(value)
    setConsent(false)
  }

  function selectAttachments(ids: string[]) {
    setDraft(previous => ({ ...previous, attachment_ids: ids, pending: undefined }))
  }

  function reconnect() {
    setError('')
    void conversation.refetch()
  }

  function onScroll() {
    const element = transcript.current
    if (element) followBottom.current = element.scrollHeight - element.scrollTop - element.clientHeight < 48
    try {
      sessionStorage.setItem(scrollKey, String(element?.scrollTop ?? 0))
      sessionStorage.setItem(scrollKey + '.follow', String(followBottom.current))
    } catch { /* Optional storage. */ }
  }

  return { navigate, draft, consent, setConsent, sending, error, providers, courses,
    conversations, conversation, turns, active, actions, providerItems, selectedProvider,
    provider, courseId, transcript, messages, changeText, create, send, cancel, options,
    selectCourse, selectProvider, selectAttachments, reconnect, onScroll }
}
