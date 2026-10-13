import { DeleteOutlined, EditOutlined, KeyOutlined, PlusOutlined, ThunderboltOutlined } from '@ant-design/icons'
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Descriptions,
  Divider,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Tag,
  Typography,
} from 'antd'
import { useState } from 'react'
import {
  useClearProviderKey,
  useCreateProvider,
  useDeleteProvider,
  useProviders,
  useTestProvider,
  useUpdateProvider,
} from '../../lib/api/hooks'
import { toApiError, apiErrorText } from '../../lib/api/http'
import { REVIEW_PROTOCOL_OPTIONS } from '../../lib/review'
import { fmtDateTime } from '../../lib/time'
import type { ProviderConfig, ProviderPatchInput, ProviderProtocol, ProviderUpsertInput } from '../../types/review'

interface ProviderFormState {
  name: string
  base_url: string
  model: string
  protocol: ProviderProtocol
  api_key: string
  timeout_seconds: number
}

const emptyForm: ProviderFormState = {
  name: '',
  base_url: '',
  model: '',
  protocol: 'openai_chat_completions',
  api_key: '',
  timeout_seconds: 120,
}

function formFrom(provider: ProviderConfig): ProviderFormState {
  return {
    name: provider.name,
    base_url: provider.base_url,
    model: provider.model,
    protocol: provider.protocol,
    api_key: '',
    timeout_seconds: provider.timeout_seconds,
  }
}

/** 按 PROVIDER_* 错误码给出的核对建议（不假装成功；远端失败也是测试结果本身） */
function providerErrorAdvice(code: string, status: number | null): string {
  switch (code) {
    case 'PROVIDER_KEY_MISSING':
      return '尚未配置密钥：请先保存密钥后再测试连通。'
    case 'PROVIDER_AUTH_FAILED':
      return '服务端拒绝了凭据：请检查密钥是否正确、是否仍在有效期。'
    case 'PROVIDER_TIMEOUT':
      return '请求超时：可稍后重试，或在编辑中调大超时时间。'
    case 'PROVIDER_RATE_LIMITED':
      return '服务端限流：请稍后重试。'
    case 'PROVIDER_INVALID_RESPONSE':
      return '响应缺少可用答案或格式异常；请结合上方具体原因检查，不能仅据此认定地址或协议错误。'
    case 'PROVIDER_OUTPUT_TRUNCATED':
      return '模型已响应，但输出预算不足；这不是网络连接失败。'
    case 'PROVIDER_UNREACHABLE':
      return '无法连接服务地址：请检查网络与地址（本地服务需已启动）。'
    case 'PROVIDER_HTTP_ERROR':
      return status
        ? `服务端返回 HTTP ${status}：请核对 API 地址与模型名。`
        : '服务端返回错误：请核对 API 地址与模型名。'
    case 'PROVIDER_NOT_FOUND':
      return '该设置不存在（可能已被删除）：请刷新页面确认。'
    default:
      return '请按服务商文档核对地址、协议与模型名。'
  }
}

function ProviderTestFeedback({ providerId }: { providerId: string }) {
  const testProvider = useTestProvider()
  const [result, setResult] = useState<
    { kind: 'ok'; text: string } | { kind: 'error'; text: string; advice: string } | null
  >(null)
  const { message } = AntApp.useApp()

  return (
    <Space orientation="vertical" size={4} style={{ width: '100%' }}>
      {result?.kind === 'ok' && <Alert type="success" showIcon title={result.text} />}
      {result?.kind === 'error' && <Alert type="error" showIcon title={result.text} description={result.advice} />}
      <Space>
        <Button
          size="small"
          icon={<ThunderboltOutlined />}
          loading={testProvider.isPending}
          onClick={() =>
            testProvider.mutate(providerId, {
              onSuccess: (data) => {
                if (data.result.ok) {
                  const latency = data.result.latency_ms !== null ? `延迟约 ${data.result.latency_ms}ms` : '延迟未知'
                  setResult({ kind: 'ok', text: `连接成功：${data.provider.model}，${latency}` })
                  message.success('连接测试通过')
                } else {
                  const code = data.result.error?.code ?? 'PROVIDER_UNKNOWN'
                  setResult({
                    kind: 'error',
                    text: `连接未通过（${code}）：${data.result.error?.message ?? '未提供原因'}`,
                    advice: providerErrorAdvice(code, data.result.http_status),
                  })
                }
              },
              onError: (err) => {
                const apiError = toApiError(err)
                setResult({
                  kind: 'error',
                  text: `测试请求失败（${apiError.code}）：${apiError.message}`,
                  advice: providerErrorAdvice(apiError.code, apiError.status),
                })
              },
            })
          }
        >
          测试连接
        </Button>
        {result && (
          <Button size="small" type="text" onClick={() => setResult(null)}>
            收起
          </Button>
        )}
      </Space>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        连接测试只发送一次最小请求验证地址与密钥，不会发送课件内容，也不会启动生成。
      </Typography.Text>
    </Space>
  )
}

function ProviderEditor({
  open,
  editing,
  onClose,
}: {
  open: boolean
  editing: ProviderConfig | null
  onClose: () => void
}) {
  const { message } = AntApp.useApp()
  const createProvider = useCreateProvider()
  const updateProvider = useUpdateProvider()
  const [form, setForm] = useState<ProviderFormState>(editing ? formFrom(editing) : emptyForm)
  const [errorText, setErrorText] = useState<string | null>(null)

  const pending = createProvider.isPending || updateProvider.isPending
  const canSubmit = form.name.trim().length > 0 && form.base_url.trim().length > 0 && form.model.trim().length > 0

  const submit = () => {
    setErrorText(null)
    // 编辑时留空 = 保留已保存密钥；新建时留空 = 暂不设密钥（稍后补填）
    const apiKey = form.api_key.length > 0 ? form.api_key : undefined
    const onDone = () => {
      if (editing) {
        message.success(
          apiKey !== undefined ? '设置已保存' : '设置已保存（未填密钥表示保留原密钥）',
        )
      } else {
        message.success('已创建模型设置')
      }
      onClose()
    }
    const onFail = (err: unknown) => setErrorText(apiErrorText(err))
    if (editing) {
      const patch: ProviderPatchInput = {
        name: form.name.trim(),
        protocol: form.protocol,
        base_url: form.base_url.trim(),
        model: form.model.trim(),
        api_key: apiKey,
        timeout_seconds: form.timeout_seconds,
      }
      updateProvider.mutate({ id: editing.id, patch }, { onSuccess: onDone, onError: onFail })
    } else {
      const input: ProviderUpsertInput = {
        name: form.name.trim(),
        protocol: form.protocol,
        base_url: form.base_url.trim(),
        model: form.model.trim(),
        api_key: apiKey,
        timeout_seconds: form.timeout_seconds,
      }
      createProvider.mutate(input, { onSuccess: onDone, onError: onFail })
    }
  }

  return (
    <Modal
      title={editing ? `编辑模型设置：${editing.name}` : '添加模型设置'}
      open={open}
      onCancel={onClose}
      onOk={submit}
      okText="保存设置"
      okButtonProps={{ disabled: !canSubmit }}
      confirmLoading={pending}
      destroyOnHidden
    >
      <Space orientation="vertical" size={12} style={{ width: '100%' }}>
        {errorText && <Alert type="error" showIcon title="保存失败" description={errorText} />}
        <div>
          <Typography.Text>显示名称</Typography.Text>
          <Input
            value={form.name}
            placeholder="例如：本地推理服务 / 某兼容服务"
            onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
          />
        </div>
        <div>
          <Typography.Text>API 地址（根地址或 /v1 地址）</Typography.Text>
          <Input
            value={form.base_url}
            placeholder="https://api.example.com 或 https://api.example.com/v1"
            onChange={(e) => setForm((f) => ({ ...f, base_url: e.target.value }))}
          />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            本地服务可用 http://127.0.0.1:PORT/v1；不要填到 /chat/completions 等具体端点。地址会按契约规范化
            （去末尾斜杠、补 /v1）。DeepSeek 应填 https://api.deepseek.com/v1，
            platform.deepseek.com 是管理后台；模型名需填写具体标识，不能仅填 deepseek。
          </Typography.Text>
        </div>
        <div>
          <Typography.Text>协议</Typography.Text>
          <Select
            style={{ width: '100%' }}
            value={form.protocol}
            onChange={(value) => setForm((f) => ({ ...f, protocol: value as ProviderProtocol }))}
            options={REVIEW_PROTOCOL_OPTIONS.map((opt) => ({
              value: opt.value,
              label: opt.supported ? opt.label : `${opt.label}（暂不支持）`,
              disabled: !opt.supported,
            }))}
          />
        </div>
        <div>
          <Typography.Text>模型名</Typography.Text>
          <Input
            value={form.model}
            placeholder="填写服务商提供的模型标识（不预设默认值）"
            onChange={(e) => setForm((f) => ({ ...f, model: e.target.value }))}
          />
        </div>
        <div>
          <Typography.Text>API 密钥</Typography.Text>
          <Input.Password
            value={form.api_key}
            placeholder={
              editing?.has_api_key
                ? '已保存密钥；留空表示保留，如需替换请填写新密钥'
                : '粘贴服务商提供的密钥（保存后不回显）'
            }
            autoComplete="new-password"
            onChange={(e) => setForm((f) => ({ ...f, api_key: e.target.value }))}
          />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            密钥以 password 输入提交给本机后端保存；页面不回填、不写入 localStorage、不进入调试日志。
            {editing && !editing.has_api_key ? ' 当前尚未设置密钥。' : ''}
          </Typography.Text>
        </div>
        <div>
          <Typography.Text>超时（秒）</Typography.Text>
          <InputNumber
            min={5}
            max={600}
            value={form.timeout_seconds}
            onChange={(value) => setForm((f) => ({ ...f, timeout_seconds: value ?? 120 }))}
            style={{ display: 'block', width: 140 }}
          />
        </div>
      </Space>
    </Modal>
  )
}

function lastTestSummary(provider: ProviderConfig) {
  if (!provider.last_tested_at || !provider.last_test_result) {
    return <Typography.Text type="secondary">未测试</Typography.Text>
  }
  const result = provider.last_test_result
  return (
    <Space size={4} wrap>
      <Tag color={result.ok ? 'green' : 'red'}>{result.ok ? '通过' : '未通过'}</Tag>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {fmtDateTime(provider.last_tested_at)}
        {!result.ok && result.error ? ` · ${result.error.code}` : ''}
      </Typography.Text>
    </Space>
  )
}

/** 模型服务设置：自定义 API 地址 / 模型 / 密钥（password）；密钥不回显、不进 localStorage */
export function AiProviderCard() {
  const { message } = AntApp.useApp()
  const providersQuery = useProviders()
  const clearKey = useClearProviderKey()
  const deleteProvider = useDeleteProvider()
  const [editorOpen, setEditorOpen] = useState(false)
  const [editing, setEditing] = useState<ProviderConfig | null>(null)

  const providers = providersQuery.data?.items ?? []

  return (
    <Card
      title="模型服务（复习卷生成用）"
      size="small"
      extra={
        <Button
          size="small"
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => {
            setEditing(null)
            setEditorOpen(true)
          }}
        >
          添加模型设置
        </Button>
      }
    >
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        用于复习卷的 AI／混合模式生成。支持本地模型与第三方兼容服务（OpenAI 兼容协议）；
        不预置任何厂商默认地址与模型，请按你的服务信息填写。纯网络题库模式无需此处配置。
      </Typography.Paragraph>
      {providersQuery.isLoading && <Typography.Text type="secondary">加载中…</Typography.Text>}
      {providersQuery.isError && (
        <Alert
          type="error"
          showIcon
          title="模型设置加载失败"
          description={toApiError(providersQuery.error).message}
        />
      )}
      {!providersQuery.isLoading && !providersQuery.isError && providers.length === 0 && (
        <Alert
          type="info"
          showIcon
          title="尚未配置模型服务"
          description="AI／混合模式生成复习卷前，请先添加模型设置（API 地址、模型名与密钥）。"
        />
      )}
      <Space orientation="vertical" size={12} style={{ width: '100%' }}>
        {providers.map((provider) => (
          <Card key={provider.id} size="small" type="inner" title={provider.name}>
            <Descriptions
              column={1}
              size="small"
              items={[
                {
                  key: 'protocol',
                  label: '协议',
                  children:
                    REVIEW_PROTOCOL_OPTIONS.find((o) => o.value === provider.protocol)?.label ??
                    `${provider.protocol}（未识别的协议，未承诺支持）`,
                },
                { key: 'base_url', label: 'API 地址', children: provider.base_url },
                { key: 'model', label: '模型', children: provider.model },
                {
                  key: 'key',
                  label: '密钥',
                  children: provider.has_api_key ? (
                    <Tag color="green">已设置</Tag>
                  ) : (
                    <Tag color="orange">未设置</Tag>
                  ),
                },
                { key: 'timeout', label: '超时', children: `${provider.timeout_seconds} 秒` },
                { key: 'last_test', label: '最近测试', children: lastTestSummary(provider) },
              ]}
            />
            <Divider style={{ margin: '8px 0' }} />
            <Space wrap size={8}>
              <Button
                size="small"
                icon={<EditOutlined />}
                onClick={() => {
                  setEditing(provider)
                  setEditorOpen(true)
                }}
              >
                编辑
              </Button>
              <Popconfirm
                title="清除已保存的密钥？"
                description="清除后测试连接与生成将失败，需要重新填写密钥。"
                okText="清除"
                cancelText="取消"
                onConfirm={() =>
                  clearKey.mutate(provider.id, {
                    onSuccess: () => message.success('密钥已清除'),
                    onError: (err) => message.error(toApiError(err).message),
                  })
                }
              >
                <Button size="small" icon={<KeyOutlined />} disabled={!provider.has_api_key}>
                  清除密钥
                </Button>
              </Popconfirm>
              <Popconfirm
                title="删除该模型设置？"
                description="有进行中的出卷任务时无法删除；已生成的试卷不受影响。"
                okText="删除"
                cancelText="取消"
                onConfirm={() =>
                  deleteProvider.mutate(provider.id, {
                    onSuccess: () => message.success('已删除'),
                    onError: (err) => message.error(toApiError(err).message),
                  })
                }
              >
                <Button size="small" danger icon={<DeleteOutlined />}>
                  删除
                </Button>
              </Popconfirm>
            </Space>
            <Divider style={{ margin: '8px 0' }} />
            <ProviderTestFeedback providerId={provider.id} />
          </Card>
        ))}
      </Space>
      {editorOpen && (
        <ProviderEditor
          key={editing?.id ?? 'new'}
          open={editorOpen}
          editing={editing}
          onClose={() => {
            setEditorOpen(false)
            setEditing(null)
          }}
        />
      )}
    </Card>
  )
}
