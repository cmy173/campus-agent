/// <reference types="vitest/config" />
/**
 * C2 真实模式端到端联调专用配置（不进入默认测试集）。
 *
 * 运行（需后端在线，先按 apps/api README 启动并 seed）：
 *   VITE_API_MODE=real VITE_API_BASE_URL=http://127.0.0.1:8000 \
 *     npx vitest run --config vitest.real.config.ts
 */
import { defineConfig } from 'vite'

export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/lib/api/real-mode.e2e.ts'],
    globals: true,
    css: false,
    testTimeout: 90_000,
    hookTimeout: 90_000,
  },
})
