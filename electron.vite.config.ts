import { resolve } from 'path'
import { defineConfig, externalizeDepsPlugin } from 'electron-vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  main: {
    plugins: [externalizeDepsPlugin()],
    build: {
      outDir: 'out/main',
      rollupOptions: {
        input: {
          index: resolve(__dirname, 'electron/main.ts')
        }
      }
    }
  },
  preload: {
    plugins: [externalizeDepsPlugin()],
    build: {
      outDir: 'out/preload',
      rollupOptions: {
        input: {
          index: resolve(__dirname, 'electron/preload.ts')
        }
      }
    }
  },
  renderer: {
    root: '.',
    // 启动性能优化：终端（@xterm/*）是首屏真实依赖，显式登记进预构建，避免 Vite
    // 在首个请求时才"发现"它们 → 触发一次 optimize + 页面 reload 的启动抖动。
    // monaco-editor 刻意不在内：它已懒加载到 ./monaco-runtime（见 src/monaco-setup.ts），
    // 只在首次打开文件编辑器时才 import，预构建它只会白白拖慢冷启动。
    optimizeDeps: {
      include: ['@xterm/xterm', '@xterm/addon-fit', '@xterm/addon-web-links']
    },
    // 绑定 IPv4 地址，避免 Electron 渲染进程连接超时（默认只绑 IPv6 ::1）
    // dev:web 场景设 VITE_HOST=0.0.0.0 允许局域网访问（electron-vite 不支持 --host CLI 选项）
    server: {
      host: process.env.VITE_HOST || '127.0.0.1',
      port: Number(process.env.VITE_PORT) || 5173,
      // 启动性能优化：Electron 拉起 dev server 后立即预热入口与重依赖，
      // transform 与窗口加载并行 —— 否则首屏每个未缓存模块都要现场编译，
      // 白屏时间被 monaco/xterm 等大依赖的按需 transform 串起来。
      warmup: {
        clientFiles: [
          './src/main.ts',
          './src/monaco-setup.ts',
          './src/App.vue',
          './src/components/ssh/RemoteTerminal.vue',
          './src/components/ssh/LocalTerminal.vue',
        ],
      },
    },
    build: {
      outDir: 'out/renderer',
      rollupOptions: {
        input: {
          index: resolve(__dirname, 'index.html')
        }
      }
    },
    resolve: {
      alias: {
        '@': resolve(__dirname, 'src')
      }
    },
    plugins: [vue()]
  }
})
