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
 * 后端时间戳 → Date。
 * 后端统一存 UTC naive 字符串（database._now / compute_next_run 都是 "YYYY-MM-DD HH:MM:SS"），
 * 而 new Date("2026-09-21T07:00:00") 没有时区标记时按【本地时区】解释 —— UTC+8 下
 * 所有时间早 8 小时：定时任务的「下次执行」显示成过去、倒计时恒为「即将」。
 * 已带 Z / ±hh:mm 的（ISO 串）保持原样解析。
 */
export function parseBackendTs(ts?: string | null): Date {
  const s = ts || ''
  return new Date(s.replace(' ', 'T') + (/[Z+]/.test(s) ? '' : 'Z'))
}
