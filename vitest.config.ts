import { defineConfig } from 'vitest/config'

// 前端单测均为纯 TS 逻辑（无 DOM 依赖），统一 node 环境。
export default defineConfig({
  test: {
    include: ['src/**/*.{test,spec}.ts'],
    environment: 'node',
    globals: false,
  },
})
