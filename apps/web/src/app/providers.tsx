import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App as AntApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { useState } from 'react'
import type { ReactNode } from 'react'
import { ApiError } from '../lib/api/http'

export function AppProviders({ children }: { children: ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            refetchOnWindowFocus: false,
            retry: (failureCount, error) => {
              if (error instanceof ApiError) {
                return error.retryable && failureCount < 2
              }
              return failureCount < 1
            },
          },
        },
      }),
  )

  return (
    <QueryClientProvider client={queryClient}>
      <ConfigProvider locale={zhCN} theme={{
        token: {
          colorPrimary: '#315f50',
          colorSuccess: '#47775a',
          colorWarning: '#a87127',
          colorError: '#ad5144',
          colorInfo: '#456b78',
          colorText: '#25332d',
          colorTextSecondary: '#737d75',
          colorBorder: '#dfe1d8',
          colorBgBase: '#f4f3ed',
          colorBgContainer: '#fffefa',
          borderRadius: 8,
          borderRadiusLG: 11,
          controlHeight: 40,
          controlHeightSM: 32,
          fontFamily: '"Segoe UI", "Microsoft YaHei UI", "PingFang SC", "Noto Sans CJK SC", sans-serif',
          fontSize: 14,
          lineHeight: 1.55,
          boxShadow: 'none',
          boxShadowSecondary: '0 12px 34px rgba(37, 51, 45, 0.12)',
        },
      }}>
        <AntApp>{children}</AntApp>
      </ConfigProvider>
    </QueryClientProvider>
  )
}
