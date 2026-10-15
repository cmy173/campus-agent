import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App as AntApp } from 'antd'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../lib/api/client'
import { getDb, resetMockDb } from '../../lib/api/mock/db'
import { PlatformCourseImport } from './PlatformCourseImport'

afterEach(() => vi.restoreAllMocks())

describe('平台课程导入', () => {
  it('智云打开即自动读取本学期课表，无需手填课号，跨页去重后导入', async () => {
    resetMockDb()
    const connection = getDb().connections.find(c => c.platform === 'zykt')!
    const course = { ...getDb().courses[0], id: 'zy_real', title: '个人课表中的数学课', source_refs: [{ platform: 'zykt', external_id: 'zy_real', canonical_url: null, observed_at: '2026-10-11T00:00:00Z' }] }
    const discover = vi.spyOn(api, 'discoverCourses')
      .mockResolvedValueOnce({ items: [course], next_cursor: 'current:month:2026-10:' })
      .mockResolvedValueOnce({ items: [course], next_cursor: null })
    const importer = vi.spyOn(api, 'importPlatformCourses').mockResolvedValue({ items: [course], next_cursor: null })
    render(<QueryClientProvider client={new QueryClient()}><AntApp><PlatformCourseImport connection={connection} /></AntApp></QueryClientProvider>)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /导入.*课程/ }))
    expect(await screen.findByText('个人课表中的数学课')).toBeInTheDocument()
    await waitFor(() => expect(discover).toHaveBeenCalledTimes(2))
    expect(discover).toHaveBeenLastCalledWith(connection.id, 'current:month:2026-10:', 'current')
    expect(screen.queryByPlaceholderText('例如 12345')).not.toBeInTheDocument()
    await user.click(screen.getAllByRole('checkbox')[1])
    await user.click(screen.getByRole('button', { name: '导入所选课程' }))
    await waitFor(() => expect(importer).toHaveBeenCalledWith(connection.id, [expect.objectContaining({ external_id: 'zy_real' })]))
  })
  it('读取课程、选择、导入包含真实平台标识，而非虚构课号', async () => {
    resetMockDb()
    const connection = getDb().connections.find(c => c.platform === 'xzzd')!
    const course = { ...getDb().courses[0], id: '12345', title: '实际读取的课程', source_refs: [{ platform: 'xzzd', external_id: '12345', canonical_url: null, observed_at: '2026-10-11T00:00:00Z' }] }
    vi.spyOn(api, 'discoverCourses').mockResolvedValue({ items: [course], next_cursor: null })
    const importer = vi.spyOn(api, 'importPlatformCourses').mockResolvedValue({ items: [course], next_cursor: null })
    render(<QueryClientProvider client={new QueryClient()}><AntApp><PlatformCourseImport connection={connection} /></AntApp></QueryClientProvider>)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /导入.*课程/ }))
    await user.click(screen.getByRole('button', { name: '读取平台课程' }))
    await screen.findByText('实际读取的课程')
    await user.click(screen.getAllByRole('checkbox')[1])
    await user.click(screen.getByRole('button', { name: '导入所选课程' }))
    await waitFor(() => expect(importer).toHaveBeenCalledWith(connection.id, [expect.objectContaining({ external_id: '12345', title: '实际读取的课程' })]))
  })
  it('未启动服务时显示错误，导入不产生半成品', async () => {
    resetMockDb()
    const connection = getDb().connections.find(c => c.platform === 'xzzd')!
    vi.spyOn(api, 'discoverCourses').mockRejectedValue(new Error('本地连接器未启动'))
    const importer = vi.spyOn(api, 'importPlatformCourses')
    render(<QueryClientProvider client={new QueryClient()}><AntApp><PlatformCourseImport connection={connection} /></AntApp></QueryClientProvider>)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /导入.*课程/ }))
    await user.click(screen.getByRole('button', { name: '读取平台课程' }))
    expect(await screen.findByText(/本地连接器未启动/)).toBeInTheDocument()
    expect(importer).not.toHaveBeenCalled()
  })
})
