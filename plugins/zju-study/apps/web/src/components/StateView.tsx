import { Alert, Button, Empty, Result, Spin, Typography } from 'antd'
import type { ReactNode } from 'react'
import { toApiError } from '../lib/api/http'

export type ViewState = 'loading' | 'ready' | 'empty' | 'error' | 'partial' | 'stale'

interface StateViewProps {
  state: ViewState
  children?: ReactNode
  error?: unknown
  onRetry?: () => void
  emptyText?: string
  partialMessage?: string
  staleMessage?: string
  extra?: ReactNode
}

/**
 * 统一处理 loading / empty / partial / error / stale 状态，
 * 避免各页面只实现成功路径。
 */
export function StateView({
  state,
  children,
  error,
  onRetry,
  emptyText = '暂无数据',
  partialMessage,
  staleMessage,
  extra,
}: StateViewProps) {
  if (state === 'loading') {
    return (
      <div style={{ textAlign: 'center', padding: '48px 0' }}>
        <Spin />
        <div style={{ marginTop: 12 }}>
          <Typography.Text type="secondary">加载中…</Typography.Text>
        </div>
      </div>
    )
  }

  if (state === 'error') {
    const apiError = toApiError(error)
    return (
      <Result
        status="error"
        title={`加载失败（${apiError.code}）`}
        subTitle={
          <div>
            <div>{apiError.message}</div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              诊断 ID：{apiError.requestId}
              {apiError.retryable ? ' · 可重试' : ' · 请检查输入或稍后再试'}
            </Typography.Text>
          </div>
        }
        extra={
          onRetry ? (
            <Button type="primary" onClick={onRetry}>
              {apiError.retryable ? '重试' : '重新加载'}
            </Button>
          ) : undefined
        }
      />
    )
  }

  if (state === 'empty') {
    return <Empty description={emptyText}>{extra}</Empty>
  }

  if (state === 'partial' || state === 'stale') {
    const message =
      state === 'partial'
        ? (partialMessage ?? '部分数据未能加载，以下内容可能不完整')
        : (staleMessage ?? '数据可能已过期，建议同步后再查看')
    return (
      <div>
        <Alert
          type="warning"
          showIcon
          title={message}
          action={
            onRetry ? (
              <Button size="small" onClick={onRetry}>
                刷新
              </Button>
            ) : undefined
          }
          style={{ marginBottom: 12 }}
        />
        {children}
      </div>
    )
  }

  return <>{children}</>
}
