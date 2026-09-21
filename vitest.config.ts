import { resolve } from 'node:path'
import { defineConfig } from 'vitest/config'

// 前端单测均为纯 TS 逻辑（无 DOM 依赖），统一 node 环境。
export default defineConfig({
  // 与 electron.vite.config.ts 的 renderer alias 保持一致，测试里才能按 '@/…' 导入
  resolve: {
    alias: { '@': resolve(__dirname, 'src') },
  },
  test: {
    include: ['src/**/*.{test,spec}.ts'],
    environment: 'node',
    globals: false,
  },
})
