import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App as AntApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../lib/api/client'
import { getDb, resetMockDb } from '../../lib/api/mock/db'
import { PlanPage } from './PlanPage'

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <ConfigProvider locale={zhCN}>
        <AntApp>
          <MemoryRouter>
            <PlanPage />
          </MemoryRouter>
        </AntApp>
      </ConfigProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  resetMockDb()
})
afterEach(() => vi.restoreAllMocks())

describe('PlanPage', () => {
  it.each(['已安排', '未安排'])('计划中的后页%s任务显示中文名称，包含归档任务而不显示内部编号', async (kind) => {
    const db = getDb()
    const taskId = kind === '已安排' ? db.activePlan!.blocks[0].task_id : db.activePlan!.unassigned[0].task_id
    const task = db.tasks.find(t => t.id === taskId)!
    const title = '后页归档课件的复习任务'
    vi.spyOn(api, 'getTasks')
      .mockResolvedValueOnce({ items: db.tasks.filter(t => t.id !== task.id), next_cursor: 'second' })
      .mockResolvedValueOnce({ items: [{ ...task, title, archived_at: '2026-10-10T00:00:00Z' }], next_cursor: null })
    renderPage()
    expect(await screen.findByText(title)).toBeInTheDocument()
    expect(screen.queryByText(task.id)).not.toBeInTheDocument()
    expect(api.getTasks).toHaveBeenLastCalledWith(expect.objectContaining({ cursor: 'second', include_archived: true }))
  })
  it('展示生效计划与无法安排任务的原因码', async () => {
    renderPage()
    expect(await screen.findByText('当前生效计划')).toBeInTheDocument()
    expect(screen.getByText('生效中')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /生成预览/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /撤销该计划/ })).toBeInTheDocument()
    expect(screen.getByText('无法安排的任务（2）')).toBeInTheDocument()
    expect(screen.getByText('已过截止时间')).toBeInTheDocument()
    expect(screen.getByText('任务不可拆分且超出单日窗口')).toBeInTheDocument()
  })

  it('生成预览后展示差异标签与应用/放弃入口，放弃后回到生效计划', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.click(await screen.findByRole('button', { name: /生成预览/ }))

    expect(await screen.findByText('计划预览')).toBeInTheDocument()
    expect(screen.getByText('这是预览，尚未应用')).toBeInTheDocument()
    // 锁定块 task_ds_ex3 的时间与生效计划一致，应归入「保留」
    expect((await screen.findAllByText('保留')).length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: /^应用此计划/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /撤销该计划/ })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /放弃预览/ }))
    expect(await screen.findByText('当前生效计划')).toBeInTheDocument()
    expect(screen.queryByText('这是预览，尚未应用')).not.toBeInTheDocument()
  })
})
