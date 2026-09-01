/**
 * SSH Manager — ssh2 远程连接管理
 * 在 Electron 主进程中运行，管理 SSH 连接和 Shell 通道
 */
import { Client, ClientChannel } from 'ssh2'
import type { SFTPWrapper } from 'ssh2'
import { LS_COLORS_NO_BACKGROUND } from './utils/terminal-colors'
import { assertKeyFileSafe } from './utils/path-guard'
import { BrowserWindow, app } from 'electron'
import { readFileSync, statSync } from 'fs'
import { promises as fsPromises } from 'fs'
import { posix, join } from 'path'
import { createHash } from 'crypto'

/** exec 命令输出累积上限（1MB），超过后截断并在返回结果附提示，防止无界内存增长 */
const MAX_EXEC_OUTPUT_BYTES = 1024 * 1024

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
      || msg.includes('socket hang up') || msg.includes('read epipe') || msg.includes('write epipe')
      // D13 修复：SSH_FX_FAILURE（SFTP 状态码 4，ssh2 报 "SSH_FX_FAILURE: ..."）是
      // 服务端通用错误，常由瞬时负载导致 — 原实现按 'failure' 子串一律判 PERMISSION
      // 不可重试，一抖动就删 .part 丢全部进度。归 NETWORK（可重试，保留 .part）
      || msg.includes('ssh_fx_failure')) {
    return { code: 'NETWORK', message: '网络中断，已保存进度可续传', retryable: true, originalError: err as Error }
  }
  if (msg.includes('enospc') || msg.includes('no space left') || msg.includes('disk full')) {
    return { code: 'DISK_FULL', message: '磁盘空间不足', retryable: false, originalError: err as Error }
  }
  // 仅明确的 permission denied 类状态判不可重试（不再匹配泛化的 'failure' 子串）
  if (msg.includes('eacces') || msg.includes('permission denied') || msg.includes('ssh_fx_permission_denied')) {
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
      } else if (this.stalled) {
        // 审查 MINOR：进度恢复后解除粘滞。原实现 stalled 一旦置 true 永不复位，
        // 一次瞬时卡顿之后，本次传输的后续任何错误都会被 heartbeat.isStalled()
        // 误判为 STALLED（错误分类失真，前端提示与重试策略都跟着错）
        this.stalled = false
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

/**
 * 审查 M-5：并行读 + 顺序写的传输流水线。
 *
 * 背景：原上传/下载用 8 路并行绝对偏移写（读、写都乱序）。当写偏移超过目标
 * 文件当前 EOF 时，SFTP 服务端（OpenSSH）与本地文件系统都会用【零字节填充】
 * 中间间隙。单次传输内后续 chunk 会覆盖回正确数据，但若传输在间隙尚未被
 * 覆盖前中断（取消/暂停/崩溃/断网），.part 文件大小已扩张到覆盖零填充区，
 * 下次续传 startOffset = partStat.size 会【跳过】中间的全零块 — rename 后
 * 文件静默损坏。
 *
 * 修复策略（读并行、写顺序）：
 * - 读侧保持预读窗口（window 个 chunk 并行读入内存，上限 window*CHUNK_SIZE）
 * - 写侧严格按 chunk 偏移顺序提交（高水位 = 已写连续前缀），永不越过 EOF，
 *   零填充不可能发生；中断后续传只信任 .part 的连续前缀
 */
interface PipelineChunk {
  offset: number
  length: number
}

async function pipelineReadWrite(
  chunks: PipelineChunk[],
  window: number,
  readChunk: (index: number, chunk: PipelineChunk) => Promise<Buffer>,
  writeChunk: (index: number, chunk: PipelineChunk, buf: Buffer) => Promise<void>,
  isCancelled: () => boolean,
  onWritten: (chunk: PipelineChunk) => void,
): Promise<void> {
  // D15 修复：slot 懒创建 + 写后释放 — 原实现为每个 chunk 预分配 slot+Promise
  // （100GB = 40 万对象常驻到传输结束）。现在只在读侧按窗口创建，写循环用完即
  // 置空 buffer 并从 Map 删除，内存占用恒约为 window 个 chunk。
  // 不变量保持不变：读失败经 readError 在写循环中传播、isCancelled break、
  // onWritten 严格按 chunk 顺序回调。
  const slots = new Map<number, { buf: Buffer | null; ready: Promise<void>; resolve: () => void }>()
  const createSlot = (i: number): { buf: Buffer | null; ready: Promise<void>; resolve: () => void } => {
    let resolve!: () => void
    const ready = new Promise<void>(r => { resolve = r })
    const slot = { buf: null, ready, resolve }
    slots.set(i, slot)
    return slot
  }

  let readIdx = 0
  let writeIdx = 0
  let readError: Error | null = null

  const issueReads = (): void => {
    // 预读窗口：最多领先写指针 window 个 chunk
    while (readIdx < chunks.length && readIdx < writeIdx + window) {
      const i = readIdx++
      const slot = createSlot(i)
      readChunk(i, chunks[i]).then((buf) => {
        slot.buf = buf
        slot.resolve()
      }).catch((e: Error) => {
        readError = e
        slot.resolve()
      })
    }
  }

  issueReads()
  while (writeIdx < chunks.length) {
    if (isCancelled()) break
    const slot = slots.get(writeIdx)!
    await slot.ready
    if (readError) throw readError
    const c = chunks[writeIdx]
    await writeChunk(writeIdx, c, slot.buf!)
    slot.buf = null // 释放引用，及时 GC
    slots.delete(writeIdx) // slot 使命完成，立即回收
    writeIdx++
    onWritten(c)
    issueReads()
  }
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
  /** D14 修复：close 通知已发送标志 — 去重，保证 ssh:close 恰好一次 */
  closeNotified?: boolean
}

export class SSHManager {
  private sessions = new Map<string, SSHSession>()
  private sftpSessions = new Map<string, SFTPWrapper>()
  private transfers = new Map<string, () => void>()
  private nextId = 1
  private mainWindow: BrowserWindow | null = null
  // s1 修复：known_hosts 存储（host:port → sha256 指纹），TOFU 模型
  private knownHosts = new Map<string, string>()
  private knownHostsLoaded = false
  // E-1 修复：single-flight 加载 Promise — 并发连接共享同一次磁盘加载，
  // 消除"标志先置位、文件未读完"的竞态窗口（并发连接按空表 TOFU 并覆写磁盘表）
  private loadPromise: Promise<void> | null = null
  // E-1b 修复：串行化写入队列 — 每次保存排队到前一次完成之后，
  // 快照在实际写入时刻（then 回调内）构建，并发首连不再把磁盘表写成子集
  private savePromise: Promise<void> = Promise.resolve()

  setWindow(win: BrowserWindow): void {
    this.mainWindow = win
  }

  /** known_hosts 文件路径（与 ssh-connections.json 同级） */
  private get knownHostsPath(): string {
    try {
      return join(app.getPath('userData'), 'known-hosts.json')
    } catch {
      // app 未就绪时的回退（理论上不会发生）
      return join(process.cwd(), 'known-hosts.json')
    }
  }

  /**
   * 加载 known_hosts 文件到内存（懒加载，single-flight）
   * E-1 修复：原实现 `knownHostsLoaded = true` 在 await readFile 完成前同步置位，
   * 并发连接在竞态窗口内按空表 TOFU，且各自的后续 save 以子集覆写磁盘 known_hosts。
   * 现在所有并发调用共享同一个 Promise，标志在加载完成（finally）后才置位。
   */
  private loadKnownHosts(): Promise<void> {
    if (!this.loadPromise) {
      this.loadPromise = (async () => {
        try {
          const raw = await fsPromises.readFile(this.knownHostsPath, 'utf-8')
          const data = JSON.parse(raw) as Record<string, string>
          for (const [key, fp] of Object.entries(data)) {
            this.knownHosts.set(key, fp)
          }
        } catch {
          // 文件不存在或解析失败 → 空表，后续 TOFU 逐步填充
        } finally {
          this.knownHostsLoaded = true
        }
      })()
    }
    return this.loadPromise
  }

  /**
   * 持久化 known_hosts 到磁盘（E-1b 修复：串行化）
   * 快照在 then 回调内构建 — 即实际写入时刻的全量内存表，排队保证
   * 并发首连的多次保存按序执行、后写覆盖先写，不再互相丢条目。
   */
  saveKnownHosts(): Promise<void> {
    this.savePromise = this.savePromise.then(async () => {
      const data: Record<string, string> = {}
      for (const [key, fp] of this.knownHosts) {
        data[key] = fp
      }
      await fsPromises.writeFile(this.knownHostsPath, JSON.stringify(data, null, 2), 'utf-8')
    }).catch((e) => {
      console.warn('[ssh] 保存 known_hosts 失败:', e)
    })
    return this.savePromise
  }

  /**
   * TOFU（Trust On First Use）主机密钥验证
   * - 首次连接：记录指纹并通知前端
   * - 指纹匹配：允许连接
   * - 指纹不匹配：拒绝连接（可能的中间人攻击）
   *
   * 审查 M-1 修复：hostKey 由每次连接构造的闭包显式传入（不再读共享的
   * _verifyingHostKey 字段）。原实现在并发连接时存在两类问题：
   * 1) 单槽位串扰 — 连接 A 设置 host:port 后，连接 B 在 A 的密钥到达前覆盖
   *    槽位，A 的密钥被按 B 的主机校验（跨主机误判/误信任）；
   * 2) fail-open — 槽位为 null 时（竞态窗口/清理过早）直接 return true，
   *    主机密钥完全未经验证即放行。
   */
  private verifyHostKey(hostKey: string, key: Buffer): boolean {
    const fp = createHash('sha256').update(key).digest('base64')

    const stored = this.knownHosts.get(hostKey)
    if (stored === undefined) {
      // 首次连接：信任并记录
      this.knownHosts.set(hostKey, fp)
      this.saveKnownHosts() // fire-and-forget
      this.safeSend('ssh:host-key-new', { hostKey, fingerprint: fp })
      return true
    }
    if (stored === fp) return true
    // 指纹不匹配 → 拒绝（中间人攻击风险）
    this.safeSend('ssh:host-key-mismatch', { hostKey, expected: stored, got: fp })
    return false
  }

  /** 安全发送 IPC 消息（窗口已销毁时跳过） */
  private safeSend(channel: string, data: unknown): void {
    if (this.mainWindow && !this.mainWindow.isDestroyed()) {
      try { this.mainWindow.webContents.send(channel, data) } catch { /* EPIPE */ }
    }
  }

  /**
   * D14 修复：统一的会话清理 — 删除 session 并按 session 级标志去重通知。
   * 原实现 client 'error' 先于 'close' 删除 session，导致无 shell 流的
   * SFTP 专用连接（stream: null）中断后 'close' 分支不再发 ssh:close，
   * UI 残留死会话。现在 error 处理器不删 session，close 处理器经此方法
   * 统一清理 + 通知，closeNotified 标志保证 ssh:close 恰好一次。
   */
  private destroySession(id: string): void {
    const session = this.sessions.get(id)
    this.sessions.delete(id)
    if (session && !session.closeNotified) {
      session.closeNotified = true
      this.safeSend('ssh:close', { id })
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
      // s1 修复 + 审查 M-1：主机密钥验证（TOFU 模型），防止中间人攻击。
      // 每次连接构造独立闭包捕获本连接的 host:port，避免并发连接共享单槽位
      // 导致的跨主机串扰与 fail-open（见 verifyHostKey 注释）。
      hostVerifier: (key: Buffer) => this.verifyHostKey(`${config.host}:${config.port}`, key),
    }

    if (config.authType === 'password') {
      connectConfig.password = config.password
    } else if (config.authType === 'privateKey') {
      if (config.privateKeyPath) {
        // 修复 1：读取私钥前做路径白名单校验（~/.ssh、userData 或文件选择对话框选取），
        // 防止渲染进程用任意路径读取敏感文件；校验失败直接抛出，由 ipcMain.handle 转 reject。
        assertKeyFileSafe(config.privateKeyPath)
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

  private async doConnect(
    id: string,
    config: SSHConnectionConfig,
    withShell: boolean,
    cols?: number,
    rows?: number,
  ): Promise<{ id: string; name: string }> {
    // s1 修复：加载 known_hosts（审查 M-1：验证上下文已由 buildConnectConfig
    // 的每连接闭包捕获，不再使用共享槽位）
    await this.loadKnownHosts()
    const client = new Client()
    const connectConfig = this.buildConnectConfig(config)
    // s3 修复：连接配置已提取后立即清除内存中的明文密码/passphrase
    config.password = undefined
    config.passphrase = undefined

    return new Promise((resolve, reject) => {
      const wrappedResolve = (val: { id: string; name: string }) => { resolve(val) }
      const wrappedReject = (err: Error) => { reject(err) }

      client.on('ready', () => {
        if (!withShell) {
          const name = config.name || `${config.username}@${config.host}`
          this.sessions.set(id, { id, connectionId: config.id, client, stream: null, name, cwd: '' })
          wrappedResolve({ id, name })
          return
        }

        // 创建 Shell 通道（注入 LS_COLORS 去掉 ANSI 背景色）
        client.shell(
          { term: 'xterm-256color', cols: cols || 80, rows: rows || 24 },
          { env: { LS_COLORS: LS_COLORS_NO_BACKGROUND } },
          (err, stream) => {
            if (err) {
              client.end()
              wrappedReject(new Error('打开终端失败，请检查 SSH 连接状态'))
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
            // 1. 整段脚本必须是【单行命令】：多行 if/function 结构会让 bash 逐行读取并
            //    输出 PS2 续行提示符（"> "），且每行作为独立命令都会重新打印一次 PS1，
            //    导致终端出现大量空提示符行和 "> " 垃圾输出
            // 2. stty -echo 无法阻止本行自身被内核回显（回显发生在命令执行前），
            //    但前端会过滤含 __ckun_ 标识的整行回显
            // 3. PROMPT_COMMAND 追加而非覆盖，保留用户 .bashrc 原有设置
            // 4. 同时兼容 bash (PROMPT_COMMAND) 和 zsh (precmd_functions)
            // 5. host 作为 printf 参数传入，避免 % 冲突和注入
            const escapedHost = config.host.replace(/'/g, "'\\''")
            const initCmd = Buffer.from(
              'stty -echo 2>/dev/null; ' +
              `__ckun_host='${escapedHost}'; ` +
              '__ckun_osc7() { printf \'\\033]7;file://%s%s\\007\' "$__ckun_host" "$PWD"; }; ' +
              'if [ -n "$BASH_VERSION" ]; then PROMPT_COMMAND="__ckun_osc7${PROMPT_COMMAND:+; $PROMPT_COMMAND}"; ' +
              'elif [ -n "$ZSH_VERSION" ]; then precmd_functions+=(__ckun_osc7); fi; ' +
              'stty echo 2>/dev/null\n'
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

            // Shell 关闭（D14：统一走 destroySession，去重通知）
            stream.on('close', () => {
              this.destroySession(id)
              // 审查 M-6 修复：shell 通道关闭不等于 SSH 连接结束 — 原实现只删
              // session 并通知前端，底层 client socket 一直挂着（client 的 'close'
              // 事件只在连接级关闭时触发），远端断开后连接/文件句柄持续泄漏直到
              // TCP 自行超时。此处主动 end() 整个连接（已关闭时 end() 为 no-op）。
              try { client.end() } catch { /* ignore */ }
            })

            wrappedResolve({ id, name })
          }
        )
      })

      client.on('error', (err) => {
        // D14 修复：error 不再删除 session、不单独通知 — 连接级 error 之后
        // 必然跟随 'close'（ssh2 语义），由 close 处理器统一清理 + 通知，
        // 保证 SFTP 专用连接（无 shell 流）中断后 UI 也能收到 ssh:close
        const msg = err?.message || ''
        if (msg.includes('ECONNREFUSED') || msg.includes('Connection refused')) {
          wrappedReject(new Error('连接被拒绝，请检查主机地址和端口是否正确'))
        } else if (msg.includes('ETIMEDOUT') || msg.includes('timed out')) {
          wrappedReject(new Error('连接超时，请检查网络或增加超时时间'))
        } else if (msg.includes('Authentication') || msg.includes('auth') || msg.includes('password')) {
          wrappedReject(new Error('认证失败，请检查用户名、密码或私钥'))
        } else if (msg.includes('host key') || msg.includes('verification failed') || msg.includes('Host verification')) {
          // s1 修复：主机密钥验证失败的友好提示（可能的中间人攻击）
          wrappedReject(new Error('主机密钥验证失败：服务器密钥与记录不符，可能存在中间人攻击'))
        } else {
          wrappedReject(new Error('SSH 连接失败，请检查连接信息后重试'))
        }
      })

      client.on('close', () => {
        // D14 修复：close 统一清理 + 通知（destroySession 内部去重，恰好一次）
        this.destroySession(id)
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
        let stdoutTruncated = false
        let stderrTruncated = false
        let exitCode: number | null = null

        // 修复 3：输出累积上限——达到 1MB 后停止追加，保留已截断内容并标记
        const appendCapped = (acc: string, text: string): string => {
          if (acc.length >= MAX_EXEC_OUTPUT_BYTES) return acc
          const remain = MAX_EXEC_OUTPUT_BYTES - acc.length
          return text.length > remain ? acc + text.slice(0, remain) : acc + text
        }

        stream.on('data', (data: Buffer) => {
          stdout = appendCapped(stdout, data.toString('utf-8'))
          if (stdout.length >= MAX_EXEC_OUTPUT_BYTES) stdoutTruncated = true
        })
        if (stream.stderr) {
          stream.stderr.on('data', (data: Buffer) => {
            stderr = appendCapped(stderr, data.toString('utf-8'))
            if (stderr.length >= MAX_EXEC_OUTPUT_BYTES) stderrTruncated = true
          })
        }
        stream.on('exit', (code: number | null) => {
          exitCode = code
        })
        stream.on('close', () => {
          clearTimeout(timer)
          if (!settled) {
            settled = true
            const TRUNCATED_NOTE = '\n[输出已截断，超过 1MB 上限]'
            resolve({
              stdout: stdoutTruncated ? stdout + TRUNCATED_NOTE : stdout,
              stderr: stderrTruncated ? stderr + TRUNCATED_NOTE : stderr,
              exitCode,
            })
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
    // s1 修复：测试连接也走同样的 host key 验证（每连接闭包，审查 M-1）
    await this.loadKnownHosts()
    const client = new Client()
    const connectConfig = this.buildConnectConfig(config)

    return new Promise((resolve) => {
      let settled = false
      let done = (ok: boolean, message: string) => {
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
        } else if (msg.includes('host key') || msg.includes('verification failed') || msg.includes('Host verification')) {
          done(false, '主机密钥验证失败：服务器密钥与记录不符，可能存在中间人攻击')
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
      // s6 修复：保存 timerId 以便连接提前完成时清除，避免无谓等待
      const fallbackTimer = setTimeout(() => done(false, '连接超时'), (config.readyTimeout ?? 15000) + 2000)
      const _origDone = done
      done = (ok, message) => {
        clearTimeout(fallbackTimer)
        _origDone(ok, message)
      }
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
		const cb = (err: Error | null | undefined) => {
			if (!err) { resolve(); return }
			// 保留原始 SFTP 错误信息，便于用户定位原因
			const msg = err.message || '未知错误'
			// 常见错误给出更友好的提示
			if (msg.includes('Permission denied')) {
				reject(new Error('权限不足，无法删除'))
			} else if (msg.includes('Directory not empty') || msg.includes('not empty')) {
				reject(new Error('目录不为空，无法删除'))
			} else if (msg.includes('No such file')) {
				reject(new Error('文件或目录不存在'))
			} else {
				reject(new Error(`删除失败: ${msg}`))
			}
		}
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
    // C10 修复：心跳判定停滞后除发 sftp:stalled 事件外，还要 reject 该传输的
    // IPC Promise（渲染层 retryable 自动续传依赖它）。标记失效后同一传输只
    // reject 一次，后续进度回调对已失效传输忽略
    let stalled = false
    let stallReject: ((err: Error) => void) | null = null
    const heartbeat = new TransferHeartbeat()

    const cancelFn = () => {
      cancelled = true
      heartbeat.stop()
    }
    this.transfers.set(transferId, cancelFn)

    heartbeat.start(5000, 30000, () => lastProgress, () => {
      this.safeSend('sftp:stalled', { id: transferId })
      if (!stalled) {
        stalled = true
        // 错误对象形态与 NETWORK 一致（Error + code/retryable），渲染层按
        // retryable 统一走自动续传路径
        stallReject?.(toTransferError({ code: 'STALLED', message: '传输停滞，网络可能不稳定', retryable: true }))
      }
    })

    // 已失效传输（已按 STALLED reject）不再发进度，避免渲染层收到已放弃
    // 传输的迟到事件
    const sendProgress = (data: { transferred: number; total: number }): void => {
      if (stalled) return
      this.safeSend('sftp:progress', { id: transferId, ...data })
    }

    // M7 修复（上传侧补齐）：句柄声明在 try 外，确保 finally 块始终可访问。
    // （原为 try 内 const，finally 引用会抛 ReferenceError 并被内层 catch 吞掉，
    //   导致每次上传完成/异常时本地 fd 与远程句柄泄漏）
    let remoteHandle: Buffer | null = null
    let localFd: Awaited<ReturnType<typeof fsPromises.open>> | null = null

    const runBody = async (): Promise<void> => {
    try {
      // 1. 确定续传起点
      let startOffset = 0
      let partExists = false
      try {
        const partStat = await sftpStat(sftp, partPath)
        if (partStat.size === total) {
          // .part 与源文件大小一致，视为已完成，直接 rename
          await sftpRename(sftp, partPath, remotePath)
          sendProgress({ transferred: total, total })
          return
        }
        // D12 修复：原实现 `partStat.size >= total` 直接 rename — 源文件缩小后
        // 会把陈旧、过大的 .part 当完整结果。仅 `===` 才 rename；`>` 时前 4KB
        // 校验无法区分"完成"与"源缩小"，fail-safe：删 .part 从头传
        let resumeSize = partStat.size
        if (resumeSize > total) {
          try { await sftpUnlink(sftp, partPath) } catch { /* ignore */ }
          resumeSize = 0
        }
        if (resumeSize > 0) {
          // 校验前 VERIFY_SIZE 字节
          const verifyLen = Math.min(VERIFY_SIZE, resumeSize)
          const localHead = Buffer.alloc(verifyLen)
          const localFd = await fsPromises.open(localPath, 'r')
          await localFd.read(localHead, 0, verifyLen, 0)
          await localFd.close()
          const partHandle = await sftpOpen(sftp, partPath, 'r')
          const remoteHead = await sftpRead(sftp, partHandle, 0, verifyLen)
          await sftpClose(sftp, partHandle)
          if (localHead.slice(0, verifyLen).equals(remoteHead.slice(0, verifyLen))) {
            startOffset = resumeSize
            transferred = startOffset
            partExists = true
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
      remoteHandle = await sftpOpen(sftp, partPath, partExists ? 'r+' : 'w')

      // 3. 打开本地文件
      localFd = await fsPromises.open(localPath, 'r')
      // 类型收窄（此时两者保证非空），供异步闭包内使用
      const uploadRemoteHandle = remoteHandle
      const uploadLocalFd = localFd

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
          sendProgress({ transferred, total })
        }
      }

      if (chunks.length > 0) {
        // 审查 M-5：本地读并行（窗口 2*CONCURRENCY）+ 远程 .part 顺序追加写，
        // 杜绝"越过 EOF 的零填充 + 中断后续传跳过零块"导致的静默损坏
        await pipelineReadWrite(
          chunks,
          2 * CONCURRENCY,
          async (_i, chunk) => {
            const buf = Buffer.alloc(chunk.length)
            const { bytesRead } = await uploadLocalFd.read(buf, 0, chunk.length, chunk.offset)
            if (bytesRead !== chunk.length) {
              throw new Error(`本地读取不完整: 期望 ${chunk.length}, 实际 ${bytesRead}`)
            }
            return buf
          },
          async (_i, chunk, buf) => {
            await sftpWrite(sftp, uploadRemoteHandle, buf, chunk.offset)
          },
          () => cancelled,
          (chunk) => {
            transferred += chunk.length
            lastProgress = Date.now()
            reportProgress()
          },
        )
      }

      // 6. 取消则抛错，不执行 rename（防止 .part 被误认为完成）
      if (cancelled) {
        throw new Error('CANCELLED')
      }

      // 7. 完成：rename .part → remotePath
      await sftpRename(sftp, partPath, remotePath)
      sendProgress({ transferred: total, total })
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
      // M7 修复（上传侧补齐）：FD/句柄关闭在 finally 可达（null 安全）
      try { if (localFd) await localFd.close() } catch { /* ignore */ }
      try { if (remoteHandle) await sftpClose(sftp, remoteHandle) } catch { /* ignore */ }
      heartbeat.stop()
      // C10 配套：渲染层按 STALLED 重试时可能已用同一 transferId 启动新传输，
      // 只清理本传输自己注册的取消函数，避免误删新传输的条目
      if (this.transfers.get(transferId) === cancelFn) {
        this.transfers.delete(transferId)
      }
    }
    }

    // C10 修复：传输体与停滞后 reject 竞速 — 原实现心跳判定停滞只发事件，
    // 传输 Promise 永不 reject，渲染层"正在自动续传"的 retryable 自动重试
    // 路径永远不触发。现在停滞时 IPC Promise 先 reject（Error + code:'STALLED'
    // + retryable:true）；传输体无论之后成功或失败都照常收尾（.part 保留，
    // 重试续传），互不干扰
    return new Promise<void>((resolve, reject) => {
      const stallPromise = new Promise<never>((_, rej) => { stallReject = rej })
      void Promise.race([
        runBody().then(() => resolve()),
        stallPromise,
      ]).then(null, (err) => reject(err as Error))
    })
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
    // C10 修复：心跳判定停滞后除发 sftp:stalled 事件外，还要 reject 该传输的
    // IPC Promise（渲染层 retryable 自动续传依赖它）。标记失效后同一传输只
    // reject 一次，后续进度回调对已失效传输忽略
    let stalled = false
    let stallReject: ((err: Error) => void) | null = null
    const heartbeat = new TransferHeartbeat()

    const cancelFn = () => {
      cancelled = true
      heartbeat.stop()
    }
    this.transfers.set(transferId, cancelFn)

    heartbeat.start(5000, 30000, () => lastProgress, () => {
      this.safeSend('sftp:stalled', { id: transferId })
      if (!stalled) {
        stalled = true
        // 错误对象形态与 NETWORK 一致（Error + code/retryable），渲染层按
        // retryable 统一走自动续传路径
        stallReject?.(toTransferError({ code: 'STALLED', message: '传输停滞，网络可能不稳定', retryable: true }))
      }
    })

    // 已失效传输（已按 STALLED reject）不再发进度，避免渲染层收到已放弃
    // 传输的迟到事件
    const sendProgress = (data: { transferred: number; total: number }): void => {
      if (stalled) return
      this.safeSend('sftp:progress', { id: transferId, ...data })
    }

    // M7 修复：声明在 try 外部，确保 finally 块始终可访问
    let remoteHandle: Buffer | null = null
    let localFd: Awaited<ReturnType<typeof fsPromises.open>> | null = null

    const runBody = async (): Promise<void> => {
    try {
      // 1. 打开远程文件（后续校验和下载都复用）
      remoteHandle = await sftpOpen(sftp, remotePath, 'r')

      // 2. 确定续传起点（检查本地 .part + 内容校验）
      let startOffset = 0
      let partExists = false
      try {
        const partStat = statSync(partPath)
        if (partStat.size === total) {
          // .part 与远程文件大小一致，视为已完成，直接 rename
          await fsPromises.rename(partPath, localPath)
          sendProgress({ transferred: total, total })
          return
        }
        // D12 修复：原实现 `partStat.size >= total` 直接 rename — 远程文件缩小后
        // 会把陈旧、过大的 .part 当完整结果。仅 `===` 才 rename；`>` 时前 4KB
        // 校验无法区分"完成"与"源缩小"，fail-safe：删 .part 从头传
        let resumeSize = partStat.size
        if (resumeSize > total) {
          try { await fsPromises.unlink(partPath) } catch { /* ignore */ }
          resumeSize = 0
        }
        if (resumeSize > 0) {
          // 校验前 VERIFY_SIZE 字节与远程一致
          const verifyLen = Math.min(VERIFY_SIZE, resumeSize)
          const localPartFd = await fsPromises.open(partPath, 'r')
          const localHead = Buffer.alloc(verifyLen)
          await localPartFd.read(localHead, 0, verifyLen, 0)
          await localPartFd.close()
          const remoteHead = await sftpRead(sftp, remoteHandle, 0, verifyLen)
          if (localHead.slice(0, verifyLen).equals(remoteHead.slice(0, verifyLen))) {
            startOffset = resumeSize
            transferred = startOffset
            partExists = true
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
      localFd = await fsPromises.open(partPath, partExists ? 'r+' : 'w')
      // 类型收窄（此时两者保证非空），供异步闭包内使用
      const downloadRemoteHandle = remoteHandle
      const downloadLocalFd = localFd

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
          sendProgress({ transferred, total })
        }
      }

      if (chunks.length > 0) {
        // 审查 M-5：远程读并行（窗口 2*CONCURRENCY）+ 本地 .part 顺序追加写，
        // 杜绝"越过 EOF 的零填充 + 中断后续传跳过零块"导致的静默损坏
        await pipelineReadWrite(
          chunks,
          2 * CONCURRENCY,
          async (_i, chunk) => {
            // 读满循环：SFTP 单次 read 可能返回短读（部分字节），按剩余长度续读；
            // 返回 0 字节说明到达 EOF（文件在传输中缩小），属异常，抛错交由重试
            const buf = Buffer.alloc(chunk.length)
            let off = 0
            while (off < chunk.length) {
              const part = await sftpRead(sftp, downloadRemoteHandle, chunk.offset + off, chunk.length - off)
              if (part.length === 0) {
                throw new Error(`远程读取不完整: 期望 ${chunk.length}, 实际 ${off}`)
              }
              part.copy(buf, off)
              off += part.length
            }
            return buf
          },
          async (_i, chunk, buf) => {
            // D11 修复：FileHandle.write 可能短写（bytesWritten < buf.length）—
            // 忽略返回值会破坏".part 大小 = 连续前缀"的续传不变量（下次续传
            // startOffset = partStat.size 会跳过缺失区间）。取回返回值，短写时
            // 从 chunk.offset + bytesWritten 起循环补写剩余，直到写满
            let off = 0
            while (off < buf.length) {
              const { bytesWritten } = await downloadLocalFd.write(buf, off, buf.length - off, chunk.offset + off)
              if (bytesWritten <= 0) {
                throw new Error(`本地写入停滞: 偏移 ${chunk.offset + off} 处期望写入 ${buf.length - off} 字节`)
              }
              off += bytesWritten
            }
          },
          () => cancelled,
          (chunk) => {
            transferred += chunk.length
            lastProgress = Date.now()
            reportProgress()
          },
        )
      }

      // 6. 取消则抛错，不执行 rename（防止 .part 被误认为完成）
      if (cancelled) {
        throw new Error('CANCELLED')
      }

      // 7. 完成：rename .part → localPath
      await fsPromises.rename(partPath, localPath)
      sendProgress({ transferred: total, total })
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
      // M7 修复：FD 关闭移入 finally，确保异常路径也不泄漏
      try { await localFd?.close() } catch { /* ignore */ }
      try { if (remoteHandle) await sftpClose(sftp, remoteHandle) } catch { /* ignore */ }
      heartbeat.stop()
      // C10 配套：渲染层按 STALLED 重试时可能已用同一 transferId 启动新传输，
      // 只清理本传输自己注册的取消函数，避免误删新传输的条目
      if (this.transfers.get(transferId) === cancelFn) {
        this.transfers.delete(transferId)
      }
    }
    }

    // C10 修复：传输体与停滞后 reject 竞速 — 原实现心跳判定停滞只发事件，
    // 传输 Promise 永不 reject，渲染层"正在自动续传"的 retryable 自动重试
    // 路径永远不触发。现在停滞时 IPC Promise 先 reject（Error + code:'STALLED'
    // + retryable:true）；传输体无论之后成功或失败都照常收尾（.part 保留，
    // 重试续传），互不干扰
    return new Promise<void>((resolve, reject) => {
      const stallPromise = new Promise<never>((_, rej) => { stallReject = rej })
      void Promise.race([
        runBody().then(() => resolve()),
        stallPromise,
      ]).then(null, (err) => reject(err as Error))
    })
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
    // M8 修复：用 python 脚本打包（写临时文件后执行，避免 shell 拼接注入风险）
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
    // 写临时文件后通过 python3 <file> 执行，完全绕过 shell 对脚本内容的解释
    const tmpScript = `/tmp/ckun_zip_${Date.now()}_${Math.random().toString(36).slice(2)}.py`
    await this.sftpWriteFile(sshSessionId, tmpScript, pyScript)
    let result: { stdout: string; stderr: string; exitCode: number | null }
    try {
      result = await this.execCommandWithExitCode(sshSessionId, `python3 ${shellQuoteRemote(tmpScript)}`, 300)
    } finally {
      // 清理临时脚本（无论成功与否）
      try { await this.sftpDelete(sshSessionId, tmpScript) } catch { /* ignore */ }
    }
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
    // M8 修复：用 python 脚本解压（写临时文件后执行，避免 shell 拼接注入风险）
    const pyScript = [
      'import os, sys, zipfile',
      `zip_path = ${JSON.stringify(remoteZipPath)}`,
      `dest = ${JSON.stringify(remoteDestDir)}`,
      'os.makedirs(dest, exist_ok=True)',
      'with zipfile.ZipFile(zip_path, "r") as zf:',
      '    zf.extractall(dest)',
      'print("ok")',
    ].join('\n')
    // 写临时文件后通过 python3 <file> 执行，完全绕过 shell 对脚本内容的解释
    const tmpScript = `/tmp/ckun_unzip_${Date.now()}_${Math.random().toString(36).slice(2)}.py`
    await this.sftpWriteFile(sshSessionId, tmpScript, pyScript)
    let result: { stdout: string; stderr: string; exitCode: number | null }
    try {
      result = await this.execCommandWithExitCode(sshSessionId, `python3 ${shellQuoteRemote(tmpScript)}`, 300)
    } finally {
      // 清理临时脚本（无论成功与否）
      try { await this.sftpDelete(sshSessionId, tmpScript) } catch { /* ignore */ }
    }
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
