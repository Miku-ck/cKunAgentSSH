/**
 * SSH Manager — ssh2 远程连接管理
 * 在 Electron 主进程中运行，管理 SSH 连接和 Shell 通道
 */
import { Client, ClientChannel } from 'ssh2'
import type { SFTPWrapper } from 'ssh2'
import { LS_COLORS_NO_BACKGROUND } from './utils/terminal-colors'
import { BrowserWindow } from 'electron'
import { readFileSync, statSync } from 'fs'
import { promises as fsPromises } from 'fs'
import { posix } from 'path'

// ════════════════════════════════════════════════
//   传输错误分类（断点续传 / 友好提示 / 自动重试依据）
// ════════════════════════════════════════════════
export type TransferErrorCode =
  | 'NETWORK' | 'DISK_FULL' | 'PERMISSION' | 'NOT_FOUND'
  | 'CANCELLED' | 'STALLED' | 'REMOTE_CHANGED' | 'UNKNOWN'

export interface TransferError {
  code: TransferErrorCode
  message: string
  retryable: boolean
  originalError?: Error
}

/** 根据原始错误归类为 TransferError，便于前端决定是否自动重试 */
function classifyTransferError(err: unknown, cancelled: boolean): TransferError {
  if (cancelled) {
    return { code: 'CANCELLED', message: '已取消', retryable: false }
  }
  const msg = (err instanceof Error ? err.message : String(err)).toLowerCase()
  if (msg.includes('econnreset') || msg.includes('etimedout') || msg.includes('epipe')
      || msg.includes('socket hang up') || msg.includes('read epipe') || msg.includes('write epipe')) {
    return { code: 'NETWORK', message: '网络中断，已保存进度可续传', retryable: true, originalError: err as Error }
  }
  if (msg.includes('enospc') || msg.includes('no space left') || msg.includes('disk full')) {
    return { code: 'DISK_FULL', message: '磁盘空间不足', retryable: false, originalError: err as Error }
  }
  if (msg.includes('eacces') || msg.includes('permission denied') || msg.includes('failure')) {
    return { code: 'PERMISSION', message: '权限不足或目标被占用', retryable: false, originalError: err as Error }
  }
  if (msg.includes('enoent') || msg.includes('no such file')) {
    return { code: 'NOT_FOUND', message: '文件不存在或已被删除', retryable: false, originalError: err as Error }
  }
  return { code: 'UNKNOWN', message: `传输失败: ${err instanceof Error ? err.message : String(err)}`, retryable: false, originalError: err as Error }
}

/** 将 TransferError 包装为可抛出的 Error（携带 code/retryable 字段） */
function toTransferError(tErr: TransferError): Error {
  const e = new Error(tErr.message) as Error & { code?: string; retryable?: boolean }
  e.code = tErr.code
  e.retryable = tErr.retryable
  return e
}

// ════════════════════════════════════════════════
//   SFTP Promise 化辅助函数
// ════════════════════════════════════════════════
function sftpStat(sftp: SFTPWrapper, path: string): Promise<any> {
  return new Promise((resolve, reject) => {
    sftp.stat(path, (err, res) => err ? reject(err) : resolve(res))
  })
}

function sftpUnlink(sftp: SFTPWrapper, path: string): Promise<void> {
  return new Promise((resolve, reject) => {
    sftp.unlink(path, (err) => err ? reject(err) : resolve())
  })
}

function sftpRename(sftp: SFTPWrapper, oldPath: string, newPath: string): Promise<void> {
  return new Promise((resolve, reject) => {
    sftp.rename(oldPath, newPath, (err) => err ? reject(err) : resolve())
  })
}

function sftpOpen(sftp: SFTPWrapper, path: string, flags: string): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    // ssh2 的 SFTPWrapper.open() 同时支持 OpenMode 字符串与 number，
    // 直接传字符串让 ssh2 内部按 SFTP OPEN_MODE 转换，避免手动数值映射错误。
    sftp.open(path, flags as any, (err, handle) => err ? reject(err) : resolve(handle))
  })
}

function sftpClose(sftp: SFTPWrapper, handle: Buffer): Promise<void> {
  return new Promise((resolve, reject) => {
    sftp.close(handle, (err) => err ? reject(err) : resolve())
  })
}

function sftpRead(sftp: SFTPWrapper, handle: Buffer, position: number, length: number): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const buf = Buffer.alloc(length)
    sftp.read(handle, buf, 0, length, position, (err, bytesRead, data) => {
      if (err) reject(err)
      else resolve(data ? data : buf.slice(0, bytesRead || 0))
    })
  })
}

function sftpWrite(sftp: SFTPWrapper, handle: Buffer, buffer: Buffer, position: number): Promise<void> {
  return new Promise((resolve, reject) => {
    sftp.write(handle, buffer, 0, buffer.length, position, (err) => err ? reject(err) : resolve())
  })
}

// ════════════════════════════════════════════════
//   传输级心跳：检测卡住（30s 无进度）
// ════════════════════════════════════════════════
class TransferHeartbeat {
  private timer: NodeJS.Timeout | null = null
  private stalled = false

  start(intervalMs: number, stallThresholdMs: number, getLastProgress: () => number, onStall: () => void): void {
    this.stop()
    this.stalled = false
    this.timer = setInterval(() => {
      if (Date.now() - getLastProgress() > stallThresholdMs) {
        if (!this.stalled) {
          this.stalled = true
          onStall()
        }
      }
    }, intervalMs)
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer)
      this.timer = null
    }
  }

  isStalled(): boolean {
    return this.stalled
  }
}

/** 简易并发池：以 concurrency 个 worker 抢占式处理 items，任一失败立即 reject */
async function runConcurrent<T>(
  items: T[],
  concurrency: number,
  worker: (item: T, index: number) => Promise<void>,
  shouldAbort: () => boolean,
): Promise<void> {
  let nextIndex = 0
  const workers: Promise<void>[] = []

  const runOne = async (): Promise<void> => {
    while (nextIndex < items.length && !shouldAbort()) {
      const idx = nextIndex++
      await worker(items[idx], idx)
    }
  }

  const parallel = Math.min(concurrency, items.length)
  for (let i = 0; i < parallel; i++) {
    workers.push(runOne())
  }
  await Promise.all(workers)
}

export interface SSHConnectionConfig {
  id: string
  name: string
  host: string
  port: number
  username: string
  authType: 'password' | 'privateKey' | 'agent'
  password?: string
  privateKeyPath?: string
  passphrase?: string
  // 高级选项(可选)
  readyTimeout?: number
  keepaliveInterval?: number
}

export interface SSHTestResult {
  ok: boolean
  message: string
  latencyMs: number
}

export interface SftpEntry {
  name: string
  path: string
  isDirectory: boolean
  size: number
  modified: number
  mode: string
}

interface SSHSession {
  id: string
  connectionId: string
  client: Client
  stream: ClientChannel | null
  name: string
  /** 交互式 shell 当前工作目录，由 stream 数据中解析 OSC 7 得到 */
  cwd: string
}

export class SSHManager {
  private sessions = new Map<string, SSHSession>()
  private sftpSessions = new Map<string, SFTPWrapper>()
  private transfers = new Map<string, () => void>()
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

  /** 根据认证方式构建 ssh2 connect 配置 */
  private buildConnectConfig(config: SSHConnectionConfig): any {
    const connectConfig: any = {
      host: config.host,
      port: config.port,
      username: config.username,
      readyTimeout: config.readyTimeout ?? 15000,
      keepaliveInterval: config.keepaliveInterval ?? 10000,
      keepaliveCountMax: 3,
      // 启用 SSH 压缩：对文本/代码/日志类小文件有明显加速效果
      // （对已压缩的图片/视频/zip 等无收益但也不会显著变慢，ssh2 会自适应）
      compress: true,
    }

    if (config.authType === 'password') {
      connectConfig.password = config.password
    } else if (config.authType === 'privateKey') {
      if (config.privateKeyPath) {
        connectConfig.privateKey = readFileSync(config.privateKeyPath, 'utf-8')
      }
      if (config.passphrase) {
        connectConfig.passphrase = config.passphrase
      }
    } else if (config.authType === 'agent') {
      // 优先使用环境变量(Linux/macOS);Windows 回退到 OpenSSH 命名管道
      const sock = process.env.SSH_AUTH_SOCK
      if (sock) {
        connectConfig.agent = sock
      } else if (process.platform === 'win32') {
        connectConfig.agent = '\\\\.\\pipe\\openssh-ssh-agent'
      } else {
        connectConfig.agent = undefined
      }
    }

    return connectConfig
  }

  /** 建立 SSH 连接并创建 Shell 通道 */
  async connect(config: SSHConnectionConfig, cols: number, rows: number): Promise<{ id: string; name: string }> {
    const id = `ssh-${this.nextId++}`
    return this.doConnect(id, config, true, cols, rows)
  }

  /** 建立 SSH 连接,不创建 Shell 通道(用于 SFTP) */
  async connectSftp(config: SSHConnectionConfig): Promise<{ id: string; name: string }> {
    const id = `sftp-${this.nextId++}`
    return this.doConnect(id, config, false)
  }

  private doConnect(
    id: string,
    config: SSHConnectionConfig,
    withShell: boolean,
    cols?: number,
    rows?: number,
  ): Promise<{ id: string; name: string }> {
    const client = new Client()
    const connectConfig = this.buildConnectConfig(config)

    return new Promise((resolve, reject) => {
      client.on('ready', () => {
        if (!withShell) {
          const name = config.name || `${config.username}@${config.host}`
          this.sessions.set(id, { id, connectionId: config.id, client, stream: null, name, cwd: '' })
          resolve({ id, name })
          return
        }

        // 创建 Shell 通道（注入 LS_COLORS 去掉 ANSI 背景色）
        client.shell(
          { term: 'xterm-256color', cols: cols || 80, rows: rows || 24 },
          { env: { LS_COLORS: LS_COLORS_NO_BACKGROUND } },
          (err, stream) => {
            if (err) {
              client.end()
              reject(new Error('打开终端失败，请检查 SSH 连接状态'))
              return
            }

            const name = config.name || `${config.username}@${config.host}`
            this.sessions.set(id, {
              id,
              connectionId: config.id,
              client,
              stream,
              name,
              cwd: '',
            })

            // 在 shell 启动时注入 OSC 7 报告机制，跟踪交互式 shell 的当前工作目录
            // 业界标准：iTerm2 / Windows Terminal / GNOME Terminal 均使用 OSC 7 跟踪 cwd
            // 格式: ESC ] 7 ; file://<host><path> BEL 或 ST (ESC \)
            //
            // 关键设计：
            // 1. 使用 stty -echo 避免注入命令被回显到终端
            // 2. PROMPT_COMMAND 追加而非覆盖，保留用户 .bashrc 原有设置
            // 3. 同时兼容 bash (PROMPT_COMMAND) 和 zsh (precmd_functions)
            // 4. host 作为 printf 参数传入，避免 % 冲突和注入
            const escapedHost = config.host.replace(/'/g, "'\\''")
            const initCmd = Buffer.from(
              [
                'stty -echo 2>/dev/null',
                `__ckun_host='${escapedHost}'`,
                `__ckun_osc7() { printf '\\033]7;file://%s%s\\007' "$__ckun_host" "$PWD"; }`,
                // bash: 追加到 PROMPT_COMMAND（不覆盖原有设置）
                'if [ -n "$BASH_VERSION" ]; then',
                '  PROMPT_COMMAND="__ckun_osc7${PROMPT_COMMAND:+; $PROMPT_COMMAND}"',
                // zsh: 使用 precmd_functions
                'elif [ -n "$ZSH_VERSION" ]; then',
                '  precmd_functions+=(__ckun_osc7)',
                'fi',
                'stty echo 2>/dev/null',
                '', // 末尾换行
              ].join('\n')
            )
            stream.write(initCmd)

            // Shell 数据 → 渲染进程（同时解析 OSC 7 提取 cwd）
            stream.on('data', (data: Buffer) => {
              const text = data.toString('utf-8')
              // 解析 OSC 7 序列提取当前工作目录，支持 BEL (\x07) 和 ST (ESC \) 两种结束符
              const osc7Match = text.match(/\x1b\]7;file:\/\/[^\/]*([^\x07\x1b]*)(?:\x07|\x1b\\)/)
              if (osc7Match) {
                const session = this.sessions.get(id)
                if (session) {
                  // 路径可能含 URL 编码字符（如 %20），尝试解码；失败则使用原始值
                  try {
                    session.cwd = decodeURIComponent(osc7Match[1] || session.cwd)
                  } catch {
                    session.cwd = osc7Match[1] || session.cwd
                  }
                }
              }
              this.safeSend('ssh:data', { id, data: text })
            })

            // Shell 关闭
            stream.on('close', () => {
              this.sessions.delete(id)
              this.safeSend('ssh:close', { id })
            })

            resolve({ id, name })
          }
        )
      })

      client.on('error', (err) => {
        this.sessions.delete(id)
        const msg = err?.message || ''
        if (msg.includes('ECONNREFUSED') || msg.includes('Connection refused')) {
          reject(new Error('连接被拒绝，请检查主机地址和端口是否正确'))
        } else if (msg.includes('ETIMEDOUT') || msg.includes('timed out')) {
          reject(new Error('连接超时，请检查网络或增加超时时间'))
        } else if (msg.includes('Authentication') || msg.includes('auth') || msg.includes('password')) {
          reject(new Error('认证失败，请检查用户名、密码或私钥'))
        } else {
          reject(new Error('SSH 连接失败，请检查连接信息后重试'))
        }
      })

      client.on('close', () => {
        if (this.sessions.has(id)) {
          this.sessions.delete(id)
          this.safeSend('ssh:close', { id })
        }
      })

      client.connect(connectConfig)
    })
  }

  /** 写入数据到 Shell */
  write(id: string, data: string): void {
    const session = this.sessions.get(id)
    if (session?.stream) {
      session.stream.write(data)
    }
  }

  /** 调整终端大小 */
  resize(id: string, cols: number, rows: number): void {
    const session = this.sessions.get(id)
    if (session?.stream) {
      try {
        session.stream.setWindow(rows, cols, 0, 0)
      } catch {
        // ignore resize errors
      }
    }
  }

  /** 在已建立的 SSH 连接上执行一次性命令，返回 stdout / stderr / exitCode */
  async execCommandWithExitCode(
    sshSessionId: string,
    command: string,
    timeout = 30,
  ): Promise<{ stdout: string; stderr: string; exitCode: number | null }> {
    const session = this.sessions.get(sshSessionId)
    if (!session) throw new Error('SSH session not found')

    return new Promise((resolve, reject) => {
      let settled = false
      let streamRef: ClientChannel | null = null

      const timer = setTimeout(() => {
        if (!settled) {
          settled = true
          try { streamRef?.close() } catch { /* ignore */ }
          reject(new Error(`命令执行超时（${timeout}秒）`))
        }
      }, timeout * 1000)

      session.client.exec(command, (err, stream) => {
        if (err) {
          clearTimeout(timer)
          if (!settled) {
            settled = true
            reject(new Error('命令执行失败，请检查命令格式或连接状态'))
          }
          return
        }

        streamRef = stream
        let stdout = ''
        let stderr = ''
        let exitCode: number | null = null

        stream.on('data', (data: Buffer) => {
          stdout += data.toString('utf-8')
        })
        if (stream.stderr) {
          stream.stderr.on('data', (data: Buffer) => {
            stderr += data.toString('utf-8')
          })
        }
        stream.on('exit', (code: number | null) => {
          exitCode = code
        })
        stream.on('close', () => {
          clearTimeout(timer)
          if (!settled) {
            settled = true
            resolve({ stdout, stderr, exitCode })
          }
        })
        stream.on('error', (err: Error) => {
          clearTimeout(timer)
          if (!settled) {
            settled = true
            reject(err)
          }
        })
      })
    })
  }

  /** 在已建立的 SSH 连接上执行一次性命令（独立 exec 通道，不干扰交互 shell） */
  async execCommand(sshSessionId: string, command: string): Promise<string> {
    const { stdout, stderr } = await this.execCommandWithExitCode(sshSessionId, command)
    return stdout || stderr || ''
  }

  /** 获取交互式 shell 的当前工作目录（由 OSC 7 跟踪） */
  getCwd(sshSessionId: string): string {
    const session = this.sessions.get(sshSessionId)
    return session?.cwd || ''
  }

  /** 断开连接 */
  disconnect(id: string): void {
    const session = this.sessions.get(id)
    if (session) {
      this.closeSftp(id)
      try {
        session.stream?.close()
      } catch {
        // ignore
      }
      try {
        session.client.end()
      } catch {
        // ignore
      }
      this.sessions.delete(id)
    }
  }

  /** 清理所有会话 */
  disconnectAll(): void {
    for (const id of this.sftpSessions.keys()) {
      this.closeSftp(id)
    }
    for (const session of this.sessions.values()) {
      try {
        session.stream?.close()
      } catch {
        // ignore
      }
      try {
        session.client.end()
      } catch {
        // ignore
      }
    }
    this.sessions.clear()
    this.transfers.clear()
  }

  /** 测试连接:建立成功后立即断开,不写入 sessions */
  async testConnection(config: SSHConnectionConfig): Promise<SSHTestResult> {
    const start = Date.now()
    const client = new Client()
    const connectConfig = this.buildConnectConfig(config)

    return new Promise((resolve) => {
      let settled = false
      const done = (ok: boolean, message: string) => {
        if (settled) return
        settled = true
        try { client.end() } catch { /* ignore */ }
        resolve({ ok, message, latencyMs: Date.now() - start })
      }

      client.on('ready', () => done(true, '连接成功'))
      client.on('error', (err: Error) => {
        const msg = err?.message || ''
        if (msg.includes('ECONNREFUSED') || msg.includes('Connection refused')) {
          done(false, '连接被拒绝，请检查主机地址和端口')
        } else if (msg.includes('ETIMEDOUT') || msg.includes('timed out')) {
          done(false, '连接超时，请检查网络或增加超时时间')
        } else if (msg.includes('Authentication') || msg.includes('auth') || msg.includes('password')) {
          done(false, '认证失败，请检查用户名、密码或私钥')
        } else {
          done(false, '连接失败，请检查连接信息后重试')
        }
      })
      client.on('close', () => done(false, '连接已关闭'))

      try {
        client.connect(connectConfig)
      } catch (err: any) {
        done(false, '连接失败，请检查连接信息后重试')
      }

      // 兜底超时(比 readyTimeout 多 2s)
      setTimeout(() => done(false, '连接超时'), (config.readyTimeout ?? 15000) + 2000)
    })
  }

  // ════════════════════════════════════════════════
  //   SFTP
  // ════════════════════════════════════════════════

  private requireSftp(sshSessionId: string): SFTPWrapper {
    const sftp = this.sftpSessions.get(sshSessionId)
    if (!sftp) throw new Error('SFTP 会话未打开,请先连接远程')
    return sftp
  }

  /** 复用已建立的 SSH Client,打开 SFTP 子系统 */
  async openSftp(sshSessionId: string): Promise<void> {
    const session = this.sessions.get(sshSessionId)
    if (!session) throw new Error('SSH 会话不存在或已断开')
    if (this.sftpSessions.has(sshSessionId)) return
    await new Promise<void>((resolve, reject) => {
      session.client.sftp((err, sftp) => {
        if (err) return reject(new Error('SFTP 子系统打开失败，请检查远程服务器是否支持 SFTP'))
        sftp.on('close', () => {
          this.sftpSessions.delete(sshSessionId)
          this.safeSend('sftp:closed', { id: sshSessionId })
        })
        this.sftpSessions.set(sshSessionId, sftp)
        resolve()
      })
    })
  }

  /** 关闭 SFTP 子系统,保留 SSH Shell */
  closeSftp(sshSessionId: string): void {
    const sftp = this.sftpSessions.get(sshSessionId)
    if (sftp) {
      try { sftp.end() } catch { /* ignore */ }
      this.sftpSessions.delete(sshSessionId)
    }
  }

  /** 列远程目录,返回排序后的条目(目录在前,同类型按名称) */
  async sftpList(sshSessionId: string, remotePath: string): Promise<SftpEntry[]> {
    const sftp = this.requireSftp(sshSessionId)
    const list: Array<{ filename: string; attrs: any }> = await new Promise((resolve, reject) => {
      sftp.readdir(remotePath, (err, res) => {
        if (err) reject(new Error('读取远程目录失败，请检查路径是否存在或是否有访问权限'))
        else resolve(res as any)
      })
    })
    const entries: SftpEntry[] = list.map(item => ({
      name: item.filename,
      path: posix.join(remotePath, item.filename),
      isDirectory: (item.attrs.mode & 0o170000) === 0o040000,
      size: item.attrs.size || 0,
      modified: (item.attrs.mtime || 0) * 1000,
      mode: modeToString(item.attrs.mode),
    }))
    entries.sort((a, b) => {
      if (a.isDirectory !== b.isDirectory) return a.isDirectory ? -1 : 1
      return a.name.localeCompare(b.name, 'en')
    })
    return entries
  }

  async sftpMkdir(sshSessionId: string, remotePath: string): Promise<void> {
    const sftp = this.requireSftp(sshSessionId)
    await new Promise<void>((resolve, reject) => {
      sftp.mkdir(remotePath, (err) => {
        if (err) reject(new Error('创建目录失败，请检查路径是否存在或是否有权限'))
        else resolve()
      })
    })
  }

  /**
   * 递归创建远程目录（类似 mkdir -p）
   * 用于目录上传时确保远程父目录存在
   */
  async sftpMkdirRecursive(sshSessionId: string, remotePath: string): Promise<void> {
    const sftp = this.requireSftp(sshSessionId)
    const parts = remotePath.split('/').filter(Boolean)
    let current = ''
    for (const part of parts) {
      current = current ? `${current}/${part}` : `/${part}`
      try {
        await sftpStat(sftp, current)
      } catch {
        // 不存在则创建
        await new Promise<void>((resolve, reject) => {
          sftp.mkdir(current, (err) => {
            // 已存在（并发创建场景）视为成功
            if (err && !err.message.includes('exists')) reject(err)
            else resolve()
          })
        })
      }
    }
  }

  /**
   * 判断远程路径是否存在（文件或目录）
   * 用于 ZIP 覆盖确认等场景
   */
  async sftpExists(sshSessionId: string, remotePath: string): Promise<boolean> {
    if (!remotePath) return false
    const sftp = this.requireSftp(sshSessionId)
    try {
      await sftpStat(sftp, remotePath)
      return true
    } catch {
      return false
    }
  }

  async sftpRename(sshSessionId: string, oldPath: string, newPath: string): Promise<void> {
    const sftp = this.requireSftp(sshSessionId)
    await new Promise<void>((resolve, reject) => {
      sftp.rename(oldPath, newPath, (err) => {
        if (err) reject(new Error('重命名失败，请检查目标是否已存在或是否有权限'))
        else resolve()
      })
    })
  }

  async sftpDelete(sshSessionId: string, remotePath: string): Promise<void> {
    const sftp = this.requireSftp(sshSessionId)
    const stat: any = await new Promise((resolve, reject) => {
      sftp.stat(remotePath, (err, res) => {
        if (err) reject(new Error('获取文件信息失败，请检查路径是否存在'))
        else resolve(res)
      })
    })
    const isDir = (stat.mode & 0o170000) === 0o040000
    await new Promise<void>((resolve, reject) => {
      const cb = (err: Error | null | undefined) => err
        ? reject(new Error('删除失败，请检查是否有权限或目标是否被占用'))
        : resolve()
      if (isDir) sftp.rmdir(remotePath, cb)
      else sftp.unlink(remotePath, cb)
    })
  }

  /**
   * 上传本地文件到远程（并发分块 + 断点续传 + 心跳检测 + 错误分类）
   * - 使用 remotePath + '.part' 作为临时文件，完成后 rename
   * - 续传前校验远程 .part 前 4KB 与本地一致，防止污染
   * - NETWORK/STALLED 错误保留 .part 供下次续传，其他错误清理 .part
   */
  async sftpUpload(
    transferId: string,
    sshSessionId: string,
    localPath: string,
    remotePath: string,
  ): Promise<void> {
    const sftp = this.requireSftp(sshSessionId)
    const CHUNK_SIZE = 262144 // 256KB
    const CONCURRENCY = 8
    const VERIFY_SIZE = 4096 // 续传校验前 4KB

    // 本地文件大小
    const total = (() => {
      try { return statSync(localPath).size } catch { throw new Error(`本地文件不存在或不可访问: ${localPath}`) }
    })()

    const partPath = remotePath + '.part'
    let cancelled = false
    let lastProgress = Date.now()
    let transferred = 0
    const heartbeat = new TransferHeartbeat()

    this.transfers.set(transferId, () => {
      cancelled = true
      heartbeat.stop()
    })

    heartbeat.start(5000, 30000, () => lastProgress, () => {
      this.safeSend('sftp:stalled', { id: transferId })
    })

    try {
      // 1. 确定续传起点
      let startOffset = 0
      let partExists = false
      try {
        const partStat = await sftpStat(sftp, partPath)
        partExists = partStat.size > 0
        if (partStat.size >= total) {
          // .part 已完成，直接 rename
          await sftpRename(sftp, partPath, remotePath)
          this.safeSend('sftp:progress', { id: transferId, transferred: total, total })
          return
        }
        if (partStat.size > 0) {
          // 校验前 VERIFY_SIZE 字节
          const verifyLen = Math.min(VERIFY_SIZE, partStat.size)
          const localHead = Buffer.alloc(verifyLen)
          const localFd = await fsPromises.open(localPath, 'r')
          await localFd.read(localHead, 0, verifyLen, 0)
          await localFd.close()
          const partHandle = await sftpOpen(sftp, partPath, 'r')
          const remoteHead = await sftpRead(sftp, partHandle, 0, verifyLen)
          await sftpClose(sftp, partHandle)
          if (localHead.slice(0, verifyLen).equals(remoteHead.slice(0, verifyLen))) {
            startOffset = partStat.size
            transferred = startOffset
          } else {
            // 校验失败，从头开始
            await sftpUnlink(sftp, partPath)
            partExists = false
          }
        }
      } catch {
        // .part 不存在，正常流程
      }

      // 2. 打开远程 .part 文件（存在用 r+ 续写，不存在用 w 新建）
      const remoteHandle = await sftpOpen(sftp, partPath, partExists ? 'r+' : 'w')

      // 3. 打开本地文件
      const localFd = await fsPromises.open(localPath, 'r')

      // 4. 构造分块列表
      const chunks: Array<{ offset: number; length: number }> = []
      for (let off = startOffset; off < total; off += CHUNK_SIZE) {
        chunks.push({ offset: off, length: Math.min(CHUNK_SIZE, total - off) })
      }

      // 5. 并发上传
      let lastReport = 0
      const reportProgress = () => {
        const now = Date.now()
        if (now - lastReport > 100) {
          lastReport = now
          this.safeSend('sftp:progress', { id: transferId, transferred, total })
        }
      }

      if (chunks.length > 0) {
        await runConcurrent(
          chunks,
          CONCURRENCY,
          async (chunk) => {
            if (cancelled) return
            const buf = Buffer.alloc(chunk.length)
            const { bytesRead } = await localFd.read(buf, 0, chunk.length, chunk.offset)
            if (bytesRead !== chunk.length) {
              throw new Error(`本地读取不完整: 期望 ${chunk.length}, 实际 ${bytesRead}`)
            }
            await sftpWrite(sftp, remoteHandle, buf, chunk.offset)
            transferred += chunk.length
            lastProgress = Date.now()
            reportProgress()
          },
          () => cancelled,
        )
      }

      await localFd.close()
      await sftpClose(sftp, remoteHandle)

      // 6. 取消则抛错，不执行 rename（防止 .part 被误认为完成）
      if (cancelled) {
        throw new Error('CANCELLED')
      }

      // 7. 完成：rename .part → remotePath
      await sftpRename(sftp, partPath, remotePath)
      this.safeSend('sftp:progress', { id: transferId, transferred: total, total })
    } catch (err) {
      // 错误分类
      const tErr = heartbeat.isStalled()
        ? { code: 'STALLED' as const, message: '传输停滞，网络可能不稳定', retryable: true }
        : classifyTransferError(err, cancelled)
      // 不可重试的错误清理 .part
      if (!tErr.retryable && tErr.code !== 'CANCELLED') {
        try { await sftpUnlink(sftp, partPath) } catch { /* ignore */ }
      }
      throw toTransferError(tErr)
    } finally {
      heartbeat.stop()
      this.transfers.delete(transferId)
    }
  }

  /**
   * 下载远程文件到本地（并发分块 + 断点续传 + 心跳检测 + 错误分类）
   * - 使用 localPath + '.part' 作为临时文件，完成后 rename
   * - 续传前校验本地 .part 前 4KB 与远程一致，防止污染
   * - NETWORK/STALLED 错误保留 .part 供下次续传，其他错误清理 .part
   */
  async sftpDownload(
    transferId: string,
    sshSessionId: string,
    remotePath: string,
    localPath: string,
  ): Promise<void> {
    const sftp = this.requireSftp(sshSessionId)
    const CHUNK_SIZE = 262144 // 256KB
    const CONCURRENCY = 8
    const VERIFY_SIZE = 4096 // 续传校验前 4KB

    // 远程文件大小
    const stat = await sftpStat(sftp, remotePath)
    const total = stat.size || 0

    const partPath = localPath + '.part'
    let cancelled = false
    let lastProgress = Date.now()
    let transferred = 0
    const heartbeat = new TransferHeartbeat()

    this.transfers.set(transferId, () => {
      cancelled = true
      heartbeat.stop()
    })

    heartbeat.start(5000, 30000, () => lastProgress, () => {
      this.safeSend('sftp:stalled', { id: transferId })
    })

    try {
      // 1. 打开远程文件（后续校验和下载都复用）
      const remoteHandle = await sftpOpen(sftp, remotePath, 'r')

      // 2. 确定续传起点（检查本地 .part + 内容校验）
      let startOffset = 0
      let partExists = false
      try {
        const partStat = statSync(partPath)
        partExists = partStat.size > 0
        if (partStat.size >= total) {
          // .part 已完成，直接 rename
          await fsPromises.rename(partPath, localPath)
          this.safeSend('sftp:progress', { id: transferId, transferred: total, total })
          return
        }
        if (partStat.size > 0) {
          // 校验前 VERIFY_SIZE 字节与远程一致
          const verifyLen = Math.min(VERIFY_SIZE, partStat.size)
          const localPartFd = await fsPromises.open(partPath, 'r')
          const localHead = Buffer.alloc(verifyLen)
          await localPartFd.read(localHead, 0, verifyLen, 0)
          await localPartFd.close()
          const remoteHead = await sftpRead(sftp, remoteHandle, 0, verifyLen)
          if (localHead.slice(0, verifyLen).equals(remoteHead.slice(0, verifyLen))) {
            startOffset = partStat.size
            transferred = startOffset
          } else {
            // 校验失败，从头开始
            await fsPromises.unlink(partPath)
            partExists = false
          }
        }
      } catch {
        // .part 不存在，正常流程
      }

      // 3. 打开本地 .part（存在用 r+ 续写，不存在用 w 新建）
      const localFd = await fsPromises.open(partPath, partExists ? 'r+' : 'w')

      // 4. 构造分块列表
      const chunks: Array<{ offset: number; length: number }> = []
      for (let off = startOffset; off < total; off += CHUNK_SIZE) {
        chunks.push({ offset: off, length: Math.min(CHUNK_SIZE, total - off) })
      }

      // 5. 并发下载
      let lastReport = 0
      const reportProgress = () => {
        const now = Date.now()
        if (now - lastReport > 100) {
          lastReport = now
          this.safeSend('sftp:progress', { id: transferId, transferred, total })
        }
      }

      if (chunks.length > 0) {
        await runConcurrent(
          chunks,
          CONCURRENCY,
          async (chunk) => {
            if (cancelled) return
            const buf = await sftpRead(sftp, remoteHandle, chunk.offset, chunk.length)
            await localFd.write(buf, 0, buf.length, chunk.offset)
            transferred += buf.length
            lastProgress = Date.now()
            reportProgress()
          },
          () => cancelled,
        )
      }

      await localFd.close()
      await sftpClose(sftp, remoteHandle)

      // 6. 取消则抛错，不执行 rename（防止 .part 被误认为完成）
      if (cancelled) {
        throw new Error('CANCELLED')
      }

      // 7. 完成：rename .part → localPath
      await fsPromises.rename(partPath, localPath)
      this.safeSend('sftp:progress', { id: transferId, transferred: total, total })
    } catch (err) {
      const tErr = heartbeat.isStalled()
        ? { code: 'STALLED' as const, message: '传输停滞，网络可能不稳定', retryable: true }
        : classifyTransferError(err, cancelled)
      // 不可重试的错误清理 .part
      if (!tErr.retryable && tErr.code !== 'CANCELLED') {
        try { await fsPromises.unlink(partPath) } catch { /* ignore */ }
      }
      throw toTransferError(tErr)
    } finally {
      heartbeat.stop()
      this.transfers.delete(transferId)
    }
  }

  /** 读取远程文件内容到内存（带大小/二进制校验） */
  async sftpReadFile(
    sshSessionId: string,
    remotePath: string,
  ): Promise<{ content: string; size: number; readOnly: boolean }> {
    const sftp = this.requireSftp(sshSessionId)
    const stat: any = await new Promise((resolve, reject) => {
      sftp.stat(remotePath, (err, res) => {
        if (err) reject(new Error('获取远程文件信息失败，请检查路径是否存在'))
        else resolve(res)
      })
    })
    const size = stat.size || 0
    if (size > 10 * 1024 * 1024) {
      throw new Error('文件过大（超过 10MB），无法打开')
    }
    const buf: Buffer = await new Promise((resolve, reject) => {
      sftp.readFile(remotePath, (err, data) => {
        if (err) reject(new Error('读取远程文件失败，请检查是否有访问权限'))
        else resolve(data as Buffer)
      })
    })
    const checkLen = Math.min(buf.length, 8192)
    for (let i = 0; i < checkLen; i++) {
      if (buf[i] === 0) throw new Error('二进制文件，无法预览编辑')
    }
    return {
      content: buf.toString('utf-8'),
      size,
      readOnly: size > 5 * 1024 * 1024,
    }
  }

  /** 将字符串内容写回远程文件 */
  async sftpWriteFile(
    sshSessionId: string,
    remotePath: string,
    content: string,
  ): Promise<void> {
    const sftp = this.requireSftp(sshSessionId)
    const buf = Buffer.from(content, 'utf-8')
    await new Promise<void>((resolve, reject) => {
      sftp.writeFile(remotePath, buf, (err) => {
        if (err) reject(new Error('写入远程文件失败，请检查是否有写入权限'))
        else resolve()
      })
    })
  }

  /** 取消进行中的传输 */
  sftpCancel(transferId: string): void {
    const cancel = this.transfers.get(transferId)
    if (cancel) cancel()
  }

  // ════════════════════════════════════════════════
  //   ZIP 打包 / 解压（远程侧通过 SSH exec 执行）
  // ════════════════════════════════════════════════

  /**
   * 在远程服务器打包文件/目录为 zip
   * @param sshSessionId SSH 会话 ID
   * @param sources 要打包的远程路径列表（相对 cwd 或绝对路径）
   * @param remoteZipPath 输出的 zip 文件远程路径
   * @param cwd 工作目录（sources 相对此目录），默认 /
   * @returns 命令输出（stdout + stderr）
   */
  async sftpZipRemote(
    sshSessionId: string,
    sources: string[],
    remoteZipPath: string,
    cwd = '/',
  ): Promise<{ stdout: string; stderr: string; exitCode: number | null }> {
    if (!sources || sources.length === 0) {
      throw new Error('请至少选择一个要压缩的文件/目录')
    }
    // 优先使用 zip，回退到 python zipfile（部分精简系统无 zip 命令）
    // 先检测 zip 是否可用
    const checkZip = await this.execCommandWithExitCode(sshSessionId, 'command -v zip >/dev/null 2>&1 && echo yes || echo no', 5)
    const hasZip = (checkZip.stdout || '').trim() === 'yes'

    if (hasZip) {
      // 构建 zip 命令：cd <cwd> && zip -r -y <zipPath> <src1> <src2> ...
      // 注：sources 中的 "." 会让 zip 内文件路径以 "./" 开头，
      // 但主流解压工具（unzip、Windows Explorer、macOS Archive Utility）都能正确处理，
      // 不会创建额外嵌套层级，因此无需特殊处理。
      const quotedSources = sources.map(s => shellQuoteRemote(s)).join(' ')
      const cmd = `cd ${shellQuoteRemote(cwd)} && zip -r -y ${shellQuoteRemote(remoteZipPath)} ${quotedSources}`
      const result = await this.execCommandWithExitCode(sshSessionId, cmd, 300)
      if (result.exitCode !== 0) {
        // Bug 3 修复：失败时清理半成品 zip 文件，避免残留在远程服务器
        try { await this.sftpDelete(sshSessionId, remoteZipPath) } catch { /* ignore */ }
        throw new Error(`远程打包失败: ${(result.stderr || result.stdout || '').trim() || '未知错误'}`)
      }
      return result
    }

    // 回退：python3 zipfile
    const checkPy = await this.execCommandWithExitCode(sshSessionId, 'command -v python3 >/dev/null 2>&1 && echo yes || echo no', 5)
    const hasPy = (checkPy.stdout || '').trim() === 'yes'
    if (!hasPy) {
      throw new Error('远程服务器缺少 zip 命令和 python3，无法打包')
    }
    // 用 python 脚本打包
    const pyScript = [
      'import os, sys, zipfile',
      `os.chdir(${JSON.stringify(cwd)})`,
      `sources = ${JSON.stringify(sources)}`,
      `zip_path = ${JSON.stringify(remoteZipPath)}`,
      'with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:',
      '    for s in sources:',
      '        if os.path.isdir(s):',
      '            for root, dirs, files in os.walk(s):',
      '                for f in files:',
      '                    fp = os.path.join(root, f)',
      '                    arc = os.path.relpath(fp, ".")',
      '                    zf.write(fp, arc)',
      '        elif os.path.isfile(s):',
      '            zf.write(s, s)',
      'print("ok")',
    ].join('\n')
    const escaped = pyScript.replace(/'/g, "'\\''")
    const cmd = `python3 -c '${escaped}'`
    const result = await this.execCommandWithExitCode(sshSessionId, cmd, 300)
    if (result.exitCode !== 0) {
      // Bug 3 修复：失败时清理半成品 zip 文件，避免残留在远程服务器
      try { await this.sftpDelete(sshSessionId, remoteZipPath) } catch { /* ignore */ }
      throw new Error(`远程打包失败: ${(result.stderr || result.stdout || '').trim() || '未知错误'}`)
    }
    return result
  }

  /**
   * 在远程服务器解压 zip 文件
   * @param sshSessionId SSH 会话 ID
   * @param remoteZipPath zip 文件远程路径
   * @param remoteDestDir 解压目标目录（必须已存在或会被创建）
   */
  async sftpUnzipRemote(
    sshSessionId: string,
    remoteZipPath: string,
    remoteDestDir: string,
  ): Promise<{ stdout: string; stderr: string; exitCode: number | null }> {
    // 确保 destDir 存在
    await this.sftpMkdirRecursive(sshSessionId, remoteDestDir)

    const checkUnzip = await this.execCommandWithExitCode(sshSessionId, 'command -v unzip >/dev/null 2>&1 && echo yes || echo no', 5)
    const hasUnzip = (checkUnzip.stdout || '').trim() === 'yes'

    if (hasUnzip) {
      // -o 覆盖不询问，-q 安静（避免大量输出），-d 指定目录
      const cmd = `unzip -o -q ${shellQuoteRemote(remoteZipPath)} -d ${shellQuoteRemote(remoteDestDir)}`
      const result = await this.execCommandWithExitCode(sshSessionId, cmd, 300)
      if (result.exitCode !== 0) {
        throw new Error(`远程解压失败: ${(result.stderr || result.stdout || '').trim() || '未知错误'}`)
      }
      return result
    }

    // 回退：python3 zipfile
    const checkPy = await this.execCommandWithExitCode(sshSessionId, 'command -v python3 >/dev/null 2>&1 && echo yes || echo no', 5)
    const hasPy = (checkPy.stdout || '').trim() === 'yes'
    if (!hasPy) {
      throw new Error('远程服务器缺少 unzip 命令和 python3，无法解压')
    }
    const pyScript = [
      'import os, sys, zipfile',
      `zip_path = ${JSON.stringify(remoteZipPath)}`,
      `dest = ${JSON.stringify(remoteDestDir)}`,
      'os.makedirs(dest, exist_ok=True)',
      'with zipfile.ZipFile(zip_path, "r") as zf:',
      '    zf.extractall(dest)',
      'print("ok")',
    ].join('\n')
    const escaped = pyScript.replace(/'/g, "'\\''")
    const cmd = `python3 -c '${escaped}'`
    const result = await this.execCommandWithExitCode(sshSessionId, cmd, 300)
    if (result.exitCode !== 0) {
      throw new Error(`远程解压失败: ${(result.stderr || result.stdout || '').trim() || '未知错误'}`)
    }
    return result
  }
}

/** 远程路径单引号转义（用于 SSH exec 命令拼接） */
function shellQuoteRemote(s: string): string {
  return `'${s.replace(/'/g, "'\\''")}'`
}

/** 将数值 mode(含文件类型位)转为 'drwxr-xr-x' 形式 */
function modeToString(mode: number): string {
  const typeMap: Record<number, string> = {
    0o170000: '', // mask
    0o140000: 's', 0o120000: 'l', 0o100000: '-',
    0o060000: 'b', 0o020000: 'c', 0o040000: 'd', 0o010000: 'p',
  }
  let typeChar = '?'
  for (const k of Object.keys(typeMap)) {
    if ((mode & 0o170000) === Number(k)) {
      typeChar = typeMap[Number(k)]
      break
    }
  }
  const permChars = ['r', 'w', 'x']
  let s = typeChar
  for (let i = 0; i < 9; i++) {
    s += (mode & (1 << (8 - i))) ? permChars[i % 3] : '-'
  }
  return s
}
