/**
 * Electron Bridge — 本地 HTTP 服务，供 Python 后端调用主进程能力
 * 仅监听 127.0.0.1，使用随机端口 + token 认证
 */
import http from 'http'
import net from 'net'
import { randomBytes, timingSafeEqual } from 'crypto'
import { appendFile } from 'fs'
import { join } from 'path'
import { app } from 'electron'
import type { SSHManager } from './ssh-manager'
import type { SSHStore } from './ssh-store'

// 桥接启动/错误持久化日志，便于排查"主进程 socket 假监听/崩溃"类环境问题
// P3 修复：原写 process.cwd()，应用运行后工作区残留 electron-bridge.log 运行时文件；
// 改写到 userData 目录（与其他持久化数据同处，不污染代码目录）。
const BRIDGE_LOG = join(app.getPath('userData'), 'electron-bridge.log')
/**
 * 写桥接日志。
 *
 * 用异步 appendFile 而非 appendFileSync：本函数在请求路径上被调用（REQ/RESP 记录），
 * 同步写盘会阻塞主进程事件循环 —— 而事件循环阻塞正是桥接不可达时最难排查的形态之一，
 * 诊断日志本身不能成为新的阻塞源。
 */
function bridgeLog(msg: string): void {
  appendFile(BRIDGE_LOG, `[${new Date().toISOString()}] ${msg}\n`, () => {
    /* 日志失败不影响功能 */
  })
}

export interface ElectronBridgeConfig {
  sshManager: SSHManager
  /** 用于按连接 ID 反查 SSH 连接配置，实现定时任务的自动建会话 */
  sshStore?: SSHStore
}

export interface BridgeEnv {
  port: number
  token: string
}

/** 桥接业务操作：HTTP 路径与 WS req 帧共用同一实现（dispatch） */
export type BridgeOp = 'ping' | 'ssh_exec' | 'ssh_connect' | 'ssh_disconnect'
/** dispatch 结果：status 为 HTTP 风格状态码，body 与旧 HTTP 响应体同构 */
export interface DispatchResult {
  status: number
  body: unknown
}
/** HTTP 路径 → 操作映射（单一事实来源；WS 通道直接用 op，不经过此表） */
const PATH_TO_OP: Record<string, BridgeOp> = {
  '/ping': 'ping',
  '/ssh/exec': 'ssh_exec',
  '/ssh/connect': 'ssh_connect',
  '/ssh/disconnect': 'ssh_disconnect',
}

// 请求体大小上限（字节），防止恶意/异常请求耗尽内存
const MAX_BODY_SIZE = 1024 * 1024 // 1MB
// timeout 钳制范围，防止调用方传入极端值
const MIN_TIMEOUT = 1
const MAX_TIMEOUT = 300

// ── 端口绑定与自检参数 ──────────────────────────────
const PORT_RANGE_START = 30000
const PORT_RANGE_END = 30100
/** 启动自检的连接超时：端口"假监听"时 connect 不会失败而是挂起，必须有上限 */
const SELF_PROBE_TIMEOUT_MS = 1500
/**
 * 启动时最多尝试几轮（每轮换一个端口）。
 *
 * 取 2 而非更大值：换端口只能绕过"单个端口幽灵"，对"按可执行文件拦截回环"
 * 这类与端口无关的成因完全无效 —— 实测该场景下多试只是白白拖慢启动
 * （每轮自检超时 1.5s）。2 轮足以区分这两类成因。
 */
const START_MAX_ATTEMPTS = 2
/** 运行期自检间隔：端口中途变为不可达时至少在日志中留痕 */
const RUNTIME_PROBE_INTERVAL_MS = 60_000

export class ElectronBridge {
  private server: http.Server | null = null
  private port = 0
  private token = ''
  private sshManager: SSHManager
  private sshStore: SSHStore | undefined
  private probeTimer: NodeJS.Timeout | null = null
  /** 启动自检判定为"端口不可达"（绑定成功但入站连不上）*/
  private unreachable = false

  constructor(config: ElectronBridgeConfig) {
    this.sshManager = config.sshManager
    this.sshStore = config.sshStore
  }

  /**
   * 启动桥接服务，返回后端可用的端口/token。
   *
   * 核心修复（假监听自愈）：listen 回调触发只证明 bind+listen 系统调用成功，
   * **不证明端口真正可达**。实测 Windows 上存在这种状态：
   *   - netstat / Get-NetTCPConnection 显示 127.0.0.1:PORT LISTENING，owner 是主进程
   *   - 主进程存活、事件循环健康（CPU 采样无异常、线程均为 UserRequest 等待）
   *   - 但对 127.0.0.1:PORT 的 TCP connect 直接超时（SYN 无响应、accept 队列无堆积）
   *   - 后果：后端拿到一个永远不会响应的端口，所有定时任务以
   *     [BRIDGE_PORT_CLOSED]/探活超时 失败，且日志上看不出"端口其实是通的"
   *
   * 因此启动时主动 connect 探测自身：不可达就整体换端口重建，避免把坏端口注入后端。
   * 此自检发生在 backendManager.start(extraEnv) 之前，换端口不会破坏后端侧契约。
   */
  async start(): Promise<BridgeEnv> {
    this.token = randomBytes(32).toString('hex')

    // 记录最后一次实际绑定到的端口：自检全部失败时仍返回它（降级交给后端，
    // 让后端探活失败并给出可操作指引，而不是让整个应用起不来）
    let lastPort = 0
    let lastError: Error | null = null
    // 本轮绑定的起始端口。自检失败后必须递增：server 被销毁后端口会被释放，
    // 若下一轮仍从 PORT_RANGE_START 开始，就会重新绑到同一个端口 ——
    // 重试沦为原地空转。（实测现象：三轮日志全部是 30000。）
    let nextStartPort = PORT_RANGE_START

    for (let attempt = 1; attempt <= START_MAX_ATTEMPTS; attempt++) {
      const server = http.createServer((req, res) => this.handleRequest(req, res))

      let port: number
      try {
        port = await this.listenFirstFree(server, nextStartPort, PORT_RANGE_END)
      } catch (err) {
        // 整个端口区间都绑不上，换端口重试没有意义
        lastError = err instanceof Error ? err : new Error(String(err))
        bridgeLog(`listenFirstFree failed: ${lastError.message}`)
        this.destroyServer(server)
        break
      }

      this.server = server
      this.port = port
      console.log(
        `[ElectronBridge] listening on 127.0.0.1:${port} (attempt ${attempt}/${START_MAX_ATTEMPTS})`,
      )
      bridgeLog(`listen callback fired successfully on 127.0.0.1:${port} (attempt ${attempt})`)

      if (await this.probeSelf(port)) {
        bridgeLog(`self-probe OK on 127.0.0.1:${port}`)
        this.startRuntimeProbe()
        return { port, token: this.token }
      }

      lastPort = port
      console.warn(
        `[ElectronBridge] self-probe failed on port ${port} (端口不可达), rebinding on a new port`,
      )
      bridgeLog(`self-probe FAILED on 127.0.0.1:${port} — 绑定成功但入站不可达，换端口重建`)
      this.destroyServer(server)
      this.server = null
      this.port = 0

      // 递增起始端口，确保下一轮真的换端口；已到区间末尾则再试也是同一端口，直接结束
      if (port >= PORT_RANGE_END) break
      nextStartPort = port + 1
    }

    // ── 降级路径：自检全部失败 ──────────────────────
    // 不抛异常。桥接不可达只影响"定时任务的无头远程执行"这一条链路；
    // 手动 SSH 终端 / 文件管理 / 对话 / 审计都不经过桥接。
    // 若在此抛异常，main.ts 的 catch 会连带跳过 Python 后端的启动，
    // 把局部功能故障放大成整个应用不可用 —— 这是不可接受的 trade-off。
    // 实测成因：Windows 防火墙对回环 TCP 入站按可执行文件放行，electron.exe
    // 没有 Allow 规则时，其监听端口无法被连接（同一端口交给有规则的
    // python.exe 绑定即立即可达）。换端口绕不过该规则，故只告警不再重试。
    this.unreachable = true
    // 与返回值保持一致：调用方（及后续诊断）读 this.port 时应拿到同一个端口，
    // 由 isUnreachable() 表达"该端口虽返回但入站不可达"这一状态。
    this.port = lastPort
    const hint =
      'Electron bridge 端口绑定成功但入站不可达。最常见原因：Windows 防火墙未放行 ' +
      'electron.exe 的入站连接。请以管理员身份为该 exe 添加入站允许规则' +
      '（或直接以管理员身份运行 scripts\\fix-electron-firewall.ps1）后重启应用。'
    if (lastPort === 0) {
      // 整个端口区间被占满：从未成功绑定过，失败根因是 listenFirstFree 报错而非防火墙，
      // 单独提示，避免误导用户去修防火墙
      console.error(
        `[ElectronBridge] 桥接无法启动：${PORT_RANGE_START}-${PORT_RANGE_END} 区间无可用端口（${lastError?.message ?? 'unknown error'}）`,
      )
      bridgeLog(
        `UNREACHABLE: no free port in ${PORT_RANGE_START}-${PORT_RANGE_END}: ${lastError?.message ?? 'unknown error'}`,
      )
    } else if (lastError) {
      // 混合失败：前一轮绑定成功但自检失败（lastPort 非 0），最后一轮连端口都没绑上。
      // 两个根因都要给出，否则用户只看到防火墙提示，漏掉"端口区间耗尽"这一半
      console.error(
        `[ElectronBridge] ${hint} (lastPort=${lastPort}；另：${lastError.message})`,
      )
      bridgeLog(
        `UNREACHABLE: ${hint} lastPort=${lastPort}; listenFirstFree error: ${lastError.message}`,
      )
    } else {
      console.error(`[ElectronBridge] ${hint} (lastPort=${lastPort})`)
      bridgeLog(
        `UNREACHABLE after ${START_MAX_ATTEMPTS} attempts: ${hint} lastPort=${lastPort}`,
      )
    }
    return { port: lastPort, token: this.token }
  }

  /**
   * 桥接是否在启动自检中被判定为不可达（端口已监听但入站连不上）。
   * 供主进程按需向渲染层提示"定时任务将不可用"。
   */
  isUnreachable(): boolean {
    return this.unreachable
  }

  stop(): void {
    this.stopRuntimeProbe()
    if (this.server) {
      this.destroyServer(this.server)
      this.server = null
    }
    // 审查 MINOR：复位端口/token，避免 stop 后残留陈旧值误导调用方
    this.port = 0
    this.token = ''
  }

  // ── 生命周期辅助 ────────────────────────────────

  /** 关闭 server 并强制断开 keep-alive 连接（否则 close() 会挂起等待空闲连接超时）*/
  private destroyServer(server: http.Server): void {
    try {
      server.close()
      server.closeAllConnections?.()
    } catch {
      /* server 可能已关闭，忽略 */
    }
  }

  /**
   * 通过回环 connect 探测端口是否真正可达。
   *
   * 与 listen 回调互补：回调只反映本地系统调用结果，本探测反映"外部能否连上"。
   * 连接建立后立即 destroy —— 不发送任何数据，不产生 HTTP 请求，服务端无副作用。
   */
  private probeSelf(port: number, timeoutMs: number = SELF_PROBE_TIMEOUT_MS): Promise<boolean> {
    return new Promise((resolve) => {
      const sock = net.connect({ port, host: '127.0.0.1' })
      let settled = false
      const finish = (ok: boolean): void => {
        if (settled) return
        settled = true
        sock.destroy()
        resolve(ok)
      }
      sock.setTimeout(timeoutMs)
      sock.once('connect', () => finish(true))
      sock.once('timeout', () => finish(false))
      sock.once('error', () => finish(false))
    })
  }

  /** 运行期周期性自检：端口中途失效时留下可排查的日志痕迹 */
  private startRuntimeProbe(): void {
    this.stopRuntimeProbe()
    this.probeTimer = setInterval(() => {
      const port = this.port
      if (!port) return
      void this.probeSelf(port).then((ok) => {
        if (!ok) {
          console.warn(`[ElectronBridge] runtime self-probe failed on port ${port}`)
          bridgeLog(
            `runtime self-probe FAILED on 127.0.0.1:${port} — 桥接不可达，定时任务将失败，请重启应用`,
          )
        }
      })
    }, RUNTIME_PROBE_INTERVAL_MS)
    // 不因该定时器阻止进程退出
    this.probeTimer.unref?.()
  }

  private stopRuntimeProbe(): void {
    if (this.probeTimer) {
      clearInterval(this.probeTimer)
      this.probeTimer = null
    }
  }

  // ── 请求处理 ────────────────────────────────────

  private async handleRequest(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
    const startedAt = Date.now()
    // E-3 修复：必须在 auth 检查之前挂 'error' 监听 — 客户端中途断开时，
    // 401/404 路径的 res.end() 会抛 EPIPE，无监听即成未捕获异常，崩掉主进程
    res.on('error', () => { /* 客户端已断开，忽略 */ })
    res.setHeader('Content-Type', 'application/json')

    // 请求到达即落盘。这是区分两类故障的唯一依据：
    //   - 日志中无 REQ 记录 → 请求没到（网络/端口层故障，即"假监听"）
    //   - 有 REQ 无 RESP    → 请求已到但处理挂起（应用层故障）
    bridgeLog(`REQ ${req.method} ${req.url || ''} from ${req.socket.remoteAddress || '?'}`)
    res.on('finish', () => {
      bridgeLog(
        `RESP ${res.statusCode} ${req.method} ${req.url || ''} ${Date.now() - startedAt}ms`,
      )
    })

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
    const op = PATH_TO_OP[path]
    if (!op) {
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
        const data = JSON.parse(body || '{}') as Record<string, any>
        // 业务逻辑统一走 dispatch() —— 与 WS 通道（bridge-ws-client）共用同一实现
        const { status, body: respBody } = await this.dispatch(op, data)
        if (!res.headersSent) {
          res.writeHead(status)
          res.end(JSON.stringify(respBody))
        }
      } catch (err: any) {
        // 审查 MINOR：原实现把 err 整个吞掉，500 根因（execCommandWithExitCode 抛错、
        // 会话丢失等）永远查不到；记日志后再回 500
        console.error(`[ElectronBridge] ${path} 处理失败:`, err)
        bridgeLog(`ERROR ${path}: ${err?.message || err}`)
        if (!res.headersSent) {
          res.writeHead(500)
          res.end(JSON.stringify({ success: false, error: '请求处理失败，请稍后重试' }))
        }
      }
    })
  }

  /** 桥接 token（WS 客户端 hello 握手用；与注入后端 env 的是同一个） */
  getBridgeToken(): string {
    return this.token
  }

  /**
   * 桥接业务逻辑的**唯一实现**，HTTP（兜底）与 WS（主链路）两通道共用：
   * 一处校验/执行逻辑，行为天然一致，杜绝双通道分叉。
   *
   * - ping：纯健康检查，不触碰 SSH/业务逻辑（独立端点让"桥可达"与"业务可用"解耦）
   * - ssh_exec：在既有 SSH 会话上执行远程命令
   * - ssh_connect：按连接 ID 建立轻量 SSH 会话（无 Shell，专供定时任务无头执行）
   * - ssh_disconnect：关闭桥建立的 SSH 会话
   *
   * 返回 {status, body}：status 为 HTTP 风格状态码，body 与旧 HTTP 响应体同构。
   * 业务异常在此捕获记日志并回 500（用户可见文案保持通用，细节只进日志）。
   */
  async dispatch(op: BridgeOp, data: Record<string, any>): Promise<DispatchResult> {
    data = data || {}
    try {
      switch (op) {
        case 'ping':
          return { status: 200, body: { success: true, pong: true, ts: Date.now() } }

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
      // 原实现把 err 整个吞掉，500 根因（execCommandWithExitCode 抛错、会话丢失等）
      // 永远查不到；记日志后再回 500（用户可见文案保持通用）
      console.error(`[ElectronBridge] ${op} 处理失败:`, err)
      bridgeLog(`ERROR ${op}: ${err?.message || err}`)
      return { status: 500, body: { success: false, error: '请求处理失败，请稍后重试' } }
    }
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
    return timingSafeEqual(bufA, bufB)
  }

  /**
   * 直接在端口区间内顺序尝试绑定，返回实际绑定的端口。
   *
   * 关键修复：不再使用"先用临时 server 探测空闲端口 → close → 再用正式 server 绑定"
   * 的两段式做法。该做法在 Windows 上存在竞态：探测阶段创建并关闭的 socket 结构，
   * 其内核侧释放与紧随其后的重新绑定可能重叠，导致正式 server 的 listen 回调触发
   * （端口显示 LISTENING）但 SYN 无人应答 — 即"假监听"，TCP connect 直接超时。
   * 改为"绑定失败即换下一个端口"，全程只有一次真实绑定，从根本上消除该竞态。
   */
  private listenFirstFree(server: http.Server, start: number, end: number): Promise<number> {
    return new Promise((resolve, reject) => {
      const tryPort = (port: number): void => {
        if (port > end) {
          reject(new Error(`未找到可用端口（${start}-${end}）`))
          return
        }
        const onError = (err: NodeJS.ErrnoException): void => {
          server.removeListener('listening', onListening)
          if (err.code === 'EADDRINUSE' || err.code === 'EACCES') {
            tryPort(port + 1)
          } else {
            reject(err)
          }
        }
        const onListening = (): void => {
          server.removeListener('error', onError)
          resolve(port)
        }
        server.once('error', onError)
        server.once('listening', onListening)
        server.listen(port, '127.0.0.1')
      }
      tryPort(start)
    })
  }
}
