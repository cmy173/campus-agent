import { App as AntApp, Button, Form, Input, Modal, Select, Space, Typography } from 'antd'
import type { ButtonProps } from 'antd'
import { useState } from 'react'
import type { ReactNode } from 'react'
import { useAddSourceRef } from '../../lib/api/hooks'
import { toApiError } from '../../lib/api/http'
import { nowIso } from '../../lib/time'
import { PLATFORM_LABELS } from '../../lib/platforms'
import type { SourceRef } from '../../types/contract'

const PLATFORM_OPTIONS = [
  { value: 'xzzd', label: '学在浙大（xzzd）' },
  { value: 'zykt', label: '智云课堂（zykt）' },
  { value: 'manual', label: '手动整理（manual）' },
]

/** 手动挂接平台来源（v0.1 课程映射仅人工确认）：把平台侧课程挂接到本课程 */
export function AttachSourceRefButton({
  courseId,
  courseTitle,
  size,
  type,
  children,
}: {
  courseId: string
  courseTitle: string
  size?: ButtonProps['size']
  type?: ButtonProps['type']
  children?: ReactNode
}) {
  const { message } = AntApp.useApp()
  const addSourceRef = useAddSourceRef()
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm<{ platform: string; external_id: string; canonical_url?: string }>()

  const handleOk = async () => {
    let values
    try { values = await form.validateFields() } catch { return }
    const sourceRef: SourceRef = {
      platform: values.platform,
      external_id: values.external_id.trim(),
      canonical_url: values.canonical_url?.trim() ? values.canonical_url.trim() : null,
      observed_at: nowIso(),
    }
    addSourceRef.mutate(
      { courseId, sourceRef },
      {
        onSuccess: () => {
          message.success(`已挂接「${PLATFORM_LABELS[sourceRef.platform] ?? sourceRef.platform}」来源`)
          setOpen(false)
          form.resetFields()
        },
        onError: (err) => {
          const apiError = toApiError(err)
          if (apiError.code === 'CONFLICT') {
            const owner = (apiError.details as { course_title?: string } | undefined)?.course_title
            message.error(
              owner
                ? `该平台来源已挂接到「${owner}」，请打开该课程继续使用，或选择其他来源`
                : apiError.message,
            )
          } else {
            message.error(apiError.message)
          }
        },
      },
    )
  }

  return (
    <>
      <Button size={size} type={type} onClick={() => setOpen(true)}>
        {children ?? '挂接平台来源'}
      </Button>
      <Modal
        title={`挂接平台来源 · ${courseTitle}`}
        open={open}
        onCancel={() => {
          setOpen(false)
          form.resetFields()
        }}
        onOk={handleOk}
        confirmLoading={addSourceRef.isPending}
        okText="挂接"
      >
        <Space orientation="vertical" size={8} style={{ width: '100%' }}>
          <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
            填写平台侧的课程标识（如平台课程页地址中的课程 id）。挂接后，该平台课程的资料、作业与回放会归并到本课程；
            若该来源已属于其他课程，服务端会拒绝并给出明确提示。
          </Typography.Paragraph>
          <Form form={form} layout="vertical">
            <Form.Item name="platform" label="平台" rules={[{ required: true, message: '请选择平台' }]}>
              <Select aria-label="选择平台" options={PLATFORM_OPTIONS} placeholder="选择平台" />
            </Form.Item>
            <Form.Item
              name="external_id"
              label="平台课程标识"
              rules={[{ required: true, whitespace: true, message: '请填写平台课程标识' }]}
            >
              <Input placeholder="例如 12345；智云课堂取 course_id 参数" />
            </Form.Item>
            <Form.Item name="canonical_url" label="平台链接（可选）">
              <Input placeholder="https://…（用于从任务跳转到平台对象）" />
            </Form.Item>
          </Form>
        </Space>
      </Modal>
    </>
  )
}
