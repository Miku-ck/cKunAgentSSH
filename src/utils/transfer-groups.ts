/**
 * 传输列表的归并逻辑 —— 把扁平的 TransferItem 折叠成「组头 + 子行」的渲染序列。
 *
 * 这是传输中心真正的复杂度所在（标签过滤、目录分组、组级汇总、排序、渲染上限），
 * 所以放在这里而不是组件里：本仓前端单测统一跑 node 环境、不挂 DOM（见
 * vitest.config.ts 首行注释），逻辑困在 SFC 里就等于测不到。
 *
 * 分组只是**展示归并**：调度、取消、续传仍逐条按 id 走 store 的既有方法，
 * 这里不产生任何新语义。
 */
import { isActiveTransferStatus, type TransferKind, type TransferStatus } from './transfer-format'

/** TransferItem 结构上满足它；刻意不 import store，避免 util 拖进 Pinia 实例化时序 */
export interface GroupableTransfer {
  id: string
  name: string
  from: string
  to: string
  direction: 'up' | 'down'
  kind: TransferKind
  status: TransferStatus
  transferred: number
  total: number
  startTime: number
  endTime?: number
  error?: string
  groupId?: string
  groupLabel?: string
  /** 上次运行被中断（崩溃/断线）⇒ 行内提示「未完成 · 可继续」 */
  interrupted?: boolean
}

/** TransferRow.vue 的 props 形状。定义在逻辑侧，组件与调用方共用一份。 */
export interface TransferRowView {
  key: string
  isGroup: boolean
  isChild: boolean
  expanded: boolean
  label: string
  meta: string
  from: string
  to: string
  direction: 'up' | 'down'
  kind: TransferKind
  status: TransferStatus
  transferred: number
  total: number
  startTime: number
  endTime?: number
  error?: string
  /** 中断任务：paused 行提示可继续（与普通暂停区分） */
  interrupted?: boolean
  /** 动作要作用的 transfer id 列表：单行 1 个，组头 = 组内全部 */
  ids: string[]
  /** 组头专用：因渲染上限而未展开的子项数 */
  truncated?: number
}

export type TransferTabKey = 'active' | 'queued' | 'done' | 'error' | 'all'
export type TransferSortKey = 'name' | 'path' | 'percent' | 'speed' | 'size' | 'eta' | 'status' | 'queue'

/** 组头一次展开最多渲染多少子行：递归目录可能有几千上万个文件 */
const CHILD_RENDER_LIMIT = 200

/** 组头的状态优先级：只要组里还有一个真在传，组头就不能显示成「完成」 */
const STATUS_RANK: Record<TransferStatus, number> = {
  running: 0, paused: 1, pending: 2, error: 3, cancelled: 4, done: 5,
}

function inTab(t: GroupableTransfer, tab: TransferTabKey): boolean {
  if (tab === 'all') return true
  if (tab === 'active') return isActiveTransferStatus(t.status)
  if (tab === 'queued') return t.status === 'pending'
  if (tab === 'done') return t.status === 'done' || t.status === 'cancelled'
  return t.status === 'error'
}

function matches(t: GroupableTransfer, needle: string): boolean {
  if (!needle) return true
  const n = needle.toLowerCase()
  return t.name.toLowerCase().includes(n)
    || t.from.toLowerCase().includes(n)
    || t.to.toLowerCase().includes(n)
}

/** 一组任务共同的源/目标目录：取最长公共前缀后回退到最后一个分隔符，
 *  避免把 "…/file-" 这种半截文件名当目录显示；只有一条时直接给全路径。 */
export function commonDir(paths: string[]): string {
  if (paths.length === 0) return ''
  if (paths.length === 1) return paths[0]
  let prefix = paths[0]
  for (const p of paths.slice(1)) {
    let i = 0
    const n = Math.min(prefix.length, p.length)
    while (i < n && prefix[i] === p[i]) i++
    prefix = prefix.slice(0, i)
    if (!prefix) return paths[0]
  }
  const cut = Math.max(prefix.lastIndexOf('/'), prefix.lastIndexOf('\\'))
  return cut > 0 ? prefix.slice(0, cut) : paths[0]
}

/** 最早 startTime。用 reduce 而不是 Math.min(...arr)：上万个成员的组展开成实参栈会先炸。 */
function earliestStart(items: GroupableTransfer[]): number {
  return items.reduce((m, t) => (t.startTime < m ? t.startTime : m), Number.POSITIVE_INFINITY)
}

function buildSingleUnit(t: GroupableTransfer, isChild: boolean): TransferRowView {
  return {
    key: `t:${t.id}`,
    isGroup: false,
    isChild,
    expanded: false,
    label: t.name,
    meta: t.interrupted ? '未完成 · 可继续' : '',
    from: t.from,
    to: t.to,
    direction: t.direction,
    kind: t.kind,
    status: t.status,
    transferred: t.transferred,
    total: t.total,
    startTime: t.startTime,
    endTime: t.endTime,
    error: t.status === 'error' ? `失败：${t.error || '未知原因'}` : undefined,
    interrupted: t.interrupted,
    ids: [t.id],
  }
}

function buildGroupUnit(gid: string, items: GroupableTransfer[], expanded: boolean): TransferRowView {
  const ranked = [...items].sort((a, b) => STATUS_RANK[a.status] - STATUS_RANK[b.status])
  const head = ranked[0]
  const interrupted = items.some(t => t.interrupted)
  return {
    key: `g:${gid}`,
    isGroup: true,
    isChild: false,
    expanded,
    label: head.groupLabel || gid,
    meta: `${items.filter(t => t.status === 'done').length} / ${items.length} 个文件${interrupted ? ' · 可继续' : ''}`,
    from: commonDir(items.map(t => t.from)),
    to: commonDir(items.map(t => t.to)),
    direction: head.direction,
    kind: head.kind,
    status: head.status,
    transferred: items.reduce((s, t) => s + t.transferred, 0),
    total: items.reduce((s, t) => s + t.total, 0),
    startTime: earliestStart(items),
    interrupted,
    ids: items.map(t => t.id),
  }
}

/** 速率（字节/秒）与剩余秒数：与 TransferRow 显示的是同一口径，排序才不会和读数打架 */
function speedOf(u: TransferRowView, now: number): number {
  if (u.status !== 'running' || !u.startTime) return 0
  const sec = (now - u.startTime) / 1000
  return sec <= 0 ? 0 : u.transferred / sec
}
function etaOf(u: TransferRowView, now: number): number {
  const speed = speedOf(u, now)
  if (u.status !== 'running' || u.total <= 0 || u.transferred >= u.total || speed <= 0) {
    return Number.POSITIVE_INFINITY
  }
  return (u.total - u.transferred) / speed
}

/** buildRenderUnits 的入参。刻意不导出：调用方传对象字面量即可，类型由函数签名自带。 */
interface BuildUnitsOptions {
  transfers: GroupableTransfer[]
  tab: TransferTabKey
  /** 已 trim 的筛选词，空串表示不过滤 */
  needle?: string
  /** 展开中的 groupId 集合 */
  expanded?: Set<string>
  sortKey?: TransferSortKey
  sortDir?: 1 | -1
  /** 排序按速度/剩余时需要的时间基准 */
  now?: number
  childLimit?: number
}

export function buildRenderUnits(o: BuildUnitsOptions): TransferRowView[] {
  const { tab, expanded = new Set(), sortKey = 'queue', sortDir = 1 } = o
  const needle = o.needle ?? ''
  const childLimit = o.childLimit ?? CHILD_RENDER_LIMIT
  const pool = o.transfers.filter(t => inTab(t, tab) && matches(t, needle))

  // 按 groupId 归并；只有一员的组当单行渲染（一个组头套一个文件纯属噪音）
  const groups = new Map<string, GroupableTransfer[]>()
  const singles: GroupableTransfer[] = []
  for (const t of pool) {
    if (t.groupId) {
      const arr = groups.get(t.groupId)
      if (arr) arr.push(t)
      else groups.set(t.groupId, [t])
    } else {
      singles.push(t)
    }
  }

  const units: TransferRowView[] = singles.map(t => buildSingleUnit(t, false))
  for (const [gid, items] of groups) {
    if (items.length === 1) {
      units.push(buildSingleUnit(items[0], false))
      continue
    }
    const head = buildGroupUnit(gid, items, expanded.has(gid))
    units.push(head)
    if (!head.expanded) continue
    // 「还有 N 项没画出来」只在展开时成立：收起是用户的选择，不是渲染上限
    head.truncated = Math.max(0, items.length - childLimit)
    units.push(...[...items]
      .sort((a, b) => STATUS_RANK[a.status] - STATUS_RANK[b.status])
      .slice(0, childLimit)
      .map(t => buildSingleUnit(t, true)))
  }

  if (sortKey === 'queue') return units
  const now = o.now ?? Date.now()
  const get: Record<Exclude<TransferSortKey, 'queue'>, (u: TransferRowView) => number | string> = {
    name: u => u.label.toLowerCase(),
    // 按目标路径排：一次目录上传里同批文件按落点归拢，比按源路径排更好扫
    path: u => u.to.toLowerCase(),
    percent: u => (u.total > 0 ? u.transferred / u.total : -1),
    speed: u => speedOf(u, now),
    size: u => u.total,
    eta: u => etaOf(u, now),
    status: u => STATUS_RANK[u.status],
  }
  const pick = get[sortKey]
  return [...units].sort((a, b) => {
    const av = pick(a)
    const bv = pick(b)
    const base = typeof av === 'number' ? av - (bv as number) : String(av).localeCompare(String(bv))
    return base * sortDir
  })
}
