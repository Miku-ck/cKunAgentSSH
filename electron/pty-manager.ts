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
    const shell = opts?.shell || this.getDefaultShell()
    const args = opts?.args || []
    const cwd = opts?.cwd || this.getDefaultCwd()
    const cols = opts?.cols || 80
    const rows = opts?.rows || 24

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
