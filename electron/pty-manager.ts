/**
 * PTY Manager — node-pty 终端实例池管理
 * 在 Electron 主进程中运行
 */
import * as pty from 'node-pty'
import { BrowserWindow } from 'electron'
import { homedir } from 'os'
import { join } from 'path'
import { LS_COLORS_NO_BACKGROUND } from './utils/terminal-colors'

interface PtySession {
  id: string
  process: pty.IPty
  shell: string
  cwd: string
}

// M3 修复：允许的 shell 白名单，防止渲染进程启动任意命令
const ALLOWED_SHELLS: Record<string, string[]> = {
  win32: ['cmd.exe', 'powershell.exe', 'pwsh.exe', 'bash.exe', 'git-bash.exe'],
  darwin: ['/bin/zsh', '/bin/bash', '/bin/sh', '/usr/bin/zsh', '/usr/bin/bash'],
  linux: ['/bin/bash', '/bin/sh', '/bin/zsh', '/usr/bin/bash', '/usr/bin/zsh'],
}
// 禁止的危险参数（正则匹配）
const DANGEROUS_ARG_PATTERNS = [
  /-enc/i, /-encodedcommand/i, /-command/i, /-c\b/,
  /\/c\b/, /&&/, /\|\|/, /;\s/, /\$\(/, /`/, /Invoke-Expression/i,
]

/** 钳制终端维度参数（审查 MINOR：防渲染进程传入非法值） */
function clampPtyDimension(value: unknown, fallback: number, min: number, max: number): number {
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
  create(opts?: { shell?: string; args?: string[]; cwd?: string; cols?: number; rows?: number }): { id: string; shell: string } {
    const id = `pty-${this.nextId++}`
    const defaultShell = this.getDefaultShell()
    let shell = opts?.shell || defaultShell
    const args = opts?.args || []
    const cwd = typeof opts?.cwd === 'string' && opts.cwd ? opts.cwd : this.getDefaultCwd()
    // 审查 MINOR：cols/rows 来自渲染进程，原实现直接透传给 pty.spawn —
    // 负数/小数/超大值会让 spawn 抛异常或分配巨型终端缓冲。统一钳制。
    const cols = clampPtyDimension(opts?.cols, 80, 1, 1000)
    const rows = clampPtyDimension(opts?.rows, 24, 1, 500)

    // M3 修复：验证 shell 在白名单中，防止渲染进程启动任意命令
    const platform = process.platform
    const allowed = ALLOWED_SHELLS[platform] || []
    const shellBase = shell.toLowerCase()
    const isAllowed = allowed.some(s => shellBase === s.toLowerCase() || shellBase.endsWith('/' + s.toLowerCase()) || shellBase.endsWith('\\' + s.toLowerCase()))
    if (!isAllowed) {
      console.warn(`[PtyManager] shell "${shell}" 不在白名单中，回退到默认: ${defaultShell}`)
      shell = defaultShell
    }

    // M3 修复：检测并拒绝危险参数
    for (const arg of args) {
      if (DANGEROUS_ARG_PATTERNS.some(p => p.test(arg))) {
        throw new Error(`检测到危险的 shell 参数，已拒绝: ${arg}`)
      }
    }

    console.log(`[PtyManager] spawning shell: ${shell}, args: ${JSON.stringify(args)}, cwd: ${cwd}`)
    const proc = pty.spawn(shell, args, {
      name: 'xterm-256color',
      cols,
      rows,
      cwd,
      env: {
        ...process.env,
        LS_COLORS: LS_COLORS_NO_BACKGROUND,
      } as Record<string, string>,
    })

    this.sessions.set(id, { id, process: proc, shell, cwd })

    // 数据输出 → 发送到渲染进程
    proc.onData((data) => {
      this.safeSend('pty:data', { id, data })
    })

    // 退出事件
    proc.onExit(({ exitCode }) => {
      console.log(`[PtyManager] shell exited: ${shell}, exitCode: ${exitCode}`)
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
