import { Alert, App as AntApp, Button, Card, Space, Typography } from 'antd'
import { useState } from 'react'
import { JobProgressInline } from '../../components/JobProgressInline'
import { useAvailability, useExport } from '../../lib/api/hooks'
import { apiErrorText, toApiError } from '../../lib/api/http'
import { AvailabilityForm } from './AvailabilityForm'

function DataExportCard() {
  const { message } = AntApp.useApp()
  const exportData = useExport()
  const [jobId, setJobId] = useState<string | null>(null)
  return (
    <Card title="数据管理" size="small">
      <Space orientation="vertical" size={8} style={{ width: '100%' }}>
        <Typography.Text type="secondary">JSON 与 ICS 日历导出均已接通，生成后可在任务结果中下载。</Typography.Text>
        <Space wrap size={8}>
          <Typography.Text>导出学习数据（JSON）</Typography.Text>
          <Button
            size="small"
            loading={exportData.isPending}
            onClick={() =>
              exportData.mutate(undefined, {
                onSuccess: (data) => {
                  setJobId(data.job.id)
                  message.info('已提交导出任务')
                },
                onError: (err) => message.error(toApiError(err).message),
              })
            }
          >
            开始导出
          </Button>
        </Space>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          导出范围：课程、资料元数据、任务与学习反馈；不含平台登录凭据。生成完成后在任务结果中提供受控下载。
        </Typography.Text>
        <Space wrap size={8}>
          <Typography.Text>导出计划到日历（ICS）</Typography.Text>
          <Button
            size="small"
            loading={exportData.isPending}
            onClick={() =>
              exportData.mutate({ format: 'ics' }, {
                onSuccess: (data) => {
                  setJobId(data.job.id)
                  message.info('已提交导出任务')
                },
                onError: (err) => message.error(toApiError(err).message),
              })
            }
          >
            导出 ICS
          </Button>
        </Space>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          ICS 文件包含当前生效计划的时间块与全部任务截止，可导入系统日历；为整体导出，不支持按类别筛选。
        </Typography.Text>
        {jobId && <JobProgressInline jobId={jobId} />}
      </Space>
    </Card>
  )
}

export function PersonalSettings() {
  const availability = useAvailability()
  return <Space orientation="vertical" size={16} style={{ width: '100%' }}>
    {availability.isLoading && <Typography.Text>正在读取可用时间…</Typography.Text>}
    {availability.isError && <Alert type="error" title="可用时间读取失败" description={apiErrorText(availability.error)}
      action={<Button onClick={() => void availability.refetch()}>重试</Button>} />}
    {availability.data && <AvailabilityForm key={availability.data.updated_at} initial={availability.data} />}
    <DataExportCard />
    <Typography.Text type="secondary">展示时区：Asia/Shanghai。平台登录在助手浏览器或连接器窗口完成。</Typography.Text>
  </Space>
}
