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
    // 默认所有 .css 都被替换成空模块 —— variables.test.ts 用 ?raw 读令牌表与
    // 消费方，空模块会让它"找不到主题块"（或更坏：断言悄悄通过）而不是读到源码。
    // 放行 global.css：::selection 那条消费方在它里面，令牌表与它的读取方必须在
    // 同一份断言里对得上，否则 --wash 又是一个只写不读的令牌。
    css: { include: [/variables\.css/, /global\.css/] },
  },
})
