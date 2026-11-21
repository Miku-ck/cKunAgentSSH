/**
 * SSH 传输工具层 —— 从 ssh-manager.ts 拆出（P2 单文件单域）。
 *
 * 这里只放**与 SSHManager 类状态无关**的传输基础设施：ssh2 懒加载、传输错误
 * 分类、SFTP Promise 化包装、停滞心跳、速率采样、读并行写顺序的流水线、
 * 独立传输连接池的数据结构与压缩判定。
 *
 * 拆分动机：ssh-manager.ts 曾 3121 行，工具层与 2500 行的连接/传输类混在一起，
 * 读代码时两类关注点互相干扰。搬出的符号由 ssh-manager.ts re-export，既有
 * 导入路径（含测试 `from './ssh-manager'`）不变。
 */
import type { Client, OpenMode, SFTPWrapper, Stats } from 'ssh2'

import { promises as fsPromises } from 'fs'
import { encodeTransferErrorName, isTransferRetryable, type TransferErrorCode } from '../../shared/transfer-error-core'

// ssh2 的 require 实测 ~1.3s（纯 JS+crypto 依赖链），静态导入会串在主进程模块
// 评估期、窗口 show 之前。挪为懒加载：setupSSHIPC 后台预热（用户点连接前已就位，
// 零额外等待），doConnect/testConnection 内 await 兜底预热未完成的首次连接。
type Ssh2ClientCtor = typeof import('ssh2').Client
let ssh2Loading: Promise<Ssh2ClientCtor> | null = null
export function loadSsh2(): Promise<Ssh2ClientCtor> {
  if (!ssh2Loading) {
    ssh2Loading = import('ssh2').then((m) => m.Client)
  }
  return ssh2Loading
}

// ════════════════════════════════════════════════
//   传输错误分类（断点续传 / 友好提示 / 自动重试依据）
//   码集与 retryable 判定在 shared/transfer-error-core.ts（S0-15：渲染端要按
//   同一口径决定是否自动续传，两处各抄一份必然漂移）
// ════════════════════════════════════════════════
export interface TransferError {
  code: TransferErrorCode
  message: string
  originalError?: Error
}

/** 根据原始错误归类为 TransferError，便于前端决定是否自动重试 */
function classifyTransferError(err: unknown, cancelled: boolean): TransferError {
  if (cancelled) {
    return { code: 'CANCELLED', message: '已取消' }
  }
  const msg = (err instanceof Error ? err.message : String(err)).toLowerCase()
  // SFTP 状态码 4 (SSH_FX_FAILURE)：ssh2 对该状态只报裸文本 "Failure"（无
  // "SSH_FX_FAILURE:" 前缀），原始 err.code 即数值 4。与下方 ssh_fx_failure 同归
  // NETWORK — 服务端通用错误多由瞬时负载引起，不可判 UNKNOWN 删 .part 丢进度
  if ((err as { code?: unknown } | null)?.code === 4) {
    return { code: 'NETWORK', message: '网络中断，已保存进度可续传', originalError: err as Error }
  }
  if (msg.includes('econnreset') || msg.includes('etimedout') || msg.includes('epipe')
      || msg.includes('socket hang up') || msg.includes('read epipe') || msg.includes('write epipe')
      // D13 修复：SSH_FX_FAILURE（SFTP 状态码 4，ssh2 报 "SSH_FX_FAILURE: ..."）是
      // 服务端通用错误，常由瞬时负载导致 — 原实现按 'failure' 子串一律判 PERMISSION
      // 不可重试，一抖动就删 .part 丢全部进度。归 NETWORK（可重试，保留 .part）
      || msg.includes('ssh_fx_failure')) {
    return { code: 'NETWORK', message: '网络中断，已保存进度可续传', originalError: err as Error }
  }
  if (msg.includes('enospc') || msg.includes('no space left') || msg.includes('disk full')) {
    return { code: 'DISK_FULL', message: '磁盘空间不足', originalError: err as Error }
  }
  // 仅明确的 permission denied 类状态判不可重试（不再匹配泛化的 'failure' 子串）
  if (msg.includes('eacces') || msg.includes('permission denied') || msg.includes('ssh_fx_permission_denied')) {
    return { code: 'PERMISSION', message: '权限不足或目标被占用', originalError: err as Error }
  }
  if (msg.includes('enoent') || msg.includes('no such file')) {
    return { code: 'NOT_FOUND', message: '文件不存在或已被删除', originalError: err as Error }
  }
  return { code: 'UNKNOWN', message: `传输失败: ${err instanceof Error ? err.message : String(err)}`, originalError: err as Error }
}

/**
 * 将 TransferError 包装为可抛出的 Error。
 *
 * S0-15：自定义 code/retryable 属性是「可能存活」的那条腿，name 腿才是本仓
 * 实证保真性最强的通道（normalizeIpcError 对 SSHError 就是这么双写的）。
 * 渲染端 describeTransferError 两条腿都读，任一存活即可恢复 code，retryable
 * 由 code 现推 —— 不再依赖那个可能已被 IPC 抹掉的布尔属性。
 */
function toTransferError(tErr: TransferError): Error {
  const e = new Error(tErr.message) as Error & { code?: string; retryable?: boolean }
  e.name = encodeTransferErrorName(tErr.code)
  e.code = tErr.code
  e.retryable = isTransferRetryable(tErr.code)
  return e
}

// ════════════════════════════════════════════════
//   SFTP Promise 化辅助函数
// ════════════════════════════════════════════════
function sftpStat(sftp: SFTPWrapper, path: string): Promise<Stats> {
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
  } catch (err) {
    const code = (err as NodeJS.ErrnoException)?.code
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

function sftpOpen(sftp: SFTPWrapper, path: string, flags: OpenMode): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    // ssh2 的 SFTPWrapper.open() 同时支持 OpenMode 字符串与 number，
    // 直接传字符串让 ssh2 内部按 SFTP OPEN_MODE 转换，避免手动数值映射错误。
    sftp.open(path, flags, (err, handle) => err ? reject(err) : resolve(handle))
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
//   （导出供回归测试直接驱动行为）
// ════════════════════════════════════════════════
export class TransferHeartbeat {
  private timer: NodeJS.Timeout | null = null
  private stalled = false

  /**
   * S5-34：停滞阈值支持两种口径。
   *
   * 固定阈值（原实现）：30s 内没有字节增长就判停滞。对 100MB 以下够用，但
   * 15GB 在慢链路上（100Mbps → 每 30s 只传 375KB）只要有一次网络抖动就会
   * 误判 STALLED 并中断，然后走自动重试 —— 用户看到的是「传着传着就断了」。
   *
   * 动态阈值（新增）：按「最近一段时间的平均速率」推算「多久没进展算异常」。
   * 速率采样窗口 60s，阈值 = max(速率 × 20s, 30s) 且上限 5min。含义是：
   *   - 正常速率 1MB/s → 20s 没进展才判停滞（比固定 30s 宽松）
   *   - 正常速率 10MB/s → 20s 没进展判停滞（同样宽松）
   *   - 刚起步速率未建立 → 兜底 30s（与原实现一致）
   *   - 极慢链路（10KB/s）→ 200s 阈值，仍受 5min 上限约束
   *
   * 动态口径需要调用方提供速率采样器（getRecentRateBytesPerSec）。不传则
   * 保持固定阈值，向后兼容。
   */
  start(
    intervalMs: number,
    stallThresholdMs: number,
    getLastProgress: () => number,
    onStall: () => void,
    getRecentRateBytesPerSec?: () => number,
  ): void {
    this.stop()
    this.stalled = false
    this.timer = setInterval(() => {
      const threshold = getRecentRateBytesPerSec
        ? this.dynamicThreshold(getRecentRateBytesPerSec())
        : stallThresholdMs
      if (Date.now() - getLastProgress() > threshold) {
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

  /**
   * 动态阈值：按「传满一个在途窗口（8 × 256KB = 2MB）所需时间」的 3 倍算，
   * 钳在 [30s, 5min]。
   *
   * 语义：正常传输时，2MB 应该在一个窗口周期内传完。如果 3 倍时间还没传完
   * 一个窗口，说明链路真的出问题了。
   *   - 100MB/s（8s 传 2MB）→ 24s，钳到 30s
   *   - 10MB/s（0.2s 传 2MB）→ 0.6s，钳到 30s
   *   - 1MB/s（2s 传 2MB）→ 6s，钳到 30s
   *   - 100KB/s（20s 传 2MB）→ 60s
   *   - 10KB/s（200s 传 2MB）→ 600s，钳到 300s
   *
   * 速率未建立（≤0）时返回下限 30s —— 与原固定阈值同口径，避免「刚起步就判死」。
   */
  private dynamicThreshold(rateBytesPerSec: number): number {
    if (!Number.isFinite(rateBytesPerSec) || rateBytesPerSec <= 0) return 30000
    const windowBytes = 8 * 256 * 1024 // 8 路 × 256KB = 在途窗口
    const windowMs = (windowBytes / rateBytesPerSec) * 1000
    return Math.min(300000, Math.max(30000, Math.round(windowMs * 3)))
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
 * S5-34：滑动窗口速率采样。
 *
 * 只保留 windowMs 内的 (时间戳, 累计字节数) 采样点，每次查询时先丢弃过期点，
 * 再用「窗口内首末两点」算平均速率。用累计值而非增量值：增量需要自己维护
 * 累加器并在查询时清零，多一处状态就多一处与传输循环不同步的机会。
 *
 * 内存：采样点按 chunk 完成写入（256KB 一块），60s 窗口在 10MB/s 下约 240 个点
 * （每个点 16 字节）≈ 4KB，可忽略。
 */
export class TransferRateSampler {
  private readonly windowMs: number
  private readonly points: Array<{ t: number; bytes: number }> = []

  constructor(windowMs: number) {
    this.windowMs = windowMs
  }

  /** 记一次字节数（累计值，不是增量）。 */
  record(bytes: number): void {
    const now = Date.now()
    // 同一毫秒内的多次记录只保留最后一个 —— 避免高频上报把采样表撑成噪声
    const last = this.points[this.points.length - 1]
    if (last && now - last.t < 10) { last.bytes = bytes; return }
    this.points.push({ t: now, bytes })
    this.prune(now)
  }

  /** 近 windowMs 的平均字节/秒；样本不足两个点时返回 0（速率未建立）。 */
  getRateBytesPerSec(): number {
    const now = Date.now()
    this.prune(now)
    if (this.points.length < 2) return 0
    const first = this.points[0]
    const last = this.points[this.points.length - 1]
    const spanMs = last.t - first.t
    if (spanMs < 100) return 0 // 时间跨度太短，算出的速率没有统计意义
    return ((last.bytes - first.bytes) / spanMs) * 1000
  }

  private prune(now: number): void {
    const cutoff = now - this.windowMs
    let i = 0
    while (i < this.points.length && this.points[i].t < cutoff) i++
    if (i > 0) this.points.splice(0, i)
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

/**
 * 独立传输连接（每传输一条专用 SSH 连接）。
 *
 * 传输提速动机：ssh2 的 SSH 通道窗口硬编码为 2 MB（Channel.js MAX_WINDOW），
 * 单条 SSH 连接任何时刻最多 2 MB 数据在途，速度上限 ≈ 2MB ÷ RTT。多个传输
 * 共用一条连接时瓜分同一个 2 MB 窗口，整体吞吐上不去。这里为传输单独维护
 * 一批 SSH 连接（每主机一批，每条同时只服务一个传输）：N 个并发传输 = N 条
 * 独立连接 = N × 2 MB 在途窗口，且与终端交互流量彻底解耦。
 *
 * 并发上限天然受渲染层并发门（maxConcurrent，默认 3、上限 8）约束 —— 每主机
 * 同时在途的传输数不超过它，故连接数不会无界增长。
 *
 * 生命周期：租出（leased=true）→ 归还（leased=false，延时空闲回收）；意外
 * 断开 dead 置位后下次 acquire 重建。不进重连体系（传输有 .part 续传）。
 */
export interface TransferConn {
  client: Client
  sftp: SFTPWrapper
  name: string
  /** 是否已租给某个在途传输（同一连接同时只服务一个传输，独占 2 MB 窗口） */
  leased: boolean
  /** 连接/子系统已死标记（对端关闭后置位，避免继续派发新传输） */
  dead: boolean
  /** 空闲回收定时器 */
  cleanupTimer: NodeJS.Timeout | null
  /** 本连接的 SSH 压缩开关（建连时按传输文件类型决定；池内复用按同值匹配，
   *  避免「传媒体拿到一条给文本建的压缩连接」互相错配） */
  compress: boolean
}

/** 单主机独立传输连接的软上限（超过则复用最久未用连接，退化为共享窗口而非无界建连） */
export const TRANSFER_CONN_PER_HOST_CAP = 8
/** 独立传输连接空闲回收超时（全部归还后保留多久供下一批复用） */
export const TRANSFER_CONN_IDLE_MS = 300000

/** 已压缩容器/媒体/二进制等不可再压的扩展名：对这类数据开 SSH zlib 是纯烧两端
 *  CPU（包体持平甚至反膨胀），独立传输连接按文件类型改为关闭压缩。未收录的
 *  扩展名按「可能可压缩」保持压缩 —— 保守方向，文本/代码/日志类的既有收益不动。
 *  注意不含 'ts'：TypeScript 源码是文本（MPEG 传输流极少出现在 SFTP 场景）。 */
const INCOMPRESSIBLE_TRANSFER_EXT = new Set([
  // 压缩容器与打包格式
  'zip', 'gz', 'bz2', 'xz', 'zst', 'lz4', '7z', 'rar', 'tar', 'tgz', 'tbz2', 'txz',
  'jar', 'war', 'apk', 'docx', 'xlsx', 'pptx', 'epub',
  // 图片
  'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'ico', 'tif', 'tiff', 'heic', 'avif',
  // 音视频
  'mp4', 'mkv', 'avi', 'mov', 'wmv', 'flv', 'webm', 'm4v', '3gp',
  'mp3', 'flac', 'ogg', 'opus', 'aac', 'wav', 'm4a', 'wma',
  // 文档/磁盘镜像/二进制/字体
  'pdf', 'iso', 'img', 'dmg', 'bin', 'exe', 'dll', 'so', 'dylib',
  'woff', 'woff2', 'ttf', 'otf',
])

/** 按路径扩展名判断独立传输连接是否启用 SSH 压缩 */
export function shouldCompressTransfer(path: string): boolean {
  const dot = path.lastIndexOf('.')
  if (dot < 0) return true
  const ext = path.slice(dot + 1).toLowerCase()
  if (ext.length === 0 || ext.length > 5) return true
  return !INCOMPRESSIBLE_TRANSFER_EXT.has(ext)
}

// 供 SSHManager 类使用的内部工具（不 re-export 给外部）
export {
  classifyTransferError,
  renameOverwrite,
  sftpClose,
  sftpOpen,
  sftpRead,
  sftpRename,
  sftpSetstatSize,
  sftpStat,
  sftpUnlink,
  sftpWrite,
  toTransferError,
  pipelineReadWrite,
}
