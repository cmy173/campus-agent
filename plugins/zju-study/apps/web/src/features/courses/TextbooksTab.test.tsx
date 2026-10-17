import { App } from 'antd'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { TextbooksTab } from './TextbooksTab'
import { apiRequest } from '../../lib/api/http'

vi.mock('../../lib/api/hooks', () => ({ useCourseResources: () => ({ data: { items: [] }, refetch: vi.fn() }) }))
vi.mock('../../lib/api/http', async original => ({ ...(await original<typeof import('../../lib/api/http')>()), apiRequest: vi.fn() }))

describe('教材检查与自动索引', () => {
  beforeEach(() => {
    vi.mocked(apiRequest).mockReset()
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path === '/preferences/textbooks') return { rights_check_enabled: options?.method === 'PUT' ? (options.body as { rights_check_enabled: boolean }).rights_check_enabled : true } as never
      if (path.endsWith('/textbooks')) return { items: [{ id: 'bok_test', course_id: 'crs_test', title: '测试教材', author: '测试作者', edition: '第5版', isbn: '', source_url: '', resource_id: 'res_test', confirmed: true, rights_check: { state: 'unknown', evidence: [], message: '无法判断授权，仍可处理教材。' } }] } as never
      if (path.endsWith('/exercises')) return { items: [] } as never
      if (path.endsWith('/auto-index')) return { message: '自动找到 2 道候选', ambiguous: [] } as never
      return {} as never
    })
  })

  it('关闭检查会保存偏好，不隐藏习题或禁用教材上传', async () => {
    render(<App><TextbooksTab courseId="crs_test" /></App>)
    const toggle = await screen.findByRole('switch')
    await waitFor(() => expect(toggle).not.toBeDisabled())
    expect(await screen.findByText('无法判断')).toBeInTheDocument()
    fireEvent.click(toggle)
    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith('/preferences/textbooks', { method: 'PUT', body: { rights_check_enabled: false } }))
    expect(screen.getByRole('button', { name: '上传教材文件' })).not.toBeDisabled()
    expect(screen.getByRole('button', { name: '自动定位全书章节与习题（无需页码）' })).not.toBeDisabled()
    expect(screen.queryByRole('button', { name: '自动获取已确认教材并导入' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '读取当前教材页面' })).not.toBeInTheDocument()
  })

  it('无需填写页码即可请求全书索引，并展示真实识别结果', async () => {
    render(<App><TextbooksTab courseId="crs_test" /></App>)
    const index = await screen.findByRole('button', { name: '自动定位全书章节与习题（无需页码）' })
    fireEvent.click(index)
    expect(await screen.findByText('自动找到 2 道候选')).toBeInTheDocument()
    expect(apiRequest).toHaveBeenCalledWith('/textbooks/bok_test/auto-index', { method: 'POST' })
  })
})
