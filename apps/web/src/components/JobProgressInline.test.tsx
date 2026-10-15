import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App as AntApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { resetMockDb } from '../lib/api/mock/db'
import { JobProgressInline } from './JobProgressInline'

function renderInline(jobId: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <ConfigProvider locale={zhCN}>
        <AntApp>
          <JobProgressInline jobId={jobId} />
        </AntApp>
      </ConfigProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  resetMockDb()
})

describe('JobProgressInline 逐项错误（C17：A5b 口径）', () => {
  it('按 403/404/5xx 分别标注重试性与口径提示，页面无凭据输入', async () => {
    renderInline('job_sync_1')

    expect(await screen.findByText('以下项目处理失败')).toBeInTheDocument()
    expect(
      screen.getByText('第6章 图算法 讲义.pdf：该内容受平台权限限制，已跳过'),
    ).toBeInTheDocument()
    expect(
      screen.getByText('该内容受平台权限限制，已跳过；与登录状态无关，重试也不会通过'),
    ).toBeInTheDocument()
    expect(screen.getByText('该内容在平台侧不存在或已被移除，不视为同步故障')).toBeInTheDocument()
    expect(screen.getByText('平台暂时异常，可稍后重试')).toBeInTheDocument()
    expect(screen.getAllByText('暂不可重试')).toHaveLength(2)
    expect(screen.getByText('可重试')).toBeInTheDocument()
    // antd 会在两字中文按钮中自动插入空格（重 试）
    expect(screen.getByRole('button', { name: /重\s*试/ })).toBeInTheDocument()
    expect(document.querySelectorAll('input')).toHaveLength(0)
  })
})
