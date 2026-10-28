/**
 * 后端时间戳是 UTC 的 `YYYY-MM-DD HH:mm:ss`（database._now），
 * 直接用字符串切片会把 UTC 当本地时间，东八区看起来少 8 小时。
 */
export function formatLocalDateTime(value: string, now: Date = new Date()): string {
  if (!value) return ''
  const normalized = value.includes('T') ? value : value.replace(' ', 'T')
  const hasZone = /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(normalized)
  const date = new Date(hasZone ? normalized : `${normalized}Z`)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  const stamp = `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
  return date.getFullYear() === now.getFullYear() ? stamp : `${date.getFullYear()}-${stamp}`
}
