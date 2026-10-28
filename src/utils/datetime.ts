import { parseBackendTs } from './format'

const pad = (n: number) => String(n).padStart(2, '0')

/**
 * 后端时间戳是 UTC 的 `YYYY-MM-DD HH:mm:ss`（database._now），
 * 直接用字符串切片会把 UTC 当本地时间，东八区看起来少 8 小时。
 * 解析口径交给 parseBackendTs（naive 串按 UTC 读），这里只负责渲染。
 */
export function formatLocalDateTime(value: string, now: Date = new Date()): string {
  if (!value) return ''
  const date = parseBackendTs(value)
  if (Number.isNaN(date.getTime())) return ''
  const stamp = `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
  return date.getFullYear() === now.getFullYear() ? stamp : `${date.getFullYear()}-${stamp}`
}

/** epoch 毫秒 → 本地 `YYYY-MM-DD`；无时间戳用短横占位（列表行的「创建于」列） */
export function formatLocalDate(ts: number): string {
  if (!ts) return '-'
  const d = new Date(ts)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}
