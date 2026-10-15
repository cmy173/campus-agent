import { Button, Space, Tag, Typography } from 'antd'
import { fileContentUrl } from './api'
import type { AgentEvent, ConversationFile } from './api'

/** Published-file events have one renderer regardless of the producing tool. */
export function FileActivities({ events, conversationId, onOpen }: { events: AgentEvent[]; conversationId: string; onOpen?: (file: ConversationFile) => void }) {
  const files = new Map<string, ConversationFile>()
  for (const event of events) {
    if (event.kind !== 'file.published') continue
    const file = event.data.file as ConversationFile | undefined
    if (file?.conversation_id === conversationId && typeof file.id === 'string' && typeof file.name === 'string' && file.kind === 'output') files.set(file.id, file)
  }
  if (!files.size) return null
  return <section className="agent-file-results" aria-label="本轮生成成果">
    {[...files.values()].map(file => <Space key={file.id} wrap>
      <Tag>生成成果</Tag><Typography.Text>{file.name}</Typography.Text>
      {onOpen && <Button size="small" onClick={() => onOpen(file)}>打开成果 · {file.name}</Button>}
      <Button size="small" href={fileContentUrl(conversationId, file.id)}>下载 {file.name}</Button>
    </Space>)}
  </section>
}
