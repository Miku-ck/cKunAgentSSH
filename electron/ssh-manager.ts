/**
 * SSH Manager — ssh2 远程连接管理
 * 在 Electron 主进程中运行，管理 SSH 连接和 Shell 通道
 */
import { Client, ClientChannel } from 'ssh2'
import type { SFTPWrapper } from 'ssh2'
import { LS_COLORS_NO_BACKGROUND } from './utils/terminal-colors'
import { assertKeyFileSafe } from './utils/path-guard'
import { BrowserWindow, app } from 'electron'
import { SSHError, classifyRawError, toFriendlyZh } from './ssh-error'
import { readFileSync, statSync } from 'fs'
import { promises as fsPromises } from 'fs'
import { posix, join } from 'path'
import { createHash } from 'crypto'

/** exec 命令输出累积上限（1MB），超过后截断并在返回结果附提示，防止无界内存增长 */
const MAX_EXEC_OUTPUT_BYTES = 1024 * 1024

/** 断连修复：会话处于自动/手动重连期间，exec 等待会话重新入池的上限。
 *  等待仅在存在重连条目时触发；覆盖大多数前几秒内恢复的路径级抖动，
 *  超时/条目消失仍抛 SESSION_NOT_FOUND（重连随后会继续，无需阻塞 exec）。 */
const EXEC_RECONNECT_WAIT_MS = 10000

// ════════════════════════════════════════════════
//   传输错误分类（断点续传 / 友好提示 / 自动重试依据）
// ════════════════════════════════════════════════
type TransferErrorCode =
  | 'NETWORK' | 'DISK_FULL' | 'PERMISSION' | 'NOT_FOUND'
  | 'CANCELLED' | 'STALLED' | 'REMOTE_CHANGED' | 'RENAME_FAILED' | 'UNKNOWN'

interface TransferError {
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
  // SFTP 状态码 4 (SSH_FX_FAILURE)：ssh2 对该状态只报裸文本 "Failure"（无
  // "SSH_FX_FAILURE:" 前缀），原始 err.code 即数值 4。与下方 ssh_fx_failure 同归
  // NETWORK — 服务端通用错误多由瞬时负载引起，不可判 UNKNOWN 删 .part 丢进度
  if ((err as { code?: unknown } | null)?.code === 4) {
    return { code: 'NETWORK', message: '网络中断，已保存进度可续传', retryable: true, originalError: err as Error }
  }
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

function sftpSetstatSize(sftp: SFTPWrapper, path: string, size: number): Promise<void> {
  return new Promise((resolve, reject) => {
    sftp.setstat(path, { size }, (err) => err ? reject(err) : resolve())
  })
}

/**
 * 审查修复（P2）：覆盖语义 rename。
 * POSIX 的 rename 原子覆盖已存在目标；Windows 上目标已存在时抛 EPERM，
 * 跨设备（.part 与最终路径不同卷）抛 EXDEV — 原下载收尾直接用
 * fsPromises.rename，这两种情况都会失败进入通用 catch，被归类为不可
 * 重试并删除 .part，已下载完整的数据丢失。
 * 此处模拟 POSIX 行为：EPERM/EACCES 先删除目标再重试；EXDEV 降级为
 * copyFile + unlink（.part 数据完整，拷贝不丢数据）。
 */
async function renameOverwrite(src: string, dest: string): Promise<void> {
  try {
    await fsPromises.rename(src, dest)
    return
  } catch (err: any) {
    const code = err?.code as string | undefined
    if (code !== 'EPERM' && code !== 'EACCES' && code !== 'EXDEV') throw err
    try { await fsPromises.unlink(dest) } catch { /* 目标不存在，忽略 */ }
    if (code === 'EXDEV') {
      await fsPromises.copyFile(src, dest)
      await fsPromises.unlink(src)
    } else {
      await fsPromises.rename(src, dest)
    }
  }
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

interface SSHConnectionConfig {
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

/** 渲染层副本见 src/env.d.ts（跨 tsconfig 边界不能 import，与本模块是否导出无关） */
interface SSHTestResult {
  ok: boolean
  message: string
  latencyMs: number
}

interface SftpEntry {
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
  /** 重连修复：最新终端尺寸（连接时初始化、resize 时更新），重连建 shell 时复用 */
  cols?: number
  rows?: number
}

/**
 * 重连修复：自动重连状态（每个交互式 shell 会话一条，key 为会话 id）。
 * - 意外断开（网络抖动/服务端断连）时由 destroySession 触发，最多 12 次、
 *   退避 1/3/9/27s…封顶 30s（累计约 4~5 分钟窗口）；凭据由主进程自 SSHStore
 *   读取，渲染层无需传递
 * - 用户主动断开（关 tab/断开按钮）与删除连接配置会取消本条目
 */
interface ReconnectEntry {
  /** 连接配置 id — 重连时主进程据此从 SSHStore 自取凭据 */
  connectionId: string
  /** 重连建 shell 用的终端尺寸（连接时初始值，之后随 resize 更新） */
  cols: number
  rows: number
  /** 代数 — 取消/手动重启时 +1，使 in-flight 旧连接的结果作废（防僵尸连接/双连接） */
  generation: number
  /** 待执行的退避定时器（null = 无排程） */
  timer: NodeJS.Timeout | null
  /** 是否有一次重连连接正在进行中 */
  inFlight: boolean
}

/** 自动重连最大尝试次数 */
const RECONNECT_MAX_ATTEMPTS = 12
/** 单次退避延迟的上限（秒基数）—— 防止指数退避在长窗口内拖到数百秒 */
const RECONNECT_MAX_DELAY_MS = 30000
/**
 * 断连修复：重连退避 1s/3s/9s/27s…，单次封顶 30s，共 12 次=累计约 4~5 分钟窗前
 * 才 give-up。对齐开源方案（Tabby 等）：大多数路径级抖动在前几秒内恢复，
 * 拉长窗口让终端在网络恢复后自己续上，而不是短暂断网即判死。
 */
function reconnectDelayMs(attempt: number): number {
  return Math.min(1000 * Math.pow(3, attempt - 1), RECONNECT_MAX_DELAY_MS)
}

/**
 * 重连状态变化事件（主进程 → 渲染层，channel: ssh:reconnect）。
 * 渲染层类型副本见 src/env.d.ts（两个 tsconfig 程序互不包含，沿用
 * SSHConnectionRecord 的跨上下文复制模式）
 */
export interface SshReconnectStatus {
  id: string
  /** reconnecting=首轮排程（断开后自动/手动点击后）| retry=上一次失败后的重试排程 | success=重连成功 | gave-up=放弃 */
  phase: 'reconnecting' | 'retry' | 'success' | 'gave-up'
  /** 当前（reconnecting/retry）或最后一次（success/gave-up）尝试序号，1 起；gave-up + cancelled 时为 0（哨兵值） */
  attempt: number
  maxAttempts: number
  /** reconnecting/retry 阶段：下一次尝试前的退避等待（ms） */
  delayMs?: number
  /** gave-up 阶段：exhausted=3 次均失败 | config-missing=连接配置已删除 | cancelled=重连被取消（如断开后连接配置被删除，tab 仍在） */
  reason?: 'exhausted' | 'config-missing' | 'cancelled'
}

export class SSHManager {
  private sessions = new Map<string, SSHSession>()
  private sftpSessions = new Map<string, SFTPWrapper>()
  private transfers = new Map<string, () => void>()
  private nextId = 1
  private mainWindow: BrowserWindow | null = null
  // s1 修复：known_hosts 存储（host:port → sha256 指纹），TOFU 模型
  private knownHosts = new Map<string, string>()
  // E-1 修复：single-flight 加载 Promise — 并发连接共享同一次磁盘加载，
  // 消除"标志先置位、文件未读完"的竞态窗口（并发连接按空表 TOFU 并覆写磁盘表）
  private loadPromise: Promise<void> | null = null
  // E-1b 修复：串行化写入队列 — 每次保存排队到前一次完成之后，
  // 快照在实际写入时刻（then 回调内）构建，并发首连不再把磁盘表写成子集
  private savePromise: Promise<void> = Promise.resolve()
  // 重连修复：重连状态表（key = shell 会话 id）+ 配置读取器（main.ts 注入，
  // 重连时主进程自取已保存的连接配置与凭据，渲染层全程不参与）
  private reconnects = new Map<string, ReconnectEntry>()
  private configProvider: ((connectionId: string) => Promise<SSHConnectionConfig | null>) | null = null
  private onReconnected: ((connectionId: string) => void) | null = null

  /** 注入连接配置读取函数（main.ts 用 SSHStore.get 实现） */
  setConfigProvider(fn: (connectionId: string) => Promise<SSHConnectionConfig | null>): void {
    this.configProvider = fn
  }

  /** 重连成功回调（main.ts 据此更新 lastConnectedAt，与首连成功路径同语义） */
  setOnReconnected(fn: (connectionId: string) => void): void {
    this.onReconnected = fn
  }

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
   * E-1 修复：原实现用一个 `loaded` 布尔标志，且在 await readFile 完成前同步置位，
   * 并发连接在竞态窗口内按空表 TOFU，且各自的后续 save 以子集覆写磁盘 known_hosts。
   * 现在所有并发调用共享同一个 loadPromise：加载未完成前它非空即天然挡住重复触发，
   * 调用方 await 它即可保证读到已填充的表，无需再另设就绪标志。
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
   *
   * 重连修复：
   * 1) client 参数 — 重连会用同一 id 建立新 client，旧 client 的迟到 close
   *    事件必须识别为陈旧事件跳过，否则会误毁已建立的新会话；
   * 2) 断开来源判定 — 走到这里且 session 仍在池中，必然是"意外断开"
   *    （用户主动断开走 disconnect()/disconnectAll()，会先把 session 从池
   *    中删除，其迟到的 close 事件在此直接 no-op）。因此 session 存在 +
   *    有重连条目 = 意外断开，自动触发重连。
   */
  private destroySession(id: string, client?: Client): void {
    const session = this.sessions.get(id)
    if (!session) return
    // 陈旧事件守卫：session 的 client 已是新连接（重连已重建），跳过
    if (client && session.client !== client) return
    this.closeSftp(id)
    this.sessions.delete(id)
    if (session.closeNotified) return
    session.closeNotified = true
    const willReconnect = this.reconnects.has(id)
    this.safeSend('ssh:close', { id, willReconnect })
    if (willReconnect) {
      this.startAutoReconnect(id)
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
      // 断连修复：keepaliveCountMax 由 3 提升到 10 — 原值连续 3 次心跳无应答
      // （约 30~50s 网络抖动）即判死主动掐断，是"频繁自动断开"的直接诱因。
      // 10 次≈容忍 100s 无应答，短暂休眠/WiFi 漫游/NAT 空闲冻结不再触发断开。
      keepaliveCountMax: 10,
      // 启用 SSH 压缩：对文本/代码/日志类小文件有明显加速效果
      // （对已压缩的图片/视频/zip 等无收益但也不会显著变慢，ssh2 会自适应）
      compress: true,
      // s1 修复 + 审查 M-1：主机密钥验证（TOFU 模型），防止中间人攻击。
      // 每次连接构造独立闭包捕获本连接的 host:port，避免并发连接共享单槽位
      // 导致的跨主机串扰与 fail-open（见 verifyHostKey 注释）。
      hostVerifier: (key: Buffer) => this.verifyHostKey(`${config.host}:${config.port}`, key),
    }

    // 断连诊断：设置环境变量 SSH_DEBUG=1 时，把 ssh2 传输层日志打到主进程控制台，
    // 用于拿到"频繁自动断开"的确切断开原因（超时/对端 DISCONNECT/握手失败/RST 等）。
    // 平时关闭避免刷屏。
    if (process.env.SSH_DEBUG) {
      connectConfig.debug = (msg: string) => {
        const tag = config.id || `${config.username}@${config.host}:${config.port}`
        console.log(`[ssh2] ${tag} ${msg}`)
      }
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
        // P2 修复：原实现静默置 undefined → 落入笼统的"所有认证方法均失败"，
        // 用户无从得知真正原因。明确抛出，由 IPC 层透传给前端展示。
        throw new Error('未检测到 SSH Agent：请先运行 ssh-agent 并加载密钥（ssh-add），或设置 SSH_AUTH_SOCK 环境变量')
      }
    }

    return connectConfig
  }

  /** 建立 SSH 连接并创建 Shell 通道 */
  async connect(config: SSHConnectionConfig, cols: number, rows: number): Promise<{ id: string; name: string }> {
    const id = `ssh-${this.nextId++}`
    // 审查R2修复：doConnect 内部多返回 client 字段（供重连拆除用），此处剥离 —
    // Client 对象不可结构化克隆，泄漏给渲染进程 IPC 会报错
    const { client: _client, ...pub } = await this.doConnect(id, config, true, cols, rows)
    return pub
  }

  /** 建立 SSH 连接,不创建 Shell 通道(用于 SFTP) */
  async connectSftp(config: SSHConnectionConfig): Promise<{ id: string; name: string }> {
    const id = `sftp-${this.nextId++}`
    const { client: _client, ...pub } = await this.doConnect(id, config, false)
    return pub
  }

  private async doConnect(
    id: string,
    config: SSHConnectionConfig,
    withShell: boolean,
    cols?: number,
    rows?: number,
  ): Promise<{ id: string; name: string; client: Client }> {
    // 审查修复：SSH/SFTP 会话数上限 — 原实现无上限，断连清理失败或 LLM Agent
    // 批量触发 connect 时可无限累积会话（每个含 TCP 连接 + 事件监听器）
    if (this.sessions.size >= 64) {
      throw new SSHError('SESSION_LIMIT')
    }
    // s1 修复：加载 known_hosts（审查 M-1：验证上下文已由 buildConnectConfig
    // 的每连接闭包捕获，不再使用共享槽位）
    await this.loadKnownHosts()
    const client = new Client()
    const connectConfig = this.buildConnectConfig(config)
    // s3 修复：连接配置已提取后立即清除内存中的明文密码/passphrase
    config.password = undefined
    config.passphrase = undefined

    return new Promise((resolve, reject) => {
      const wrappedResolve = (val: { id: string; name: string; client: Client }) => { resolve(val) }
      const wrappedReject = (err: Error) => { reject(err) }

      client.on('ready', () => {
        if (!withShell) {
          const name = config.name || `${config.username}@${config.host}`
          this.sessions.set(id, { id, connectionId: config.id, client, stream: null, name, cwd: '' })
          // 审查R2修复：携带 client，供陈旧重连续体精确拆除自身连接（见 runReconnectAttempt）
          wrappedResolve({ id, name, client })
          return
        }

        // 创建 Shell 通道（注入 LS_COLORS 去掉 ANSI 背景色）
        client.shell(
          { term: 'xterm-256color', cols: cols || 80, rows: rows || 24 },
          { env: { LS_COLORS: LS_COLORS_NO_BACKGROUND } },
          (err, stream) => {
            if (err) {
              client.end()
              wrappedReject(new SSHError('EXEC_FAIL', { original: err }))
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
              // 重连修复：记录终端尺寸，重连建 shell 时复用（resize 会持续更新）
              cols: cols || 80,
              rows: rows || 24,
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
              console.warn(`[ssh] 会话 ${id} 交互 shell 通道已关闭`)
              this.destroySession(id, client)
              // 审查 M-6 修复：shell 通道关闭不等于 SSH 连接结束 — 原实现只删
              // session 并通知前端，底层 client socket 一直挂着（client 的 'close'
              // 事件只在连接级关闭时触发），远端断开后连接/文件句柄持续泄漏直到
              // TCP 自行超时。此处主动 end() 整个连接（已关闭时 end() 为 no-op）。
              try { client.end() } catch { /* ignore */ }
            })

            // 重连修复：为交互式 shell 会话登记重连上下文（SFTP 专用连接不登记）。
            // 意外断开时 destroySession 据此自动触发重连；用户主动断开
            // （disconnect/disconnectAll）会取消该条目，保证主动关闭绝不重连。
            // 无连接配置 id 的会话（理论仅程序内直连）无法自取凭据，不登记。
            // 注意：重连成功路径条目已存在 — 必须【原地更新】而非替换对象：
            // runReconnectAttempt 的 in-flight 续体按"条目引用 + 代数"识别同一轮，
            // 若此处替换为新对象，成功的重连会被误判为陈旧结果而被拆除。
            if (config.id) {
              const existingEntry = this.reconnects.get(id)
              if (existingEntry) {
                existingEntry.connectionId = config.id
                existingEntry.cols = cols || 80
                existingEntry.rows = rows || 24
                existingEntry.inFlight = false
              } else {
                this.reconnects.set(id, {
                  connectionId: config.id,
                  cols: cols || 80,
                  rows: rows || 24,
                  generation: 0,
                  timer: null,
                  inFlight: false,
                })
              }
            }

            wrappedResolve({ id, name, client })
          }
        )
      })

      client.on('error', (err) => {
        // D14 修复：error 不再删除 session、不单独通知 — 连接级 error 之后
        // 必然跟随 'close'（ssh2 语义），由 close 处理器统一清理 + 通知，
        // 保证 SFTP 专用连接（无 shell 流）中断后 UI 也能收到 ssh:close
        // v2 改进：用 SSHError.from 统一分类原始错误，前端可按 code 做差异化呈现
        // 修复：传入 authType —— ssh2 认证失败的错误消息不含方法名，按用户
        // 配置的认证方式归类，密码错才报 AUTH_PASSWORD（"密码认证失败"）
        // 而非泛化的 AUTH_UNKNOWN。password/passphrase 已在 buildConnectConfig
        // 后清除，但 authType 未被清除，此处可用
        wrappedReject(SSHError.from(err, undefined, config.authType))
        // 断连诊断：连接就绪后（exec/监控期间）的传输层错误，原实现因 wrappedReject
        // 早已 resolve 而无从观测。记入主进程控制台，用于定位"频繁自动断开"的根因。
        console.error(`[ssh] 会话 ${id} 连接级错误:`, err?.message || err, { name: config.name })
      })

      client.on('close', () => {
        // 断连诊断：记录连接级 close（在此并不会带原因，原因在上方 error 已打点）
        console.warn(`[ssh] 会话 ${id} 连接已关闭（${this.sessions.has(id) ? '意外' : '已移除'}）`)
        // D14 修复：close 统一清理 + 通知（destroySession 内部去重，恰好一次）
        // 重连修复：传入 client — 重连场景下旧 client 的迟到 close 事件据此识别为陈旧事件
        this.destroySession(id, client)
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
    // 重连修复：记住最新终端尺寸（session 与重连条目同步更新），
    // 重连建 shell 时以断开前的实际大小重建，避免回到 80x24
    if (session) {
      session.cols = cols
      session.rows = rows
      const entry = this.reconnects.get(id)
      if (entry) {
        entry.cols = cols
        entry.rows = rows
      }
    }
  }

  /**
   * 断连修复：取 exec 的目标会话。若会话瞬时缺失但存在重连条目（该会话正在
   * 自动/手动重连，会话 id 不变），则等待会话重新入池，避免重连窗口内的
   * ssh:exec（监控轮询 / Agent 工具调用）立即抛 SESSION_NOT_FOUND。
   * 等待上限 EXEC_RECONNECT_WAIT_MS；期间重连条目消失（取消/gave-up/主动断开）
   * 或超时仍无会话 → 返回 null（由调用方抛 SESSION_NOT_FOUND）。
   */
  private async resolveSessionForExec(sshSessionId: string): Promise<SSHSession | null> {
    if (this.sessions.has(sshSessionId)) return this.sessions.get(sshSessionId)!
    // 无重连条目（如 SFTP 专用连接、用户主动断开）：不等待，立即失败
    if (!this.reconnects.has(sshSessionId)) return null
    const deadline = Date.now() + EXEC_RECONNECT_WAIT_MS
    while (this.reconnects.has(sshSessionId)) {
      const session = this.sessions.get(sshSessionId)
      if (session) return session
      // 审查R2修复：重连次数耗尽（exhausted）后条目为手动重连而永久保留，
      // 但此刻既无退避排程（timer=null）也无进行中连接（inFlight=false）——
      // 原实现只看 has(id) 会让每次 exec 白等满 10s 才报 SESSION_NOT_FOUND。
      // 无进行中重连即立即失败（用户点击手动重连后 timer/inFlight 恢复，仍会等待）。
      const entry = this.reconnects.get(sshSessionId)
      if (entry && !entry.timer && !entry.inFlight) return null
      if (Date.now() >= deadline) return null
      await new Promise<void>((r) => setTimeout(r, 250))
    }
    return null
  }

  /** 在已建立的 SSH 连接上执行一次性命令，返回 stdout / stderr / exitCode */
  async execCommandWithExitCode(
    sshSessionId: string,
    command: string,
    timeout = 30,
  ): Promise<{ stdout: string; stderr: string; exitCode: number | null }> {
    const session = await this.resolveSessionForExec(sshSessionId)
    if (!session) throw new SSHError('SESSION_NOT_FOUND')

    return new Promise((resolve, reject) => {
      let settled = false
      let streamRef: ClientChannel | null = null

      const timer = setTimeout(() => {
        if (!settled) {
          settled = true
          try { streamRef?.close() } catch { /* ignore */ }
          // 结构化错误：超时 → EXEC_TIMEOUT，经 normalizeIpcError 序列化为
          // __sshError transport 透传，前端 extractSSHError 据此还原友好文案
          reject(new SSHError('EXEC_TIMEOUT', { detail: `${timeout}秒` }))
        }
      }, timeout * 1000)

      session.client.exec(command, (err, stream) => {
        if (err) {
          clearTimeout(timer)
          if (!settled) {
            settled = true
            // 分类原始错误（连接断开/重置等），不再手抛通用英文提示
            reject(SSHError.from(err))
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
            // 通道级错误（连接断开/重置）→ 结构化分类，前端友好呈现
            reject(SSHError.from(err))
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

  /** 断开连接（用户主动：先取消所有待执行重连，不触发重连、不发 ssh:close） */
  disconnect(id: string): void {
    // 重连修复：主动断开取消该会话的重连（清 timer + 代数 +1 使 in-flight 旧连接作废）
    this.cancelReconnect(id)
    this.teardownSession(id)
  }

  /** 清理所有会话（应用退出/关全部：同样取消所有待执行重连，防退出后定时器空转） */
  disconnectAll(): void {
    for (const id of this.sftpSessions.keys()) {
      this.closeSftp(id)
    }
    for (const id of Array.from(this.reconnects.keys())) {
      this.cancelReconnect(id)
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

  // ════════════════════════════════════════════════
  //   断线重连（自动 + 手动）
  //   修复动机：远程 shell 意外断开（网络抖动/服务端断连）时原实现仅通知
  //   前端关 tab，无任何重连。现在：意外断开自动最多 12 次、退避 1s/3s/9s/27s…封顶 30s，
  //   复用已保存连接配置（主进程自取凭据）与完整 doConnect 路径
  //   （新 client + 新 shell + OSC 7 注入，会话 id 不变，前端 scrollback 保留）；
  //   用户主动断开/关 tab/删除连接配置会取消所有待执行重连，不产生僵尸连接。
  // ════════════════════════════════════════════════

  /** 自动重连入口：意外断开时由 destroySession 触发 */
  private startAutoReconnect(id: string): void {
    this.beginReconnect(id)
  }

  /**
   * 手动重连：用户在终端状态区点击「重连」— 重置计数，重新走完整退避循环
   * （最多 12 次、1s/3s/9s/27s…封顶 30s）。cols/rows 由渲染层传入当前终端实际尺寸。
   */
  reconnect(id: string, cols?: number, rows?: number): void {
    const entry = this.reconnects.get(id)
    if (!entry) throw new SSHError('SESSION_NOT_FOUND')
    // 会话仍存活（防御：按钮只应在断开后出现）：无需重连
    if (this.sessions.has(id)) return
    if (typeof cols === 'number' && cols > 0) entry.cols = cols
    if (typeof rows === 'number' && rows > 0) entry.rows = rows
    this.beginReconnect(id)
  }

  /** 开始一轮重连循环（自动/手动共用）：作废旧 in-flight 连接 + 排程第 1 次尝试 */
  private beginReconnect(id: string): void {
    const entry = this.reconnects.get(id)
    if (!entry) return
    // 代数 +1：任何进行中的旧连接完成后结果作废（防止用户重启重连期间
    // 旧连接恰好成功造成双连接/僵尸连接）
    entry.generation++
    if (entry.timer) {
      clearTimeout(entry.timer)
      entry.timer = null
    }
    entry.inFlight = false
    this.scheduleReconnectAttempt(id, 1)
  }

  /**
   * 排程第 N 次尝试：先通知前端（驱动终端系统行 + 状态区 spinner），
   * 再按退避延迟建立连接。仅对 N ∈ [1, max] 调用，超限由调用方 give-up。
   */
  private scheduleReconnectAttempt(id: string, attempt: number): void {
    const entry = this.reconnects.get(id)
    if (!entry) return
    // 防御：等待期间会话已被其他方式重建（不应发生），不再重连
    if (this.sessions.has(id)) return
    const delayMs = reconnectDelayMs(attempt)
    this.safeSend('ssh:reconnect', {
      id,
      // attempt 1 = 首轮（断开后自动 / 手动点击后）；attempt > 1 = 上一次失败后的重试
      phase: attempt === 1 ? 'reconnecting' : 'retry',
      attempt,
      maxAttempts: RECONNECT_MAX_ATTEMPTS,
      delayMs,
    })
    entry.timer = setTimeout(() => {
      entry.timer = null
      void this.runReconnectAttempt(id, attempt)
    }, delayMs)
  }

  /** 执行一次重连尝试：取配置 → 复用 doConnect（同 id）→ 通知结果 */
  private async runReconnectAttempt(id: string, attempt: number): Promise<void> {
    const entry = this.reconnects.get(id)
    if (!entry || this.sessions.has(id)) return
    const gen = entry.generation
    entry.inFlight = true

    // 主进程自取已保存的连接配置（safeStorage 解密凭据），渲染层全程不参与
    let config: SSHConnectionConfig | null = null
    if (this.configProvider) {
      try {
        config = await this.configProvider(entry.connectionId)
      } catch (e) {
        console.warn('[ssh] 重连读取连接配置失败:', e)
      }
    }
    // await 期间条目可能已被取消（关 tab/删连接）或手动重启：本轮结果作废
    if (!this.isReconnectCurrent(id, entry, gen)) return
    if (!config) {
      // 连接配置已被删除：重连失去意义（手动重试也无凭据），移除条目并通知前端
      this.reconnects.delete(id)
      this.safeSend('ssh:reconnect', {
        id, phase: 'gave-up', attempt, maxAttempts: RECONNECT_MAX_ATTEMPTS, reason: 'config-missing',
      })
      return
    }

    // 审查R2修复：持有本轮 doConnect 建立的 client — 陈旧分支拆除时按 owner 身份
    // 校验，避免关掉新代重连刚建好的同 id 会话
    let conn: { client: Client } | null = null
    try {
      // 复用现有连接路径（不复制逻辑）：同一会话 id（前端 xterm 实例/scrollback
      // 监听不变），doConnect 内部完成新 Client + 新 shell 通道 + OSC 7 cwd 注入
      conn = await this.doConnect(id, config, true, entry.cols, entry.rows)
    } catch (err) {
      if (!this.isReconnectCurrent(id, entry, gen)) return
      entry.inFlight = false
      console.warn(`[ssh] 重连第 ${attempt}/${RECONNECT_MAX_ATTEMPTS} 次失败:`, err instanceof Error ? err.message : err)
      if (attempt >= RECONNECT_MAX_ATTEMPTS) {
        // 全部尝试失败：通知前端显示手动「重连」按钮
        this.safeSend('ssh:reconnect', {
          id, phase: 'gave-up', attempt, maxAttempts: RECONNECT_MAX_ATTEMPTS, reason: 'exhausted',
        })
      } else {
        this.scheduleReconnectAttempt(id, attempt + 1)
      }
      return
    }

    // 陈旧成功：连接期间条目已被取消/手动重启（用户操作覆盖了本轮）—
    // 只拆除刚建立的连接。注意不能走 disconnect()（会连带取消新一轮的状态），
    // 也不发 ssh:close（前端 tab 由新一轮重连接管）
    if (!this.isReconnectCurrent(id, entry, gen)) {
      // 审查R2修复：传入本轮 client（doConnect 成功即 conn 非空）——若池中同 id
      // 会话已被新代重连替换，按身份守卫跳过拆除，不再误关新会话
      this.teardownSession(id, conn?.client)
      // 条目对象被替换（取消删除后由 doConnect 重新登记）= 悬挂条目，清理；
      // 同一对象但代数变化 = 手动重启（新一轮仍有效，原地更新保证对象不变），保留
      if (this.reconnects.get(id) !== entry) {
        this.reconnects.delete(id)
      }
      return
    }
    entry.inFlight = false
    this.safeSend('ssh:reconnect', { id, phase: 'success', attempt, maxAttempts: RECONNECT_MAX_ATTEMPTS })
    // 重连成功后更新最后连接时间（与首连成功路径同语义）
    try { this.onReconnected?.(entry.connectionId) } catch { /* ignore */ }
  }

  /** in-flight 连接完成时校验：重连条目仍是开始时那条且代数未变（未被取消/重启） */
  private isReconnectCurrent(id: string, entry: ReconnectEntry, gen: number): boolean {
    return this.reconnects.get(id) === entry && entry.generation === gen
  }

  /** 取消某会话的重连：清退避 timer + 代数 +1 作废旧 in-flight 连接 + 移除条目 */
  private cancelReconnect(id: string): void {
    const entry = this.reconnects.get(id)
    if (!entry) return
    // 取消时确有重连活动（退避等待中或连接在途）才通知前端 —
    // 典型场景：会话断开后用户删除了连接配置（tab 仍在，状态条须停止转圈）；
    // 常规关 tab 路径组件随即卸载，事件无接收者，天然无害
    const hadPending = !!entry.timer || entry.inFlight
    if (entry.timer) {
      clearTimeout(entry.timer)
      entry.timer = null
    }
    entry.generation++
    entry.inFlight = false
    this.reconnects.delete(id)
    if (hadPending) {
      this.safeSend('ssh:reconnect', {
        id, phase: 'gave-up', attempt: 0, maxAttempts: RECONNECT_MAX_ATTEMPTS, reason: 'cancelled',
      })
    }
  }

  /** 取消指定连接配置名下所有会话的重连（删除连接配置时由 main.ts 调用） */
  cancelReconnectsByConnection(connectionId: string): void {
    for (const [id, entry] of this.reconnects) {
      if (entry.connectionId === connectionId) this.cancelReconnect(id)
    }
  }

  /**
   * 底层会话拆除：关闭 shell 流与连接、移出会话池。
   * 不发 ssh:close、不动重连状态 — 供用户主动断开与陈旧重连连接拆除复用。
   *
   * 审查R2修复：新增可选 ownerClient 身份参数（参照 destroySession 的既有同款
   * 守卫）— 陈旧重连续体拆除时传入本轮连接的 client，池中会话已被新代替换
   * 时跳过，避免误关新代刚建好的同 id 会话；不传（用户主动断开）保持原行为。
   */
  private teardownSession(id: string, ownerClient?: Client): void {
    const session = this.sessions.get(id)
    if (!session) return
    // 陈旧事件守卫：session 的 client 已属其他连接（新代重连已重建），跳过
    if (ownerClient && session.client !== ownerClient) return
    this.closeSftp(id)
    try { session.stream?.close() } catch { /* ignore */ }
    try { session.client.end() } catch { /* ignore */ }
    this.sessions.delete(id)
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
        // v2 改进：统一使用 classifyRawError + toFriendlyZh 分类错误
        // 修复：传入 authType，认证失败时按配置的认证方式给出精确文案
        const code = classifyRawError(err, config.authType)
        done(false, toFriendlyZh(code, err.message))
      })
      client.on('close', () => done(false, '连接已关闭'))

      try {
        client.connect(connectConfig)
      } catch (err: any) {
        const code = classifyRawError(err)
        done(false, toFriendlyZh(code, err?.message))
      }

      // 兜底超时(比 readyTimeout 多 2s)
      // s6 修复：保存 timerId 以便连接提前完成时清除，避免无谓等待
      const fallbackTimer = setTimeout(() => done(false, toFriendlyZh('CONN_TIMEOUT')), (config.readyTimeout ?? 15000) + 2000)
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
if (!sftp) throw new SSHError('SFTP_SESSION_REQUIRED')
return sftp
}

  /** 复用已建立的 SSH Client,打开 SFTP 子系统 */
async openSftp(sshSessionId: string): Promise<void> {
const session = this.sessions.get(sshSessionId)
if (!session) throw new SSHError('SESSION_NOT_FOUND')
if (this.sftpSessions.has(sshSessionId)) return
await new Promise<void>((resolve, reject) => {
session.client.sftp((err, sftp) => {
if (err) return reject(new SSHError('SFTP_INIT_FAIL', { original: err }))
        sftp.on('close', () => {
          // 陈旧 close 守卫：意外断开→重连复用同 id 后，旧通道迟到的 close 若无条件
          // delete 会误删新 wrapper（面板随即 requireSftp 抛 SESSION_NOT_FOUND）。
          // 仅当映射仍指向本次 sftp 才清理并通知，与 D14 的 client 身份守卫同源。
          if (this.sftpSessions.get(sshSessionId) !== sftp) return
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
            if (!err) return resolve()
            // 审查修复（P2）：原实现以 err.message.includes('exists') 判定"并发已创建"，
            // 但 OpenSSH SFTP 服务器对已存在路径返回 SSH_FX_FAILURE（通用错误，
            // 无 'exists' 字样）→ 判定落空，目录上传/远程解压在并发或
            // 部分已存在的父目录场景下误报失败。改为失败后重新 stat：
            // 路径现已存在即视为创建成功（幂等语义），否则抛出真实错误。
            sftpStat(sftp, current)
              .then(() => resolve())
              .catch(() => reject(err))
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
        // 停滞即停本传输写循环（上传并发 worker 的截断竞态防护，下载同形）：
        // 渲染层按 STALLED 自动续传、新传输对同一 .part 重新校验续写；若旧
        // worker 仍在跑，其失败/收尾时的 .part 截断会越过新传输已写范围
        // （网络差 → stall 后旧写入更易出错 → 截断把新写入删成洞 → rename
        // 出损坏文件）。停后本传输走 CANCELLED 收尾，stalled=true 使 catch
        // 按 STALLED 分类（retryable、保留 .part），续传不受影响。
        cancelFn()
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
        // 并发写：各 chunk 写偏移互不重叠，可安全并发（M-5 的"顺序追加写"约束
        // 是为续传无洞，这里改为失败时截断回连续写入边界来保证）。原实现写循环
        // 串行 await，同时只有一个 256KB 块在途，吞吐被 RTT 锁死。
        // 原语义保留：M-5 杜绝"越过 EOF 的零填充 + 中断后续传跳过零块"导致的
        // 静默损坏 — 失败/取消时 .part 截断回连续完成边界，续传不踩洞
        let firstError: Error | null = null
        let contiguousIdx = 0 // 从 startOffset 起连续完成的 chunk 数
        const doneSet = new Set<number>()
        let nextIdx = 0
        const worker = async (): Promise<void> => {
          while (!firstError && !cancelled) {
            const i = nextIdx++
            if (i >= chunks.length) return
            const chunk = chunks[i]
            try {
              const buf = Buffer.alloc(chunk.length)
              const { bytesRead } = await uploadLocalFd.read(buf, 0, chunk.length, chunk.offset)
              if (bytesRead !== chunk.length) {
                throw new Error(`本地读取不完整: 期望 ${chunk.length}, 实际 ${bytesRead}`)
              }
              await sftpWrite(sftp, uploadRemoteHandle, buf, chunk.offset)
            } catch (err) {
              firstError = firstError ?? (err instanceof Error ? err : new Error(String(err)))
              return
            }
            doneSet.add(i)
            // 进度按连续边界推进汇报（乱序完成的超前 chunk 不计入），保证
            // transferred 不超过失败/取消时实际保住的连续字节数，避免进度倒挂
            while (doneSet.has(contiguousIdx)) {
              doneSet.delete(contiguousIdx)
              transferred += chunks[contiguousIdx].length
              contiguousIdx++
              lastProgress = Date.now()
              reportProgress()
            }
          }
        }
        await Promise.all(Array.from({ length: Math.min(CONCURRENCY, chunks.length) }, () => worker()))

        // 失败或用户取消：.part 可能留有越过未写区的洞，截断回连续写入边界
        // （startOffset + 已连续完成字节数），保证后续续传顺序写不踩洞。
        // 停滞（stalled）路径例外：stallReject 已 reject IPC，渲染层可能立即用
        // 同一 .part 启动续传传输，此处迟到的 setstat 会把新传输已写范围截小，
        // 反而制造洞 — 跳过截断，由续传侧的 4KB 校验 + 顺序写兜底
        if (firstError || (cancelled && !stalled)) {
          const boundary = contiguousIdx >= chunks.length
            ? total
            : (contiguousIdx === 0 ? startOffset : chunks[contiguousIdx].offset)
          if (boundary < total) {
            let truncated = false
            try {
              await sftpSetstatSize(sftp, partPath, boundary)
              truncated = true
            } catch { /* 截断失败走下方兜底 */ }
            if (!truncated) {
              // P1 兜底：截断失败则 .part 含洞且不可信，续传按 partStat.size
              // 起点会跳过零填充区导致静默损坏 — 删除 .part 让重试从头传，
              // 宁可丢进度不可丢数据（删除也失败时连接多半已不可用，重试自会报错）
              try { await sftpUnlink(sftp, partPath) } catch { /* ignore */ }
            }
          }
        }
        if (firstError) throw firstError
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
        // 停滞即停本传输写循环（上传并发 worker 的截断竞态防护，下载同形）：
        // 渲染层按 STALLED 自动续传、新传输对同一 .part 重新校验续写；若旧
        // worker 仍在跑，其失败/收尾时的 .part 截断会越过新传输已写范围
        // （网络差 → stall 后旧写入更易出错 → 截断把新写入删成洞 → rename
        // 出损坏文件）。停后本传输走 CANCELLED 收尾，stalled=true 使 catch
        // 按 STALLED 分类（retryable、保留 .part），续传不受影响。
        cancelFn()
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
    // 审查修复（P2）：标记数据是否已全部写入（rename 之前）。
    // 为 true 后任何异常都属 rename 阶段 — .part 是完整数据，须保留供重试
    let downloadComplete = false

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
          // 审查修复（P2）：覆盖语义 rename，避免 Windows 目标已存在 EPERM
          downloadComplete = true
          await renameOverwrite(partPath, localPath)
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

      // 7. 完成：rename .part → localPath（覆盖语义，见 renameOverwrite）
      // 审查修复（P2）：先标记数据已全部写入 — rename 失败时据此保留 .part
      downloadComplete = true
      await renameOverwrite(partPath, localPath)
      sendProgress({ transferred: total, total })
    } catch (err) {
      // 审查修复（P2）：rename 阶段（数据已完整写入）失败 — .part 是完整
      // 数据，原实现落入通用 catch 被判不可重试并删 .part，已下载数据丢失。
      // 现在保留 .part 并归类 RENAME_FAILED（retryable），重试可续传完成
      let tErr: TransferError
      if (downloadComplete && !heartbeat.isStalled()) {
        tErr = {
          code: 'RENAME_FAILED',
          message: `下载已完成，但重命名为最终文件失败：${err instanceof Error ? err.message : String(err)}。请重试下载（数据已保留为 .part）`,
          retryable: true,
          originalError: err as Error,
        }
      } else {
        tErr = heartbeat.isStalled()
          ? { code: 'STALLED' as const, message: '传输停滞，网络可能不稳定', retryable: true }
          : classifyTransferError(err, cancelled)
      }
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
      // 审查R2修复：zip -r 对已存在的归档是增量 update —— "覆盖打包"会残留旧条目，
      // 且失败清理（sftpDelete）会把被 update 修改过的旧归档整体删掉。打包前先删除
      // 已存在的目标，与下方 Python 回退分支 zipfile "w" 截断语义对齐；
      // sftp 未开/删除失败时静默跳过，保持原行为交由 zip 处理。
      try {
        if (await this.sftpExists(sshSessionId, remoteZipPath)) {
          await sftpUnlink(this.requireSftp(sshSessionId), remoteZipPath)
        }
      } catch { /* ignore */ }
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
