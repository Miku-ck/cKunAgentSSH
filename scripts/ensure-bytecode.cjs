'use strict'
/**
 * 开发态后端字节码补齐（按需运行，不挂进 dev 启动链）。
 *
 * 为什么需要它：scripts/package.js 出包前会清空 backend/**\/__pycache__，再用内嵌
 * Python 3.11 重编；而开发态后端用的是 userData 缓存里的解释器（本机是 conda
 * 3.13）。两者 magic tag 不同，打包跑完再 `npm run dev` 时，dev 解释器的字节码
 * 缓存是空的。另外 git checkout/merge 会重写源码 mtime，让 timestamp 模式的 .pyc
 * 整批失效。这两种情况下后端每次启动都要从源码重新编译一遍。
 *
 * 实测口径（scripts/bench-imports.py，本机 conda 3.13）：
 *   有 .pyc  973ms / 无 .pyc 1146ms —— 收益约 0.17s，属启动卫生项，不是主瓶颈。
 *   打包态目标机上 scripts/package.js 注释记录的是 5240ms → 430ms，那一档由打包
 *   流程自己负责（checked-hash 预编译随包分发），本脚本只补开发态。
 *
 * 取舍：
 *   · 不加 -f，只补缺失/失效项，常态 < 0.3s；
 *   · 用 checked-hash，源码 mtime 变化而内容未变时不会白重编；
 *   · 不清空任何现存 .pyc，3.11 / 3.13 字节码可并存，CPython 只认自己的 tag；
 *   · 解释器口径与 electron/managers/backend-python-cache.ts 的缓存一致
 *     （userData/backend-python.json 的 { cmd, prefixArgs }）。
 *
 * 用法：
 *   npm run warm:bytecode
 *   CKUN_PYTHON=D:/ProgramData/miniconda3/python.exe node scripts/ensure-bytecode.cjs
 *   CKUN_SKIP_BYTECODE=1 可跳过
 */
const { spawnSync } = require('node:child_process')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const root = path.resolve(__dirname, '..')
const backend = path.join(root, 'backend')

function userDataDir() {
  if (process.platform === 'win32') {
    return path.join(
      process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming'),
      'cKunAgentSSH',
    )
  }
  if (process.platform === 'darwin') {
    return path.join(os.homedir(), 'Library', 'Application Support', 'cKunAgentSSH')
  }
  return path.join(
    process.env.XDG_CONFIG_HOME || path.join(os.homedir(), '.config'),
    'cKunAgentSSH',
  )
}

/** 与 BackendManager.resolvePythonCommand 用同一个解释器，避免补出一份没人认的字节码 */
function resolvePython() {
  if (process.env.CKUN_PYTHON) {
    return { cmd: process.env.CKUN_PYTHON, prefixArgs: [] }
  }
  try {
    const parsed = JSON.parse(
      fs.readFileSync(path.join(userDataDir(), 'backend-python.json'), 'utf8'),
    )
    if (
      typeof parsed.cmd === 'string' && parsed.cmd.length > 0 &&
      Array.isArray(parsed.prefixArgs) &&
      parsed.prefixArgs.every((s) => typeof s === 'string')
    ) {
      return parsed
    }
  } catch {
    // 缓存不存在/损坏是常态，回退 PATH 上的解释器
  }
  return { cmd: process.platform === 'win32' ? 'python' : 'python3', prefixArgs: [] }
}

function main() {
  if (process.env.CKUN_SKIP_BYTECODE === '1') {
    console.log('[bytecode] CKUN_SKIP_BYTECODE=1，跳过')
    return
  }
  const py = resolvePython()
  const t0 = Date.now()
  const r = spawnSync(
    py.cmd,
    [
      ...py.prefixArgs,
      '-m', 'compileall', '-q', '-j', '0',
      '--invalidation-mode', 'checked-hash',
      '-x', '(^|[\\\\/])(tests|_tmp)([\\\\/]|$)',
      backend,
    ],
    { stdio: 'inherit', shell: false, timeout: 60_000 },
  )
  const ms = Date.now() - t0
  if (r.error || r.status !== 0) {
    console.warn(
      `[bytecode] 预编译失败（不影响启动，可忽略）：${r.error ? r.error.message : `exit=${r.status}`}`,
    )
    process.exitCode = 1
    return
  }
  console.log(`[bytecode] 完成 ${ms}ms | 解释器 ${py.cmd}`)
}

main()
