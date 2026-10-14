import { lazy, Suspense } from 'react'
import { Drawer, Spin } from 'antd'
import type { useTaskWorkspace } from './useTaskWorkspace'

const CourseLibrary = lazy(() => import('../courses/CourseLibrary').then(m => ({ default: m.CourseLibrary })))
const CourseEditor = lazy(() => import('../courses/CourseEditor').then(m => ({ default: m.CourseEditor })))
const Tasks = lazy(() => import('../today/TaskWorkspace').then(m => ({ default: m.TaskWorkspace })))
const PlanEditor = lazy(() => import('../plan/PlanEditor').then(m => ({ default: m.PlanEditor })))
const ReviewLibrary = lazy(() => import('../reviews/ReviewLibrary').then(m => ({ default: m.ReviewLibrary })))
const ReviewEditor = lazy(() => import('../reviews/ReviewEditor').then(m => ({ default: m.ReviewEditor })))
const Settings = lazy(() => import('../settings/SettingsWorkspace').then(m => ({ default: m.SettingsWorkspace })))
const titles = { courses: '课程与资料', tasks: '任务与学习反馈', calendar: '编辑学习安排', reviews: '复习产物', settings: '助手设置' }

/** Domain editors mount on demand inside the same chat, never as parallel navigation. */
export function TaskWorkspace({ state }: { state: ReturnType<typeof useTaskWorkspace> }) {
  const current = state.workspace
  const visible = Boolean(current && (current !== 'calendar' || state.calendarEditor))
  return <Drawer title={current ? titles[current] : ''} open={visible} onClose={state.close}
    destroyOnHidden size="min(900px, 100vw)" className="agent-workspace agent-task-workspace">
    <Suspense fallback={<div style={{ padding: 32, textAlign: 'center' }}><Spin /></div>}>
      {visible && current === 'courses' && (state.course ? <CourseEditor key={state.course} id={state.course} /> : <CourseLibrary />)}
      {visible && current === 'tasks' && <Tasks />}
      {visible && current === 'calendar' && <PlanEditor />}
      {visible && current === 'reviews' && (state.paper ? <ReviewEditor key={state.paper} id={state.paper} /> : <ReviewLibrary />)}
      {visible && current === 'settings' && <Settings />}
    </Suspense>
  </Drawer>
}
