import { resolve } from 'path'
import { defineConfig, externalizeDepsPlugin } from 'electron-vite'
import vue from '@vitejs/plugin-vue'

/** 渲染层模块图真正落在哪些根级目录：src 越出自身的 import 只到 shared/（已核实） */
const RENDERER_WATCH_ROOT_DIRS = ['src', 'shared']
const PROJECT_ROOT = resolve(__dirname).replace(/\\/g, '/')

/**
 * dev server 文件监听的忽略判定：根级目录默认不进监听，只放行源码目录。
 *
 * 为什么需要它：Vite 的默认忽略表只有 `.git`/`node_modules`/`test-results`/cacheDir
 * （已核实 node_modules/vite/dist/node/chunks 里的 resolveChokidarOptions），而本仓库
 * 根下躺着 runtime/（8.9k 文件）与 .mimosa/hook-state（16.9k 文件）这些 git 已忽略的
 * 工具态目录。chokidar 开局仍要把这 2.7 万个文件走一遍，首个请求排在它后面 —— 实测
 * 首屏因此慢 16~22 秒（同一份热缓存下关掉监听，首个 CSS 变换 21.9s → 39ms）。
 *
 * 为什么是谓词而不是往数组里枚举几个目录名：新工具再往根上扔一个缓存目录，也不会
 * 把启动时间重新吃回去。代价是白名单语义 —— 渲染层新增跨目录 import 时必须同时登记。
 * 本函数是**追加**在 Vite 默认忽略表之后（`ignored` 传函数时 Vite 把它 arraify 进数组，
 * chokidar 支持混合元素），不是替换，所以不必在这里重写 node_modules 那几条。
 */
export function rendererWatchIgnored(path: string): boolean {
  const abs = path.replace(/\\/g, '/')
  // 仓库之外不表态（含根目录本身）：monorepo/软链依赖要能被 Vite 照常 watch
  if (!abs.startsWith(PROJECT_ROOT + '/')) return false
  const rel = abs.slice(PROJECT_ROOT.length + 1)
  const top = rel.split('/')[0]
  // 根级文件（index.html、配置文件、.env*）改了要能触发 reload / 主进程重启
  if (top === rel) return false
  return !RENDERER_WATCH_ROOT_DIRS.includes(top)
}

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
      include: ['@xterm/xterm', '@xterm/addon-fit', '@xterm/addon-search', '@xterm/addon-web-links']
    },
    // 绑定 IPv4 地址，避免 Electron 渲染进程连接超时（默认只绑 IPv6 ::1）
    // dev:web 场景设 VITE_HOST=0.0.0.0 允许局域网访问（electron-vite 不支持 --host CLI 选项）
    server: {
      host: process.env.VITE_HOST || '127.0.0.1',
      port: Number(process.env.VITE_PORT) || 5555,
      watch: {
        ignored: rendererWatchIgnored,
      },
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
