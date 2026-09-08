/**
 * ssh-error-types.ts — 渲染端 SSH 错误体系
 *
 * 与 electron/ssh-error.ts 共用错误码契约，提供前端消费侧工具函数：
 *   - detectSSHError(err)  — 自动识别普通 Error 是否携带 __sshError transport
 *   - extractSSHError(err) — 从 Error 中提取 SSHErrorTransport（优先 __sshError，回退 name 解析）
 *   - toFriendlyMessage(code) — 按 code 返回中文友好文案（对应 electron 侧的 toFriendlyZh）
 *
 * 此文件在前端全局可用,不依赖 Electron 主进程代码
 */

// ════════════════════════════════════════════════
//   与 electron/ssh-error.ts 共享的错误码契约（注意：字符串值保持严格同步）
// ════════════════════════════════════════════════
export type SSHErrorCode =
  // 网络层
  | 'CONN_REFUSED' | 'CONN_TIMEOUT' | 'CONN_RESET'
  | 'DNS_FAIL' | 'NETWORK_ERROR'
  // 认证层
  | 'AUTH_PASSWORD' | 'AUTH_KEY' | 'AUTH_PASSPHRASE' | 'AUTH_AGENT' | 'AUTH_UNKNOWN'
  // 主机密钥
  | 'HOST_KEY_MISMATCH' | 'HOST_KEY_UNKNOWN'
  // SFTP
  | 'SFTP_INIT_FAIL' | 'SFTP_PERMISSION' | 'SFTP_NOT_FOUND'
  | 'SFTP_SPACE' | 'SFTP_FAIL'
  // 命令
  | 'EXEC_TIMEOUT' | 'EXEC_FAIL'
  // 会话 / 资源
  | 'SESSION_NOT_FOUND' | 'SESSION_LIMIT' | 'SFTP_SESSION_REQUIRED'
  // 兜底
  | 'UNKNOWN'

/** IPC 透传格式（与 electron 侧 SSHErrorTransport 完全对齐） */
export interface SSHErrorTransport {
  __sshError: true
  code: SSHErrorCode
  message: string
  retryable: boolean
  detail?: string
}

const SSH_ERROR_NAME_PREFIX = 'SSHError:'

/** 是否可重试（与 electron/ssh-error.ts 逻辑同步） */
export function isRetryable(code: SSHErrorCode): boolean {
  switch (code) {
    case 'CONN_REFUSED':
    case 'CONN_TIMEOUT':
    case 'CONN_RESET':
    case 'NETWORK_ERROR':
    case 'EXEC_TIMEOUT':
    case 'SFTP_FAIL':
      return true
    default:
      return false
  }
}

/** 对应 electron/ssh-error.ts 的 toFriendlyZh — 前端按 code 生成文案 */
export function toFriendlySshMessage(code: SSHErrorCode, detail?: string): string {
  switch (code) {
    case 'CONN_REFUSED':
      return '连接被拒绝。请检查主机地址和端口是否正确，以及 SSH 服务是否已启动'
    case 'CONN_TIMEOUT':
      return '连接超时。请检查网络连通性，或在连接配置中增大"连接超时"值'
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

/** 判断错误是否为 SSH 结构化错误 */
export function isSSHError(err: unknown): boolean {
  return extractSSHError(err) !== null
}

/** 从 Error 中获取最佳显示文案：结构化错误按 code 翻译；普通 Error 直接返回 message */
export function bestDisplayMessage(err: unknown): string {
  const transport = extractSSHError(err)
  if (transport) {
    return toFriendlySshMessage(transport.code, transport.detail)
  }
  if (err instanceof Error) return err.message
  return String(err || '未知错误')
}
