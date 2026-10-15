import { Drawer, Empty, Space, Tooltip, Typography } from 'antd'
import { LocatorText } from './LocatorText'
import { SourceTag } from './tags'
import { useResourceChunks } from '../lib/api/hooks'
import { toApiError } from '../lib/api/http'

/** 资料 / 回放字幕的文本片段抽屉：展示定位、来源与资源版本，未支持跳转时提供可复制定位文字 */
export function ChunksDrawer({
  resourceId,
  title,
  open,
  onClose,
  url,
}: {
  resourceId: string | undefined
  title: string
  open: boolean
  onClose: () => void
  url?: string | null
}) {
  const chunksQuery = useResourceChunks(open ? resourceId : undefined)

  return (
    <Drawer title={`文本片段 · ${title}`} open={open} onClose={onClose} size={480}>
      {chunksQuery.isLoading && <Typography.Text type="secondary">加载中…</Typography.Text>}
      {chunksQuery.isError && (
        <Typography.Text type="danger">
          片段加载失败（{toApiError(chunksQuery.error).code}）：{toApiError(chunksQuery.error).message}
        </Typography.Text>
      )}
      {chunksQuery.data &&
        (chunksQuery.data.items.length === 0 ? (
          <Empty description="暂无文本片段（可能尚未解析或解析失败）" />
        ) : (
          <Space orientation="vertical" size={16} style={{ width: '100%' }}>
            {chunksQuery.data.items.map((chunk) => (
              <div key={chunk.id}>
                <Space size={8} wrap>
                  <LocatorText locator={chunk.locator} url={url} />
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    片段 {chunk.ordinal + 1} · 资源版本 v{chunk.resource_version}
                  </Typography.Text>
                </Space>
                <Typography.Paragraph style={{ marginTop: 4, marginBottom: 4 }}>
                  {chunk.text}
                </Typography.Paragraph>
                <Space size={8}>
                  <SourceTag source={chunk.provenance.fact_source} />
                  <Tooltip title={`由 ${chunk.provenance.extractor} v${chunk.provenance.extractor_version} 解析`}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      解析时间 {chunk.provenance.observed_at.slice(0, 10)}
                    </Typography.Text>
                  </Tooltip>
                </Space>
              </div>
            ))}
          </Space>
        ))}
    </Drawer>
  )
}
