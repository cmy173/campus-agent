import { Spin } from 'antd'
import { lazy, Suspense, type ComponentType } from 'react'
import { createBrowserRouter, Navigate } from 'react-router-dom'
import { AppLayout } from '../components/AppLayout'
import { NotFoundPage } from '../components/NotFoundPage'

/** 路由级代码分割：页面按需加载，主包仅保留布局与框架 */
function lazyPage(factory: () => Promise<{ default: ComponentType }>) {
  const Page = lazy(factory)
  return (
    <Suspense
      fallback={
        <div style={{ display: 'flex', justifyContent: 'center', padding: 48 }}>
          <Spin />
        </div>
      }
    >
      <Page />
    </Suspense>
  )
}

export const router = createBrowserRouter([
  {
    path: '/',
    element: <AppLayout />,
    children: [
      { index: true, element: <Navigate to="/today" replace /> },
      {
        path: 'today',
        element: lazyPage(() => import('../features/today/TodayPage').then((m) => ({ default: m.TodayPage }))),
      },
      {
        path: 'courses',
        element: lazyPage(() =>
          import('../features/courses/CourseListPage').then((m) => ({ default: m.CourseListPage })),
        ),
      },
      {
        path: 'courses/:courseId',
        element: lazyPage(() =>
          import('../features/courses/CourseDetailPage').then((m) => ({ default: m.CourseDetailPage })),
        ),
      },
      {
        path: 'plan',
        element: lazyPage(() => import('../features/plan/PlanPage').then((m) => ({ default: m.PlanPage }))),
      },
      {
        path: 'reviews',
        element: lazyPage(() =>
          import('../features/reviews/ReviewListPage').then((m) => ({ default: m.ReviewListPage })),
        ),
      },
      {
        path: 'reviews/:paperId',
        element: lazyPage(() =>
          import('../features/reviews/ReviewPaperPage').then((m) => ({ default: m.ReviewPaperPage })),
        ),
      },
      {
        path: 'sync',
        element: lazyPage(() =>
          import('../features/sync/SyncCenterPage').then((m) => ({ default: m.SyncCenterPage })),
        ),
      },
      {
        path: 'settings',
        element: lazyPage(() =>
          import('../features/settings/SettingsPage').then((m) => ({ default: m.SettingsPage })),
        ),
      },
      {
        path: 'onboarding',
        element: lazyPage(() =>
          import('../features/onboarding/OnboardingPage').then((m) => ({ default: m.OnboardingPage })),
        ),
      },
      { path: 'classroom', element: lazyPage(() => import('../features/classroom/ClassroomPage').then(m => ({ default: m.ClassroomPage }))) },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
  {
    // 打印页独立布局（不套用导航/页头，打印与另存 PDF 时不带调试信息与密钥）
    path: '/print/reviews/:paperId',
    element: lazyPage(() =>
      import('../features/reviews/PrintPaperPage').then((m) => ({ default: m.PrintPaperPage })),
    ),
  },
])
