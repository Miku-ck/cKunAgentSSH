/**
 * ssh-error-core.ts — SSH 错误码契约的单一实现源（主进程与渲染进程共享）
 *
 * 背景：electron/ssh-error.ts 与 src/types/ssh-error-types.ts 曾各自手抄一份
 * 错误码联合类型 / isRetryable / 中文文案映射，并已发生漂移
 * （渲染版 CONN_REFUSED 丢失 `(${detail})` 后缀）。此文件收敛三者，
 * 两端只做 re-export 与各自进程特有的组装（SSHError 类 / IPC transport）。
 *
 * 约束：本文件必须保持平台无关 — 不得 import electron / node 内置模块。
 */

// ════════════════════════════════════════════════
//   错误码枚举（稳定契约，不要轻易改字符串值）
//   S0-23：成员表与运行时校验函数由**同一个数组**派生 —— 类型 union 与
//   isSSHErrorCode 的名单不可能再各改各的（新增成员漏进名单 = 回读侧把合法码
//   判成非法；旧成员删了没删名单 = 幽灵码继续被当成契约的一部分）。
// ════════════════════════════════════════════════
export const SSH_ERROR_CODES = [
  // 网络层
  'CONN_REFUSED',       // 主机拒绝连接（端口未开 / 防火墙）
  'CONN_TIMEOUT',       // TCP 超时（网络不可达、IP 错）
  'CONN_RESET',         // 连接被远端重置
  'DNS_FAIL',           // 域名解析失败
  'NETWORK_ERROR',      // 其他网络异常

  // 认证层
  'AUTH_PASSWORD',      // 密码认证失败
  'AUTH_KEY',           // 私钥认证失败（密钥不匹配 / 路径错）
  'AUTH_PASSPHRASE',    // 私钥需要 passphrase 但没给或给错
  'AUTH_AGENT',         // SSH agent 认证失败
  'AUTH_UNKNOWN',       // 认证阶段失败，原因不明

  // 主机密钥
  'HOST_KEY_MISMATCH',  // 服务器密钥与 known_hosts 不匹配
  'HOST_KEY_UNKNOWN',   // 新主机且未确认信任

  // SFTP 子系统
  'SFTP_INIT_FAIL',     // SFTP 子系统打开失败（服务端不支持 / 没装）
  'SFTP_PERMISSION',    // 远端文件权限不足
  'SFTP_NOT_FOUND',     // 远端路径不存在
  'SFTP_SPACE',         // 远端磁盘空间不足
  'SFTP_FAIL',          // 其他 SFTP 操作失败

  // 命令执行
  'EXEC_TIMEOUT',       // 命令执行超时
  'EXEC_FAIL',          // 命令执行失败

  // 会话 / 资源
  'SESSION_NOT_FOUND',  // 会话不存在或已断开
  'SESSION_LIMIT',      // 会话数已达上限
  'SFTP_SESSION_REQUIRED', // 需要 SFTP 会话但未打开

  // 兜底
  'UNKNOWN',
] as const

export type SSHErrorCode = typeof SSH_ERROR_CODES[number]

/** 运行时校验：跨进程传来的 code 字符串是否属于本契约（S0-23） */
export function isSSHErrorCode(value: unknown): value is SSHErrorCode {
  return typeof value === 'string' && (SSH_ERROR_CODES as readonly string[]).includes(value)
}

/**
 * IPC 透传形状 —— 主进程 toIpcTransport 产出、渲染端 extractSSHError 消费。
 * S0-23：原先两个进程各抄一份 interface，注释写着「完全对齐」但没有任何东西
 * 保证它；收敛到这里之后，形状漂移会变成编译期错误。
 */
export interface SSHErrorTransport {
  /** 类型哨兵，用于前端 extractSSHError 识别 */
  __sshError: true
  code: SSHErrorCode
  /** 友好中文描述（始终可用；生产端渲染，消费端优先采信） */
  message: string
  retryable: boolean
  /**
   * 原始技术信息（S0-24）：生产方的 detail 优先，其次才是原始 Error 的消息。
   * 契约是「只在开发模式的详情区显示」—— 渲染端必须按 import.meta.env.DEV 门控，
   * 不要把 getaddrinfo / Cannot parse privateKey 这类串直接端给用户。
   */
  detail?: string
}

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
    default:
      return false
  }
}

/** 用户友好中文描述（toast 主文案；两端共用，消灭手抄漂移） */
export function toFriendlyZh(code: SSHErrorCode, detail?: string): string {
  switch (code) {
    case 'CONN_REFUSED':
      return `连接被拒绝。请检查主机地址和端口是否正确，以及 SSH 服务是否已启动${detail ? ` (${detail})` : ''}`
    case 'CONN_TIMEOUT':
      return `连接超时。请检查网络连通性，或在连接配置中增大"连接超时"值`
    case 'CONN_RESET':
      return '连接被远端重置，可能是服务拒绝、防火墙拦截或网络波动。可稍后重试'
    case 'DNS_FAIL': {
      // S0-14：原实现是唯一没有 detail 判空的成员 ⇒ detail 缺失时产出
      // 「无法解析主机名""」。detail 通常是 Node 原始消息
      // （"getaddrinfo ENOTFOUND foo.example"），整串塞进引号会把英文技术串
      // 混进中文 toast，因此只取其中的主机名。
      const host = /(?:enotfound|enoname|eai_again)\s+(\S+)/i.exec(detail || '')?.[1]
      return host
        ? `无法解析主机名"${host}"，请检查主机地址是否正确`
        : '主机名无法解析，请检查主机地址是否正确'
    }
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
