import { useEffect, useRef, useState } from 'react'
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query'
import { useLocation, useNavigate } from 'react-router-dom'
import { useCourses, useProviders } from '../../lib/api/hooks'
import { API_MODE, isApiError } from '../../lib/api/http'
import { agentApi, isActive } from './api'
import type { AgentConversation, PlanAction } from './api'
import { prepareSend, readDraft, saveDraft } from './draft'

const providerPreferenceKey = 'campus.agent.provider'

function readProviderPreference() {
  try { return localStorage.getItem(providerPreferenceKey) ?? undefined } catch { return undefined }
}

/** Owns API state, retries and recovery independently of the chat layout. */
export function useConversationController(id?: string) {
  const navigate = useNavigate()
  const location = useLocation()
  const queryClient = useQueryClient()
  const draftKey = id ?? 'new'
  const [draft, setDraft] = useState(() => readDraft(draftKey))
  const latestDraft = useRef(draft)
  latestDraft.current = draft
  const [providerId, setProviderId] = useState<string | undefined>(readProviderPreference)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<unknown>(() => location.state?.firstSendError ?? null)
  useEffect(() => {
    if (location.state?.firstSendError) navigate(location.pathname + location.search, { replace: true, state: null })
  }, [location.pathname, location.search, location.state, navigate])
  const createdId = useRef<string | undefined>(id)
  const [historySearch, setHistorySearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(historySearch.trim()), 200)
    return () => window.clearTimeout(timer)
  }, [historySearch])
  const sendingRef = useRef(false)
  const transcript = useRef<HTMLDivElement>(null)
  const messages = useRef<HTMLDivElement>(null)
  const followBottom = useRef(true)
  const scrollKey = 'campus.agent.scroll.' + draftKey
  const restoredScroll = useRef(false)
  const providers = useProviders()
  const courses = useCourses()
  const conversations = useInfiniteQuery({ queryKey: ['agent', 'list', debouncedSearch],
    queryFn: ({ pageParam }) => agentApi.list({ cursor: pageParam, search: debouncedSearch || undefined }),
    initialPageParam: undefined as string | undefined, getNextPageParam: page => page.next_cursor ?? undefined, enabled: API_MODE === 'real' })
  const historyItems = [...new Map((conversations.data?.pages.flatMap(page => page.items) ?? []).map(item => [item.id, item])).values()]
  const conversation = useQuery({ queryKey: ['agent', 'conversation', id], queryFn: ({ signal }) => agentApi.read(id!, signal), enabled: Boolean(id),
    refetchInterval: query => query.state.data?.turns?.some(isActive) ? 650 : false })
  const turns = conversation.data?.turns ?? []
  const active = turns.some(isActive)
  const actions = useQuery({ queryKey: ['agent', 'actions', id], queryFn: () => agentApi.actions(id!), enabled: Boolean(id),
    refetchInterval: active ? 650 : false })
  useEffect(() => { if (id) void queryClient.invalidateQueries({ queryKey: ['agent', 'actions', id] }) }, [active, id, queryClient])
  const providerItems = (providers.data?.items ?? []).filter(p => p.enabled !== false)
  const selectedProvider = providerItems.some(p => p.id === providerId) ? providerId : providerItems[0]?.id
  const provider = providers.data?.items.find(p => p.id === (conversation.data?.provider_id ?? selectedProvider))
  const courseId = draft.course_id ?? undefined

  useEffect(() => {
    if (!selectedProvider) return
    setProviderId(selectedProvider)
    try { localStorage.setItem(providerPreferenceKey, selectedProvider) } catch { /* Optional, non-secret preference. */ }
  }, [selectedProvider])
  useEffect(() => { saveDraft(createdId.current ?? draftKey, draft) }, [draftKey, draft])
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
  async function ensureConversation() {
    if (createdId.current) return createdId.current
    const value = await agentApi.create(selectedProvider!, courseId)
    createdId.current = value.id
    // Move the entire recoverable draft before any send or route remount.
    saveDraft(value.id, { ...latestDraft.current, course_id: courseId ?? null })
    saveDraft('new', { text: '' })
    queryClient.setQueryData(['agent', 'conversation', value.id], { ...value, turns: [] })
    void queryClient.invalidateQueries({ queryKey: ['agent', 'list'] })
    return value.id
  }

  async function create() {
    if (API_MODE !== 'real' || !selectedProvider || sendingRef.current) return
    sendingRef.current = true
    setSending(true)
    setError(null)
    try {
      const cid = await ensureConversation()
      navigate('/chat/' + encodeURIComponent(cid), { replace: true })
    } catch (e) { setError(e) }
    finally { sendingRef.current = false; setSending(false) }
  }

  async function send(override?: { text: string; action: PlanAction; operation: 'apply' | 'revoke' }) {
    const text = override?.text ?? draft.text
    if (API_MODE !== 'real' || (!id && !selectedProvider) || !text.trim() || active || sendingRef.current) return
    sendingRef.current = true
    setSending(true)
    setError(null)
    followBottom.current = true
    let cid = createdId.current
    let sendError: unknown = null
    try {
      cid = await ensureConversation()
      let actionId = draft.pending?.payload.text === text && (!override || override.action.id === draft.pending.payload.action_id)
        ? draft.pending.payload.action_id : undefined
      const operation = override?.operation ?? (text.trim() === '按这个来' ? 'apply' : text.trim() === '撤销刚才安排' ? 'revoke' : undefined)
      const target = override?.action ?? actions.data?.items.find(a => a.kind === 'apply' &&
        (operation === 'apply' ? a.status === 'pending' || a.status === 'approved' : a.status === 'completed'))
      if (operation && !actionId) {
        if (!target) throw new Error('当前没有可确认的具体方案，请先生成并查看预览。')
        const accepted = target.status === 'approved' && target.kind === operation ? target : await agentApi.accept(cid, target.id, operation, crypto.randomUUID())
        actionId = accepted.id
      }
      const pending = prepareSend(draft, { text, ...(courseId ? { course_id: courseId } : { clear_course_context: true }), ...(actionId ? { action_id: actionId } : {}),
        ...(!override && draft.attachment_ids?.length ? { attachment_ids: draft.attachment_ids } : {}) })
      const retained = { ...latestDraft.current, course_id: courseId ?? null, pending }
      latestDraft.current = retained
      saveDraft(cid, retained)
      setDraft(retained)
      const turn = await agentApi.send(cid, pending.payload, pending.key)
      queryClient.setQueryData<AgentConversation>(['agent', 'conversation', cid], old => old ? {
        ...old, current_course_id: courseId ?? null,
        turns: [...(old.turns ?? []).filter(t => t.id !== turn.id), turn],
      } : old)
      const current = latestDraft.current
      const edited = current.text !== draft.text
      const nextDraft = { text: override || edited ? current.text : '', course_id: courseId ?? null,
        ...((override || edited) && current.attachment_ids?.length ? { attachment_ids: current.attachment_ids } : {}) }
      latestDraft.current = nextDraft
      setDraft(nextDraft)
      saveDraft(cid, nextDraft)
      void queryClient.invalidateQueries({ queryKey: ['agent', 'conversation', cid] })
      void queryClient.invalidateQueries({ queryKey: ['agent', 'list'] })
      requestAnimationFrame(() => { if (transcript.current) transcript.current.scrollTop = transcript.current.scrollHeight })
    } catch (e) {
      // Browser history structured-clones state, so transfer public error fields
      // rather than an Error instance whose custom code may be lost.
      sendError = isApiError(e) ? { message: e.message, code: e.code, request_id: e.requestId }
        : { message: e instanceof Error ? e.message : String(e) }
      setError(e)
      // Only a definitive client rejection can discard the key. A 5xx or a
      // malformed success response may arrive after the server committed the turn.
      if (isApiError(e) && e.status >= 400 && e.status < 500 && e.status !== 408) {
        const rejected = { ...latestDraft.current, pending: undefined }
        latestDraft.current = rejected
        setDraft(rejected)
        saveDraft(cid ?? draftKey, rejected)
      }
      if (cid) void queryClient.invalidateQueries({ queryKey: ['agent', 'conversation', cid] })
    } finally {
      if (cid) void queryClient.invalidateQueries({ queryKey: ['agent', 'actions', cid] })
      sendingRef.current = false; setSending(false)
      // Keep create+send in one guarded operation; the restored page sees the
      // final draft/key and cannot race the in-flight first send.
      if (!id && cid) navigate('/chat/' + encodeURIComponent(cid), { replace: true, state: { firstSendError: sendError } })
    }
  }

  async function cancel(turnId: string) {
    try {
      const cancelled = await agentApi.cancel(turnId)
      await queryClient.cancelQueries({ queryKey: ['agent', 'conversation', id] })
      queryClient.setQueryData<AgentConversation>(['agent', 'conversation', id], old => old ? {
        ...old, turns: old.turns?.map(t => t.id === turnId ? cancelled : t),
      } : old)
      void queryClient.invalidateQueries({ queryKey: ['agent', 'conversation', id] })
    } catch (e) { setError(e) }
  }

  const options = (courses.data?.items ?? []).filter(c => !id || conversation.data?.course_ids.includes(c.id)).map(c => ({ value: c.id, label: c.title }))
  if (courseId && !options.some(o => o.value === courseId)) options.push({ value: courseId, label: courseId })
  function selectCourse(value?: string) {
    setDraft(previous => ({ ...previous, course_id: value ?? null, pending: undefined }))
  }

  function selectProvider(value: string) {
    setProviderId(value)
    try { localStorage.setItem(providerPreferenceKey, value) } catch { /* Only the public configuration ID is persisted. */ }
  }

  function selectAttachments(ids: string[]) {
    setDraft(previous => ({ ...previous, attachment_ids: ids, pending: undefined }))
  }

  function reconnect() {
    setError(null)
    if (id) void conversation.refetch()
    void providers.refetch()
    void courses.refetch()
    void conversations.refetch()
  }

  function onScroll() {
    const element = transcript.current
    if (element) followBottom.current = element.scrollHeight - element.scrollTop - element.clientHeight < 48
    try {
      sessionStorage.setItem(scrollKey, String(element?.scrollTop ?? 0))
      sessionStorage.setItem(scrollKey + '.follow', String(followBottom.current))
    } catch { /* Optional storage. */ }
  }

  return { navigate, draft, sending, error, providers, courses,
    conversations, historyItems, historySearch, setHistorySearch, conversation, turns, active, actions, providerItems, selectedProvider,
    provider, courseId, transcript, messages, changeText, create, send, cancel, options,
    selectCourse, selectProvider, selectAttachments, reconnect, onScroll }
}
