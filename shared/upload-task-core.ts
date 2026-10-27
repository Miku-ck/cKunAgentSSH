/**
 * upload-task-core.ts — SFTP 上传记录的共享契约（主进程 / 渲染端 / vitest 共用）
 *
 * 上传的真实生命周期在主进程（.part、进度、rename），但展示在渲染层。记录必须
 * 跨重启存活，否则闪退后远端 `.part` 成为孤儿任务，用户没有任何入口把它接上。
 * 状态表、校验与恢复归一化放在这里：两侧各写一份迟早对 `running` 的「上次没传完」
 * 语义分叉，重启后一边显示可继续、另一边显示已完成。
 *
 * 约束：本文件必须保持平台无关 — 不得 import electron / node 内置模块。
 */

export type UploadTaskStatus =
  | 'pending'
  | 'running'
  | 'paused'
  | 'done'
  | 'error'
  | 'cancelled'

export const UPLOAD_TASK_STATUSES: readonly UploadTaskStatus[] = [
  'pending',
  'running',
  'paused',
  'done',
  'error',
  'cancelled',
]

/** 落盘的一条上传记录。`connectionId` 是归属键：切换主机后只显示该主机的记录。 */
export interface UploadTaskRecord {
  id: string
  connectionId: string
  localPath: string
  remotePath: string
  name: string
  total: number
  transferred: number
  status: UploadTaskStatus
  error?: string
  errorCode?: string
  groupId?: string
  groupLabel?: string
  /** 上次运行被中断（崩溃/退出/断线）。恢复后与 `paused` 搭配展示「未完成 · 可继续」。 */
  interrupted?: boolean
  createdAt: number
  updatedAt: number
}

/** 渲染层入队时交给主进程的字段（connectionId 与时间戳由主进程补齐）。 */
export interface RegisteredUploadTask {
  id: string
  localPath: string
  remotePath: string
  name: string
  total: number
  groupId?: string
  groupLabel?: string
}

/** `sftp:upload` 的可选元数据。`resume` 只有用户显式点「继续」才为 true。 */
export interface UploadTaskMeta {
  name?: string
  groupId?: string
  groupLabel?: string
  size?: number
  resume?: boolean
}

/** 终态：不再接受进度/失败状态回写，只有用户显式操作能再改。 */
export function isTerminalUploadStatus(status: UploadTaskStatus): boolean {
  return status === 'done' || status === 'error' || status === 'cancelled'
}

function isFiniteNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

function isOptionalString(v: unknown): boolean {
  return v === undefined || typeof v === 'string'
}

/**
 * 磁盘记录的有效性判据。跨版本读盘时宁可丢弃单条坏记录（JsonFileStore 会跳过
 * 并计数），也不能把缺 localPath 的记录交给续传 —— 那会以 `undefined` 为源路径
 * 去 stat，错误信息离现场很远。
 */
export function isValidUploadTaskRecord(r: unknown): r is UploadTaskRecord {
  if (!r || typeof r !== 'object') return false
  const o = r as Record<string, unknown>
  if (typeof o.id !== 'string' || !o.id) return false
  if (typeof o.connectionId !== 'string' || !o.connectionId) return false
  if (typeof o.localPath !== 'string' || !o.localPath) return false
  if (typeof o.remotePath !== 'string' || !o.remotePath) return false
  if (typeof o.name !== 'string') return false
  if (!(UPLOAD_TASK_STATUSES as readonly string[]).includes(o.status as string)) return false
  if (!isFiniteNumber(o.total) || !isFiniteNumber(o.transferred)) return false
  if (!isFiniteNumber(o.createdAt) || !isFiniteNumber(o.updatedAt)) return false
  if (!isOptionalString(o.error)) return false
  if (!isOptionalString(o.errorCode) || !isOptionalString(o.groupId)) return false
  if (!isOptionalString(o.groupLabel)) return false
  if (o.interrupted !== undefined && typeof o.interrupted !== 'boolean') return false
  return true
}

/**
 * 恢复后新任务 id 的起点。渲染层 id 形如 `t<number>`（传输）与 `g<number>`
 * （分组），重启后计数器从 1 重来会撞上历史 id：主进程按 id 覆盖记录，
 * 新上传会把一条旧记录「改头换面」，两行同一个 id。
 */
export function nextTransferSeqFrom(records: readonly { id: string }[]): number {
  let max = 0
  for (const r of records) {
    const m = /^[tg](\d+)$/.exec(r.id)
    if (!m) continue
    const n = Number(m[1])
    if (Number.isFinite(n) && n > max) max = n
  }
  return max + 1
}

/**
 * 启动时的归一化：`pending/running` 意味着上次进程没走完，磁盘上的状态已不可信，
 * 统一变成 `paused + interrupted`（手动继续）。`paused` 保留其「已暂停」语义，
 * 但同样标记为中断 —— 上次是用户暂停还是进程被杀，对恢复动作没有区别。
 */
export function restoredStatusOf(
  record: Pick<UploadTaskRecord, 'status' | 'interrupted'>,
): { status: UploadTaskStatus; interrupted: boolean } {
  if (record.status === 'pending' || record.status === 'running' || record.status === 'paused') {
    return { status: 'paused', interrupted: true }
  }
  return { status: record.status, interrupted: false }
}

/** 取末段文件名，Windows/Unix 分隔符都认（记录里的 localPath 来自渲染层，两种都可能）。 */
export function basenameAny(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean)
  return parts.length > 0 ? parts[parts.length - 1] : path
}
