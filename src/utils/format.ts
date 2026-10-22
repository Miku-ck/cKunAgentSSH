/**
 * 通用展示格式化（原 SftpPane/SftpView/SftpFileEditor 各持一份 formatSize，
 * 其中 SftpFileEditor 版无 ≥PB/TB 钳制会输出 undefined —— 收敛为单一实现源）
 */
const SIZE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']

/** 字节数 → 人类可读（0/1 位小数；单位钳制到 PB，C30 修复语义保留） */
export function formatBytes(bytes: number): string {
  if (!bytes || bytes < 0) return '0 B'
  const i = Math.min(
    Math.floor(Math.log(bytes) / Math.log(1024)),
    SIZE_UNITS.length - 1,
  )
  return (bytes / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1) + ' ' + SIZE_UNITS[i]
}

/**
 * 字节数 → 紧凑人类可读（无空格分隔，对齐 docker 自身口径）。
 * 与 formatBytes 的语义差异是承重的：docker 的 Size 可能是预格式化字符串
 * 或 int64 —— 字符串原样直通；非法输入（null/空串/负数/NaN）返回 undefined，
 * 由调用方决定兜底文案（如「—」）。原 docker-core.ts humanSize 收编于此。
 */
export function formatBytesCompact(v: unknown): string | undefined {
  if (v === null || v === undefined) return undefined
  if (typeof v === 'string') return v || undefined
  const n = Number(v)
  if (!Number.isFinite(n) || n < 0) return undefined
  if (n === 0) return '0B'
  let i = 0
  let x = n
  while (x >= 1024 && i < SIZE_UNITS.length - 1) { x /= 1024; i++ }
  return (i === 0 ? x.toFixed(0) : x.toFixed(1)) + SIZE_UNITS[i]
}

/**
 * 时间戳 → 相对时间（连接列表「最后连接」列）。
 * now 必须由调用方传入（父级定时 tick）；Date.now() 写在 computed 里不会随
 * 时间流逝重算，会一直停在渲染那一刻。
 */
export function formatRelativeTime(
  ts: number | null | undefined,
  now: number,
  fallback = '从未连接',
): string {
  if (!ts) return fallback
  const diff = now - ts
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  if (diff < 30 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`
  return new Date(ts).toLocaleDateString()
}

/**
 * 后端时间戳 → Date。
 * 后端统一存 UTC naive 字符串（database._now / compute_next_run 都是 "YYYY-MM-DD HH:MM:SS"），
 * 而 new Date("2026-09-21T07:00:00") 没有时区标记时按【本地时区】解释 —— UTC+8 下
 * 所有时间早 8 小时：定时任务的「下次执行」显示成过去、倒计时恒为「即将」。
 * 已带 Z / ±hh:mm 的（ISO 串）保持原样解析。
 */
export function parseBackendTs(ts?: string | null): Date {
  const s = ts || ''
  // 只有 naive 串才补 Z 当 UTC 读；已带时区标记（尾部 Z 或 ±hh:mm 偏移）的按原样解析。
  // 用结尾锚定的模式而非 /[Z+]/：既不漏负偏移 -hh:mm（旧式会把整串当 naive 追加 Z，
  // 得到 "...-05:00Z" → Invalid Date），也不会被 naive 串日期部分里的 '-' 误判。
  const hasTz = /[Zz]$/.test(s) || /[+-]\d{2}:?\d{2}$/.test(s)
  return new Date(s.replace(' ', 'T') + (hasTz ? '' : 'Z'))
}

/**
 * 后端时间戳 → 本地「YYYY-MM-DD HH:MM[:SS]」。
 * 解析口径交给 parseBackendTs（naive 串按 UTC 读），这里只负责渲染；
 * 非法/空串原样返回，让调用方看到脏数据而不是凭空变成 1970。
 */
export function formatBackendTs(ts?: string | null, withSeconds = false): string {
  if (!ts) return ''
  const d = parseBackendTs(ts)
  const ms = d.getTime()
  if (Number.isNaN(ms)) return ts
  const p = (n: number) => String(n).padStart(2, '0')
  const date = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
  const hms = `${p(d.getHours())}:${p(d.getMinutes())}${withSeconds ? `:${p(d.getSeconds())}` : ''}`
  return `${date} ${hms}`
}
