/**
 * 陪聊聊天界面的「装饰」纯逻辑 —— 全部只依赖会话 external_id 与消息时间戳/角色，
 * 不采集任何真实昵称/头像/群名（QQ 官方机器人拿不到，也不该拿）。这里做的都是
 * 「合规替身」：用 openid 尾号派生匿名代号、按本地日历分天、合并连发。
 * 头像不再做任何自造图案：没设图就是空位（透明白），见 PersonaAvatar。
 * 决策逻辑集中在此以便单测覆盖（前端单测为纯 TS 无 DOM），Vue 侧只做数据映射。
 */
import { parseBackendTs } from './format'

/**
 * 从会话 external_id 抽出对方 openid 作头像种子。
 * 后端格式：`group:{group_openid}` / `c2c:{user_openid}`（机器人归属走
 * channel_id 列，不再拼进 key）；本地会话（null/空）或无法识别的来源返回空串。
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

/**
 * 列表行的会话类型短标：只有群聊需要标（私聊是默认形态，标了等于噪声）。
 * 完整代号与时间不进这一行 —— 窄栏折两行比多一段信息更难看，挪进 title 悬浮。
 */
export function peerTag(externalId?: string | null): string {
  return externalId?.startsWith('group:') ? '群' : ''
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
