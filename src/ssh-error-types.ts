/**
 * ssh-error-types.ts — 渲染端 SSH 错误体系
 *
 * 错误码契约 / isRetryable / 中文文案与 electron/ssh-error.ts 共享同一
 * 单一实现源（shared/ssh-error-core.ts），本文件只提供消费侧工具：
 *   - extractSSHError(err) — 从 Error 中提取 SSHErrorTransport（优先 __sshError，回退 name 解析）
 *   - toFriendlySshMessage(code) — 按 code 返回中文友好文案（共享实现的别名导出）
 *
 * 此文件在前端全局可用,不依赖 Electron 主进程代码
 */

import type { SSHErrorCode } from '../shared/ssh-error-core'
import { isRetryable, toFriendlyZh } from '../shared/ssh-error-core'

export type { SSHErrorCode }
export { isRetryable }

/** 按 code 生成文案 — 共享实现别名（原手抄副本已删，CONN_REFUSED 丢 detail 后缀的漂移随之修复） */
export const toFriendlySshMessage = toFriendlyZh

/** IPC 透传格式（与 electron 侧 SSHErrorTransport 完全对齐） */
export interface SSHErrorTransport {
  __sshError: true
  code: SSHErrorCode
  message: string
  retryable: boolean
  detail?: string
}

const SSH_ERROR_NAME_PREFIX = 'SSHError:'

// ════════════════════════════════════════════════
//   错误识别与提取工具
// ════════════════════════════════════════════════

/** 从 IPC Error 中提取 SSHErrorTransport */
export function extractSSHError(err: unknown): SSHErrorTransport | null {
  if (!err || typeof err !== 'object') return null
  const e = err as any

  // 优先：__sshError 完整 transport（Electron 32+ 直接映射）
  if (e.__sshError && typeof e.__sshError === 'object' && e.__sshError.__sshError === true) {
    return e.__sshError as SSHErrorTransport
  }
  // 回退：解析 e.name = 'SSHError:<code>' 格式
  if (typeof e.name === 'string' && e.name.startsWith(SSH_ERROR_NAME_PREFIX)) {
    const code = e.name.slice(SSH_ERROR_NAME_PREFIX.length) as SSHErrorCode
    return {
      __sshError: true,
      code,
      message: e.message || toFriendlySshMessage(code),
      retryable: isRetryable(code),
      detail: undefined,
    }
  }
  return null
}


