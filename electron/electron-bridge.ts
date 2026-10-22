import { randomBytes } from 'crypto'
import { appendFile } from 'fs'
import { join } from 'path'
import { app } from 'electron'
import type { SSHManager } from './ssh-manager'
import type { SSHStore } from './ssh-store'
import type { SSHErrorCode } from '../shared/ssh-error-core'
import { isSSHErrorCode, toFriendlyZh } from '../shared/ssh-error-core'

// 桥接错误持久化日志，便于排查"WS 通道不通/命令执行失败"类环境问题
// 写到 userData 目录（与其他持久化数据同处，不污染代码目录）。
const BRIDGE_LOG = join(app.getPath('userData'), 'electron-bridge.log')
/**
 * 写桥接日志。
 *
 * 用异步 appendFile 而非 appendFileSync：本函数在请求路径上被调用（ERROR 记录），
 * 同步写盘会阻塞主进程事件循环，诊断日志本身不能成为新的阻塞源。
 */
function bridgeLog(msg: string): void {
  appendFile(BRIDGE_LOG, `[${new Date().toISOString()}] ${msg}\n`, () => {
    /* 日志失败不影响功能 */
  })
}

interface ElectronBridgeConfig {
  sshManager: SSHManager
  /** 用于按连接 ID 反查 SSH 连接配置，实现定时任务的自动建会话 */
  sshStore?: SSHStore
}

interface BridgeEnv {
  token: string
}

/** 桥接业务操作：与 WS req 帧（bridge-ws-client）共用同一实现（dispatch） */
export type BridgeOp = 'ssh_exec' | 'ssh_connect' | 'ssh_disconnect' | 'ssh_list_connections' | 'ssh_session_info'
/** dispatch 结果：status 为 HTTP 风格状态码，body 与后端预期的响应体同构 */
export interface DispatchResult {
  status: number
  body: unknown
}

// timeout 钳制范围，防止调用方传入极端值
const MIN_TIMEOUT = 1
const MAX_TIMEOUT = 300

/**
 * Electron 桥接（阶段 2，WS-only）。
 *
 * 原阶段 1 为"HTTP 端口桥（30000-30100 入站）+ WS 反向通道"双通道并存；
 * WS 反向通道（主进程出站拨号后端 /ws/bridge）稳定后，HTTP 端口桥按设计移除：
 * 出站长连接不受 Windows 防火墙对 electron.exe 的入站规则影响，
 * "端口假监听 / 防火墙拦截回环入站"这一整类故障随之根除。
 *
 * 本类现仅承载两件事：
 * 1. 生成桥接 token（注入后端 env + WS hello 握手共用，单一事实来源）
 * 2. dispatch() —— WS 客户端（bridge-ws-client）收到 req 帧后的唯一业务实现
 */
export class ElectronBridge {
  private token = ''
  private sshManager: SSHManager
  private sshStore: SSHStore | undefined

  constructor(config: ElectronBridgeConfig) {
    this.sshManager = config.sshManager
    this.sshStore = config.sshStore
  }

  /** 生成桥接 token。start() 不再监听任何端口，不会失败。 */
  start(): BridgeEnv {
    this.token = randomBytes(32).toString('hex')
    return { token: this.token }
  }

  stop(): void {
    // 审查 MINOR：复位 token，避免 stop 后残留陈旧值误导调用方
    this.token = ''
  }

  /**
   * 桥接业务逻辑的**唯一实现**，WS 客户端（bridge-ws-client）收到 req 帧后调用：
   *
   * - ssh_exec：在既有 SSH 会话上执行远程命令
   * - ssh_connect：按连接 ID 建立轻量 SSH 会话（无 Shell，专供定时任务无头执行）
   * - ssh_disconnect：关闭桥建立的 SSH 会话
   * - ssh_list_connections：只投影调度侧需要的定位字段（不含凭据）
   *
   * 心跳不在这里：`{type:'ping'}` 帧由 bridge_relay 就地应答，永不进 dispatch
   * （S0-11：原先的 docstring 与 BridgeOp 都声明了一个不存在的 ping op）。
   *
   * 返回 {status, body}：status 为 HTTP 风格状态码，body 与后端预期的响应体同构。
   */
  async dispatch(op: BridgeOp, data: Record<string, any>): Promise<DispatchResult> {
    data = data || {}
    try {
      switch (op) {
        case 'ssh_exec': {
          if (!data.sshSessionId || typeof data.sshSessionId !== 'string') {
            return { status: 400, body: { success: false, error: 'Missing sshSessionId' } }
          }
          if (!data.command || typeof data.command !== 'string') {
            return { status: 400, body: { success: false, error: 'Missing command' } }
          }
          // 钳制 timeout 到安全范围
          const timeout = Math.max(MIN_TIMEOUT, Math.min(MAX_TIMEOUT, data.timeout ?? 30))
          const { stdout, stderr, exitCode } = await this.sshManager.execCommandWithExitCode(
            data.sshSessionId,
            data.command,
            timeout,
          )
          return {
            status: 200,
            body: { success: exitCode === 0, output: stdout, error: stderr, exit_code: exitCode },
          }
        }

        case 'ssh_connect': {
          const connectionId = data.connectionId
          if (typeof connectionId !== 'string' || !connectionId) {
            return { status: 400, body: { success: false, error: 'Missing connectionId' } }
          }
          if (!this.sshStore) {
            return { status: 500, body: { success: false, error: 'SSH store 未配置' } }
          }
          const connections = await this.sshStore.list()
          const conn = connections.find((c) => c.id === connectionId)
          if (!conn) {
            return { status: 404, body: { success: false, error: `连接配置不存在: ${connectionId}` } }
          }
          const { id, name } = await this.sshManager.connectSftp(conn)
          // S0-06：登记这条会话归桥接所有 —— ssh_disconnect 只认这个标记
          this.sshManager.markBridgeSession(id)
          return { status: 200, body: { success: true, sessionId: id, name } }
        }

        case 'ssh_list_connections': {
          if (!this.sshStore) {
            return { status: 500, body: { success: false, error: 'SSH store 未配置' } }
          }
          const all = await this.sshStore.list()
          // 只投影调度侧需要的定位字段。sshStore.list() 返回的是**已解密**的记录
          // （ssh_connect 正是拿它去建连的），原样回给后端等于把口令/passphrase
          // 私钥路径发到 127.0.01 的 HTTP 响应体里，而这条通道没有鉴权。
          return {
            status: 200,
            body: {
              success: true,
              connections: all.map((c) => ({
                id: c.id,
                name: c.name,
                host: c.host,
                port: c.port,
                username: c.username,
                authType: c.authType,
                environment: c.environment ?? 'test',
              })),
            },
          }
        }

        case 'ssh_session_info': {
          const sessionId = data.sshSessionId
          if (typeof sessionId !== 'string' || !sessionId) {
            return { status: 400, body: { success: false, error: 'Missing sshSessionId' } }
          }
          if (!this.sshStore) {
            return { status: 500, body: { success: false, error: 'SSH store 未配置' } }
          }
          // 会话→连接配置的映射只能由主进程给：后端据此把「这次 run 到底在哪台主机上」
          // 和服务端持久化的 environment 标记对上（渲染层自报的 environment 不可信）。
          // 会话不在池里（已断开/旧 id）→ 404，后端会降级为「未核实」而不是打掉 run。
          const connectionId = this.sshManager.getSessionConnectionId(sessionId)
          if (!connectionId) {
            return { status: 404, body: { success: false, error: 'SSH 会话不存在或已断开' } }
          }
          const all = await this.sshStore.list()
          const conn = all.find((c) => c.id === connectionId)
          if (!conn) {
            return { status: 404, body: { success: false, error: '连接配置已删除' } }
          }
          // 与 ssh_list_connections 同一投影口径：只回定位字段，绝不带凭据
          return {
            status: 200,
            body: {
              success: true,
              connectionId: conn.id,
              name: conn.name,
              host: conn.host,
              port: conn.port,
              username: conn.username,
              environment: conn.environment ?? 'test',
            },
          }
        }

        case 'ssh_disconnect': {
          const sessionId = data.sessionId
          if (typeof sessionId !== 'string' || !sessionId) {
            return { status: 400, body: { success: false, error: 'Missing sessionId' } }
          }
          // S0-06：这条 docstring 承诺（「关闭由 connect_ssh 建立的 SSH 会话」）原先
          // 没有机制承载 —— 只验非空字符串，后端拿着 id 就能关掉用户的终端 tab。
          // 现在的判据是桥接建连时登记的归属集合。
          if (!this.sshManager.isBridgeSession(sessionId)) {
            return {
              status: 403,
              body: { success: false, error: '桥接只能关闭由 connect_ssh 建立的会话' },
            }
          }
          this.sshManager.disconnect(sessionId)
          return { status: 200, body: { success: true } }
        }

        default:
          return { status: 404, body: { success: false, error: 'Not found' } }
      }
    } catch (err: any) {
      // 原实现把 err 整个吞掉，500 根因（execCommandWithExitCode 抛错、
      // 会话丢失等）永远查不到；记日志后再回 500。
      console.error(`[ElectronBridge] ${op} 处理失败:`, err)
      // S0-09：这条腿原先把任意失败折成同一句「请求处理失败，请稍后重试」，而
      // 渲染层那条腿（main.ts handleTrusted → toIpcTransport）保住了 code ——
      // 同一个错误两种保真度。后端因此分不清「会话没了（可 ssh_connect 救回）」
      // 「命令合法超时」「会话数触顶」，classify_bridge_error 的
      // `"timeout" in msg` / `"未配置" in msg` 探针对该类恒不可达。
      // 这里把已分类的 code 与它的用户文案带回响应体：error 走共享文案表
      // （成员串本就是设计给用户看的中文），UNKNOWN 除外 —— 那一支的 detail
      // 是原始技术串（getaddrinfo / Cannot parse privateKey），不该端给用户。
      const code: SSHErrorCode | undefined = isSSHErrorCode(err?.code) ? err.code : undefined
      const friendly = code && code !== 'UNKNOWN' ? toFriendlyZh(code, err?.detail) : undefined
      bridgeLog(`ERROR ${op}: ${err?.message || err}${code ? ` [${code}]` : ''}`)
      return {
        status: 500,
        body: {
          success: false,
          error: friendly || '请求处理失败，请稍后重试',
          ...(code ? { code, retryable: err?.retryable === true } : {}),
        },
      }
    }
  }

}

