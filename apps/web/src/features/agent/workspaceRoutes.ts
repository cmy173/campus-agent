export const workspaceNames = ['courses', 'tasks', 'calendar', 'reviews', 'settings'] as const
export type CampusWorkspace = typeof workspaceNames[number]

/** One product URL. Legacy links retain their entity/filter query in a task workspace. */
export function workspaceHref(destination: string, chatPath = '/chat'): string {
  const url = new URL(destination, 'http://campus.local')
  const paths = url.pathname.split('/').filter(Boolean).map(segment => {
    try { return decodeURIComponent(segment) } catch { return segment }
  })
  const mapping: Record<string, CampusWorkspace> = {
    courses: 'courses', today: 'tasks', plan: 'calendar', reviews: 'reviews',
    settings: 'settings', sync: 'settings', onboarding: 'settings',
  }
  const workspace = mapping[paths[0]]
  if (!workspace) return destination
  url.searchParams.set('workspace', workspace)
  if (paths[0] === 'courses' && paths[1]) url.searchParams.set('course', paths[1])
  if (paths[0] === 'reviews' && paths[1]) url.searchParams.set('paper', paths[1])
  if (paths[0] === 'sync' || paths[0] === 'onboarding') url.searchParams.set('settings_tab', 'accounts')
  return `${chatPath}?${url.searchParams.toString()}${url.hash}`
}

