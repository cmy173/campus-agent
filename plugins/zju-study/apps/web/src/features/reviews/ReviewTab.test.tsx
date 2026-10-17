import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App as AntApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../../lib/api/client'
import { resetMockDb } from '../../lib/api/mock/db'
import { ReviewTab } from './ReviewTab'

function renderTab() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <ConfigProvider locale={zhCN}>
        <AntApp>
          <MemoryRouter>
            <ReviewTab courseId="course_ds" courseTitle="数据结构与算法" />
          </MemoryRouter>
        </AntApp>
      </ConfigProvider>
    </QueryClientProvider>,
  )
}

async function makeProvider() {
  return api.createProvider({
    name: '本地推理服务',
    protocol: 'openai_chat_completions',
    base_url: 'http://127.0.0.1:11434/v1',
    model: 'demo-model',
    api_key: 'sk-demo-abcd1234',
    timeout_seconds: 120,
  })
}

/** 勾选可用的「第3章 哈希表 讲义.pdf」材料（同名旧版本行不可用则跳过） */
async function selectUsableResource(user: ReturnType<typeof userEvent.setup>) {
  const titles = await screen.findAllByText('第3章 哈希表 讲义.pdf')
  for (const title of titles) {
    const checkbox = title
      .closest('label')
      ?.querySelector<HTMLInputElement>('input[type="checkbox"]')
    if (checkbox && !checkbox.disabled) {
      await user.click(checkbox)
      return
    }
  }
  throw new Error('未找到可勾选的「第3章 哈希表 讲义.pdf」材料行')
}

beforeEach(() => {
  resetMockDb()
})

describe('ReviewTab 取消后再生成（幂等键代数）', () => {
  it('取消终态后再点生成：提交的是新任务，不复用已取消任务的幂等键', async () => {
    const user = userEvent.setup()
    await makeProvider()
    renderTab()

    await selectUsableResource(user)
    await user.click(await screen.findByRole('button', { name: /预览将发送的内容/ }))
    await screen.findByText(/请求指纹/)

    await user.click(await screen.findByRole('button', { name: /生成复习卷/ }))
    const firstJobNode = await screen.findByText(/job_review_generate_\d+/, {}, { timeout: 5000 })
    const firstJobId = firstJobNode.textContent

    // 取消：等到任务进入 cancelled 终态（轮询间隔 1.2s）
    await user.click(await screen.findByRole('button', { name: '取消生成' }))
    await screen.findByText(
      '已取消：未生成新的复习卷（旧卷保留在历史列表）',
      {},
      { timeout: 5000 },
    )

    // 终态后（genNonce+1）再点生成：必须产生新任务
    await user.click(await screen.findByRole('button', { name: /生成复习卷/ }))
    await waitFor(
      () => {
        const node = screen.getByText(/job_review_generate_\d+/)
        expect(node.textContent).not.toBe(firstJobId)
      },
      { timeout: 5000 },
    )
  }, 20000)
})
