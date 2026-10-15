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

const { spawn, spawnSync } = require('node:child_process')
const path = require('node:path')

// 启动环境自检：Electron 主进程在以下两类环境会崩溃（典型特征：
//   无 --no-sandbox：启动即崩 -2147483645 (0x80000003 STATUS_BREAKPOINT)
//   有 --no-sandbox：主进程 ~4s 后访问违例 (0xC0000005) 并报 crashpad "not connected"）
// 原因 ①（最常见）：安全软件拦截未签名的 node_modules 二进制（如腾讯电脑管家 QQPCRTP），
//   其进程创建钩子会破坏 Chromium 沙箱初始化并击杀多进程树；签名 IDE（如 Trae）不受影响。
// 原因 ②：终端处于受限 Windows Job Object（agent 沙箱 / 受管控终端）。
// 仅告警不阻断，提示用户对应的解决路径。
if (process.platform === 'win32') {
  // 检测腾讯电脑管家实时防护（tasklist 为 System32 自带，无需依赖 PATH 上的 powershell）
  const tl = spawnSync('tasklist', ['/FI', 'IMAGENAME eq QQPCRTP.exe', '/FO', 'CSV', '/NH'], { timeout: 10000 })
  const qqpcRunning = (tl.stdout || '').toString().toUpperCase().includes('QQPCRTP.EXE')
  if (qqpcRunning) {
    console.error('[dev.cjs] ⚠ 检测到腾讯电脑管家实时防护（QQPCRTP）正在运行，其会拦截未签名的 Electron 开发二进制：')
    console.error('[dev.cjs] ⚠ 表现为 electron.exe 启动即崩（STATUS_BREAKPOINT）或数秒后被访问违例终止。')
    console.error('[dev.cjs] ⚠ 解决：退出腾讯电脑管家后重跑 npm run dev；确认有效后将其「信任区」加入本目录')
    console.error('[dev.cjs] ⚠       （病毒查杀 → 信任区 → 添加文件夹 d:\\workspace\\cKunAgentSSH）。')
  } else {
    const ps = [
      `Add-Type -Namespace J -Name K -MemberDefinition '[DllImport("kernel32.dll")] public static extern bool IsProcessInJob(IntPtr h, IntPtr j, out bool r);'`,
      '$in = $false',
      '[J.K]::IsProcessInJob((Get-Process -Id $PID).Handle, [IntPtr]::Zero, [ref]$in) | Out-Null',
      'exit ([int]$in)',
    ].join('; ')
    // spawnSync 按 PATH 解析 'powershell' 在部分环境会 ENOENT，用 SystemRoot 拼绝对路径
    const powershell = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    const r = spawnSync(powershell, ['-NoProfile', '-EncodedCommand', Buffer.from(ps, 'utf16le').toString('base64')], { timeout: 15000 })
    if (r.status === 1) {
      console.error('[dev.cjs] ⚠ 当前终端处于受限 Windows Job Object 中（常见于 agent 沙箱 / 受管控终端），')
      console.error('[dev.cjs] ⚠ Electron 在此环境下会启动即崩（STATUS_BREAKPOINT）或数秒后被终止。')
      console.error('[dev.cjs] ⚠ 解决方法：在独立终端（Windows Terminal / 普通 PowerShell 窗口）中运行 npm run dev。')
    }
  }
}

// electron-vite 的 bin 入口（其 package.json 的 exports 未暴露子路径，故直接拼绝对路径）
const bin = path.resolve(__dirname, '..', 'node_modules', 'electron-vite', 'bin', 'electron-vite.js')

const child = spawn(process.execPath, [bin, ...process.argv.slice(2)], { stdio: 'inherit' })
child.on('close', (code) => process.exit(code ?? 0))
child.on('error', (err) => {
  console.error('[dev] 无法启动 electron-vite:', err)
  process.exit(1)
})
