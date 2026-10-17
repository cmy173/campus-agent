import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App as AntApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { resetMockDb } from '../../lib/api/mock/db'
import { BuildTasksDrawer } from './BuildTasksDrawer'

function renderDrawer(props?: { defaultCourseId?: string | null; notice?: string }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <ConfigProvider locale={zhCN}>
        <AntApp>
          <BuildTasksDrawer open onClose={() => {}} {...props} />
        </AntApp>
      </ConfigProvider>
    </QueryClientProvider>,
  )
}

async function waitForListReady() {
  expect(await screen.findByText(/该范围现有 \d+ 项任务/, {}, { timeout: 5000 })).toBeInTheDocument()
}

beforeEach(() => {
  resetMockDb()
})

describe('BuildTasksDrawer（任务生成入口与候选确认）', () => {
  it(
    '触发生成 → 展示候选/来源/时长依据/告警 → 改耗时保存 → 归档',
    async () => {
      const user = userEvent.setup()
      renderDrawer({ defaultCourseId: 'course_ds' })
      await waitForListReady()

      await user.click(screen.getByRole('button', { name: /生成候选任务/ }))

      // 生成完成：结果摘要（新建/更新计数）
      expect(
        await screen.findByText(/本次生成：新建 \d+ 项、更新 \d+ 项/, {}, { timeout: 8000 }),
      ).toBeInTheDocument()

      // warnings 如实展示（D/B13 的 TASK_VERSION_UPDATED 与 DUPLICATE_CONTENT）
      expect(screen.getByText('资料有新版本，已对齐')).toBeInTheDocument()
      expect(screen.getByText('内容重复，已合并')).toBeInTheDocument()

      // 候选表：本次操作标签、时长依据（推断）、来源链接（等列表刷新后出现）
      await screen.findByText('候选任务', {}, { timeout: 8000 })
      expect((await screen.findAllByText('新建', {}, { timeout: 5000 })).length).toBeGreaterThan(0)
      expect((await screen.findAllByText('更新', {}, { timeout: 5000 })).length).toBeGreaterThan(0)
      expect(
        (await screen.findAllByText('推断 · 规则估算', {}, { timeout: 5000 })).length,
      ).toBeGreaterThan(0)
      const sourceLinks = screen
        .getAllByRole('link')
        .filter((a) => a.getAttribute('href')?.includes('example.com'))
      expect(sourceLinks.length).toBeGreaterThan(0)

      // 改预计耗时并保存
      const spinButtons = screen.getAllByRole('spinbutton')
      const first = spinButtons[0] as HTMLInputElement
      await user.clear(first)
      await user.type(first, '47')
      await user.click(screen.getByRole('button', { name: '保存' }))
      expect(await screen.findByText(/已保存「/, {}, { timeout: 5000 })).toBeInTheDocument()

      // 归档候选条目：Popconfirm 确认后行从列表移除（软归档）
      const rowsBefore = screen.getAllByRole('row').length
      await user.click(screen.getAllByRole('button', { name: /归档/ })[0])
      await user.click(await screen.findByRole('button', { name: '确认归档' }))
      expect(await screen.findByText(/已归档「/, {}, { timeout: 5000 })).toBeInTheDocument()
      await waitFor(
        () => {
          expect(screen.getAllByRole('row').length).toBeLessThan(rowsBefore)
        },
        { timeout: 5000 },
      )
    },
    20000,
  )

  it(
    '生成中重复触发接入同一任务；完成后重复触发提示无变化（不产生重复任务）',
    async () => {
      const user = userEvent.setup()
      renderDrawer({ defaultCourseId: 'course_ds' })
      await waitForListReady()

      const button = screen.getByRole('button', { name: /生成候选任务/ })
      await user.click(button)
      await user.click(button)

      // 第二次触发检测到同范围进行中任务（同 job id 复用）
      expect(
        await screen.findByText(/已有同一范围的生成任务在进行中，已接入该任务/, {}, { timeout: 5000 }),
      ).toBeInTheDocument()

      expect(
        await screen.findByText(/本次生成：新建 \d+ 项、更新 \d+ 项/, {}, { timeout: 8000 }),
      ).toBeInTheDocument()

      // 完成后再次触发：无新建/更新（match_key 对齐既有任务）
      await user.click(button)
      expect(
        await screen.findByText('本次没有新建或更新任务', {}, { timeout: 8000 }),
      ).toBeInTheDocument()
    },
    25000,
  )

  it('单资源入口的说明文案如实标注“仅支持课程范围”', async () => {
    renderDrawer({
      defaultCourseId: 'course_ds',
      notice: '单资源入口说明：当前生成接口仅支持课程范围（BuildTasksRequest 仅 course_id）',
    })
    expect(await screen.findByText(/仅支持课程范围/)).toBeInTheDocument()
  })
})
