import { CheckCircleOutlined, CloseCircleOutlined, QuestionCircleOutlined } from '@ant-design/icons'
import { Descriptions, Space, Tag, Typography } from 'antd'
import type { Capabilities, FeatureStatus } from '../types/contract'

const FEATURE_LABELS: Record<string, string> = {
  course_list: '课程列表',
  course_meta: '课程信息',
  attachments_meta: '资料元数据',
  attachment_download: '附件下载',
  assignments: '作业信息',
  recordings: '回放',
  subtitles: '字幕读取',
  playback_seek: '回放定位跳转',
  deletion_signal: '删除信号',
}

const STATUS_META: Record<FeatureStatus, { text: string; color: string }> = {
  supported: { text: '支持', color: 'success' },
  unsupported: { text: '不支持', color: 'default' },
  unknown: { text: '待实测确认', color: 'warning' },
  preview_only: { text: '仅预览', color: 'processing' },
}

/** 连接能力清单：如实展示支持 / 不支持 / 待确认的能力，未支持的能力不提供入口 */
export function ConnectionCapabilities({ capabilities }: { capabilities: Capabilities }) {
  const features = Object.entries(capabilities.features)
  return (
    <Space orientation="vertical" size={8} style={{ width: '100%' }}>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        能力清单（如实展示，未支持的能力不会假装可用；连接器 v{capabilities.connector_version}，核验于{' '}
        {capabilities.checked_at.slice(0, 10)}）
      </Typography.Text>
      <Descriptions size="small" column={1}>
        {features.map(([key, feature]) => {
          const meta = STATUS_META[feature.status]
          return (
            <Descriptions.Item
              key={key}
              label={
                <Space size={6}>
                  {feature.status === 'supported' ? (
                    <CheckCircleOutlined style={{ color: '#52c41a' }} />
                  ) : feature.status === 'unknown' ? (
                    <QuestionCircleOutlined style={{ color: '#faad14' }} />
                  ) : (
                    <CloseCircleOutlined style={{ color: '#bfbfbf' }} />
                  )}
                  <Typography.Text>{FEATURE_LABELS[key] ?? key}</Typography.Text>
                </Space>
              }
            >
              <Space size={8} wrap>
                <Tag color={meta.color}>{meta.text}</Tag>
                {feature.note && (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {feature.note}
                  </Typography.Text>
                )}
              </Space>
            </Descriptions.Item>
          )
        })}
      </Descriptions>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        登录方式：用户会话（在连接器打开的浏览器窗口内登录；SSO {capabilities.auth.sso ?? '未确认'}
        {capabilities.auth.recovery_hint ? `；${capabilities.auth.recovery_hint}` : ''}）
      </Typography.Text>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        限制：单页最多 {capabilities.limits.page_size_max ?? '未知'} 条
        {capabilities.limits.rate_limit_note ? `；${capabilities.limits.rate_limit_note}` : ''}
        {capabilities.limits.campus_network_required === true
          ? '；需要校园网'
          : capabilities.limits.campus_network_required === 'unknown'
            ? '；校园网要求待确认'
            : ''}
      </Typography.Text>
    </Space>
  )
}
