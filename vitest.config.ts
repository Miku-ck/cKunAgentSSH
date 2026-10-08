import { resolve } from 'node:path'
import { defineConfig } from 'vitest/config'

// 前端单测均为纯 TS 逻辑（无 DOM 依赖），统一 node 环境。
export default defineConfig({
  // 与 electron.vite.config.ts 的 renderer alias 保持一致，测试里才能按 '@/…' 导入
  resolve: {
    alias: { '@': resolve(__dirname, 'src') },
  },
  test: {
    // electron/** 也在范围内：数据面会话（gateway-transport）依赖 node:stream/tls，
    // 搬不进 shared/（那边同时被 tsconfig.web 收，而 web 侧没有 node 类型）。
    // 放在代码旁边、由 tsconfig.node 检查，是它唯一能被测到的位置。
    include: ['src/**/*.{test,spec}.ts', 'electron/**/*.{test,spec}.ts'],
    environment: 'node',
    globals: false,
    // 默认所有 .css 都被替换成空模块 —— variables.test.ts 用 ?raw 读令牌表与
    // 消费方，空模块会让它"找不到主题块"（或更坏：断言悄悄通过）而不是读到源码。
    // 放行 global.css：::selection 那条消费方在它里面，令牌表与它的读取方必须在
    // 同一份断言里对得上，否则 --wash 又是一个只写不读的令牌。
    // 其余三份同理，且它们是被"全仓扫"类守卫扫的对象：漏一份 = 那条守卫对那个文件
    // 恒真（approval-card.css 曾因此带着 14 处 var() 回落值躲过裸引用守卫，
    // 而回落值 #38bdf8/#ef4444 早已和 :root 的 #9cdcfe/#ff6b6b 漂移）。
    css: {
      include: [
        /variables\.css/,
        /global\.css/,
        /approval-card\.css/,
        /settings-groups\.css/,
        /settings-panel-base\.css/,
      ],
    },
  },
})
