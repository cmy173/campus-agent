import { CopyOutlined, LinkOutlined } from '@ant-design/icons'
import { Space, Tooltip, Typography } from 'antd'
import type { Locator } from '../types/contract'
import { fmtDurationSeconds } from '../lib/time'

export function locatorLabel(locator: Locator): string {
  if (locator.kind === 'page') return `第 ${locator.page} 页`
  if (locator.kind === 'slide') return `幻灯片 ${locator.slide}`
  if (locator.kind === 'timestamp') {
    const start = fmtDurationSeconds(locator.start_seconds)
    const end = locator.end_seconds !== undefined ? `–${fmtDurationSeconds(locator.end_seconds)}` : ''
    return `${start}${end}`
  }
  const end = locator.end_offset !== undefined ? `–${locator.end_offset}` : ''
  return `文本位置 ${locator.start_offset}${end}`
}

/**
 * 来源定位展示：
 * - 支持跳转时给出原平台链接；
 * - 未支持跳转时显示可复制的定位文字（页码 / 幻灯片号 / 时间区间 / 字符位置）。
 */
export function LocatorText({
  locator,
  url,
}: {
  locator: Locator
  url?: string | null
}) {
  const label = locatorLabel(locator)
  return (
    <Space size={4}>
      {url ? (
        <Typography.Link href={url} target="_blank" rel="noreferrer">
          <LinkOutlined /> {label}
        </Typography.Link>
      ) : (
        <Tooltip title="暂不支持直接跳转，可复制定位文字到原平台查找">
          <Typography.Text copyable={{ text: label, icon: <CopyOutlined /> }}>{label}</Typography.Text>
        </Tooltip>
      )}
    </Space>
  )
}
