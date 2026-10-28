/**
 * 陪聊聊天界面的「装饰」纯逻辑 —— 全部只依赖会话 external_id 与消息时间戳/角色，
 * 不采集任何真实昵称/头像/群名（QQ 官方机器人拿不到，也不该拿）。这里做的都是
 * 「合规替身」：用 openid 哈希确定性生成匿名头像与代号、按本地日历分天、合并连发。
 * 决策逻辑集中在此以便单测覆盖（前端单测为纯 TS 无 DOM），Vue 侧只做数据映射。
 */
import { parseBackendTs } from './format'

/** FNV-1a：32 位稳定哈希，同串恒同值，作为头像与配色的唯一种子来源。 */
function hashStr(s: string): number {
  let h = 2166136261
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return h >>> 0
}

/** mulberry32：从单一数值种子派生可复现的伪随机序列。 */
function prng(seed: number): () => number {
  let a = seed | 0
  return () => {
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const GRID = 5

/**
 * 由 seed 确定性生成一枚匿名 identicon（对称色块网格）。
 * - 同 seed → 同图（联系人稳定可辨识）
 * - 异 seed → 异图（不同联系人区分开）
 * - 空/脏 seed → 仍返回合法 SVG（用固定回退种子，不渲染破图）
 * 产出为内联 SVG 标记：不带 xmlns（HTML 内联无需，且避免引入外链 http 字样），
 * 不含任何真实用户资料。
 */
export function identiconSvg(seed: string): string {
  const effective = (seed && seed.trim()) || '__fallback__'
  const r = prng(hashStr(effective))
  const hue = Math.floor(r() * 360)
  const hue2 = (hue + 40 + Math.floor(r() * 80)) % 360
  const gid = `ic${hashStr(effective).toString(36)}`
  const half = Math.ceil(GRID / 2)
  const cells: Array<[number, number]> = []
  for (let y = 0; y < GRID; y++) {
    for (let x = 0; x < half; x++) {
      if (r() > 0.5) {
        cells.push([x, y])
        const mirror = GRID - 1 - x
        if (mirror !== x) cells.push([mirror, y])
      }
    }
  }
  const size = 100 / GRID
  let rects = ''
  for (const [x, y] of cells) {
    rects += `<rect x="${(x * size).toFixed(2)}" y="${(y * size).toFixed(2)}" width="${size.toFixed(2)}" height="${size.toFixed(2)}"/>`
  }
  // 网格全空（极端随机）时兜底放一枚中心方块，保证头像永不为空白。
  if (!rects) {
    rects = `<rect x="${((GRID / 2 - 0.5) * size).toFixed(2)}" y="${((GRID / 2 - 0.5) * size).toFixed(2)}" width="${size.toFixed(2)}" height="${size.toFixed(2)}"/>`
  }
  return (
    `<svg viewBox="0 0 100 100" role="img" aria-label="匿名头像">` +
    `<defs><linearGradient id="${gid}" x1="0" y1="0" x2="1" y2="1">` +
    `<stop offset="0" stop-color="hsl(${hue},62%,52%)"/>` +
    `<stop offset="1" stop-color="hsl(${hue2},66%,42%)"/>` +
    `</linearGradient></defs>` +
    `<rect width="100" height="100" fill="url(#${gid})"/>` +
    `<g fill="rgba(255,255,255,0.92)">${rects}</g>` +
    `</svg>`
  )
}

/**
 * 从会话 external_id 抽出对方 openid 作头像种子。
 * 后端格式：`group:{group_openid}:{context_id}` / `c2c:{user_openid}:{context_id}`；
 * 本地会话（null/空）或无法识别的来源返回空串（交由回退种子处理）。
 */
export function peerSeed(externalId?: string | null): string {
  if (!externalId) return ''
  const parts = externalId.split(':')
  if (parts.length < 2) return ''
  if (parts[0] !== 'group' && parts[0] !== 'c2c') return ''
  return parts[1] || ''
}

/** 会话匿名代号：群聊/私聊 + openid 尾 4 位（大写），拿不到类型回落「QQ」。 */
export function peerLabel(externalId?: string | null): string {
  const seed = peerSeed(externalId)
  if (!seed) return 'QQ'
  const kind = externalId!.startsWith('group:') ? '群聊' : '私聊'
  const tail = seed.slice(-4).toUpperCase()
  return `${kind} · ${tail}`
}

function localDayKey(d: Date): string {
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`
}

/**
 * 消息时间戳 → 日期分隔文案：今天 / 昨天 / 「M月D日」（本年）/ 「YYYY年M月D日」（跨年）。
 * 口径与 formatLocalDateTime 一致：naive 串按 UTC 读、按本地日历分天，非法/空返回空串。
 */
export function dayDividerLabel(ts?: string | null, now: Date = new Date()): string {
  if (!ts) return ''
  const d = parseBackendTs(ts)
  if (Number.isNaN(d.getTime())) return ''
  const key = localDayKey(d)
  if (key === localDayKey(now)) return '今天'
  const yesterday = new Date(now)
  yesterday.setDate(yesterday.getDate() - 1)
  if (key === localDayKey(yesterday)) return '昨天'
  if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}月${d.getDate()}日`
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`
}

export interface Decoratable {
  role: string
  created_at: string
}

export interface Decorated<T> {
  item: T
  /** 非空表示这条应挂一条日期分隔线。 */
  divider: string
  /** true 表示与上一条同角色同人，应隐藏重复头像、收紧间距。 */
  cont: boolean
}

/**
 * 给一串消息算出「日期分隔 + 连续合并」装饰元数据。
 * - divider：与上一条不同天（或首条）时给出该天的分隔文案。
 * - cont：同角色、同天、且非 system 时视为续接（system 永不作续接首锚）。
 */
export function decorateMessages<T extends Decoratable>(
  items: readonly T[],
  now: Date = new Date(),
): Decorated<T>[] {
  const out: Decorated<T>[] = []
  let prevDay = ''
  let prevRole = ''
  for (const item of items) {
    const divider = dayDividerLabel(item.created_at, now)
    const dayChanged = divider !== prevDay
    const sameDay = !dayChanged
    const cont =
      prevRole !== '' &&
      sameDay &&
      item.role !== 'system' &&
      item.role === prevRole
    out.push({ item, divider: dayChanged ? divider : '', cont })
    prevDay = divider
    prevRole = item.role
  }
  return out
}
