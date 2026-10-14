import { Alert, Button, InputNumber, Space, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import type { PDFDocumentLoadingTask, PDFDocumentProxy, RenderTask } from 'pdfjs-dist'

/** Lazy PDF rendering keeps the normal conversation free of viewer startup cost. */
export function PdfPreview({ url, name }: { url: string; name: string }) {
  const [document, setDocument] = useState<PDFDocumentProxy | null>(null)
  const [page, setPage] = useState(1)
  const [width, setWidth] = useState(0)
  const [zoom, setZoom] = useState(1)
  const [error, setError] = useState('')
  const [rendering, setRendering] = useState(false)
  const container = useRef<HTMLDivElement>(null)
  const canvas = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    let disposed = false
    let loading: PDFDocumentLoadingTask | undefined
    setDocument(null)
    setError('')
    setPage(1)
    setZoom(1)
    void (async () => {
      try {
        const [pdf, worker] = await Promise.all([import('pdfjs-dist'), import('pdfjs-dist/build/pdf.worker.min.mjs?url')])
        if (disposed) return
        pdf.GlobalWorkerOptions.workerSrc = worker.default
        loading = pdf.getDocument({ url, withCredentials: true })
        const value = await loading.promise
        if (!disposed) setDocument(value)
      } catch {
        if (!disposed) setError('PDF 预览未完成，请重新打开或下载原文件查看。')
      }
    })()
    return () => { disposed = true; void loading?.destroy().catch(() => {}) }
  }, [url])
  useEffect(() => {
    const observer = new ResizeObserver(entries => setWidth(Math.floor(entries[0].contentRect.width)))
    if (container.current) observer.observe(container.current)
    return () => observer.disconnect()
  }, [])
  useEffect(() => {
    if (!document || !width || !canvas.current) return
    let disposed = false
    let task: RenderTask | undefined
    const target = canvas.current
    setRendering(true)
    void (async () => {
      try {
        const source = await document.getPage(page)
        if (disposed) return
        const natural = source.getViewport({ scale: 1 })
        const viewportWidth = target.parentElement?.clientWidth
        const contentWidth = viewportWidth && viewportWidth > 2 ? viewportWidth - 2 : width
        const scale = Math.min(contentWidth / natural.width, 2) * zoom
        const viewport = source.getViewport({ scale })
        const density = Math.min(window.devicePixelRatio || 1, 2)
        target.width = Math.ceil(viewport.width * density)
        target.height = Math.ceil(viewport.height * density)
        target.style.width = viewport.width + 'px'
        target.style.height = viewport.height + 'px'
        task = source.render({ canvas: target, viewport, transform: density === 1 ? undefined : [density, 0, 0, density, 0, 0] })
        await task.promise
        if (!disposed) { setRendering(false); setError('') }
      } catch {
        if (!disposed) { setRendering(false); setError('这一页预览未完成，可切换页面或下载原文件查看。') }
      }
    })()
    return () => { disposed = true; task?.cancel() }
  }, [document, page, width, zoom])
  return <div className="agent-pdf-preview" ref={container}>
    {error && <Alert type="warning" title={error} />}
    {!document && !error && <Typography.Text role="status">正在加载 PDF…</Typography.Text>}
    {document && <>
      <Space wrap className="agent-pdf-controls">
        <Button size="small" disabled={page <= 1} onClick={() => setPage(value => value - 1)}>上一页</Button>
        <InputNumber aria-label="PDF 页码" size="small" min={1} max={document.numPages} precision={0} step={1} value={page}
          onChange={value => { if (value != null) setPage(Math.max(1, Math.min(document.numPages, value))) }} />
        <Typography.Text>共 {document.numPages} 页</Typography.Text>
        <Button size="small" disabled={page >= document.numPages} onClick={() => setPage(value => value + 1)}>下一页</Button>
        <Button size="small" disabled={zoom <= 1} onClick={() => setZoom(value => Math.max(1, value - .5))}>缩小</Button>
        <Button size="small" disabled={zoom >= 3} onClick={() => setZoom(value => Math.min(3, value + .5))}>放大</Button>
      </Space>
      {rendering && <Typography.Text role="status">正在显示第 {page} 页…</Typography.Text>}
      <div className="agent-pdf-pages"><canvas ref={canvas} role="img" aria-label={`${name} 第 ${page} 页`} /></div>
    </>}
  </div>
}
