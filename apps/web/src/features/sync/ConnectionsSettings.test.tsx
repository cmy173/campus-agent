import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App as AntApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it } from 'vitest'
import { resetMockDb } from '../../lib/api/mock/db'
import { ConnectionsSettings } from './ConnectionsSettings'

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <ConfigProvider locale={zhCN}>
        <AntApp>
          <MemoryRouter>
            <ConnectionsSettings />
          </MemoryRouter>
        </AntApp>
      </ConfigProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  resetMockDb()
})

async function findZyktCard(): Promise<HTMLElement> {
  const card = (await screen.findByText('智云课堂')).closest('.ant-card')
  if (!card) throw new Error('未找到智云课堂连接卡片')
  return card as HTMLElement
}

describe('ConnectionsSettings 登录等待态（C17）', () => {
  it('auth_required 连接显示「等待登录」等待态而非失败，并说明衔接方式与不循环重试', async () => {
    renderPage()
    const card = await findZyktCard()
    expect(within(card).getByText('等待登录')).toBeInTheDocument()
    expect(within(card).getByText('等待登录：同步已暂停（不是失败）')).toBeInTheDocument()
    expect(within(card).getByText(/classroom\.zju\.edu\.cn/)).toBeInTheDocument()
    expect(within(card).getByText(/不会自动循环重试/)).toBeInTheDocument()
    expect(within(card).getByText(/不会收集、代填或保存任何凭据/)).toBeInTheDocument()
    expect(screen.getByText('连接正常')).toBeInTheDocument()
  })

  it('重新登录弹窗无任何凭据输入框，展示权威域名与备用入口', async () => {
    const user = userEvent.setup()
    renderPage()
    const card = await findZyktCard()
    await user.click(within(card).getByRole('button', { name: '重新登录' }))

    expect(await screen.findByText('重新登录平台连接')).toBeInTheDocument()
    const dialog = await screen.findByRole('dialog')
    expect(dialog.querySelectorAll('input')).toHaveLength(0)
    expect(
      screen.getByText(/本机连接器会在受控浏览器窗口中打开平台首页/),
    ).toBeInTheDocument()
    expect(
      within(dialog).getByRole('link', { name: '打开官方登录页（备用入口）' }),
    ).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: '检查连接' })).toBeInTheDocument()
    expect(within(dialog).getByText('https://classroom.zju.edu.cn/')).toBeInTheDocument()
    expect(screen.queryByText(/安装浏览器扩展/)).not.toBeInTheDocument()
  })

  it('手动检查仍未登录：明确提示并留下持久记录，不假装成功', async () => {
    const user = userEvent.setup()
    renderPage()
    const card = await findZyktCard()
    await user.click(within(card).getByRole('button', { name: '检查连接' }))

    expect(
      await screen.findByText(/请在连接器打开的浏览器窗口内完成平台登录后再检查/),
    ).toBeInTheDocument()
    expect(
      await screen.findByText(/最近一次手动检查（.*）仍未检测到有效登录/),
    ).toBeInTheDocument()
    expect(screen.queryByText(/连接检查通过/)).not.toBeInTheDocument()
  })

  it('部分失败任务的逐项错误可展开且带口径提示（403/404 不可重试、5xx 可重试）', async () => {
    const user = userEvent.setup()
    renderPage()
    expect(await screen.findByText('3 项失败')).toBeInTheDocument()

    const syncRow = screen.getByText('平台同步').closest('tr')
    expect(syncRow).not.toBeNull()
    const expandBtn = syncRow!.querySelector<HTMLButtonElement>('button.ant-table-row-expand-icon')
    expect(expandBtn).not.toBeNull()
    await user.click(expandBtn!)

    expect(
      await screen.findByText(/第6章 图算法 讲义\.pdf：该内容受平台权限限制，已跳过/),
    ).toBeInTheDocument()
    expect(
      screen.getByText('该内容受平台权限限制，已跳过；与登录状态无关，重试也不会通过'),
    ).toBeInTheDocument()
    expect(screen.getByText('该内容在平台侧不存在或已被移除，不视为同步故障')).toBeInTheDocument()
    expect(screen.getByText('平台暂时异常，可稍后重试')).toBeInTheDocument()
    expect(screen.getAllByText(/（暂不可重试）/)).toHaveLength(2)
    expect(screen.getByText(/（可重试）/)).toBeInTheDocument()
  })
})
