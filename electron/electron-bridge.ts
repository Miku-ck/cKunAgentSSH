import { randomBytes } from 'crypto'
import { appendFile } from 'fs'
import { join } from 'path'
import { app } from 'electron'
import type { SSHManager } from './ssh-manager'
import type { SSHStore } from './ssh-store'

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
export type BridgeOp = 'ssh_exec' | 'ssh_connect' | 'ssh_disconnect'
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
 * "端口假监听 / 防火墙拦截回环入站"这一整类故障随之根除
 * （见 docs/superpowers/specs/2026-09-14-bridge-ws-channel-design.md 阶段 2）。
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
   * - ping：纯健康检查，不触碰 SSH/业务逻辑
   * - ssh_exec：在既有 SSH 会话上执行远程命令
   * - ssh_connect：按连接 ID 建立轻量 SSH 会话（无 Shell，专供定时任务无头执行）
   * - ssh_disconnect：关闭桥建立的 SSH 会话
   *
   * 返回 {status, body}：status 为 HTTP 风格状态码，body 与后端预期的响应体同构。
   * 业务异常在此捕获记日志并回 500（用户可见文案保持通用，细节只进日志）。
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
          return { status: 200, body: { success: true, sessionId: id, name } }
        }

        case 'ssh_disconnect': {
          const sessionId = data.sessionId
          if (typeof sessionId !== 'string' || !sessionId) {
            return { status: 400, body: { success: false, error: 'Missing sessionId' } }
          }
          this.sshManager.disconnect(sessionId)
          return { status: 200, body: { success: true } }
        }

        default:
          return { status: 404, body: { success: false, error: 'Not found' } }
      }
    } catch (err: any) {
      // 原实现把 err 整个吞掉，500 根因（execCommandWithExitCode 抛错、
      // 会话丢失等）永远查不到；记日志后再回 500（用户可见文案保持通用）
      console.error(`[ElectronBridge] ${op} 处理失败:`, err)
      bridgeLog(`ERROR ${op}: ${err?.message || err}`)
      return { status: 500, body: { success: false, error: '请求处理失败，请稍后重试' } }
    }
  }

}

