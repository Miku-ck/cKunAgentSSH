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
