/// <reference types="vitest/config" />
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  return {
    plugins: [react()],
    server: {
      host: '127.0.0.1',
      port: 5173,
      proxy: {
        // 仅当 VITE_API_MODE=real 且未设置 VITE_API_BASE_URL 时走此代理
        '/api': {
          target: env.VITE_PROXY_TARGET || 'http://127.0.0.1:8000',
          changeOrigin: true,
        },
      },
    },
    test: {
      environment: 'jsdom',
      setupFiles: './src/test/setup.ts',
      globals: true,
      css: false,
      env: { VITE_API_MODE: 'mock' },
    },
    build: {
      rolldownOptions: {
        output: {
          codeSplitting: {
            groups: [
              {
                name: 'react-vendor',
                test: /node_modules[\\/](react|react-dom|react-router|react-router-dom|scheduler|@tanstack)[\\/]/,
                priority: 20,
              },
              {
                name: 'icons-vendor',
                test: /node_modules[\\/]@ant-design[\\/]icons[\\/]/,
                priority: 15,
              },
              {
                name: 'cssinjs-vendor',
                test: /node_modules[\\/](@ant-design[\\/]cssinjs|dayjs)[\\/]/,
                priority: 14,
              },
              {
                name: 'rc-vendor',
                test: /node_modules[\\/](@rc-component|rc-[^\\/]+)[\\/]/,
                priority: 12,
              },
              {
                name: 'antd-vendor',
                test: /node_modules[\\/](antd|@ant-design|@rc-component|rc-[^\\/]+)[\\/]/,
                priority: 10,
                maxSize: 400000,
              },
            ],
          },
        },
      },
    },
  }
})
