/**
 * 同步 SHA-256（纯 JS）与规范化 JSON：供模拟层的材料指纹/备注摘要使用。
 *
 * 契约要求 fingerprint 形如 ^sha256:[0-9a-f]{64}$；浏览器 crypto.subtle 是异步的，
 * 而模拟路由是同步构建，故在此内置实现（公式与 B/D 侧一致的规范化输入在调用方组装）。
 */

const K = [
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]

function rotr(x: number, n: number): number {
  return ((x >>> n) | (x << (32 - n))) >>> 0
}

/** 输入字符串按 UTF-8 编码，返回 64 位十六进制摘要（无前缀） */
export function sha256Hex(input: string): string {
  const data = new TextEncoder().encode(input)
  const bitLen = data.length * 8
  const padLen = (data.length + 1 + 8 + 63) & ~63
  const buf = new Uint8Array(padLen)
  buf.set(data)
  buf[data.length] = 0x80
  const hi = Math.floor(bitLen / 0x100000000)
  const lo = bitLen >>> 0
  buf[padLen - 8] = (hi >>> 24) & 0xff
  buf[padLen - 7] = (hi >>> 16) & 0xff
  buf[padLen - 6] = (hi >>> 8) & 0xff
  buf[padLen - 5] = hi & 0xff
  buf[padLen - 4] = (lo >>> 24) & 0xff
  buf[padLen - 3] = (lo >>> 16) & 0xff
  buf[padLen - 2] = (lo >>> 8) & 0xff
  buf[padLen - 1] = lo & 0xff

  let h0 = 0x6a09e667
  let h1 = 0xbb67ae85
  let h2 = 0x3c6ef372
  let h3 = 0xa54ff53a
  let h4 = 0x510e527f
  let h5 = 0x9b05688c
  let h6 = 0x1f83d9ab
  let h7 = 0x5be0cd19

  const w = new Uint32Array(64)
  for (let off = 0; off < buf.length; off += 64) {
    for (let i = 0; i < 16; i += 1) {
      w[i] =
        (buf[off + i * 4] << 24) |
        (buf[off + i * 4 + 1] << 16) |
        (buf[off + i * 4 + 2] << 8) |
        buf[off + i * 4 + 3]
    }
    for (let i = 16; i < 64; i += 1) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3)
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10)
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0
    }
    let a = h0
    let b = h1
    let c = h2
    let d = h3
    let e = h4
    let f = h5
    let g = h6
    let h = h7
    for (let i = 0; i < 64; i += 1) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)
      const ch = (e & f) ^ (~e & g)
      const temp1 = (h + S1 + ch + K[i] + w[i]) >>> 0
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)
      const maj = (a & b) ^ (a & c) ^ (b & c)
      const temp2 = (S0 + maj) >>> 0
      h = g
      g = f
      f = e
      e = (d + temp1) >>> 0
      d = c
      c = b
      b = a
      a = (temp1 + temp2) >>> 0
    }
    h0 = (h0 + a) >>> 0
    h1 = (h1 + b) >>> 0
    h2 = (h2 + c) >>> 0
    h3 = (h3 + d) >>> 0
    h4 = (h4 + e) >>> 0
    h5 = (h5 + f) >>> 0
    h6 = (h6 + g) >>> 0
    h7 = (h7 + h) >>> 0
  }
  return [h0, h1, h2, h3, h4, h5, h6, h7].map((x) => x.toString(16).padStart(8, '0')).join('')
}

/**
 * 规范化 JSON：对象键递归排序、紧凑分隔符（`,`/`:`）、保留 Unicode 原字符。
 * 对应 B/D 侧 json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)。
 */
export function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value)
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, v]) => v !== undefined)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
  return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonicalJson(v)}`).join(',')}}`
}

/** 带前缀摘要：sha256:<64hex>（契约 pattern） */
export function sha256Tagged(input: string): string {
  return `sha256:${sha256Hex(input)}`
}
