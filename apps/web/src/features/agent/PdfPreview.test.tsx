import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { PdfPreview } from './PdfPreview'

const mock = vi.hoisted(() => ({ getDocument: vi.fn() }))
vi.mock('pdfjs-dist', () => ({ GlobalWorkerOptions: {}, getDocument: mock.getDocument }))
vi.mock('pdfjs-dist/build/pdf.worker.min.mjs?url', () => ({ default: '/fixture-worker.mjs' }))
const originalResizeObserver = globalThis.ResizeObserver
beforeEach(() => {
  mock.getDocument.mockReset()
  globalThis.ResizeObserver = class {
    callback: ResizeObserverCallback
    constructor(callback: ResizeObserverCallback) { this.callback = callback }
    observe() { this.callback([{ contentRect: { width: 500 } } as ResizeObserverEntry], this) }
    unobserve() {}
    disconnect() {}
  }
})
afterEach(() => { globalThis.ResizeObserver = originalResizeObserver })

it('cancels the previous page render and destroys the owned document when closed', async () => {
  const user = userEvent.setup()
  let rejectFirst!: (value: Error) => void
  const first = { promise: new Promise<void>((_, reject) => { rejectFirst = reject }), cancel: vi.fn(() => rejectFirst(new Error('cancelled'))) }
  const second = { promise: Promise.resolve(), cancel: vi.fn() }
  const page = { getViewport: ({ scale }: { scale: number }) => ({ width: 600 * scale, height: 800 * scale }),
    render: vi.fn().mockReturnValueOnce(first).mockReturnValue(second) }
  const document = { numPages: 2, getPage: vi.fn().mockResolvedValue(page) }
  const destroy = vi.fn().mockResolvedValue(undefined)
  mock.getDocument.mockReturnValue({ promise: Promise.resolve(document), destroy })
  const view = render(<PdfPreview url="/owned-file.pdf" name="资料.pdf" />)
  await waitFor(() => expect(page.render).toHaveBeenCalledTimes(1))
  expect(mock.getDocument).toHaveBeenCalledWith({ url: '/owned-file.pdf', withCredentials: true })
  await user.click(screen.getByRole('button', { name: '下一页' }))
  await waitFor(() => expect(document.getPage).toHaveBeenCalledWith(2))
  expect(first.cancel).toHaveBeenCalledTimes(1)
  expect(await screen.findByRole('img', { name: '资料.pdf 第 2 页' })).toHaveAttribute('width', '500')
  expect(screen.queryByText(/这一页预览未完成/)).not.toBeInTheDocument()
  view.unmount()
  expect(destroy).toHaveBeenCalledTimes(1)
})

it('shows a load failure instead of an empty success preview', async () => {
  mock.getDocument.mockImplementation(() => ({ promise: Promise.reject(new Error('invalid PDF')), destroy: vi.fn().mockResolvedValue(undefined) }))
  await act(async () => { render(<PdfPreview url="/broken.pdf" name="broken.pdf" />) })
  expect(await screen.findByText(/PDF 预览未完成/)).toBeInTheDocument()
  expect(screen.queryByRole('img')).not.toBeInTheDocument()
})
