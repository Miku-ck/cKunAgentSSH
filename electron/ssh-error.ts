/**
 * ssh-error.ts — SSH/SFTP 统一错误分类体系（主进程侧）
 *
 * 错误码契约 / isRetryable / 中文文案统一来自 shared/ssh-error-core.ts
 * （与渲染端 src/ssh-error-types.ts 共享单一实现源，消灭手抄漂移）。
 * 本文件仅保留主进程特有部分：
 *   - classifyRawError(raw) — 原始 ssh2 Error → 错误码分类器
 *   - SSHError — 携带 code/retryable/friendly 的 Error 子类
 *   - toIpcTransport() — 产出的纯 JSON 对象可跨进程安全传输；
 *     前端由 extractSSHError()（src/ssh-error-types.ts）解析 __sshError 哨兵还原结构化错误
 */

import type { SSHErrorCode } from '../shared/ssh-error-core'
import { isRetryable, toFriendlyZh } from '../shared/ssh-error-core'

export type { SSHErrorCode }
export { isRetryable, toFriendlyZh }

/**
 * 原始 ssh2 Error → SSHErrorCode 的分类器（一个入口）
 *
 * @param raw 原始错误
 * @param authType 本次连接配置的认证方式。ssh2 对认证失败的错误消息恒为
 *   "All configured authentication methods failed"（带 level='client-authentication'），
 *   不含方法名 —— 仅靠消息无法区分密码/私钥/agent 失败。调用方知道用户配置了
 *   哪种认证，据此给出精确错误码（密码错 → AUTH_PASSWORD 而非泛化的 AUTH_UNKNOWN）。
 */
export function classifyRawError(raw: unknown, authType?: 'password' | 'privateKey' | 'agent'): SSHErrorCode {
  const msg = (raw instanceof Error ? raw.message : String(raw || '')).toLowerCase()

  // 网络层
  if (/econnrefused|connection refused/.test(msg)) return 'CONN_REFUSED'
  if (/etimedout|timed out|timeout/.test(msg)) return 'CONN_TIMEOUT'
  if (/econnreset|connection reset/.test(msg)) return 'CONN_RESET'
  if (/enotfound|getaddrinfo|dns/.test(msg)) return 'DNS_FAIL'
  if (/enetwork|socket hang up|epipe|network/.test(msg)) return 'NETWORK_ERROR'

  // 私钥加密但缺 passphrase（keyParser 阶段报错，不含 "authentication" 字样）
  if (/no passphrase was provided/.test(msg)) return 'AUTH_PASSPHRASE'

  // 认证层（ssh2 的 client-authentication 级错误是可靠信号，消息匹配作兜底）
  if (/authentication|auth.*fail|all configured authentication methods failed/.test(msg)
      || (raw as any)?.level === 'client-authentication') {
    // 消息级细分优先（极少数场景消息本身带方法名）
    if (/passphrase/.test(msg)) return 'AUTH_PASSPHRASE'
    if (/password/.test(msg)) return 'AUTH_PASSWORD'
    if (/agent/.test(msg)) return 'AUTH_AGENT'
    if (/key/.test(msg)) return 'AUTH_KEY'
    // 消息无方法名时，按本次配置的认证方式精确归类
    if (authType === 'password') return 'AUTH_PASSWORD'
    if (authType === 'privateKey') return 'AUTH_KEY'
    if (authType === 'agent') return 'AUTH_AGENT'
    return 'AUTH_UNKNOWN'
  }

  // 主机密钥（必须在 AUTH 之后更优先匹配）
  if (/host verification|host key.*chang|key_.*not|verification failed/.test(msg)) return 'HOST_KEY_MISMATCH'
  if (/unknown host|not in the list/.test(msg)) return 'HOST_KEY_UNKNOWN'

  return 'UNKNOWN'
}

// ════════════════════════════════════════════════
//   SSHError 类
// ════════════════════════════════════════════════
export class SSHError extends Error {
  readonly code: SSHErrorCode
  readonly retryable: boolean
  readonly original: unknown
  /** 自动生成的友好中文提示 */
  readonly friendly: string

  constructor(
    code: SSHErrorCode,
    opts: {
      detail?: string
      original?: unknown
    } = {},
  ) {
    const friendly = toFriendlyZh(code, opts.detail)
    super(friendly)
    this.name = 'SSHError'
    this.code = code
    this.retryable = isRetryable(code)
    this.original = opts.original
    this.friendly = friendly
  }

  /** 工厂方法：从原始 Error 自动分类并实例化（authType 用于精确归类认证失败） */
  static from(raw: unknown, fallbackDetail?: string, authType?: 'password' | 'privateKey' | 'agent'): SSHError {
    const code = classifyRawError(raw, authType)
    const detail = fallbackDetail
      || (raw instanceof Error ? raw.message : undefined)
    return new SSHError(code, { detail, original: raw })
  }
}

// ════════════════════════════════════════════════
//   IPC 传输格式 & 序列化
// ════════════════════════════════════════════════
export interface SSHErrorTransport {
  __sshError: true       // 类型哨兵，用于前端 extractSSHError 识别
  code: SSHErrorCode
  message: string        // 友好中文描述（始终可用）
  retryable: boolean
  detail?: string        // 原始技术信息（开发模式才显示给终端用户）
}

/** 序列化为可跨 IPC 传输的纯 JSON 对象（Electron IPC 自动序列化 Error 会丢失 name/code） */
export function toIpcTransport(err: SSHError): SSHErrorTransport {
  return {
    __sshError: true,
    code: err.code,
    message: err.message,
    retryable: err.retryable,
    detail: err.original instanceof Error ? err.original.message : undefined,
  }
}


