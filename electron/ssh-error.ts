/**
 * ssh-error.ts — SSH/SFTP 统一错误分类体系
 *
 * 设计目标：
 *   1. 结构化错误码 — 前端/后端可以按 code 分支做差异化处理（重试、置灰、弹出不同文案）
 *   2. 可观测 — 保留 originalError（原始 Error 对象）供 console.error / 日志上报
 *   3. IPC 友好 — toIpcTransport() 产出的纯 JSON 对象可跨进程安全传输；
 *      fromIpcTransport() 恢复为 SSHError 实例供 instanceof 校验
 *   4. 一个入口 — classifyRawError(raw) 集中处理原始错误码，消灭重复 if/else
 */

// ════════════════════════════════════════════════
//   错误码枚举（稳定契约，不要轻易改字符串值）
// ════════════════════════════════════════════════
export type SSHErrorCode =
  // 网络层
  | 'CONN_REFUSED'       // 主机拒绝连接（端口未开 / 防火墙）
  | 'CONN_TIMEOUT'       // TCP 超时（网络不可达、IP 错）
  | 'CONN_RESET'         // 连接被远端重置
  | 'DNS_FAIL'           // 域名解析失败
  | 'NETWORK_ERROR'      // 其他网络异常

  // 认证层
  | 'AUTH_PASSWORD'      // 密码认证失败
  | 'AUTH_KEY'           // 私钥认证失败（密钥不匹配 / 路径错）
  | 'AUTH_PASSPHRASE'    // 私钥需要 passphrase 但没给或给错
  | 'AUTH_AGENT'         // SSH agent 认证失败
  | 'AUTH_UNKNOWN'       // 认证阶段失败，原因不明

  // 主机密钥
  | 'HOST_KEY_MISMATCH'  // 服务器密钥与 known_hosts 不匹配
  | 'HOST_KEY_UNKNOWN'   // 新主机且未确认信任

  // SFTP 子系统
  | 'SFTP_INIT_FAIL'     // SFTP 子系统打开失败（服务端不支持 / 没装）
  | 'SFTP_PERMISSION'    // 远端文件权限不足
  | 'SFTP_NOT_FOUND'     // 远端路径不存在
  | 'SFTP_SPACE'         // 远端磁盘空间不足
  | 'SFTP_FAIL'          // 其他 SFTP 操作失败

  // 命令执行
  | 'EXEC_TIMEOUT'       // 命令执行超时
  | 'EXEC_FAIL'          // 命令执行失败

  // 会话 / 资源
  | 'SESSION_NOT_FOUND'  // 会话不存在或已断开
  | 'SESSION_LIMIT'      // 会话数已达上限
  | 'SFTP_SESSION_REQUIRED' // 需要 SFTP 会话但未打开

  // 兜底
  | 'UNKNOWN'

/** 是否可重试（前端据此决定是否显示"重试"按钮） */
export function isRetryable(code: SSHErrorCode): boolean {
  switch (code) {
    case 'CONN_REFUSED':
    case 'CONN_TIMEOUT':
    case 'CONN_RESET':
    case 'NETWORK_ERROR':
    case 'EXEC_TIMEOUT':
    case 'SFTP_FAIL':
      return true
    case 'AUTH_PASSWORD':
    case 'AUTH_KEY':
    case 'AUTH_PASSPHRASE':
    case 'AUTH_AGENT':
    case 'AUTH_UNKNOWN':
    case 'HOST_KEY_MISMATCH':
    case 'HOST_KEY_UNKNOWN':
    case 'SFTP_PERMISSION':
    case 'SFTP_NOT_FOUND':
    case 'SFTP_SPACE':
    case 'SFTP_INIT_FAIL':
    case 'EXEC_FAIL':
    case 'SESSION_NOT_FOUND':
    case 'SESSION_LIMIT':
    case 'SFTP_SESSION_REQUIRED':
    case 'DNS_FAIL':
    case 'UNKNOWN':
      return false
    default:
      return false
  }
}

/** 用户友好中文描述（默认 toast 主文案） */
export function toFriendlyZh(code: SSHErrorCode, detail?: string): string {
  switch (code) {
    case 'CONN_REFUSED':
      return `连接被拒绝。请检查主机地址和端口是否正确，以及 SSH 服务是否已启动${detail ? ` (${detail})` : ''}`
    case 'CONN_TIMEOUT':
      return `连接超时。请检查网络连通性，或在连接配置中增大"连接超时"值`
    case 'CONN_RESET':
      return '连接被远端重置，可能是服务拒绝、防火墙拦截或网络波动。可稍后重试'
    case 'DNS_FAIL':
      return `无法解析主机名"${detail || ''}"，请检查主机地址是否正确`
    case 'NETWORK_ERROR':
      return `网络异常：${detail || '请检查网络'}`

    case 'AUTH_PASSWORD':
      return '密码认证失败，请检查用户名和密码'
    case 'AUTH_KEY':
      return '私钥认证失败，请检查私钥文件路径和权限'
    case 'AUTH_PASSPHRASE':
      return '私钥保护密码（passphrase）错误或缺失'
    case 'AUTH_AGENT':
      return 'SSH Agent 认证失败，请确认 Agent 已启动并已加载密钥'
    case 'AUTH_UNKNOWN':
      return '认证失败，请检查用户名、密码或私钥'

    case 'HOST_KEY_MISMATCH':
      return '⚠ 主机密钥验证失败：服务器密钥与记录不符，可能存在中间人攻击，或服务器已重装'
    case 'HOST_KEY_UNKNOWN':
      return '新主机密钥尚未信任，请确认服务器身份后重新连接'

    case 'SFTP_INIT_FAIL':
      return 'SFTP 子系统打开失败，请检查远程服务器是否安装/启用了 SFTP'
    case 'SFTP_PERMISSION':
      return '远端权限不足，无法执行该操作'
    case 'SFTP_NOT_FOUND':
      return '远端路径不存在或已被删除'
    case 'SFTP_SPACE':
      return '远端磁盘空间不足'
    case 'SFTP_FAIL':
      return detail ? `SFTP 操作失败：${detail}` : 'SFTP 操作失败'

    case 'EXEC_TIMEOUT':
      return `命令执行超时${detail ? `（${detail}）` : ''}，可增大命令超时时间`
    case 'EXEC_FAIL':
      return '命令执行失败，请检查命令格式或连接状态'

    case 'SESSION_NOT_FOUND':
      return 'SSH 会话不存在或已断开'
    case 'SESSION_LIMIT':
      return '会话数已达上限，请关闭闲置连接后重试'
    case 'SFTP_SESSION_REQUIRED':
      return '请先连接远程服务器'

    case 'UNKNOWN':
    default:
      return detail ? `SSH 错误：${detail}` : 'SSH 连接失败'
  }
}

/** 原始 ssh2 Error → SSHErrorCode 的分类器（一个入口） */
export function classifyRawError(raw: unknown): SSHErrorCode {
  const msg = (raw instanceof Error ? raw.message : String(raw || '')).toLowerCase()

  // 网络层
  if (/econnrefused|connection refused/.test(msg)) return 'CONN_REFUSED'
  if (/etimedout|timed out|timeout/.test(msg)) return 'CONN_TIMEOUT'
  if (/econnreset|connection reset/.test(msg)) return 'CONN_RESET'
  if (/enotfound|getaddrinfo|dns/.test(msg)) return 'DNS_FAIL'
  if (/enetwork|socket hang up|epipe|network/.test(msg)) return 'NETWORK_ERROR'

  // 认证层
  if (/authentication|auth.*fail|all configured authentication methods failed/.test(msg)) {
    if (/password/.test(msg)) return 'AUTH_PASSWORD'
    if (/passphrase/.test(msg)) return 'AUTH_PASSPHRASE'
    if (/key/.test(msg)) return 'AUTH_KEY'
    if (/agent/.test(msg)) return 'AUTH_AGENT'
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

  /** 工厂方法：从原始 Error 自动分类并实例化 */
  static from(raw: unknown, fallbackDetail?: string): SSHError {
    const code = classifyRawError(raw)
    const detail = fallbackDetail
      || (raw instanceof Error ? raw.message : undefined)
    return new SSHError(code, { detail, original: raw })
  }
}

// ════════════════════════════════════════════════
//   IPC 传输格式 & 序列化
// ════════════════════════════════════════════════
export interface SSHErrorTransport {
  __sshError: true       // 类型哨兵，用于 fromIpcTransport 识别
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

/** 从 IPC 传输格式恢复 SSHError（供 renderer/Python 侧使用） */
export function fromIpcTransport(t: SSHErrorTransport): SSHError {
  const sshErr = new SSHError(t.code, { detail: t.detail })
  return sshErr
}
