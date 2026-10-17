import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import cases from '../../../../contracts/examples/math-format-cases.json'
import { normalizeMath } from '../lib/mathFormat'
import { MathContent } from './MathContent'

describe('复习卷数学公式', () => {
  for (const item of cases) it(`兼容：${item.input.slice(0, 32)}`, () => {
    expect(normalizeMath(item.input)).toBe(item.expected)
  })
  it('旧卷公式、标准 LaTeX 和数学代码块均真正排版', () => {
    const { container } = render(<MathContent>{'已知 e^z，求 \\(\\frac{a}{b}\\)。\n\n```latex\n\\int_0^1 x^2\\,dx=\\frac13\n```'}</MathContent>)
    expect(container.querySelectorAll('.katex')).toHaveLength(3)
    expect(container.querySelector('.mfrac')).not.toBeNull()
    expect(container.querySelector('.katex-display')).not.toBeNull()
    expect(container.querySelector('.katex-error')).toBeNull()
    expect(container.querySelector('pre')).toBeNull()
  })
  it('保留普通代码，并且不执行 HTML 或可信 LaTeX 链接命令', () => {
    const { container } = render(<MathContent>{'```python\nx = 1\nprint(x)\n```\n<script>alert(1)</script>\n$\\href{javascript:alert(1)}{x}$'}</MathContent>)
    expect(container.querySelector('pre code')).toHaveTextContent('print(x)')
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull()
  })
})
