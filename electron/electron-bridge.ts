/**
 * Electron Bridge — 本地 HTTP 服务，供 Python 后端调用主进程能力
 * 仅监听 127.0.0.1，使用随机端口 + token 认证
 */
import http from 'http'
import { randomBytes } from 'crypto'
import type { SSHManager } from './ssh-manager'

export interface ElectronBridgeConfig {
  sshManager: SSHManager
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

  constructor(config: ElectronBridgeConfig) {
    this.sshManager = config.sshManager
  }

  async start(): Promise<BridgeEnv> {
    this.token = randomBytes(32).toString('hex')
    this.port = await this.findFreePort(30000, 30100)
    this.server = http.createServer((req, res) => this.handleRequest(req, res))

    return new Promise<BridgeEnv>((resolve, reject) => {
      this.server!.once('error', reject)
      this.server!.listen(this.port, '127.0.0.1', () => {
        console.log(`[ElectronBridge] listening on 127.0.0.1:${this.port}`)
        resolve({ port: this.port, token: this.token })
      })
    })
  }

  stop(): void {
    if (this.server) {
      this.server.close()
      this.server = null
    }
  }

  private async handleRequest(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
    res.setHeader('Content-Type', 'application/json')

    const authHeader = req.headers['x-bridge-token']
    if (authHeader !== this.token) {
      res.writeHead(401)
      res.end(JSON.stringify({ success: false, error: 'Unauthorized' }))
      return
    }

    if (req.method !== 'POST' || req.url !== '/ssh/exec') {
      res.writeHead(404)
      res.end(JSON.stringify({ success: false, error: 'Not found' }))
      return
    }

    // 收集请求体，带大小限制
    let body = ''
    let bodyTooLarge = false

    req.on('data', (chunk: Buffer) => {
      body += chunk.toString('utf-8')
      if (body.length > MAX_BODY_SIZE) {
        bodyTooLarge = true
        req.destroy()
      }
    })

    req.on('error', () => {
      // 客户端异常断开，无需响应
    })

    req.on('end', async () => {
      if (bodyTooLarge) {
        res.writeHead(413)
        res.end(JSON.stringify({ success: false, error: 'Request body too large' }))
        return
      }

      try {
        const data = JSON.parse(body) as ExecRequest

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
      } catch (err: any) {
        res.writeHead(500)
        res.end(JSON.stringify({ success: false, error: '命令执行失败，请稍后重试' }))
      }
    })
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
