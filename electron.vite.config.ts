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
    // 绑定 IPv4 地址，避免 Electron 渲染进程连接超时（默认只绑 IPv6 ::1）
    // dev:web 场景设 VITE_HOST=0.0.0.0 允许局域网访问（electron-vite 不支持 --host CLI 选项）
    server: {
      host: process.env.VITE_HOST || '127.0.0.1',
      port: Number(process.env.VITE_PORT) || 5173,
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
