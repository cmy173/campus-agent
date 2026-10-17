import { App as AntApp } from 'antd'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '../../lib/api/client'
import { getDb, resetMockDb } from '../../lib/api/mock/db'
import { ReferenceExamUpload } from './ReferenceExamUpload'

afterEach(() => vi.restoreAllMocks())

it('uploads a real file as a named reference and selects only after parsed text is ready', async () => {
  resetMockDb()
  const db = getDb()
  const resource = { ...db.resources[0], id: 'past-exam-resource', parse_status: 'ready' as const }
  const job = { ...db.jobs[0], id: 'past-exam-job', status: 'succeeded' as const,
    errors: [], result_refs: [{ type: 'resource' as const, id: resource.id }] }
  const upload = vi.spyOn(api, 'importFiles').mockResolvedValue({ job })
  vi.spyOn(api, 'getJob').mockResolvedValue(job)
  vi.spyOn(api, 'getResource').mockResolvedValue(resource)
  vi.spyOn(api, 'getResourceChunks').mockResolvedValue({ items: [{ id: 'chunk-past', resource_id: resource.id,
    resource_version: 1, ordinal: 0, provenance: { extractor: 'pdf-text', extractor_version: '1',
      job_id: job.id, fact_source: 'inferred', observed_at: '' }, created_at: '',
    locator: { kind: 'page', page: 1 }, text: '历年卷第一题正文' }], next_cursor: null })
  const onReady = vi.fn()
  const { container } = render(<AntApp><ReferenceExamUpload courseId="course_ds" onReady={onReady} /></AntApp>)
  const file = new File(['test paper'], '2025期末卷.pdf', { type: 'application/pdf' })
  await userEvent.setup().upload(container.querySelector('input[type=file]')!, file)
  await waitFor(() => expect(onReady).toHaveBeenCalledWith([resource.id]))
  expect(upload.mock.calls[0][0]).toEqual({ course_id: 'course_ds', kind: 'document', title: '历年卷参考：2025期末卷.pdf' })
  expect(upload.mock.calls[0][1][0]).toBe(file)
  expect(screen.getByText(/历年卷已解析并勾选/)).toBeInTheDocument()
})
