/**
 * 传输任务的展示格式化 —— 纯函数层。
 *
 * 原先这批函数住在 SftpView.vue 的 `<script setup>` 里，各自在渲染时现读
 * `Date.now()`：组件没有 tick，「剩余时间」只在 `transferred` 变化时才重算 ——
 * 暂停中、以及远端复制这类 `total === 0` 的任务收不到进度事件，数字就永久僵在
 * 最后一次更新上。搬到这里并把 `now` 收成显式入参后，调用方用同一个秒级 tick
 * 驱动，视图才真的在动。
 *
 * 状态与类别两个联合类型也定义在这里并由 store 引入：`TransferItem.status` /
 * `kind` 与判色、判「是否不确定」、判「复制类不受并发门约束」的分支必须同源，
 * 抄第二份迟早和 `'paused'` 这种成员分叉。
 *
 * 只留有人调的：`formatClock` / `remainingSeconds` 虽只被本模块的 formatEta、
 * formatDuration 用到，但它们是那两条口径真正依赖的单位，测试直接钉它们；
 * 曾经同批搬来的 `formatElapsed` 因「已运行」栏位被撤而无人消费，已删。
 */

export type TransferStatus = 'pending' | 'running' | 'paused' | 'done' | 'error' | 'cancelled'

/** 任务类别。store 的 TransferItem 与归并层的 GroupableTransfer 共指这一份，
 *  复制类任务在两侧都要被认出来（不受并发门约束、图标走 copy 分支）。 */
export type TransferKind = 'upload' | 'download' | 'copy-local' | 'copy-remote'

/** 格式化只依赖这几个字段；让 store 的 TransferItem 结构上满足它即可，
 *  反过来 import store 会把 util 拖进 Pinia 实例化时序里。 */
export interface TransferProgress {
  status: TransferStatus
  transferred: number
  total: number
  startTime: number
  endTime?: number
}

const UNKNOWN = '—'

/** 0–100 的整数百分比；`total` 未知（0）时返回 0，由 isIndeterminate 决定怎么显示 */
export function progressPercent(t: TransferProgress): number {
  if (!t.total || t.total <= 0) return 0
  return Math.min(100, Math.round((t.transferred / t.total) * 100))
}

/** 正在跑但总大小未知（远端 `cp -r`、递归展开中的目录）：只能画不确定条，不能编造百分比 */
export function isIndeterminate(t: TransferProgress): boolean {
  return t.status === 'running' && t.total <= 0
}

/** 本段平均速率（字节/秒）。非 running 返回 0 —— 暂停与已完成不该显示一个还在跳的速度。
 *  用累计平均而非瞬时值：渲染侧没有历史采样，主进程 100ms 一条进度事件也不足以定窗口。 */
export function averageSpeed(t: TransferProgress, now: number): number {
  if (t.status !== 'running' || !t.startTime) return 0
  const elapsedSec = (now - t.startTime) / 1000
  if (elapsedSec <= 0) return 0
  return t.transferred / elapsedSec
}

/** 预计剩余秒数；速率或总量未知时返回 null（调用方画 UNKNOWN，不要画 0） */
export function remainingSeconds(t: TransferProgress, now: number): number | null {
  if (t.status !== 'running' || t.total <= 0 || t.transferred >= t.total) return null
  const speed = averageSpeed(t, now)
  if (speed <= 0) return null
  return Math.ceil((t.total - t.transferred) / speed)
}

/** 字节/秒 → `3.2 MB/s`；单位钳制到 GB/s，与 formatBytes 的 B/KB/MB/GB 同一阶梯 */
export function formatRate(bytesPerSec: number): string {
  if (!bytesPerSec || bytesPerSec <= 0) return '0 B/s'
  const KB = 1024
  const MB = KB * 1024
  const GB = MB * 1024
  if (bytesPerSec < KB) return `${Math.round(bytesPerSec)} B/s`
  if (bytesPerSec < MB) return `${(bytesPerSec / KB).toFixed(1)} KB/s`
  if (bytesPerSec < GB) return `${(bytesPerSec / MB).toFixed(1)} MB/s`
  return `${(bytesPerSec / GB).toFixed(2)} GB/s`
}

/** 秒 → `mm:ss`，超过一小时带小时位 `h:mm:ss`。原实现输出裸秒（`剩余 125s`）。 */
export function formatClock(totalSeconds: number): string {
  if (!Number.isFinite(totalSeconds) || totalSeconds < 0) return UNKNOWN
  const s = Math.floor(totalSeconds)
  const pad = (n: number) => String(n).padStart(2, '0')
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (h > 0) return `${h}:${pad(m)}:${pad(s % 60)}`
  return `${pad(m)}:${pad(s % 60)}`
}

/** 剩余时间；未知 → UNKNOWN */
export function formatEta(t: TransferProgress, now: number): string {
  const sec = remainingSeconds(t, now)
  return sec === null ? UNKNOWN : formatClock(sec)
}

/** 已结束任务的实际用时；亚秒级保留一位小数，否则 `mm:ss` 会把 0.4 秒显示成 00:00 */
export function formatDuration(t: TransferProgress, now: number): string {
  if (!t.startTime) return UNKNOWN
  const end = t.endTime ?? now
  const ms = end - t.startTime
  if (ms < 0) return UNKNOWN
  if (ms < 1000) return `${(ms / 1000).toFixed(1)}s`
  return formatClock(ms / 1000)
}

/** 路径中间省略，保留末两段 —— 传输行里 `D:\a\b\c\d\e.bin` 和 `\c\d\e.bin` 一样长。
 *  本地路径的分隔符可能是 `\`，两种都要切，否则 Windows 路径整条不缩。 */
export function truncatePath(path: string, max = 30): string {
  if (!path || path.length <= max) return path
  const parts = path.split(/[\\/]/).filter(Boolean)
  if (parts.length > 2) {
    const tail = parts.slice(-2).join('/')
    return `.../${tail}`
  }
  return '...' + path.slice(-(max - 3))
}
