import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App as AntApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it } from 'vitest'
import { getDb, resetMockDb, tickJobs } from '../../lib/api/mock/db'
import { SettingsPage } from './SettingsPage'

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <ConfigProvider locale={zhCN}>
        <AntApp>
          <MemoryRouter>
            <SettingsPage />
          </MemoryRouter>
        </AntApp>
      </ConfigProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  resetMockDb()
})

describe('SettingsPage 数据导出（C19b）', () => {
  it('ICS 导出按钮提交任务并产出 ICS 独立产物（exp_ics_*）', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.click(await screen.findByRole('button', { name: '导出 ICS' }))
    await screen.findByText(/job_export_\d+/)

    // 推进模拟任务至完成（首次 tick 进入 running，第二次带未来时间点结束）
    tickJobs(getDb())
    tickJobs(getDb(), Date.now() + 5000)

    expect(await screen.findByText(/exp_ics_\d+/, {}, { timeout: 3000 })).toBeInTheDocument()
  })

  it('JSON 导出按钮仍可用，与 ICS 共用任务进度展示', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.click(await screen.findByRole('button', { name: '开始导出' }))
    await screen.findByText(/job_export_\d+/)

    tickJobs(getDb())
    tickJobs(getDb(), Date.now() + 5000)

    expect(await screen.findByText(/exp_\d+/, {}, { timeout: 3000 })).toBeInTheDocument()
  })

  it('能力清单如实标注 ICS 已接通（旧「ICS 日历导出未接通」不残留）', async () => {
    renderPage()
    expect(await screen.findByText(/JSON 与 ICS 日历导出均已接通/)).toBeInTheDocument()
    expect(screen.queryByText(/ICS 日历导出未接通/)).not.toBeInTheDocument()
  })
})
