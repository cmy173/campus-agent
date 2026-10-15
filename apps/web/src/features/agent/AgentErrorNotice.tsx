import { Alert } from 'antd'
import type { ReactNode } from 'react'
import { isApiError } from '../../lib/api/http'

/** Keep actionable language in the transcript; raw diagnostics are opt-in. */
export function AgentErrorNotice({ error, title, action }: { error: unknown; title?: string; action?: ReactNode }) {
  const record = error && typeof error === 'object' ? error as { message?: string; code?: string; request_id?: string } : null
  const raw = record?.message ?? String(error)
  const code = isApiError(error) ? error.code : record?.code
  const message = raw.replace(/（[A-Z_]+）$/, '')
  const technical = /Traceback|\n\s+at\s|Error:|[A-Z]:\\|site-packages/i.test(message)
  const network = code === 'NETWORK_ERROR' || /NETWORK_ERROR|网络中断|无法连接后端/.test(raw)
  const friendly = network ? '连接暂时中断' : title ?? (technical ? '本次操作未完成' : message)
  const detail = [raw, code, isApiError(error) ? error.requestId : record?.request_id].filter(Boolean).join('\n')
  return <Alert type="warning" title={friendly} action={action} description={<>
    {network ? <div>草稿已保留。发送结果可能尚未确认，请先重新连接；原样重试会复用请求编号。</div> :
      technical ? <div>已获得的资料和草稿会保留。检查连接或设置后可以重试。</div> : title && <div>{message}</div>}
    <details className="agent-error-details"><summary>查看错误详情</summary><pre>{detail}</pre></details>
  </>} />
}
