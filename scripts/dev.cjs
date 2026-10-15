'use strict'

// 防御性修复：某些 harness / 终端会话会向环境注入 ELECTRON_RUN_AS_NODE=1。
// 该变量被 electron 二进制在 C++ 层用 getenv() 判定——只要变量「存在」（无论值是否为空），
// electron 就退化为纯 Node 进程并静默退出：窗口不弹、无报错、npm run dev 立刻回到提示符。
// 这与「dev server 正常起来、但 electron 启动后马上关闭」的现象完全吻合。
//
// electron-vite 的 startElectron() 以 { stdio: 'inherit' } 拉起 electron，未显式传 env，
// 因此会继承当前进程的 process.env。这里在拉起 electron-vite 之前于 Node 层彻底删除该变量
// （与 shell 语法无关，绝对生效），electron 即可正常以 GUI 模式启动。
const _injectedRunAsNode = process.env.ELECTRON_RUN_AS_NODE
delete process.env.ELECTRON_RUN_AS_NODE
console.error(`[dev.cjs] wrapper active | ELECTRON_RUN_AS_NODE was=${JSON.stringify(_injectedRunAsNode)} now=${JSON.stringify(process.env.ELECTRON_RUN_AS_NODE)}`)

const { spawn } = require('node:child_process')
const path = require('node:path')

// electron-vite 的 bin 入口（其 package.json 的 exports 未暴露子路径，故直接拼绝对路径）
const bin = path.resolve(__dirname, '..', 'node_modules', 'electron-vite', 'bin', 'electron-vite.js')

const child = spawn(process.execPath, [bin, ...process.argv.slice(2)], { stdio: 'inherit' })
child.on('close', (code) => process.exit(code ?? 0))
child.on('error', (err) => {
  console.error('[dev] 无法启动 electron-vite:', err)
  process.exit(1)
})
