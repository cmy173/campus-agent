import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App as AntApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it } from 'vitest'
import { resetMockDb } from '../../lib/api/mock/db'
import { ResourcesTab } from './ResourcesTab'

function renderTab() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <ConfigProvider locale={zhCN}>
        <AntApp>
          <MemoryRouter>
            <ResourcesTab courseId="course_ds" />
          </MemoryRouter>
        </AntApp>
      </ConfigProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  resetMockDb()
})

describe('ResourcesTab 上传护栏与超大文件标注（PD-1）', () => {
  it('导入说明为单文件 64MB 以内，且明示超过 30MB 解析较慢；不出现过时 50MB 文案', async () => {
    renderTab()
    expect(await screen.findByText(/单文件 64MB 以内/)).toBeInTheDocument()
    expect(screen.getByText(/超过 30MB 的超大文件解析较慢/)).toBeInTheDocument()
    expect(screen.queryByText(/50MB/)).toBeNull()
  })

  it('42.9MB 样例行展示大小并带「超大文件」标注；普通大小行不带标注', async () => {
    renderTab()
    const bigTitle = await screen.findByText('第6章 图算法 讲义（扫描版）.pdf')
    const bigRow = bigTitle.closest('tr')
    expect(bigRow).not.toBeNull()
    expect(within(bigRow!).getByText('42.9 MB')).toBeInTheDocument()
    expect(within(bigRow!).getByText('超大文件')).toBeInTheDocument()

    const normalTitle = screen.getByText('课程大纲.pptx')
    const normalRow = normalTitle.closest('tr')
    expect(normalRow).not.toBeNull()
    expect(within(normalRow!).getByText('3.0 MB')).toBeInTheDocument()
    expect(within(normalRow!).queryByText('超大文件')).toBeNull()
  })
})
