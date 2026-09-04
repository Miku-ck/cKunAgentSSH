/**
 * PTY Manager — node-pty 终端实例池管理
 * 在 Electron 主进程中运行
 */
import * as pty from 'node-pty'
import { BrowserWindow } from 'electron'
import { homedir } from 'os'
import { join } from 'path'
import { statSync } from 'fs'
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
// E-4 修复：原"危险参数"黑名单可被绕过（cmd /S /K <任意命令>、
// PowerShell -ExecutionPolicy Bypass -File evil.ps1 都不在黑名单里）。
// 改为正向白名单：按 argv[0] 的 basename 判定 —
//   cmd / powershell / pwsh → 拒绝一切 args；
//   bash（含 git-bash 路径）→ 仅允许登录/交互式标志；
//   其他程序 → 拒绝一切 args。
const BASH_ALLOWED_ARGS = new Set(['-l', '-i', '--login', '--interactive'])
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

    // E-4：本地终端会话数上限，防止渲染进程无界 spawn
    if (this.sessions.size >= MAX_PTY_SESSIONS) {
      throw new Error('本地终端数量已达上限，请关闭闲置终端后重试')
    }

    // M3 修复：验证 shell 在白名单中，防止渲染进程启动任意命令
    const platform = process.platform
    const allowed = ALLOWED_SHELLS[platform] || []
    let shellBase = shell.toLowerCase()
    const isAllowed = allowed.some(s => shellBase === s.toLowerCase() || shellBase.endsWith('/' + s.toLowerCase()) || shellBase.endsWith('\\' + s.toLowerCase()))
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
      }
      if (baseName === 'powershell' || baseName === 'powershell.exe' || baseName === 'pwsh' || baseName === 'pwsh.exe') {
        throw new Error('PowerShell 终端不允许自定义参数，已拒绝')
      }
      if (baseName === 'bash' || baseName === 'bash.exe' || baseName === 'git-bash' || baseName === 'git-bash.exe') {
        for (const arg of finalArgs) {
          if (!BASH_ALLOWED_ARGS.has(arg.toLowerCase())) {
            throw new Error(`bash 不允许自定义参数，已拒绝: ${arg}`)
          }
        }
      } else {
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

    console.log(`[PtyManager] spawning shell: ${shell}, args: ${JSON.stringify(finalArgs)}, cwd: ${cwd}`)
    const proc = pty.spawn(shell, finalArgs, {
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
