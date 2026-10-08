/**
 * transfer-error-core.ts — SFTP 传输错误码契约的单一实现源（主进程与渲染端共享）
 *
 * 背景（S0-15）：主进程 `toTransferError` 把 code/retryable 作为**自定义属性**
 * 挂在裸 Error 上抛过 IPC，渲染端 `runUpload`/`runDownload` 直接读 `err?.code`
 * 与 `err?.retryable` 来决定「NETWORK/STALLED 自动续传一次」。而本仓对 IPC 的
 * 自述前提是「原始 Error 经过 IPC 后会退化为普通 Error（丢失 name/code）」
 * （electron/main.ts normalizeIpcError 的头注释），SSH 侧正因如此才同时写
 * `e.name` 与 `e.__sshError` 两条腿。传输侧只有一条腿 ⇒ 若自定义属性被丢，
 * 自动续传与停滞专属文案全是死分支，续传特性静默退化为「上传失败」。
 *
 * 本文件把契约收敛成可跨进程的两条腿：
 *   1. `name = 'TransferError:<CODE>'` —— 仓库实证保真性最强的通道（与 SSH 侧同法）
 *   2. 自定义 `code`/`retryable` 属性 —— 可能存活，存活时优先
 * 并把 retryable 变为 **code 的纯函数**：生产端与消费端不可能对同一 code 有两种理解
 * （同仓先例见 src/stores/server-monitor.ts:120 的注释口径）。
 *
 * 约束：本文件必须保持平台无关 — 不得 import electron / node 内置模块。
 */

export const TRANSFER_ERROR_CODES = [
  'NETWORK',
  'DISK_FULL',
  'PERMISSION',
  'NOT_FOUND',
  'CANCELLED',
  'STALLED',
  'REMOTE_CHANGED',
  'RENAME_FAILED',
  'UNKNOWN',
] as const

export type TransferErrorCode = typeof TRANSFER_ERROR_CODES[number]

/** Error.name 的携带格式前缀（跨 IPC 的第二条腿） */
export const TRANSFER_ERROR_NAME_PREFIX = 'TransferError:'

/**
 * 是否保留断点并自动续传一次。三类值得再试：
 *   NETWORK / STALLED —— 链路抖动，.part 是可信前缀，续传即接上
 *   RENAME_FAILED     —— 数据已完整落到 .part（审查 P2），重试只差 rename 一步
 * 磁盘满/权限/路径不存在这些改配置才能恢复，重试只是让用户多等一轮。
 */
export function isTransferRetryable(code: TransferErrorCode): boolean {
  return code === 'NETWORK' || code === 'STALLED' || code === 'RENAME_FAILED'
}

/** 生产端：把 code 编进 name（保真性最强的跨 IPC 通道） */
export function encodeTransferErrorName(code: TransferErrorCode): string {
  return `${TRANSFER_ERROR_NAME_PREFIX}${code}`
}

/**
 * 消费端：从 `code` 属性或 `TransferError:<CODE>` name 里取回错误码。
 * 这里的枚举归属校验是有意为之（S0-23 同族）：渲染端拿到的是跨进程字符串，
 * 裸 cast 会把「Error」「getaddrinfo」这类不相干值当成合法 code 往下传。
 */
export function parseTransferErrorCode(value: unknown): TransferErrorCode | null {
  if (typeof value !== 'string' || !value) return null
  const bare = value.startsWith(TRANSFER_ERROR_NAME_PREFIX)
    ? value.slice(TRANSFER_ERROR_NAME_PREFIX.length)
    : value
  return (TRANSFER_ERROR_CODES as readonly string[]).includes(bare)
    ? (bare as TransferErrorCode)
    : null
}
