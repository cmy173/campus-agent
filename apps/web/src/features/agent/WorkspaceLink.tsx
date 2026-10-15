import { Link as RouterLink, useLocation, type LinkProps } from 'react-router-dom'
import { workspaceHref } from './workspaceRoutes'

/** Editor links stay in the current conversation instead of navigating to a second app. */
export function WorkspaceLink({ to, ...props }: LinkProps) {
  const location = useLocation()
  const chatPath = /^\/chat(?:\/[^/]+)?$/.test(location.pathname) ? location.pathname : '/chat'
  return <RouterLink {...props} to={typeof to === 'string' ? workspaceHref(to, chatPath) : to} />
}
