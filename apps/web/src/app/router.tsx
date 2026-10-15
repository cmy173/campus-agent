import { Spin } from 'antd'
import { lazy, Suspense, type ComponentType } from 'react'
import { createBrowserRouter, Navigate, useLocation } from 'react-router-dom'
import { AppLayout } from '../components/AppLayout'
import { NotFoundPage } from '../components/NotFoundPage'
import { workspaceHref } from '../features/agent/workspaceRoutes'

function lazyPage(factory: () => Promise<{ default: ComponentType }>) {
  const Page = lazy(factory)
  return <Suspense fallback={<div style={{ padding: 48, textAlign: 'center' }}><Spin /></div>}><Page /></Suspense>
}

/** Compatibility redirects only; old URLs never mount a standalone page. */
function LegacyWorkspaceRedirect() {
  const location = useLocation()
  return <Navigate to={workspaceHref(location.pathname + location.search + location.hash)} replace />
}

export const router = createBrowserRouter([
  { path: '/', element: <AppLayout />, children: [
    { index: true, element: <Navigate to="/chat" replace /> },
    { path: 'chat/:conversationId?', element: lazyPage(() => import('../features/agent/AgentPage').then(m => ({ default: m.AgentPage }))) },
    ...['today', 'courses', 'courses/:courseId', 'plan', 'reviews', 'reviews/:paperId', 'sync', 'settings', 'onboarding']
      .map(path => ({ path, element: <LegacyWorkspaceRedirect /> })),
    ...(import.meta.env.DEV ? [{ path: '__dev/chat-layout', element: lazyPage(() => import('../features/agent/ChatLayoutPreview').then(m => ({ default: m.ChatLayoutPreview }))) }] : []),
    { path: '*', element: <NotFoundPage /> },
  ] },
  { path: '/print/reviews/:paperId', element: lazyPage(() => import('../features/reviews/PrintPaperPage').then(m => ({ default: m.PrintPaperPage }))) },
])
