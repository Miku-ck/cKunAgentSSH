/**
 * Electron Bridge — 本地 HTTP 服务，供 Python 后端调用主进程能力
 * 仅监听 127.0.0.1，使用随机端口 + token 认证
 */
import http from 'http'
import { randomBytes } from 'crypto'
import { appendFileSync } from 'fs'
import { join } from 'path'
import type { SSHManager } from './ssh-manager'
import type { SSHStore } from './ssh-store'

// 桥接启动/错误持久化日志，便于排查"主进程 socket 假监听/崩溃"类环境问题
const BRIDGE_LOG = join(process.cwd(), 'electron-bridge.log')
function bridgeLog(msg: string): void {
  try {
    appendFileSync(BRIDGE_LOG, `[${new Date().toISOString()}] ${msg}\n`)
  } catch {
    /* 日志失败不影响功能 */
  }
}

export interface ElectronBridgeConfig {
  sshManager: SSHManager
  /** 用于按连接 ID 反查 SSH 连接配置，实现定时任务的自动建会话 */
  sshStore?: SSHStore
}

export interface ExecRequest {
  sshSessionId: string
  command: string
  timeout?: number
}

export interface BridgeEnv {
  port: number
  token: string
}

// 请求体大小上限（字节），防止恶意/异常请求耗尽内存
const MAX_BODY_SIZE = 1024 * 1024 // 1MB
// timeout 钳制范围，防止调用方传入极端值
const MIN_TIMEOUT = 1
const MAX_TIMEOUT = 300

export class ElectronBridge {
  private server: http.Server | null = null
  private port = 0
  private token = ''
  private sshManager: SSHManager
  private sshStore: SSHStore | undefined

  constructor(config: ElectronBridgeConfig) {
    this.sshManager = config.sshManager
    this.sshStore = config.sshStore
  }

  async start(): Promise<BridgeEnv> {
    this.token = randomBytes(32).toString('hex')
    this.port = await this.findFreePort(30000, 30100)
    this.server = http.createServer((req, res) => this.handleRequest(req, res))

    return new Promise<BridgeEnv>((resolve, reject) => {
      this.server!.once('error', (err: Error) => {
        bridgeLog(`server 'error' event on port ${this.port}: ${err.message}`)
        reject(err)
      })
      this.server!.listen(this.port, '127.0.0.1', () => {
        bridgeLog(`listen callback fired successfully on 127.0.0.1:${this.port}`)
        console.log(`[ElectronBridge] listening on 127.0.0.1:${this.port}`)
        resolve({ port: this.port, token: this.token })
      })
    })
  }

  stop(): void {
    if (this.server) {
      this.server.close()
      // 审查 MINOR：主动断开 keep-alive 连接，否则 close() 会挂起等待空闲连接超时
      this.server.closeAllConnections?.()
      this.server = null
    }
    // 审查 MINOR：复位端口/token，避免 stop 后残留陈旧值误导调用方
    this.port = 0
    this.token = ''
  }

  private async handleRequest(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
    // E-3 修复：必须在 auth 检查之前挂 'error' 监听 — 客户端中途断开时，
    // 401/404 路径的 res.end() 会抛 EPIPE，无监听即成未捕获异常，崩掉主进程
    res.on('error', () => { /* 客户端已断开，忽略 */ })
    res.setHeader('Content-Type', 'application/json')

    const authHeader = req.headers['x-bridge-token']
    // C2 修复：使用 timingSafeEqual 防止时序攻击逐字节猜测 token
    if (!this._secureCompare(String(authHeader ?? ''), this.token)) {
      res.writeHead(401)
      res.end(JSON.stringify({ success: false, error: 'Unauthorized' }))
      return
    }

    if (req.method !== 'POST') {
      res.writeHead(404)
      res.end(JSON.stringify({ success: false, error: 'Not found' }))
      return
    }
    const path = req.url || ''
    const KNOWN_PATHS = ['/ssh/exec', '/ssh/connect', '/ssh/disconnect']
    if (!KNOWN_PATHS.includes(path)) {
      res.writeHead(404)
      res.end(JSON.stringify({ success: false, error: 'Not found' }))
      return
    }

    // 收集请求体，带大小限制
    let body = ''
    let bodyTooLarge = false

    req.on('data', (chunk: Buffer) => {
      if (bodyTooLarge) return
      body += chunk.toString('utf-8')
      if (body.length > MAX_BODY_SIZE) {
        // 审查 MINOR 修复：原实现超限即 req.destroy() — 流一断 'end' 永不触发，
        // 下方 413 分支是死代码，客户端只看到连接被重置而无任何响应。
        // 现在先回 413 再断开；后续 data/end 回调均检查 headersSent 幂等返回
        bodyTooLarge = true
        if (!res.headersSent) {
          res.writeHead(413)
          res.end(JSON.stringify({ success: false, error: 'Request body too large' }))
        }
        req.destroy()
      }
    })

    req.on('error', () => {
      // 客户端异常断开，无需响应
    })

    req.on('end', async () => {
      if (bodyTooLarge || res.headersSent) {
        return
      }

      try {
        const data = JSON.parse(body) as Record<string, any>
        // 根据路径分发到对应的处理器，异常在各自处理器内捕获回 500
        if (path === '/ssh/exec') {
          await this.handleExec(res, data as ExecRequest)
        } else if (path === '/ssh/connect') {
          await this.handleConnect(res, data)
        } else if (path === '/ssh/disconnect') {
          await this.handleDisconnect(res, data)
        }
      } catch (err: any) {
        // 审查 MINOR：原实现把 err 整个吞掉，500 根因（execCommandWithExitCode 抛错、
        // 会话丢失等）永远查不到；记日志后再回 500
        console.error(`[ElectronBridge] ${path} 处理失败:`, err)
        if (!res.headersSent) {
          res.writeHead(500)
          res.end(JSON.stringify({ success: false, error: '请求处理失败，请稍后重试' }))
        }
      }
    })
  }

  /** 在既有 SSH 会话上执行远程命令（原 /ssh/exec 逻辑）。 */
  private async handleExec(res: http.ServerResponse, data: ExecRequest): Promise<void> {
    // 验证必填字段
    if (!data.sshSessionId || typeof data.sshSessionId !== 'string') {
      res.writeHead(400)
      res.end(JSON.stringify({ success: false, error: 'Missing sshSessionId' }))
      return
    }
    if (!data.command || typeof data.command !== 'string') {
      res.writeHead(400)
      res.end(JSON.stringify({ success: false, error: 'Missing command' }))
      return
    }

    // 钳制 timeout 到安全范围
    const timeout = Math.max(MIN_TIMEOUT, Math.min(MAX_TIMEOUT, data.timeout ?? 30))

    const { stdout, stderr, exitCode } = await this.sshManager.execCommandWithExitCode(
      data.sshSessionId,
      data.command,
      timeout,
    )
    res.writeHead(200)
    res.end(JSON.stringify({
      success: exitCode === 0,
      output: stdout,
      error: stderr,
      exit_code: exitCode,
    }))
  }

  /**
   * LAST-MILE/:src 之外的分发端点 — 按连接 ID 建立一条轻量 SSH 会话（无 Shell，
   * 专供定时任务无头执行）。返回真实会话 ID，供后续 /ssh/exec 使用。
   */
  private async handleConnect(res: http.ServerResponse, data: Record<string, any>): Promise<void> {
    const connectionId = data?.connectionId
    if (typeof connectionId !== 'string' || !connectionId) {
      res.writeHead(400)
      res.end(JSON.stringify({ success: false, error: 'Missing connectionId' }))
      return
    }
    if (!this.sshStore) {
      res.writeHead(500)
      res.end(JSON.stringify({ success: false, error: 'SSH store 未配置' }))
      return
    }
    const connections = await this.sshStore.list()
    const conn = connections.find((c) => c.id === connectionId)
    if (!conn) {
      res.writeHead(404)
      res.end(JSON.stringify({ success: false, error: `连接配置不存在: ${connectionId}` }))
      return
    }
    const { id, name } = await this.sshManager.connectSftp(conn)
    res.writeHead(200)
    res.end(JSON.stringify({ success: true, sessionId: id, name }))
  }

  /** 关闭由 /ssh/connect（或桥接）建立的 SSH 会话，释放连接资源。 */
  private async handleDisconnect(res: http.ServerResponse, data: Record<string, any>): Promise<void> {
    const sessionId = data?.sessionId
    if (typeof sessionId !== 'string' || !sessionId) {
      res.writeHead(400)
      res.end(JSON.stringify({ success: false, error: 'Missing sessionId' }))
      return
    }
    this.sshManager.disconnect(sessionId)
    res.writeHead(200)
    res.end(JSON.stringify({ success: true }))
  }

  /**
   * 恒定时间字符串比较，防止时序攻击。
   * 长度不等时直接返回 false，避免任何 crypto 运算引入时序差异。
   */
  private _secureCompare(a: string, b: string): boolean {
    const bufA = Buffer.from(a, 'utf-8')
    const bufB = Buffer.from(b, 'utf-8')
    if (bufA.length !== bufB.length) {
      return false
    }
    return require('crypto').timingSafeEqual(bufA, bufB)
  }

  private findFreePort(start: number, end: number): Promise<number> {
    return new Promise((resolve, reject) => {
      const tryPort = (port: number) => {
        if (port > end) {
          reject(new Error('未找到可用端口，请检查系统端口占用情况'))
          return
        }
        const server = http.createServer()
        server.once('error', () => {
          server.close()
          tryPort(port + 1)
        })
        server.once('listening', () => {
          server.close(() => resolve(port))
        })
        server.listen(port, '127.0.0.1')
      }
      tryPort(start)
    })
  }
}
