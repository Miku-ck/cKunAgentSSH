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
    // 默认所有 .css 都被替换成空模块 —— variables.test.ts 用 ?raw 读令牌表，
    // 空模块会让它"找不到主题块"而不是读到源码。只放行这一个文件走 Vite 原样加载。
    css: { include: [/variables\.css/] },
  },
})
