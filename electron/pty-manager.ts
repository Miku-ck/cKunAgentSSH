/**
 * PTY Manager — node-pty 终端实例池管理
 * 在 Electron 主进程中运行
 */
import type * as PtyNamespace from 'node-pty'
import { BrowserWindow } from 'electron'
import { homedir } from 'os'
import { statSync } from 'fs'
import { LS_COLORS_NO_BACKGROUND } from './utils/terminal-colors'

// node-pty 的 require 含原生 .node 二进制加载（实测 ~290ms），静态导入会把
// 这笔开销挂在主进程模块评估期——窗口 show 之前。挪到首次 spawn 时动态加载，
// 模块缓存保证后续终端不再付费。
let ptyMod: typeof PtyNamespace | null = null
async function loadPty(): Promise<typeof PtyNamespace> {
  if (!ptyMod) ptyMod = await import('node-pty')
  return ptyMod
}

interface PtySession {
  id: string
  process: PtyNamespace.IPty
  shell: string
  cwd: string
}

/**
 * S4-01 修复：白名单此前按 **basename 后缀**放行 ——
 * `allowed.some(s => shell === s || shell.endsWith('/' + s) || shell.endsWith('\\' + s))`，
 * 于是渲染进程给 `C:\Users\me\AppData\Local\Temp\cmd.exe`（任何目录下改名放着的可执行
 * 文件）都算命中，起的是一个带完整 preload API 面的 PTY 会话。
 *
 * 现规则：
 * - 整串精确命中白名单项 → 放行（bare 名交系统搜索顺序解析，POSIX 绝对路径原样命中）；
 * - 带目录的路径 → 必须 ①basename 是白名单里的程序名，且 ②所在目录受信任：
 *   主进程当前 PATH 上的目录，或 SystemRoot / Program Files / 标准 bin 目录之内。
 *   PATH 由主进程环境给出、渲染进程写不进去，而把 Git、PowerShell 7 这类非标准安装
 *   位置纳进来靠的正是它 —— 只收紧「渲染进程任意指定的目录」这一条，不至于打掉真实
 *   用户的终端。
 *
 * 抽成纯函数并导出：安全判据必须可测（本仓 pty 侧此前零测试）。
 */
export interface ShellTrustContext {
  caseInsensitive: boolean
  pathDirs: string[]
  systemRoots: string[]
}

export function isAllowedShell(shellRaw: string, allowed: string[], ctx: ShellTrustContext): boolean {
  const norm = (p: string): string =>
    (ctx.caseInsensitive ? p.toLowerCase() : p).replace(/\\/g, '/').replace(/\/+$/, '')
  const nameOf = (p: string): string => {
    const i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'))
    return i < 0 ? p : p.slice(i + 1)
  }
  const shell = norm(shellRaw)
  if (allowed.some(s => norm(s) === shell)) return true

  const sepIdx = Math.max(shellRaw.lastIndexOf('/'), shellRaw.lastIndexOf('\\'))
  if (sepIdx < 0) return false
  const base = norm(nameOf(shellRaw))
  if (!base) return false
  if (!allowed.some(s => norm(nameOf(s)) === base)) return false

  const dir = norm(shellRaw.slice(0, sepIdx))
  if (ctx.pathDirs.some(d => norm(d) === dir)) return true
  return ctx.systemRoots.some(r => {
    const n = norm(r)
    return n.length > 0 && (dir === n || dir.startsWith(n + '/'))
  })
}

/** 从主进程环境构造受信任目录集合（渲染进程的输入进不了这里）。 */
export function buildShellTrustContext(
  platform: NodeJS.Platform = process.platform,
  env: NodeJS.ProcessEnv = process.env,
): ShellTrustContext {
  const win = platform === 'win32'
  const systemRoot = env.SystemRoot || (win ? 'C:\\Windows' : '')
  const roots = win
    ? [
        systemRoot,
        env.ProgramFiles,
        env['ProgramFiles(x86)'],
      ].filter((r): r is string => Boolean(r))
    : ['/bin', '/usr/bin', '/usr/local/bin', '/sbin', '/usr/sbin']
  return {
    caseInsensitive: win || platform === 'darwin',
    pathDirs: (env.PATH || env.Path || '').split(win ? ';' : ':').filter(Boolean),
    systemRoots: roots,
  }
}

// M3 修复：允许的 shell 白名单，防止渲染进程启动任意命令
const ALLOWED_SHELLS: Record<string, string[]> = {
  // P1 修复：补 wsl.exe — 此前 UI 的"WSL"选项（ssh.ts 解析为 System32\wsl.exe）
  // 不在白名单中，被下方回退逻辑静默降级为默认 shell（cmd.exe）且无任何提示。
  // wsl.exe 无参数 = 启动默认发行版的默认 shell，参数面由 E-4 白名单兜底（拒绝一切 args）。
  win32: ['cmd.exe', 'powershell.exe', 'pwsh.exe', 'bash.exe', 'git-bash.exe', 'wsl.exe'],
  darwin: ['/bin/zsh', '/bin/bash', '/bin/sh', '/usr/bin/zsh', '/usr/bin/bash'],
  linux: ['/bin/bash', '/bin/sh', '/bin/zsh', '/usr/bin/bash', '/usr/bin/zsh'],
}
// E-4 修复：原"危险参数"黑名单可被绕过（cmd /S /K <任意命令>、
// PowerShell -ExecutionPolicy Bypass -File evil.ps1 都不在黑名单里）。
// 改为正向白名单：按 argv[0] 的 basename 判定 —
//   cmd → 拒绝一切 args；
//   powershell / pwsh → 仅允许 -NoLogo / -NoExit / -NoProfile；
//   bash（含 git-bash 路径）→ 仅允许登录/交互式标志；
//   其他程序 → 拒绝一切 args。
const BASH_ALLOWED_ARGS = new Set(['-l', '-i', '--login', '--interactive'])
// 修复：PowerShell 允许无害的启动标志。渲染层 ssh.ts getShellArgs('PowerShell')
// 固定传 [-NoLogo, -NoExit]（-NoExit 是为了防止 node-pty 启动后立即退出），
// 而原实现按 E-4 一律拒绝 PowerShell 的任何 args → 本地 PowerShell 终端
// 100% 打不开，报"PowerShell 终端不允许自定义参数，已拒绝"。
// 仅放行不影响执行语义的展示/生命周期标志；-Command / -File / -EncodedCommand /
// -ExecutionPolicy 等仍被拒绝（E-4 的防注入意图不变）。
const POWERSHELL_ALLOWED_ARGS = new Set(['-nologo', '-noexit', '-noprofile'])
// E-4：本地终端会话数上限，防止渲染进程无界 spawn
const MAX_PTY_SESSIONS = 32

/** 钳制终端维度参数（审查 MINOR：防渲染进程传入非法值）；main.ts ssh:connect 复用同款钳制 */
export function clampPtyDimension(value: unknown, fallback: number, min: number, max: number): number {
  const n = typeof value === 'number' ? Math.floor(value) : NaN
  if (!Number.isFinite(n) || n < min) return fallback
  return Math.min(n, max)
}

export class PtyManager {
  private sessions = new Map<string, PtySession>()
  private nextId = 1
  private mainWindow: BrowserWindow | null = null

  setWindow(win: BrowserWindow): void {
    this.mainWindow = win
  }

  /** 安全发送 IPC 消息（窗口已销毁时跳过） */
  private safeSend(channel: string, data: unknown): void {
    if (this.mainWindow && !this.mainWindow.isDestroyed()) {
      try { this.mainWindow.webContents.send(channel, data) } catch { /* EPIPE */ }
    }
  }

  /** 检测当前平台的默认 shell */
  getDefaultShell(): string {
    switch (process.platform) {
      case 'win32':
        return process.env.COMSPEC || 'powershell.exe'
      case 'darwin':
        return '/bin/zsh'
      default:
        return process.env.SHELL || '/bin/bash'
    }
  }

  /** 获取默认工作目录 */
  getDefaultCwd(): string {
    return homedir()
  }

  /** 创建新终端会话 */
  async create(opts?: { shell?: string; args?: string[]; cwd?: string; cols?: number; rows?: number }): Promise<{ id: string; shell: string }> {
    const id = `pty-${this.nextId++}`
    const defaultShell = this.getDefaultShell()
    let shell = opts?.shell || defaultShell
    const args = opts?.args || []
    const cwd = typeof opts?.cwd === 'string' && opts.cwd ? opts.cwd : this.getDefaultCwd()
    // 审查 MINOR：cols/rows 来自渲染进程，原实现直接透传给 pty.spawn —
    // 负数/小数/超大值会让 spawn 抛异常或分配巨型终端缓冲。统一钳制。
    const cols = clampPtyDimension(opts?.cols, 80, 1, 1000)
    const rows = clampPtyDimension(opts?.rows, 24, 1, 500)

    // E-4：本地终端会话数上限，防止渲染进程无界 spawn
    if (this.sessions.size >= MAX_PTY_SESSIONS) {
      throw new Error('本地终端数量已达上限，请关闭闲置终端后重试')
    }

    // M3 修复：验证 shell 在白名单中，防止渲染进程启动任意命令
    const platform = process.platform
    const allowed = ALLOWED_SHELLS[platform] || []
    let shellBase = shell.toLowerCase()
    const isAllowed = isAllowedShell(shell, allowed, buildShellTrustContext(platform))
    let finalArgs = args
    if (!isAllowed) {
      console.warn(`[PtyManager] shell "${shell}" 不在白名单中，回退到默认: ${defaultShell}`)
      shell = defaultShell
      shellBase = shell.toLowerCase()
      // 审查修复（P2）：白名单回退后执行目标已变为默认 shell — 原实现仍按
      // 渲染进程请求的旧 shell 名校验 args，"校验对象 ≠ 启动对象"
      // （如 Windows 下 bash -l → 回退 cmd.exe，-l 按 bash 规则通过但实际
      // 启动的是 cmd.exe）。回退后丢弃自定义参数，保证校验即启动。
      if (args.length > 0) {
        console.warn('[PtyManager] shell 白名单回退，丢弃自定义参数:', args)
        finalArgs = []
      }
    }

    // E-4 修复：参数正向白名单 — 黑名单可被绕过（cmd /S /K <任意命令>、
    // PowerShell -ExecutionPolicy Bypass -File evil.ps1）。按 basename 判定：
    //   cmd / powershell / pwsh → 拒绝一切 args；
    //   bash（含 git-bash 路径）→ 仅允许登录/交互式标志；
    //   其他程序 → 拒绝一切 args
    if (finalArgs.length > 0) {
      const baseName = shellBase.split(/[\\/]/).pop() || shellBase
      if (baseName === 'cmd' || baseName === 'cmd.exe') {
        throw new Error('cmd 终端不允许自定义参数，已拒绝')
      } else if (baseName === 'powershell' || baseName === 'powershell.exe' || baseName === 'pwsh' || baseName === 'pwsh.exe') {
        for (const arg of finalArgs) {
          if (!POWERSHELL_ALLOWED_ARGS.has(arg.toLowerCase())) {
            throw new Error(`PowerShell 不允许自定义参数，已拒绝: ${arg}`)
          }
        }
      } else if (baseName === 'bash' || baseName === 'bash.exe' || baseName === 'git-bash' || baseName === 'git-bash.exe') {
        for (const arg of finalArgs) {
          if (!BASH_ALLOWED_ARGS.has(arg.toLowerCase())) {
            throw new Error(`bash 不允许自定义参数，已拒绝: ${arg}`)
          }
        }
      } else {
        // 未匹配任何已知 shell（cmd/powershell/pwsh/bash）→ 拒绝一切参数
        throw new Error(`${baseName} 不允许自定义参数，已拒绝`)
      }
    }

    // E-4：spawn 前 stat 校验 cwd 存在且为目录，失败给出明确的中文错误
    // （否则 pty.spawn 抛出底层平台错误，难以定位）
    let cwdStat
    try {
      cwdStat = statSync(cwd)
    } catch {
      throw new Error(`工作目录不存在或不可访问: ${cwd}`)
    }
    if (!cwdStat.isDirectory()) {
      throw new Error(`工作目录必须是目录: ${cwd}`)
    }

    // 将底层平台错误（node-pty spawn 抛出的 ENOENT / conpty 不可用等）转为
    // 可直接展示给用户的中文错误；否则渲染层只能拿到晦涩的英文原文。
    const shellLabel = shellBase.split(/[\\/]/).pop() || shell
    let proc: PtyNamespace.IPty
    try {
      proc = (await loadPty()).spawn(shell, finalArgs, {
        name: 'xterm-256color',
        cols,
        rows,
        cwd,
        env: {
          ...process.env,
          LS_COLORS: LS_COLORS_NO_BACKGROUND,
        } as Record<string, string>,
      })
    } catch (err: any) {
      const raw = String(err?.message || err)
      if (/enoent|file not found|找不到/i.test(raw)) {
        throw new Error(`无法启动 ${shellLabel}：未找到可执行文件，请确认已安装并检查路径`)
      }
      if (/conpty/i.test(raw)) {
        throw new Error(`无法启动 ${shellLabel}：无法创建终端控制台（ConPTY 不可用）`)
      }
      throw new Error(`无法启动 ${shellLabel}：${raw}`)
    }

    this.sessions.set(id, { id, process: proc, shell, cwd })

    // 数据输出 → 发送到渲染进程
    // 审查修复：destroy()/destroyAll() 先 kill 再从 sessions 删除，随后 kill 触发的
    // 延迟 onExit / 缓冲 onData 会向渲染进程发送已被关闭会话的陈旧事件。以 sessions
    // 是否仍持有该 id 作为存活判据，销毁后到达的回调直接丢弃。
    proc.onData((data) => {
      if (!this.sessions.has(id)) return
      this.safeSend('pty:data', { id, data })
    })

    // 退出事件
    proc.onExit(({ exitCode }) => {
      if (!this.sessions.has(id)) return
      this.sessions.delete(id)
      this.safeSend('pty:exit', { id, exitCode })
    })

    return { id, shell }
  }

  /** 写入数据到终端 */
  write(id: string, data: string): void {
    const session = this.sessions.get(id)
    if (session) {
      session.process.write(data)
    }
  }

  /** 调整终端大小 */
  resize(id: string, cols: number, rows: number): void {
    const session = this.sessions.get(id)
    if (session) {
      try {
        session.process.resize(cols, rows)
      } catch {
        // ignore resize errors
      }
    }
  }

  /** 获取会话当前工作目录（当前为初始 cwd，后续可扩展为实时跟踪） */
  getCwd(id: string): string | undefined {
    return this.sessions.get(id)?.cwd
  }

  /** 销毁终端会话 */
  destroy(id: string): void {
    const session = this.sessions.get(id)
    if (session) {
      session.process.kill()
      this.sessions.delete(id)
    }
  }

  /** 清理所有会话 */
  destroyAll(): void {
    for (const session of this.sessions.values()) {
      try {
        session.process.kill()
      } catch {
        // ignore
      }
    }
    this.sessions.clear()
  }
}
