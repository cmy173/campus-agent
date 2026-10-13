import { describe, expect, it } from 'vitest'
import { reviewGenerationKey } from './reviewRequest'

describe('复习卷生成键', () => {
  const input = { title: '期末复习', provider_config_id: 'provider_a', notes: '第一行\n第二行' }
  const fingerprint = `sha256:${'a'.repeat(64)}`
  it('同一请求可重放，改标题或换服务不会与旧请求冲突', () => {
    const original = reviewGenerationKey(input, fingerprint, 0)
    expect(reviewGenerationKey({ ...input }, fingerprint, 0)).toBe(original)
    expect(reviewGenerationKey({ ...input, title: '期中复习' }, fingerprint, 0)).not.toBe(original)
    expect(reviewGenerationKey({ ...input, provider_config_id: 'provider_b' }, fingerprint, 0)).not.toBe(original)
    expect(reviewGenerationKey(input, fingerprint, 1)).not.toBe(original)
  })
  it('长试卷编号和备注不突破请求头长度限制、不将备注写入请求头', () => {
    const key = reviewGenerationKey({ ...input, notes: '原文备注'.repeat(5000) }, fingerprint, 1, 'pap_' + 'x'.repeat(120))
    expect(key.length).toBeLessThanOrEqual(128)
    expect(key).not.toContain('原文备注')
    expect(key).not.toBe(reviewGenerationKey(input, fingerprint, 1))
  })
})
