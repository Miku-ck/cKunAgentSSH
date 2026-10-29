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
 * 后端格式：QQ `group:{group_openid}` / `c2c:{user_openid}`、微信 `wx:{openid}`
 * （机器人归属走 channel_id 列，不再拼进 key）；本地会话（null/空）或无法识别的来源返回空串。
 */
export function peerSeed(externalId?: string | null): string {
  if (!externalId) return ''
  const parts = externalId.split(':')
  if (parts.length < 2) return ''
  if (parts[0] !== 'group' && parts[0] !== 'c2c' && parts[0] !== 'wx') return ''
  return parts[1] || ''
}

/** 会话匿名代号：群聊/私聊/微信 + openid 尾 4 位（大写），拿不到类型回落「QQ」。 */
export function peerLabel(externalId?: string | null): string {
  const seed = peerSeed(externalId)
  if (!seed) return 'QQ'
  const kind = externalId!.startsWith('group:')
    ? '群聊'
    : externalId!.startsWith('wx:') ? '微信' : '私聊'
  const tail = seed.slice(-4).toUpperCase()
  return `${kind} · ${tail}`
}

/**
 * 会话归属人格的名字。
 * 服务端已把快照裁成 {id, name, avatar} 直接下发，这里不再解析 JSON；
 * 人格被删除后 persona_id 为空，名字仍来自快照，所以比查 personas 列表更可靠。
 */
export function personaName(persona: { name?: string } | null | undefined): string {
  return persona?.name?.trim() || ''
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

interface Decorated<T> {
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

// ── 分条 ──

/** 一轮回复最多拆几条气泡，与服务端 MAX_REPLY_BUBBLES 同值。 */
const MAX_REPLY_BUBBLES = 3

/** 平均每段少于这个字数就是「一句话说完被切三截」，并回一条。与服务端同值。 */
const MIN_BUBBLE_CHARS = 24

/**
 * 按空行把一轮回复切成多条气泡。规则与服务端 `bot/voice.py` 的 `split_reply_bubbles`
 * 一致：三段以上且每段都薄得只剩半句时先并回一条；超出条数不丢字，余下的并回最后一条；
 * 单条内部的换行原样保留（那是列表或引文，不算分条）。
 * 跨语言没法共享实现，改一边必须同步另一边 —— 不一致时最坏只是收尾瞬间
 * 气泡重排，不会丢内容。
 */
export function splitBubbles(text: string, maxBubbles: number = MAX_REPLY_BUBBLES): string[] {
  const chunks = (text || '')
    .trim()
    .split(/\n[ \t]*\n/)
    .map(chunk => chunk.trim())
    .filter(Boolean)
  if (chunks.length >= 3 && chunks.reduce((sum, chunk) => sum + chunk.length, 0) < MIN_BUBBLE_CHARS * chunks.length) {
    return [chunks.join('')]
  }
  const cap = Math.max(1, maxBubbles)
  if (chunks.length <= cap) return chunks
  return [...chunks.slice(0, cap - 1), chunks.slice(cap - 1).join('\n\n')]
}

export interface Bubbly {
  role: string
  content: string
  images?: unknown[] | null
}

/**
 * 把「一轮一行」的消息展开成「一条气泡一项」，交给 decorateMessages 走既有的
 * 续接逻辑（同角色相邻 → 隐头像、收间距），所以多条气泡的观感不需要任何新样式。
 * 只展开助手：用户自己打的段落是他的原文，不该由我们替他拆条。
 */
export function expandBubbles<T extends Bubbly>(
  items: readonly T[],
): (T & { bubble: string; bubbleIndex: number })[] {
  const out: (T & { bubble: string; bubbleIndex: number })[] = []
  for (const item of items) {
    const bubbles = item.role === 'assistant' ? splitBubbles(item.content) : []
    if (bubbles.length) {
      bubbles.forEach((bubble, bubbleIndex) => out.push({ ...item, bubble, bubbleIndex }))
      continue
    }
    // 正文空又没有图 = 这条没有任何可显示的东西：不占气泡位，也不许它吃掉
    // 下一轮的说话人标识（空框 + 名字被它领走，看着就像少了个头）
    if (!item.content.trim() && !item.images?.length) continue
    out.push({ ...item, bubble: item.content, bubbleIndex: 0 })
  }
  return out
}

// ── 在场感（P4）：多气泡投递节奏 ──

/**
 * 单条气泡的拟打字延迟（毫秒）：短句快、长句慢、感叹/疑问句多停一拍。
 * delay = (500 + 90·字数 + 300·句末标点数) × jitter，上限 4s——再长就不是
 * 「在打字」而是「卡了」。jitter 由调用方注入（默认 1 便于测试断言）。
 */
export function bubbleDelayMs(text: string, jitter = 1): number {
  const body = text || ''
  const chars = Array.from(body).length
  const punct = (body.match(/[！？…?!]/g) || []).length
  const raw = (500 + 90 * chars + 300 * punct) * jitter
  return Math.max(0, Math.min(4000, Math.round(raw)))
}

/**
 * 一轮回复的投递排程：**首气泡零延迟**——流式阶段它已经在逐字渲染，
 * 排程只管「已完整收到、尚未展示」的第 2~N 条，所以不影响首字延迟。
 */
export function bubbleDeliveryPlan(bubbles: readonly string[], jitter = 1): number[] {
  return bubbles.map((bubble, index) => (index === 0 ? 0 : bubbleDelayMs(bubble, jitter)))
}
