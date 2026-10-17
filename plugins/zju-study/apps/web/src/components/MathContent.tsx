import Markdown from 'react-markdown'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import { normalizeMath } from '../lib/mathFormat'
import 'katex/dist/katex.min.css'
import './MathContent.css'

export function MathContent({ children, compact = false }: { children: string; compact?: boolean }) {
  return <div className={compact ? "math-content math-content-compact" : "math-content"}>
    <Markdown remarkPlugins={[remarkMath]} rehypePlugins={[[rehypeKatex, { trust: false, maxExpand: 1000, maxSize: 10, strict: 'ignore' }]]}
      skipHtml components={{
        img: ({ alt }) => <span>{alt}</span>,
        a: ({ href, children: text }) => <a href={href} target="_blank" rel="noopener noreferrer">{text}</a>,
      }}>{normalizeMath(children)}</Markdown>
  </div>
}
