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
      <ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: '#1677ff' } }}>
        <AntApp>{children}</AntApp>
      </ConfigProvider>
    </QueryClientProvider>
  )
}
