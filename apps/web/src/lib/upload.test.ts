import { describe, expect, it } from 'vitest'
import {
  checkUploadFile,
  fmtFileSize,
  LARGE_UPLOAD_BYTES,
  MAX_UPLOAD_BYTES,
} from './upload'

describe('上传护栏（PD-1：64MB 上限 / 超过 30MB 属超大文件）', () => {
  it('类型闸门：仅 PDF / PPTX / DOCX（大小写不敏感）', () => {
    expect(checkUploadFile('讲义.pdf', 1024)).toEqual({ ok: true, large: false })
    expect(checkUploadFile('讲义.PPTX', 1024).ok).toBe(true)
    expect(checkUploadFile('讲义.docx', 1024).ok).toBe(true)
    expect(checkUploadFile('note.txt', 1024)).toEqual({ ok: false, reason: 'type' })
    expect(checkUploadFile('archive.zip', 1024)).toEqual({ ok: false, reason: 'type' })
  })

  it('大小闸门：恰 64MB 受理；64MB+1 拒绝；0 字节放行交由后端给出精确报错', () => {
    expect(checkUploadFile('a.pdf', MAX_UPLOAD_BYTES)).toEqual({ ok: true, large: true })
    expect(checkUploadFile('a.pdf', MAX_UPLOAD_BYTES + 1)).toEqual({
      ok: false,
      reason: 'too_large',
    })
    expect(checkUploadFile('a.pdf', 0).ok).toBe(true)
  })

  it('超大标记：不超过 30MB 为普通，超过置 large；42.9MB 真实样例受理且标超大', () => {
    expect(checkUploadFile('a.pdf', LARGE_UPLOAD_BYTES)).toEqual({ ok: true, large: false })
    expect(checkUploadFile('a.pdf', LARGE_UPLOAD_BYTES + 1)).toEqual({ ok: true, large: true })
    expect(checkUploadFile('第6章 图算法 讲义（扫描版）.pdf', 44983910)).toEqual({
      ok: true,
      large: true,
    })
  })

  it('fmtFileSize：MB 保留一位小数、KB 取整、未知与负数显示未知', () => {
    expect(fmtFileSize(44983910)).toBe('42.9 MB')
    expect(fmtFileSize(1024)).toBe('1 KB')
    expect(fmtFileSize(null)).toBe('未知')
    expect(fmtFileSize(-1)).toBe('未知')
    expect(fmtFileSize(Number.NaN)).toBe('未知')
  })
})
