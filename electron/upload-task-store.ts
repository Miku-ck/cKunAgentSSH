/**
 * UploadTaskStore — SFTP 上传记录的权威持久化（主进程）
 *
 * 为什么由主进程写而不是渲染层：上传的真实生命周期（.part、进度、rename）全在
 * SSHManager，渲染进程可能重载而传输还在跑。两个写者会互相覆盖，且渲染层重启后
 * 根本不知道主进程手里的进度。记录放这里，渲染层只读展示。
 *
 * 状态口径（与 shared/upload-task-core.ts 同一份表）：
 *   pending/running → 进程未走完，启动时归一化为 paused + interrupted（手动继续）
 *   done/error/cancelled → 终态，只有用户显式操作（删除/清空/重试）才会再改
 *
 * 写盘：内存态是唯一读写源。普通变更用 1s trailing + 5s max 防抖合并（一次千文件
 * 目录上传会触发上千次状态迁移，逐次全量写是 O(N²)）；用户显式操作立即 flush。
 * 按产品决策不做自动裁剪，改用紧凑 JSON 控制体积。
 *
 * 读到损坏/不可读的文件时降级为「只读内存」并禁用写：宁可本次会话不记录，也不能
 * 用空数组覆写用户全部历史（JsonFileStore 的 E-2 口径）。
 */
import { stat } from 'fs/promises'
import {
  basenameAny,
  isTerminalUploadStatus,
  isValidUploadTaskRecord,
  restoredStatusOf,
  type RegisteredUploadTask,
  type UploadTaskMeta,
  type UploadTaskRecord,
} from '../shared/upload-task-core'
import {
  encodeTransferErrorName,
  isTransferRetryable,
  parseTransferErrorCode,
  type TransferErrorCode,
} from '../shared/transfer-error-core'
import { JsonFileStore } from './utils/json-file-store'

/** 普通状态变更的合并窗口；显式操作（注册/暂停/删除/清空）走 flushNow 不受此限 */
const FLUSH_DEBOUNCE_MS = 1000
/** 持续有进度事件时，最多攒这么久必须落一次盘 */
const FLUSH_MAX_WAIT_MS = 5000

export interface TrackUploadOptions {
  /** 源文件已变、不能续传时清理远端 `.part`。失败只告警：清理是防呆，不阻断报错。 */
  discardPart?: () => Promise<void>
}

function transferError(code: TransferErrorCode, message: string): Error {
  const e = new Error(message) as Error & { code?: string; retryable?: boolean }
  e.name = encodeTransferErrorName(code)
  e.code = code
  e.retryable = isTransferRetryable(code)
  return e
}

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

export class UploadTaskStore {
  private readonly file: JsonFileStore<UploadTaskRecord>
  private readonly now: () => number
  /** 内存态是唯一读写源；磁盘只是它的快照 */
  private records: UploadTaskRecord[] = []
  /** id → 记录。进度事件是 100ms 级热路径，不能每来一条就全表 find */
  private byId = new Map<string, UploadTaskRecord>()
  private loadPromise: Promise<void> | null = null
  private loaded = false
  /** 读盘失败（非「文件不存在」）后禁止写：空内存覆盖磁盘会抹掉全部历史 */
  private writeDisabled = false
  /** 退出中：运行中的终态不再回写，磁盘保留 running，下次启动归一化为「已中断」 */
  private shuttingDown = false
  private flushTimer: ReturnType<typeof setTimeout> | null = null
  private maxWaitTimer: ReturnType<typeof setTimeout> | null = null

  constructor(path: string, now: () => number = Date.now) {
    this.now = now
    this.file = new JsonFileStore<UploadTaskRecord>({
      path,
      logTag: '[UploadTaskStore]',
      label: '上传任务记录',
      isValidRecord: isValidUploadTaskRecord,
      compact: true,
    })
  }

  init(): Promise<void> {
    if (!this.loadPromise) this.loadPromise = this.doInit()
    return this.loadPromise
  }

  private async doInit(): Promise<void> {
    try {
      this.records = await this.file.readAll()
    } catch (e) {
      console.error('[UploadTaskStore] 读取上传记录失败，本次会话禁用记录写入:', e)
      this.records = []
      this.writeDisabled = true
      this.loaded = true
      return
    }
    this.rebuildIndex()
    this.loaded = true

    const at = this.now()
    let changed = false
    for (const r of this.records) {
      const next = restoredStatusOf(r)
      if (r.status === next.status && Boolean(r.interrupted) === next.interrupted) continue
      r.status = next.status
      if (next.interrupted) r.interrupted = true
      else delete r.interrupted
      r.updatedAt = at
      changed = true
    }
    if (changed) await this.flushNow()
  }

  private rebuildIndex(): void {
    this.byId.clear()
    for (const r of this.records) this.byId.set(r.id, r)
  }

  /**
   * 入队时先把整批任务登记为 pending：崩溃后「还没开始传」的文件也在历史里，
   * 否则用户只看到一条断点，实际队列里其余文件已被遗忘。
   */
  async registerBatch(
    connectionId: string,
    tasks: readonly RegisteredUploadTask[],
  ): Promise<void> {
    await this.init()
    const at = this.now()
    let added = false
    for (const t of tasks) {
      if (this.byId.has(t.id)) continue
      const record: UploadTaskRecord = {
        id: t.id,
        connectionId,
        localPath: t.localPath,
        remotePath: t.remotePath,
        name: t.name || basenameAny(t.localPath),
        total: Number.isFinite(t.total) && t.total > 0 ? t.total : 0,
        transferred: 0,
        status: 'pending',
        groupId: t.groupId,
        groupLabel: t.groupLabel,
        createdAt: at,
        updatedAt: at,
      }
      this.records.push(record)
      this.byId.set(record.id, record)
      added = true
    }
    if (added) await this.flushNow()
  }

  /**
   * 包住一次真实上传：先落 running 再执行，终态按规则回写，原始错误始终重新抛出
   * （渲染层的自动续传判据依赖 code/name，不能被这里吞掉或改写）。
   */
  async trackUpload(
    connectionId: string,
    id: string,
    localPath: string,
    remotePath: string,
    meta: UploadTaskMeta | undefined,
    run: () => Promise<void>,
    opts: TrackUploadOptions = {},
  ): Promise<void> {
    await this.init()

    let rec = this.byId.get(id)
    if (!rec) {
      // 入队注册失败 / 记录被删后同 id 重入的兜底：只补一条最小记录
      rec = {
        id,
        connectionId,
        localPath,
        remotePath,
        name: meta?.name || basenameAny(localPath),
        total: meta?.size && meta.size > 0 ? meta.size : 0,
        transferred: 0,
        status: 'pending',
        groupId: meta?.groupId,
        groupLabel: meta?.groupLabel,
        createdAt: this.now(),
        updatedAt: this.now(),
      }
      this.records.push(rec)
      this.byId.set(id, rec)
    } else {
      rec.localPath = localPath
      rec.remotePath = remotePath
      if (meta?.name) rec.name = meta.name
      if (!rec.groupId && meta?.groupId) rec.groupId = meta.groupId
      if (!rec.groupLabel && meta?.groupLabel) rec.groupLabel = meta.groupLabel
    }

    // 续传前的一致性检查：上次记录的总大小是断点的前提。本地文件被改过还接着传，
    // 会把新旧内容拼成一个远端文件 —— 这种损坏比「重新传一次」昂贵得多。
    if (meta?.resume && rec.total > 0) {
      let localSize: number | null = null
      try {
        localSize = (await stat(localPath)).size
      } catch {
        localSize = null
      }
      if (localSize !== rec.total) {
        try {
          await opts.discardPart?.()
        } catch (e) {
          console.warn('[UploadTaskStore] 清理远端 .part 失败:', e)
        }
        rec.status = 'error'
        rec.errorCode = 'REMOTE_CHANGED'
        rec.error = '源文件已变化，无法断点续传，请重新上传'
        delete rec.interrupted
        rec.updatedAt = this.now()
        await this.flushNow()
        throw transferError('REMOTE_CHANGED', rec.error)
      }
    }

    if (meta?.size !== undefined && Number.isFinite(meta.size) && meta.size >= 0) {
      rec.total = meta.size
    }
    rec.status = 'running'
    delete rec.interrupted
    delete rec.error
    delete rec.errorCode
    rec.updatedAt = this.now()
    // 先落盘再开传：此刻崩溃，磁盘上留下 running，下次启动变成可继续
    await this.flushNow()

    try {
      await run()
    } catch (err) {
      this.finishFailure(id, err)
      await this.flushNow()
      throw err
    }

    const cur = this.byId.get(id)
    // 运行期间被暂停/取消/删除：终态由用户动作定义，成功落地不得把它改回 done
    if (!cur || cur.status !== 'running') return
    cur.status = 'done'
    if (cur.total > 0) cur.transferred = cur.total
    delete cur.interrupted
    delete cur.error
    delete cur.errorCode
    cur.updatedAt = this.now()
    await this.flushNow()
  }

  /** 失败优先级：记录已删 > 已暂停 > 已终态 > 退出中 > CANCELLED/error */
  private finishFailure(id: string, err: unknown): void {
    const cur = this.byId.get(id)
    if (!cur || cur.status !== 'running') return
    // 退出中保留 running，交给下次启动统一归一化（磁盘上的真相是「没走完」）
    if (this.shuttingDown) return
    const code = parseTransferErrorCode((err as { code?: unknown })?.code)
      ?? parseTransferErrorCode((err as { name?: unknown })?.name)
      ?? 'UNKNOWN'
    cur.status = code === 'CANCELLED' ? 'cancelled' : 'error'
    delete cur.interrupted
    if (cur.status === 'error') {
      cur.errorCode = code
      cur.error = messageOf(err)
    } else {
      delete cur.errorCode
      delete cur.error
    }
    cur.updatedAt = this.now()
  }

  /** 进度只改内存 + 合并写；真实续传点始终来自远端 `.part`，这里丢一两条无碍 */
  noteProgress(id: string, transferred: number, total: number): void {
    const cur = this.byId.get(id)
    if (!cur || cur.status !== 'running') return
    if (Number.isFinite(transferred) && transferred >= 0) cur.transferred = transferred
    if (Number.isFinite(total) && total > 0) cur.total = total
    this.scheduleFlush()
  }

  /** 暂停对非终态生效；与取消的先后不敏感 —— 后到的 markCancelled 不会覆盖 paused */
  async markPaused(id: string, interrupted: boolean): Promise<void> {
    await this.init()
    const cur = this.byId.get(id)
    if (!cur || isTerminalUploadStatus(cur.status)) return
    cur.status = 'paused'
    if (interrupted) cur.interrupted = true
    else delete cur.interrupted
    cur.updatedAt = this.now()
    await this.flushNow()
  }

  /** 只取消 running/pending：已暂停的任务应保留「可继续」，而不是被取消抹掉 */
  async markCancelled(id: string): Promise<void> {
    await this.init()
    const cur = this.byId.get(id)
    if (!cur || (cur.status !== 'running' && cur.status !== 'pending')) return
    cur.status = 'cancelled'
    delete cur.interrupted
    delete cur.error
    delete cur.errorCode
    cur.updatedAt = this.now()
    await this.flushNow()
  }

  async list(connectionId: string): Promise<UploadTaskRecord[]> {
    await this.init()
    return this.records
      .filter(r => r.connectionId === connectionId)
      .map(r => ({ ...r }))
  }

  get(id: string): UploadTaskRecord | undefined {
    const r = this.byId.get(id)
    return r ? { ...r } : undefined
  }

  async remove(id: string): Promise<void> {
    await this.init()
    const idx = this.records.findIndex(r => r.id === id)
    if (idx === -1) return
    this.records.splice(idx, 1)
    this.byId.delete(id)
    await this.flushNow()
  }

  /** 只清终态；运行中/暂停中的记录属于「还没结束」，清空按钮不该误伤 */
  async clearFinished(connectionId: string): Promise<void> {
    await this.init()
    const before = this.records.length
    this.records = this.records.filter(
      r => r.connectionId !== connectionId || !isTerminalUploadStatus(r.status),
    )
    if (this.records.length === before) return
    this.rebuildIndex()
    await this.flushNow()
  }

  /** 退出时调用：后续终态回写失效，磁盘上保留 running 供下次启动恢复 */
  beginShutdown(): void {
    this.shuttingDown = true
    this.clearFlushTimers()
  }

  async flushNow(): Promise<void> {
    if (!this.loaded || this.writeDisabled) return
    this.clearFlushTimers()
    const snapshot = this.records.map(r => ({ ...r }))
    try {
      await this.file.enqueue(() => this.file.writeAll(snapshot))
    } catch (e) {
      console.error('[UploadTaskStore] 写入上传记录失败（不影响传输本身）:', e)
    }
  }

  private scheduleFlush(): void {
    if (this.writeDisabled || this.shuttingDown) return
    if (!this.flushTimer) {
      this.flushTimer = setTimeout(() => {
        this.flushTimer = null
        void this.flushNow()
      }, FLUSH_DEBOUNCE_MS)
    }
    if (!this.maxWaitTimer) {
      this.maxWaitTimer = setTimeout(() => {
        this.maxWaitTimer = null
        void this.flushNow()
      }, FLUSH_MAX_WAIT_MS)
    }
  }

  private clearFlushTimers(): void {
    if (this.flushTimer) {
      clearTimeout(this.flushTimer)
      this.flushTimer = null
    }
    if (this.maxWaitTimer) {
      clearTimeout(this.maxWaitTimer)
      this.maxWaitTimer = null
    }
  }
}
