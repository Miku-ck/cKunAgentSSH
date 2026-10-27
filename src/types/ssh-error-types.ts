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

import type { SSHErrorCode, SSHErrorTransport } from '../../shared/ssh-error-core'
import { isRetryable, isSSHErrorCode, toFriendlyZh } from '../../shared/ssh-error-core'

export type { SSHErrorCode, SSHErrorTransport }
export { isRetryable, isSSHErrorCode }

/** 按 code 生成文案 — 共享实现别名（原手抄副本已删，CONN_REFUSED 丢 detail 后缀的漂移随之修复） */
export const toFriendlySshMessage = toFriendlyZh

const SSH_ERROR_NAME_PREFIX = 'SSHError:'

// ════════════════════════════════════════════════
//   错误识别与提取工具
// ════════════════════════════════════════════════

/**
 * 从 IPC Error 中提取 SSHErrorTransport。
 *
 * S0-23：两条腿都过 isSSHErrorCode 校验。跨进程送来的是裸字符串，原实现直接
 * `as SSHErrorTransport` / `as SSHErrorCode` —— 一个不相干的 `name`（例如第三方
 * 也按 `SSHError:` 前缀命名的错误）就能让我们拿着幽灵 code 去查文案表和 retryable，
 * 表面「已分类」实则什么都没说。
 */
export function extractSSHError(err: unknown): SSHErrorTransport | null {
  if (!err || typeof err !== 'object') return null
  const e = err as any

  // 优先：__sshError 完整 transport（Electron 32+ 直接映射）
  if (e.__sshError && typeof e.__sshError === 'object' && e.__sshError.__sshError === true) {
    const t = e.__sshError as SSHErrorTransport
    // code 不认账时降级而不是丢弃：message 本身是有效的用户文案
    return isSSHErrorCode(t.code) ? t : { ...t, code: 'UNKNOWN' }
  }
  // 回退：解析 e.name = 'SSHError:<code>' 格式
  if (typeof e.name === 'string' && e.name.startsWith(SSH_ERROR_NAME_PREFIX)) {
    const candidate = e.name.slice(SSH_ERROR_NAME_PREFIX.length)
    if (!isSSHErrorCode(candidate)) return null
    return {
      __sshError: true,
      code: candidate,
      message: e.message || toFriendlySshMessage(candidate),
      retryable: isRetryable(candidate),
      detail: undefined,
    }
  }
  return null
}


