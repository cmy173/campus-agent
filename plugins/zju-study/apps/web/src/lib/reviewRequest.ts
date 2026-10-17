import { canonicalJson, sha256Hex } from './api/mock/sha256'

/** 材料指纹不包含标题和模型设置，幂等键另按完整请求计算。 */
export function reviewGenerationKey(input: unknown, fingerprint: string, nonce: number, paperId?: string): string {
  const digest = sha256Hex(canonicalJson({ input, fingerprint, nonce, paperId: paperId ?? null }))
  return `${paperId ? 'review-rev' : 'review-gen'}:${digest}`
}
