import { defineConfig } from 'vitest/config'

// 前端单测配置：默认 node 环境；需要 DOM 的测试（markdown）
// 在文件内用 `// @vitest-environment jsdom` 声明。
export default defineConfig({
  test: {
    include: ['src/**/*.{test,spec}.ts'],
    environment: 'node',
    globals: false,
  },
})
