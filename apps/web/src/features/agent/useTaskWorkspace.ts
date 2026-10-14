import { useSearchParams } from 'react-router-dom'
import { workspaceNames, type CampusWorkspace } from './workspaceRoutes'

/** URL state makes a task workspace restorable without changing conversation state. */
export function useTaskWorkspace() {
  const [params, setParams] = useSearchParams()
  const raw = params.get('workspace')
  const workspace = workspaceNames.includes(raw as CampusWorkspace) ? raw as CampusWorkspace : null
  function open(workspace: CampusWorkspace, entity?: { course?: string; paper?: string }) {
    setParams(previous => {
      const next = new URLSearchParams(previous)
      for (const key of ['course', 'paper', 'tab', 'revision', 'base', 'view', 'settings_tab', 'course_id', 'mode']) next.delete(key)
      next.set('workspace', workspace)
      for (const [key, value] of Object.entries(entity ?? {})) if (value) next.set(key, value)
      return next
    })
  }
  function close() {
    setParams(previous => {
      const next = new URLSearchParams(previous)
      for (const key of ['workspace', 'course', 'paper', 'tab', 'revision', 'base', 'view', 'settings_tab', 'course_id', 'mode']) next.delete(key)
      return next
    }, { replace: true })
  }
  function editCalendar() {
    setParams(previous => { const next = new URLSearchParams(previous); next.set('view', 'edit'); return next })
  }
  return { workspace, course: params.get('course') ?? undefined, paper: params.get('paper') ?? undefined,
    calendarEditor: params.get('view') === 'edit', open, close, editCalendar }
}
