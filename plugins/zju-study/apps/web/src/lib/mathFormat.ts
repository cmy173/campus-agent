/** 显示时兼容旧试卷的数学写法；不修改已保存的题目原文。与 API math_format.py 对齐。 */
const words = new Set(('sin cos tan cot sec csc sinh cosh tanh exp log ln lim sum int frac sqrt bar overline ' +
  'vec hat dot ddot alpha beta gamma delta epsilon theta lambda mu nu pi rho sigma tau phi psi omega ' +
  'Gamma Delta Theta Lambda Pi Sigma Phi Psi Omega left right infty partial nabla hbar mathrm mathbf ' +
  'mathcal text operatorname begin end pmatrix bmatrix matrix cases aligned').split(' '))
const variableWords = new Set('iv iz ix iy dx dy dz dt du dv'.split(' '))
const formattingWords = new Set('begin end pmatrix bmatrix matrix cases aligned text operatorname mathrm mathbf mathcal'.split(' '))
const tokens = /```[^\n]*\n[\s\S]*?```|`[^`\n]+`|\$\$[\s\S]*?\$\$|(?<!\\)\$[^\n$]+?(?<!\\)\$|\\{1,2}\[[\s\S]*?\\{1,2}\]|\\{1,2}\([\s\S]*?\\{1,2}\)|https?:\/\/[^\s<>]+/g
const candidates = /[A-Za-z0-9\\{}^_=+*/().,!|<>:;\[\] \tα-ωΑ-Ω∞∑∫∮±×÷≤≥≠≈∂∇ℏ−→←↔⇒⇔∈∉⊂⊆∪∩ℂℝℤℕ°-]+/g
const english = /(?<![A-Za-z\\])[A-Za-z]{2,}(?![A-Za-z])/g
const commands = new RegExp('(?<![A-Za-z\\\\])(' + [...words].filter(word => !formattingWords.has(word)).sort().join('|') + ')(?=[^A-Za-z]|$)', 'g')

function looksMath(value: string) {
  if (/\b(?:const|let|def|return|import|function|console|print|class)\b/.test(value)) return false
  return /\\[A-Za-z]+|[=^∑∫∮∂∇]|_[{0-9]|\b(?:sum|int)_|\b[fgh]\([^)]*\)|\b(?:sin|cos|tan|log|ln)\s+[A-Za-z]|[A-Za-z0-9]\s*\/\s*[A-Za-z0-9]/.test(value)
}
function tex(value: string) {
  return value.replace(new RegExp('(?<!\\\\)\\\\\\\\(?=(?:' + [...words].sort().join('|') + ')(?=[^A-Za-z]|$))', 'g'), '\\')
    .split(/(\\(?:begin|end|text|operatorname|mathrm|mathbf|mathcal)\{[^{}]*\})/g)
    .map((part, index) => index % 2 ? part : part.replace(commands, '\\$1')).join('')
    .replaceAll('∞', '\\infty ').replaceAll('∑', '\\sum ').replaceAll('∫', '\\int ').replaceAll('∮', '\\oint ').trim()
}
function inline(value: string) {
  const leading = value.match(/^\s*/)?.[0] ?? ''
  const trailing = value.match(/\s*$/)?.[0] ?? ''
  let core = value.trim()
  let punctuation = ''
  while (/[.,;:]$/.test(core)) { punctuation = core.slice(-1) + punctuation; core = core.slice(0, -1) }
  const balanced = [['(', ')'], ['{', '}'], ['[', ']']].every(([left, right]) => core.split(left).length === core.split(right).length)
  return core && looksMath(core) && balanced ? leading + '$' + tex(core) + '$' + punctuation + trailing : value
}
function bare(value: string) {
  return value.replace(candidates, text => {
    const parts: string[] = []
    let pos = 0
    for (const word of text.matchAll(english)) {
      if (words.has(word[0]) || variableWords.has(word[0])) continue
      parts.push(inline(text.slice(pos, word.index)), word[0]); pos = word.index + word[0].length
    }
    parts.push(inline(text.slice(pos)))
    return parts.join('')
  })
}
export function normalizeMath(text: string): string {
  const parts: string[] = []
  let pos = 0
  for (const match of text.matchAll(tokens)) {
    parts.push(bare(text.slice(pos, match.index)))
    let token = match[0]
    if (token.startsWith('```')) {
      const content = token.slice(3, -3), split = content.indexOf('\n')
      const language = content.slice(0, split).trim().toLowerCase()
      let body = content.slice(split + 1).trim()
      if (['math', 'latex', 'tex'].includes(language) || (!language && looksMath(body))) {
        body = body.replace(/^\$\$?/, '').replace(/\$\$?$/, '').trim()
        if (/^\\[([]/.test(body) && /\\[)\]]$/.test(body)) body = body.slice(2, -2)
        token = '\n\n$$\n' + tex(body) + '\n$$\n\n'
      }
    } else if (token.startsWith('`') && looksMath(token.slice(1, -1))) token = '$' + tex(token.slice(1, -1)) + '$'
    else if (token.startsWith('$$')) token = '\n\n$$\n' + tex(token.slice(2, -2)) + '\n$$\n\n'
    else if (/^\\{1,2}[([]/.test(token)) {
      const display = token.replace(/^\\+/, '').startsWith('[')
      const body = token.replace(/^\\{1,2}[([]|\\{1,2}[)\]]$/g, '')
      token = display ? '\n\n$$\n' + tex(body) + '\n$$\n\n' : '$' + tex(body) + '$'
    }
    else if (token.startsWith('$')) token = '$' + tex(token.slice(1, -1)) + '$'
    parts.push(token); pos = match.index + match[0].length
  }
  parts.push(bare(text.slice(pos)))
  return parts.join('')
}
