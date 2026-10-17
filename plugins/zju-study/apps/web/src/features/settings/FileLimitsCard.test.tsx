import { App } from 'antd'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { apiRequest } from '../../lib/api/http'
import { checkUploadFile, configureUploadLimit } from '../../lib/upload'
import { FileLimitsCard } from './FileLimitsCard'

vi.mock('../../lib/api/http', async original => ({ ...(await original<typeof import('../../lib/api/http')>()), apiRequest: vi.fn() }))
afterEach(() => configureUploadLimit(1024))

it('加载并保存独立的下载、上传和解析限制，更新上传检查', async () => {
  vi.mocked(apiRequest).mockImplementation(async (_path, options) =>
    (options?.method === 'PUT' ? options.body : { download_mb: 1024, upload_mb: 512, parse_mb: 128 }) as never)
  render(<App><FileLimitsCard /></App>)
  const upload = screen.getByLabelText('本地文件上传上限（MiB）')
  await waitFor(() => expect(upload).toHaveValue('512'))
  expect(screen.getByLabelText('平台文件下载上限（MiB）')).toHaveValue('1024')
  expect(screen.getByLabelText('单个文档文本解析上限（MiB）')).toHaveValue('128')
  fireEvent.change(upload, { target: { value: '600' } })
  fireEvent.click(screen.getByRole('button', { name: '保存文件大小设置' }))
  await waitFor(() => expect(apiRequest).toHaveBeenCalledWith('/preferences/file-limits', {
    method: 'PUT', body: { download_mb: 1024, upload_mb: 600, parse_mb: 128 },
  }))
  await waitFor(() => expect(checkUploadFile('test.pdf', 550 * 1024 * 1024).ok).toBe(true))
  expect(checkUploadFile('test.pdf', 601 * 1024 * 1024).ok).toBe(false)
})
