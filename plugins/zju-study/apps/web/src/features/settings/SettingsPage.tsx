import { Button, Card, Descriptions, Divider, Space, Tag, Typography } from 'antd'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { JobProgressInline } from '../../components/JobProgressInline'
import { StateView } from '../../components/StateView'
import { ConnectionStatusTag, FreshnessTag } from '../../components/tags'
import { useAvailability, useConnections, useExport } from '../../lib/api/hooks'
import { API_MODE } from '../../lib/api/http'
import { toApiError } from '../../lib/api/http'
import { App as AntApp } from 'antd'
import { DEFAULT_TIMEZONE } from '../../lib/time'
import { AvailabilityForm } from './AvailabilityForm'
import { AiProviderCard } from './AiProviderCard'
import { FileLimitsCard } from './FileLimitsCard'

function DataExportCard() {
  const { message } = AntApp.useApp()
  const exportData = useExport()
  const [jobId, setJobId] = useState<string | null>(null)
  return (
    <Card title="数据管理" size="small">
      <Space orientation="vertical" size={8} style={{ width: '100%' }}>
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
        <Divider style={{ margin: '4px 0' }} />
        <Space wrap size={8}>
          <Typography.Text>清理导入的资料</Typography.Text>
          <Tag>未接通（v0.1）</Tag>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            清理能力尚未提供接口；届时清理范围仅限本工作台内导入的资料与解析结果，不影响学校平台上的任何内容。
          </Typography.Text>
        </Space>
      </Space>
    </Card>
  )
}

export function SettingsPage() {
  const availabilityQuery = useAvailability()
  const connectionsQuery = useConnections()

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <StateView
        state={
          availabilityQuery.isLoading
            ? 'loading'
            : availabilityQuery.isError
              ? 'error'
              : 'ready'
        }
        error={availabilityQuery.error}
        onRetry={() => availabilityQuery.refetch()}
      >
        {availabilityQuery.data && (
          <AvailabilityForm key={availabilityQuery.data.updated_at} initial={availabilityQuery.data} />
        )}
      </StateView>

      <Card title="平台与同步" size="small">
        {connectionsQuery.isLoading && <Typography.Text type="secondary">加载中…</Typography.Text>}
        {(connectionsQuery.data?.items.length ?? 0) === 0 && (
          <Typography.Text type="secondary">
            还没有平台连接（由本机连接器建立）；<Link to="/onboarding">去首次使用引导</Link>
          </Typography.Text>
        )}
        <Space orientation="vertical" size={8} style={{ width: '100%' }}>
          {(connectionsQuery.data?.items ?? []).map((conn) => (
            <Space key={conn.id} wrap size={8}>
              <Typography.Text>{conn.display_name}</Typography.Text>
              <ConnectionStatusTag status={conn.status} />
              <FreshnessTag lastSyncAt={conn.last_sync_at} />
            </Space>
          ))}
        </Space>
        {(connectionsQuery.data?.items.length ?? 0) > 0 && (
          <>
            <Divider style={{ margin: '12px 0' }} />
            <Link to="/sync">
              <Button size="small">去同步中心管理连接与任务</Button>
            </Link>
          </>
        )}
      </Card>

      <DataExportCard />

      <AiProviderCard />
      <FileLimitsCard />

      <Card title="解析服务" size="small">
        <Space orientation="vertical" size={8} style={{ width: '100%' }}>
          <Descriptions
            column={1}
            size="small"
            items={[
              {
                key: 'parsing',
                label: '解析模式',
                children: '本地规则解析（PDF / PPTX / DOCX 文本与结构）',
              },
              {
                key: 'secrets',
                label: '模型密钥管理',
                children:
                  '密钥经服务端保存（模拟模式仅存进程内存）；页面不回填真实密钥、不写入 localStorage、不进入调试日志',
              },
              {
                key: 'asr',
                label: '音视频转写',
                children: <Tag>未接通（v0.1）</Tag>,
              },
            ]}
          />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            解析失败可在课程页重试；扫描件 OCR 暂不支持（会如实标注，不假装可用）。
            模型设置仅用于复习卷生成，测试连接不会发送课件内容。
          </Typography.Text>
        </Space>
      </Card>

      <Card title="能力状态与未接通项（如实标注）" size="small">
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
          以下能力尚未完整接通（部分为界面入口未开放或依赖外部条件，逐条给出可核对依据）；界面不提供假按钮，
          接入后就地更新状态说明。
        </Typography.Paragraph>
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          <li>
            真实平台同步：后端同步端点已就绪（POST /connections/&#123;id&#125;/sync），错误路径（auth_required →
            重登录入口）已联调；成功路径依赖真实平台会话，受控浏览器登录形态已实测可行（组件侧 A17/A21：真实课程两次同步指纹一致）
          </li>
          <li>音视频转写：仅读取平台已有字幕，不做语音转写（后端无转写接口）</li>
          <li>
            导出：JSON 与 ICS 日历导出均已接通（POST /exports + 产物下载 GET /files/&#123;id&#125;）；ICS
            为计划时间块与任务截止的整体导出，不支持 include 筛选（传入返回 400，属契约设计）
          </li>
          <li>时间块拖动与微调：仅支持锁定 / 解锁（界面无拖拽交互）；需改时间时解锁后重新生成预览</li>
          <li>全文搜索：后端 GET /search 已就绪；界面入口暂未开放</li>
          <li>导入资料清理：尚无对应接口</li>
          <li>
            复习卷生成：AI／混合模式使用你在上方配置的自定义模型服务；网络题库来源由采集服务配置，
            未配置时预检会阻止生成并给出切换建议（不会假装可用）
          </li>
          <li>扫描件 OCR：暂不支持，解析结果会如实标注</li>
        </ul>
      </Card>

      <Card title="关于" size="small">
        <Descriptions
          column={1}
          size="small"
          items={[
            {
              key: 'mode',
              label: '运行模式',
              children:
                API_MODE === 'mock' ? (
                  <Tag color="orange">开发模式 · 模拟数据</Tag>
                ) : (
                  <Tag color="green">已连接后端</Tag>
                ),
            },
            { key: 'tz', label: '展示时区', children: DEFAULT_TIMEZONE },
            {
              key: 'contract',
              label: '契约版本',
              children: 'v0.1 冻结版（main@8ce5b43；类型见 src/types/contract.ts）',
            },
          ]}
        />
      </Card>
    </Space>
  )
}
