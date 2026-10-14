import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { workspaceHref } from './workspaceRoutes'
import { WorkspaceLink } from './WorkspaceLink'
import { useTaskWorkspace } from './useTaskWorkspace'
import { AppLayout } from '../../components/AppLayout'

function Location() { const location = useLocation(); return <output>{location.pathname + location.search}</output> }
function Controls() {
  const state = useTaskWorkspace()
  return <><output data-testid="workspace">{state.workspace}:{state.course}</output>
    <button onClick={() => state.open('calendar')}>calendar</button><button onClick={state.close}>close</button><Location /></>
}

describe('conversation workspace navigation', () => {
  it('maps every old entry to a chat workspace and preserves entity/revision filters', () => {
    expect(workspaceHref('/courses/course%20a?tab=review&base=paper_a')).toBe('/chat?tab=review&base=paper_a&workspace=courses&course=course+a')
    expect(workspaceHref('/reviews/paper_a?revision=2')).toBe('/chat?revision=2&workspace=reviews&paper=paper_a')
    for (const path of ['today', 'courses', 'plan', 'reviews', 'sync', 'settings', 'onboarding']) expect(workspaceHref('/' + path)).toMatch(/^\/chat\?/) 
    expect(workspaceHref('/print/reviews/paper_a')).toBe('/print/reviews/paper_a')
  })
  it('editor links retain current conversation while changing the task workspace', () => {
    render(<MemoryRouter initialEntries={['/chat/conv_one?workspace=courses']}><WorkspaceLink to="/reviews/paper_a?revision=2">打开复习卷</WorkspaceLink><Location /></MemoryRouter>)
    fireEvent.click(screen.getByRole('link'))
    expect(screen.getByText('/chat/conv_one?revision=2&workspace=reviews&paper=paper_a')).toBeInTheDocument()
  })
  it('restores workspace/entity from URL, removes stale entity fields and preserves the conversation', () => {
    render(<MemoryRouter initialEntries={['/chat/conv_one?workspace=courses&course=course_a&tab=review&revision=3']}><Controls /></MemoryRouter>)
    expect(screen.getByTestId('workspace')).toHaveTextContent('courses:course_a')
    fireEvent.click(screen.getByText('calendar'))
    expect(screen.getByTestId('workspace')).toHaveTextContent('calendar:')
    expect(screen.getByText('/chat/conv_one?workspace=calendar')).toBeInTheDocument()
    fireEvent.click(screen.getByText('close'))
    expect(screen.getByText('/chat/conv_one')).toBeInTheDocument()
  })
  it('shell offers the conversation entry without the retired workbench navigation', () => {
    render(<MemoryRouter initialEntries={['/chat']}><Routes><Route element={<AppLayout />}><Route path="chat" element={<p>会话</p>} /></Route></Routes></MemoryRouter>)
    expect(screen.getAllByRole('link')).toHaveLength(1)
    expect(screen.queryByText('辅助工作台')).not.toBeInTheDocument()
  })
})
