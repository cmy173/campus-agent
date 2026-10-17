import { Alert, Typography } from 'antd'
import { API_MODE } from '../lib/api/http'
import type { Connection } from '../types/contract'

export function ConnectorSetupNotice({ connection }: { connection: Connection }) {
  if (API_MODE === 'mock' || connection.status !== 'service_unavailable') return null
  return <Alert type="warning" showIcon title="连接器暂时无法连接" description={
    <>
      <Typography.Paragraph>EXE 会自动启动连接器，无需手动执行命令。请保持工作台启动器运行，确认勾选了对应平台；若服务已退出，在启动器中停止后重新启动。</Typography.Paragraph>
      <Typography.Text>服务就绪后，在受控浏览器窗口登录，再点击「检查连接」。若仍无法连接，请查看启动器日志中的具体错误。</Typography.Text>
    </>
  } />
}
