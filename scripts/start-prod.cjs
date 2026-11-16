'use strict'
/**
 * 以「生产加载路径」启动应用：不启 Vite dev server，无 HMR。
 *
 * electron/main.ts:356 的加载分支是 `isDev && ELECTRON_RENDERER_URL`，
 * 只要不注入 dev URL，未打包状态同样走 loadFile(out/renderer/index.html)。
 * 实测 2026-11-14 19:38：did-finish-load 1291ms、window ready-to-show 1100ms；
 * 对比 dev 冷启 29~32s / dev 热启 8~11s。
 *
 * 适用：日常使用应用、验收生产产物、做启动性能对比。
 * 不适用：改前端代码 —— 产物不会自动更新，改完需先 npm run build。
 *
 * 用法：npm run start:prod
 */
const { spawn } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')

const root = path.resolve(__dirname, '..')
const required = [
  'out/main/index.js',
  'out/preload/index.js',
  'out/renderer/index.html',
]
const missing = required.filter((f) => !fs.existsSync(path.join(root, f)))
if (missing.length) {
  console.error('[start:prod] 缺少构建产物：' + missing.join(', '))
  console.error('[start:prod] 请先执行 npm run build')
  process.exit(1)
}

// 某些 harness / 终端会注入 ELECTRON_RUN_AS_NODE=1，electron.exe 会退化成纯 Node 静默退出。
delete process.env.ELECTRON_RUN_AS_NODE
// 关键：不注入 dev URL，main.ts 才会走 loadFile 分支而不是 Vite dev server。
delete process.env.ELECTRON_RENDERER_URL

const exe = process.platform === 'win32'
  ? path.join(root, 'node_modules', 'electron', 'dist', 'electron.exe')
  : process.platform === 'darwin'
    ? path.join(root, 'node_modules', 'electron', 'dist', 'Electron.app', 'Contents', 'MacOS', 'Electron')
    : path.join(root, 'node_modules', 'electron', 'dist', 'electron')

if (!fs.existsSync(exe)) {
  console.error('[start:prod] 找不到 Electron 可执行文件：' + exe)
  process.exit(1)
}

console.log('[start:prod] loadFile(out/renderer/index.html) | no Vite dev server | no HMR')
const child = spawn(exe, ['.', '--no-sandbox'], {
  cwd: root,
  stdio: 'inherit',
  env: process.env,
})
child.on('close', (code) => process.exit(code ?? 0))
child.on('error', (err) => {
  console.error('[start:prod] 启动失败：', err)
  process.exit(1)
})
