import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App as AntApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import dayjs from 'dayjs'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it } from 'vitest'
import { getDb, resetMockDb } from '../../lib/api/mock/db'
import { DEFAULT_TIMEZONE, toApiIso } from '../../lib/time'
import { TaskWorkspace } from './TaskWorkspace'

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <ConfigProvider locale={zhCN}>
        <AntApp>
          <MemoryRouter>
            <TaskWorkspace />
          </MemoryRouter>
        </AntApp>
      </ConfigProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  resetMockDb()
})

describe('TaskWorkspace', () => {
  it('展示今日任务与作业截止信息', async () => {
    renderPage()
    expect(await screen.findByText('阅读《第3章 哈希表 讲义》第 1–25 页')).toBeInTheDocument()
    expect(
      screen.getByText('观看回放：第4讲 TCP 拥塞控制并整理笔记'),
    ).toBeInTheDocument()
    expect(screen.getByText('临近截止作业')).toBeInTheDocument()
  })

  it('反馈弹窗明确「学习完成 ≠ 学校平台已提交」，提交后只改学习状态', async () => {
    const user = userEvent.setup()
    renderPage()

    const feedbackButtons = await screen.findAllByRole('button', { name: '记录反馈' })
    await user.click(feedbackButtons[0])

    expect(await screen.findByText('学习完成 ≠ 学校平台已提交')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '提交反馈' }))

    expect(
      await screen.findByText('已记录学习完成（学习状态，不等同于学校平台已提交）'),
    ).toBeInTheDocument()
    expect(await screen.findAllByText('学习完成')).not.toHaveLength(0)
  })

  it('平台已提交作业对应的任务：标注、行弱化、不计入待完成统计（PD-3）', async () => {
    const { unmount } = renderPage()
    const beforeEl = await screen.findByText(/项待完成/)
    const before = Number(beforeEl.textContent!.match(/(\d+) 项待完成/)![1])
    unmount()

    const db = getDb()
    const now = toApiIso(dayjs(), DEFAULT_TIMEZONE)
    db.tasks.push({
      id: 'task_net_quiz_review',
      match_key: 't:course_net:assignment:ASG-NET-QUIZ1',
      course_id: 'course_net',
      type: 'assignment',
      title: '复习：概念测验 1 错题回顾',
      estimated_minutes: 30,
      due_at: now,
      due_precision: 'datetime',
      prerequisite_ids: [],
      source_refs: [
        {
          platform: 'xzzd',
          external_id: 'ASG-NET-QUIZ1',
          canonical_url: 'https://example.com/xzzd/assignments/net-quiz1',
          observed_at: now,
        },
      ],
      status: 'pending',
      fact_source: 'inferred',
      revision: 1,
      archived_at: null,
      created_at: now,
      updated_at: now,
    })

    renderPage()
    expect(await screen.findByText('复习：概念测验 1 错题回顾')).toBeInTheDocument()

    const row = screen.getByText('复习：概念测验 1 错题回顾').closest('div[style*="opacity"]')
    expect(row).not.toBeNull()
    expect(within(row as HTMLElement).getByText('平台已提交')).toBeInTheDocument()

    const afterEl = await screen.findByText(/项待完成/)
    const after = Number(afterEl.textContent!.match(/(\d+) 项待完成/)![1])
    expect(after).toBe(before)
    expect(afterEl.textContent).toContain('平台已提交 1 项不参与排程')
  })
})
