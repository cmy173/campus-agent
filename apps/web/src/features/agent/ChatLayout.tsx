import type { ReactNode } from 'react'

export interface ChatLayoutProps {
  toolbar: ReactNode
  context: ReactNode
  notices: ReactNode
  transcript: ReactNode
  setup: ReactNode
  composer: ReactNode
  variant?: 'conversation' | 'studio'
  theme?: 'calm' | 'contrast'
}

/** Slots contain presentation only. Layout/theme swaps do not own API or actions. */
export function ChatLayout({ toolbar, context, notices, transcript, setup, composer,
  variant = 'conversation', theme = 'calm' }: ChatLayoutProps) {
  return <div className="agent-page" data-layout={variant} data-theme={theme}>
    <div className="agent-layout-toolbar">{toolbar}</div>
    <div className="agent-layout-context">{context}</div>
    <div className="agent-layout-notices">{notices}</div>
    {transcript}
    <div className="agent-layout-setup">{setup}</div>
    <div className="agent-layout-composer">{composer}</div>
  </div>
}
