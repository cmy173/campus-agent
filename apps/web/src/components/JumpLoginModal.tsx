import { Alert, Button, Modal, Space, Typography } from 'antd'
import type { RecoveryAction } from '../types/contract'

/**
 * 连接登录引导（A14 v0.2：受控浏览器 + 持久化 profile）：
 * 登录在本机连接器打开的浏览器窗口内的平台官方页面完成——本工作台不收集、代填、
 * 保存任何凭据，也不出现"安装浏览器扩展"引导；跳转前展示权威域名；检查不循环重试。
 */
export function JumpLoginModal({
  action,
  open,
  onClose,
  onCheck,
  checking,
}: {
  action: RecoveryAction
  open: boolean
  onClose: () => void
  onCheck: () => void
  checking: boolean
}) {
  let host = action.entry_url
  try {
    host = new URL(action.entry_url).host
  } catch {
    // 展示原文
  }
  return (
    <Modal
      title="重新登录平台连接"
      open={open}
      onCancel={onClose}
      footer={
        <Space>
          <Button onClick={onClose}>稍后再说</Button>
          <Button href={action.entry_url} target="_blank" rel="noreferrer">
            打开官方登录页（备用入口）
          </Button>
          <Button type="primary" loading={checking} onClick={onCheck}>
            检查连接
          </Button>
        </Space>
      }
    >
      <Space orientation="vertical" size={8} style={{ width: '100%' }}>
        <Alert
          type="info"
          showIcon
          title="优先复用已有登录"
          description={
            <Space orientation="vertical" size={4}>
              <Typography.Text>
                本机连接器会在受控浏览器窗口中打开平台首页（官方入口：{host}）。
                以前已登录且会话有效时，可能直接进入课程页面，这是正常的，不需要再次登录。
                点击「检查连接」会验证现有会话，必要时尝试一次学校单点登录跳转。
                只有仍出现登录表单时，才需要在该窗口内自行登录；凭据只输入到平台官方页面。
              </Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                EXE 启动时会自动打开连接器窗口；若服务不可达，请查看启动器日志。
              </Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                进入课程页面后点击「检查连接」更新状态，无须先经过登录页面。
                浏览器会话刷新最多尝试一次，不会循环要求登录。
              </Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                会话只保存在本机连接器（受控浏览器配置）：请在本机操作，远程访问本网页时无法完成登录。
              </Typography.Text>
              <Typography.Text code copyable style={{ fontSize: 12 }}>
                {action.entry_url}
              </Typography.Text>
              {action.message && (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {action.message}
                </Typography.Text>
              )}
            </Space>
          }
        />
      </Space>
    </Modal>
  )
}
