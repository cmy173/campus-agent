import { Alert, Button, InputNumber, Space, Spin, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { getDocument, GlobalWorkerOptions } from 'pdfjs-dist'
import type { PDFDocumentProxy } from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'

GlobalWorkerOptions.workerSrc = workerUrl

export default function PdfReader({ url }: { url: string }) {
  const [document, setDocument] = useState<PDFDocumentProxy | null>(null)
  const [page, setPage] = useState(1)
  const [error, setError] = useState<string | null>(null)
  const [rendering, setRendering] = useState(false)
  const canvas = useRef<HTMLCanvasElement>(null)
  const container = useRef<HTMLDivElement>(null)
  useEffect(() => {
    let active = true
    const loading = getDocument({ url, withCredentials: true, cMapUrl: '/pdfjs/cmaps/', cMapPacked: true,
      standardFontDataUrl: '/pdfjs/standard_fonts/', wasmUrl: '/pdfjs/wasm/' })
    void loading.promise.then(value => { if (active) setDocument(value) })
      .catch(err => { if (active) setError(`PDF 读取失败：${String(err)}`) })
    return () => { active = false; void loading.destroy() }
  }, [url])
  useEffect(() => {
    if (!document || !canvas.current) return
    let active = true
    let renderTask: ReturnType<Awaited<ReturnType<PDFDocumentProxy['getPage']>>['render']> | undefined
    void document.getPage(page).then(async source => {
      if (!active || !canvas.current) return
      setRendering(true)
      const width = Math.min(container.current?.clientWidth || 900, 1400)
      const base = source.getViewport({ scale: 1 })
      const ratio = Math.min(window.devicePixelRatio || 1, 2)
      const viewport = source.getViewport({ scale: width / base.width * ratio })
      const element = canvas.current
      element.width = viewport.width
      element.height = viewport.height
      element.style.width = `${width}px`
      element.style.height = `${viewport.height / ratio}px`
      const context = element.getContext('2d')
      if (!context) throw new Error('浏览器无法创建 PDF 画布')
      renderTask = source.render({ canvas: element, canvasContext: context, viewport })
      await renderTask.promise
      if (active) setRendering(false)
    }).catch(err => { if (active && err?.name !== 'RenderingCancelledException') { setError(`页面绘制失败：${String(err)}`); setRendering(false) } })
    return () => { active = false; renderTask?.cancel() }
  }, [document, page])
  if (error) return <Alert type="warning" title={error} description="原文件仍保存在本地，可下载后查看。" />
  return <Space orientation="vertical" style={{ width: '100%' }}>
    <Space wrap>
      <Button disabled={!document || page <= 1} onClick={() => setPage(value => value - 1)}>上一页</Button>
      <Typography.Text>第</Typography.Text>
      <InputNumber aria-label="PDF 页码" min={1} max={document?.numPages ?? 1} value={page} disabled={!document}
        onChange={value => { if (value && document && value <= document.numPages) setPage(value) }} />
      <Typography.Text>页 / 共 {document?.numPages ?? '…'} 页</Typography.Text>
      <Button disabled={!document || page >= document.numPages} onClick={() => setPage(value => value + 1)}>下一页</Button>
    </Space>
    {(!document || rendering) && <Spin size="small" />}
    <div ref={container} style={{ width: '100%', minHeight: 180, overflow: 'auto', background: '#f0f0f0' }}>
      <canvas ref={canvas} aria-label={`PDF 第 ${page} 页`} style={{ display: 'block', maxWidth: '100%' }} />
    </div>
  </Space>
}
