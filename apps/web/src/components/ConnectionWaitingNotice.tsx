import { Alert, Typography } from 'antd'
import type { Connection } from '../types/contract'
import { fmtDateTime } from '../lib/time'

function hostOf(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return url
  }
}

/**
 * auth_required 的等待态说明：明确「等待登录、同步暂停」而非失败；
 * 说明登录在连接器窗口的官方页面完成、不循环重试、无凭据输入（A5b 实测口径）。
 */
export function ConnectionWaitingNotice({
  detail,
  lastCheckPendingAt,
}: {
  detail: Connection
  /** 最近一次手动检查仍未检测到登录的时刻；null 表示本次会话未检查过 */
  lastCheckPendingAt?: string | null
}) {
  const entryHost = detail.recovery_action ? hostOf(detail.recovery_action.entry_url) : null
  return (
    <Alert
      type="info"
      showIcon
      title="等待登录：同步已暂停（不是失败）"
      description={
        <Typography.Text style={{ fontSize: 12 }}>
          {detail.last_error?.message ?? '登录已失效，请重新登录后继续同步。'}{' '}
          {entryHost
            ? `登录将在连接器打开的浏览器窗口内的官方页面（${entryHost}）完成；本工作台不会收集、代填或保存任何凭据。`
            : '登录在连接器打开的浏览器窗口内的官方页面完成；本工作台不会收集、代填或保存任何凭据。'}{' '}
          完成登录后连接器会自动检查并衔接，也可点「检查连接」手动触发；不会自动循环重试。
          {lastCheckPendingAt
            ? ` 最近一次手动检查（${fmtDateTime(lastCheckPendingAt)}）仍未检测到有效登录；不会自动重试，请在连接器窗口完成登录后再点「检查连接」。`
            : ''}
        </Typography.Text>
      }
    />
  )
}
