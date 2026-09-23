/**
 * Bridge WS 客户端 — 主进程反向连接（出站拨号）后端 /ws/bridge。
 *
 * WS 反向通道阶段 1 的主链路：定时任务命令经此通道从后端下发到主进程，
 * 不再依赖 electron.exe 的回环入站防火墙规则（出站长连接不受入站规则影响）。
 * HTTP 端口桥保留为兜底通道。
 * 协议与实现详见 docs/superpowers/specs/2026-09-14-bridge-ws-channel-design.md
 * 及 backend/agent/bridge_relay.py（服务端中枢，协议双方须一致）。
 *
 * 使用 Node 22 原生 WebSocket（Electron 35 内置，零依赖）。主进程 tsconfig
 * 不含 DOM lib，故在此声明最小类型。
 */

const WS_PATH = '/ws/bridge'
/** 应用层心跳间隔：原生 WebSocket API 无 ping() 方法，用应用帧 */
const PING_INTERVAL_MS = 30_000
/** 连续 N 轮 ping 无 pong → 判定连接死亡，主动关闭触发重连 */
const MAX_MISSED_PONGS = 2
/** hello-ack 等待超时：连接建立但握手无响应（半开/服务端假活）→ 关闭重连 */
const HELLO_ACK_TIMEOUT_MS = 5_000
const RECONNECT_BASE_MS = 1_000
const RECONNECT_MAX_MS = 15_000
/** 重连退避抖动幅度：±30%，避免后端重启完成时主进程侧重连风暴同步共振 */
const RECONNECT_JITTER = 0.3

const WS_STATE_OPEN = 1

// 最小 WebSocket 类型（Node 22 / undici 实现的公共子集）
interface WsSocket {
  readyState: number
  send(data: string): void
  close(code?: number, reason?: string): void
  onopen: ((ev?: unknown) => void) | null
  onmessage: ((ev: { data: unknown }) => void) | null
  onclose: ((ev: { code?: number; reason?: string }) => void) | null
  onerror: ((ev?: unknown) => void) | null
}
declare const WebSocket: new (url: string) => WsSocket

import type { DispatchResult } from './electron-bridge'

/** 后端 req 帧 → 主进程业务实现的注入点（ElectronBridge.dispatch） */
type BridgeDispatch = (op: string, payload: Record<string, any>) => Promise<DispatchResult>

export class BridgeWsClient {
  private ws: WsSocket | null = null
  private port = 0
  private token = ''
  private stopped = true
  private connecting = false
  private reconnectTimer: NodeJS.Timeout | null = null
  private pingTimer: NodeJS.Timeout | null = null
  private helloAckTimer: NodeJS.Timeout | null = null
  private missedPongs = 0
  private reconnectDelay = RECONNECT_BASE_MS

  constructor(private readonly dispatch: BridgeDispatch) {}

  /**
   * （重新）指向目标后端并启动/刷新连接。
   * 后端自动重启后端口可能变化：调用方（main.ts）在每次 'ready' 事件携带
   * 端口时重新调用本方法。同目标且连接健康时为空操作。
   */
  setTarget(port: number, token: string): void {
    if (!port || !token) return
    const changed = port !== this.port || token !== this.token
    this.port = port
    this.token = token
    if (this.stopped) {
      this.stopped = false
      this.connect()
      return
    }
    if (changed || !this.isOpen()) {

      this.cancelReconnect()
      this.closeCurrent()
      this.reconnectDelay = RECONNECT_BASE_MS
      this.connect()
    }
  }

  /** 永久关闭（应用退出路径）。幂等。 */
  stop(): void {
    this.stopped = true
    this.cancelReconnect()
    this.closeCurrent()
  }

  /** 通道是否就绪（已连接；hello 握手成功与否不影响本判断，req 会自行暴露问题） */
  isOpen(): boolean {
    return !!this.ws && this.ws.readyState === WS_STATE_OPEN
  }

  // ── 连接生命周期 ────────────────────────────────

  private connect(): void {
    if (this.stopped || this.connecting || this.isOpen()) return
    this.connecting = true

    let ws: WsSocket
    try {
      ws = new WebSocket(`ws://127.0.0.1:${this.port}${WS_PATH}`)
    } catch (err) {
      this.connecting = false

      this.scheduleReconnect()
      return
    }
    this.ws = ws

    ws.onopen = () => {
      this.connecting = false
      this.reconnectDelay = RECONNECT_BASE_MS
      this.missedPongs = 0

      this.sendJson({ type: 'hello', token: this.token })
      this.armHelloAckTimeout(ws)
      this.startPing()
    }

    ws.onmessage = (ev) => {
      let msg: any
      try {
        msg = JSON.parse(String(ev.data))
      } catch {
        return // 非 JSON 帧：忽略（服务端对协议错误会自行断连）
      }
      switch (msg?.type) {
        case 'hello-ack':
          this.disarmHelloAckTimeout()

          break
        case 'pong':
          this.missedPongs = 0
          break
        case 'req':
          void this.handleReq(msg.id, msg.op, (msg.payload ?? {}) as Record<string, any>)
          break
        // 后端不主动发 res/ping（ping 方向为主→后端）；未知帧忽略（前向兼容）
      }
    }

    ws.onclose = () => {
      this.connecting = false
      this.stopPing()
      this.ws = null

      this.scheduleReconnect()
    }

    ws.onerror = () => {
      /* close 事件紧随其后，重连由 onclose 统一处理 */
    }
  }

  /** 主动关闭当前 socket（null 掉 onclose 以抑制重连调度）。 */
  private closeCurrent(): void {
    this.stopPing()
    this.disarmHelloAckTimeout()
    const ws = this.ws
    this.ws = null
    // 关键：socket 可能处于 CONNECTING（握手在途）— 其 onopen/onclose 已被 null，
    // 无人再复位 connecting 标志；不复位则后续 connect() 被守卫永久挡住，通道死亡
    this.connecting = false
    if (ws) {
      ws.onopen = null
      ws.onmessage = null
      ws.onclose = null
      ws.onerror = null
      try {
        ws.close(1000)
      } catch {
        /* 已关闭，忽略 */
      }
    }
  }

  private scheduleReconnect(): void {
    if (this.stopped) return
    this.cancelReconnect()
    // 指数退避 + ±30% 抖动：后端自动重启完成、多窗口/多场景同时恢复时，
    // 固定退避会让重连尝试同步共振；抖动把风暴摊开
    const jitter = 1 + (Math.random() * 2 - 1) * RECONNECT_JITTER
    const delay = Math.min(this.reconnectDelay * jitter, RECONNECT_MAX_MS)
    this.reconnectDelay = Math.min(this.reconnectDelay * 2, RECONNECT_MAX_MS)
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      this.connect()
    }, delay)
    this.reconnectTimer.unref?.()
  }

  private cancelReconnect(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
  }

  // ── hello-ack 超时判定（半开连接检测）──────────────

  /**
   * 连接建立后若 HELLO_ACK_TIMEOUT_MS 内未收到 hello-ack，判定为半开/
   * 服务端假活（TCP 已通但对端无响应），主动关闭让 onclose 走重连路径。
   * 正常情况下 hello-ack 在服务端 hello 鉴权后立即到达（毫秒级），不会触发。
   */
  private armHelloAckTimeout(ws: WsSocket): void {
    this.disarmHelloAckTimeout()
    this.helloAckTimer = setTimeout(() => {
      this.helloAckTimer = null
      if (this.ws !== ws) return // 已被 closeCurrent/setTarget 换掉

      // 摘除事件句柄再关闭：抑制 socket 迟到的 close 事件二次调度重连
      // （否则退避被双重翻倍 4×），与 closeCurrent 的契约一致
      ws.onopen = null
      ws.onmessage = null
      ws.onclose = null
      ws.onerror = null
      this.stopPing()
      this.ws = null
      try {
        ws.close(1000)
      } catch {
        /* 忽略 */
      }
      this.scheduleReconnect()
    }, HELLO_ACK_TIMEOUT_MS)
    this.helloAckTimer.unref?.()
  }

  private disarmHelloAckTimeout(): void {
    if (this.helloAckTimer) {
      clearTimeout(this.helloAckTimer)
      this.helloAckTimer = null
    }
  }

  // ── 应用层心跳 ─────────────────────────────────

  private startPing(): void {
    this.stopPing()
    this.pingTimer = setInterval(() => {
      const ws = this.ws
      if (!ws || ws.readyState !== WS_STATE_OPEN) return
      this.missedPongs += 1
      if (this.missedPongs >= MAX_MISSED_PONGS) {

        this.stopPing()
        this.ws = null
        // 摘除事件句柄再关闭：本分支已自行 scheduleReconnect，若不 null onclose，
        // socket 迟到的 close 事件会再调度一次重连（退避被双重翻倍 4×）——与
        // armHelloAckTimeout / closeCurrent 的契约一致。
        ws.onopen = null
        ws.onmessage = null
        ws.onclose = null
        ws.onerror = null
        try {
          ws.close(1000)
        } catch {
          /* 忽略 */
        }
        this.scheduleReconnect()
        return
      }
      this.sendJson({ type: 'ping' })
    }, PING_INTERVAL_MS)
    this.pingTimer.unref?.()
  }

  private stopPing(): void {
    if (this.pingTimer) {
      clearInterval(this.pingTimer)
      this.pingTimer = null
    }
  }

  // ── 帧收发 ─────────────────────────────────────

  private sendJson(obj: unknown): void {
    const ws = this.ws
    if (!ws || ws.readyState !== WS_STATE_OPEN) return
    try {
      ws.send(JSON.stringify(obj))
    } catch (err) {

    }
  }

  /**
   * 处理后端下发的 req 帧：分发给业务实现（ElectronBridge.dispatch），
   * 回 res 帧。id 原样带回；异常统一归为 500，绝不吞掉不回复（否则后端
   * 侧请求会挂到超时）。
   */
  private async handleReq(id: unknown, op: string, payload: Record<string, any>): Promise<void> {
    let status = 200
    let body: unknown
    try {
      const r = await this.dispatch(op, payload)
      status = r.status
      body = r.body
    } catch (err) {
      status = 500
      body = { success: false, error: '请求处理失败，请稍后重试' }
      console.error(`[BridgeWS] ${op} 处理失败:`, err)
    }
    this.sendJson({ type: 'res', id, status, result: body })
  }
}
