import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App, ConfigProvider } from 'antd'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { FilePreviewPane } from './FileWorkspace'
import { agentApi } from './api'

const file = { id: 'sheet_one', conversation_id: 'conv_one', kind: 'output' as const, name: '资料.xlsx', size: 100,
  sha256: 'fixture', mime_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', created_at: '2026-10-14T00:00:00Z' }
afterEach(() => vi.restoreAllMocks())
function show() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <ConfigProvider><App><FilePreviewPane file={file} /></App></ConfigProvider>
  </QueryClientProvider>)
}
describe('Spreadsheet file preview', () => {
  it('preserves zero and literal formulas, exposes truncation and switches worksheets', async () => {
    const user = userEvent.setup()
    vi.spyOn(agentApi, 'preview').mockResolvedValue({ kind: 'spreadsheet', truncated: true, has_formulas: true, sheets: [
      { name: '资料清单', rows: [['资料', '页数'], ['<script>source</script>', 0], ['合计', '=SUM(B2:B3)']] },
      { name: '记录', rows: [['截止时间', '日期'], ['未知', '2026-10-20']] },
    ] })
    show()
    const table = await screen.findByRole('table', { name: '资料清单 表格' })
    expect(within(table).getByText('0')).toBeInTheDocument()
    expect(within(table).getByText('=SUM(B2:B3)')).toBeInTheDocument()
    expect(within(table).getByText('<script>source</script>')).toBeInTheDocument()
    expect(document.querySelector('script')).toBeNull()
    expect(screen.getByText(/此预览不重新计算结果/)).toBeInTheDocument()
    expect(screen.getByText(/部分内容已截断/)).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: '记录' }))
    expect(within(await screen.findByRole('table', { name: '记录 表格' })).getByText('未知')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '下载 资料.xlsx' })).toHaveAttribute('href', '/api/v1/agent/conversations/conv_one/files/sheet_one/content')
  })
  it('shows an oversized-workbook download message while keeping the original available', async () => {
    vi.spyOn(agentApi, 'preview').mockResolvedValue({ kind: 'download', truncated: false, message: '表格较大，请下载后打开完整内容。' })
    show()
    expect(await screen.findByText('表格较大，请下载后打开完整内容。')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '下载 资料.xlsx' })).toBeInTheDocument()
  })
})
