/**
 * Tunnel Manager — SSH 隧道（端口转发）管理
 *
 * 设计（spec: docs/superpowers/specs/2026-10-08-ssh-tunnel-design.md）：
 * - 每条隧道持有独立 ssh2 Client（sshManager.openRawClient 产出，TOFU/认证与
 *   终端连接完全一致），不进 sessions 池、不走自动重连 —— 终端 tab 关闭/重连
 *   生命周期与隧道解耦。
 * - local(-L)：本机 net.Server → client.forwardOut(dstHost:dstPort) 双向 pipe。
 * - remote(-R)：client.forwardIn(bindHost:bindPort) + 'tcp connection' 事件 →
 *   net.connect(本机 dstHost:dstPort) 回连。
 * - dynamic(-D)：内置极简 SOCKS5 server（无认证 / 仅 CONNECT，约百行，不引依赖），
 *   按 CONNECT 目标逐连接 forwardOut。安全边界：listenHost 强制 127.0.0.1
 *   （tunnel-store 已钳制，这里只消费）。
 * - 状态机 starting → active | error，stop 幂等；状态每次变更经 safeSend 推
 *   tunnel:status（窗口销毁安全）。
 * - V1 已知限制：SSH 连接断开后隧道不自动重连（状态置 error，UI 手动重启）。
 */
import type { Client } from 'ssh2'
import * as net from 'net'
import type { BrowserWindow } from 'electron'
import type { SSHConnectionConfig } from './ssh-manager'
import type { TunnelRule, TunnelType } from './tunnel-store'

export type { TunnelType }

/** 运行状态（主进程 → 渲染层推送，不持久化）。渲染层副本见 src/env.d.ts */
export interface TunnelStatus {
  id: string
  state: 'starting' | 'active' | 'error' | 'stopped'
  /** error 时给人看的失败原因 */
  message?: string
  /** 当前活动转发通道数 */
  connections?: number
  startedAt?: number
}

interface TunnelSession {
  rule: TunnelRule
  client: Client
  /** local/dynamic 的本机 TCP listener；remote 为空 */
  server?: net.Server
  /** 活动转发通道（local/dynamic: 本机 socket；remote: 回连 socket），stop 时逐个销毁 */
  conns: Set<net.Socket>
  state: TunnelStatus['state']
  message?: string
  startedAt?: number
  /** 主动 stop / 清理已开始：之后的 client 迟到 close/error 不再推 error */
  closing: boolean
}

type ClientFactory = (config: SSHConnectionConfig) => Promise<Client>
type ConfigProvider = (connectionId: string) => Promise<SSHConnectionConfig | null>

export class TunnelManager {
  private sessions = new Map<string, TunnelSession>()
  private mainWindow: BrowserWindow | null = null
  private configProvider: ConfigProvider | null = null
  private clientFactory: ClientFactory | null = null

  setWindow(win: BrowserWindow): void {
    this.mainWindow = win
  }

  /** main.ts 注入（sshStore.get），渲染层全程不碰凭据 */
  setConfigProvider(fn: ConfigProvider): void {
    this.configProvider = fn
  }

  /** main.ts 注入（sshManager.openRawClient），复用 TOFU/认证/keepalive 配置链 */
  setClientFactory(fn: ClientFactory): void {
    this.clientFactory = fn
  }

  statusAll(): TunnelStatus[] {
    return [...this.sessions.values()].map(s => this.statusOf(s))
  }

  // ─────────────────────────── 状态推送 ───────────────────────────

  private statusOf(s: TunnelSession): TunnelStatus {
    return {
      id: s.rule.id,
      state: s.state,
      message: s.message,
      connections: s.conns.size,
      startedAt: s.startedAt,
    }
  }

  private safeSend(channel: string, data: unknown): void {
    if (this.mainWindow && !this.mainWindow.isDestroyed()) {
      try { this.mainWindow.webContents.send(channel, data) } catch { /* EPIPE */ }
    }
  }

  private pushSession(s: TunnelSession): void {
    this.safeSend('tunnel:status', this.statusOf(s))
  }

  private pushRaw(status: TunnelStatus): void {
    this.safeSend('tunnel:status', status)
  }

  private friendlyNetError(err: unknown): string {
    const e = err as { code?: string; message?: string }
    if (e?.code === 'EADDRINUSE') return '本机端口已被占用'
    if (e?.code === 'EACCES') return '端口被系统策略拒绝（1–1023 可能需要管理员权限）'
    return e?.message || String(err)
  }

  // ─────────────────────────── 启动 ───────────────────────────

  async start(rule: TunnelRule): Promise<TunnelStatus> {
    const existing = this.sessions.get(rule.id)
    // 幂等：已在跑/正在启动直接返回当前状态
    if (existing && (existing.state === 'active' || existing.state === 'starting')) {
      return this.statusOf(existing)
    }
    if (existing) {
      // error/stopped 残留：先静默拆除再重建
      existing.closing = true
      this.teardown(existing)
      this.sessions.delete(rule.id)
    }

    // 凭据主进程自取（渲染层全程无明文）；连接配置被删 → config-missing
    const config = this.configProvider ? await this.configProvider(rule.connectionId) : null
    if (!config) {
      const status: TunnelStatus = { id: rule.id, state: 'error', message: '连接配置已删除，请重建连接或删除本条隧道规则' }
      this.pushRaw(status)
      return status
    }

    // 本机端口预检（remote 的绑定端口在服务端，本地探不了 → 由 forwardIn 报错兜底）
    if (rule.type !== 'remote') {
      const host = rule.listenHost || '127.0.0.1'
      if (await this.probePortInUse(host, rule.listenPort!)) {
        const status: TunnelStatus = { id: rule.id, state: 'error', message: `本机端口 ${host}:${rule.listenPort} 已被占用` }
        this.pushRaw(status)
        return status
      }
    }

    const session: TunnelSession = {
      rule,
      client: null as unknown as Client,
      conns: new Set(),
      state: 'starting',
      closing: false,
    }
    this.sessions.set(rule.id, session)
    this.pushSession(session)

    try {
      if (!this.clientFactory) throw new Error('TunnelManager 未初始化 clientFactory')
      const client = await this.clientFactory(config)
      session.client = client
      client.on('error', (err: Error) => this.onClientDead(session, `SSH 连接错误: ${err.message}`))
      client.on('close', () => this.onClientDead(session, 'SSH 连接已断开'))

      if (rule.type === 'local') await this.startLocal(session)
      else if (rule.type === 'remote') await this.startRemote(session)
      else await this.startDynamic(session)

      session.state = 'active'
      session.startedAt = Date.now()
      this.pushSession(session)
    } catch (err) {
      session.closing = true
      this.teardown(session)
      this.sessions.delete(rule.id)
      const status: TunnelStatus = {
        id: rule.id,
        state: 'error',
        message: this.friendlyNetError(err),
      }
      this.pushRaw(status)
      return status
    }
    return this.statusOf(session)
  }

  /** SSH 连接死亡（error/close）→ 隧道置 error（V1 不自动重连，UI 手动重启） */
  private onClientDead(session: TunnelSession, message: string): void {
    if (session.closing) return
    session.closing = true
    session.state = 'error'
    session.message = message
    this.teardown(session)
    this.sessions.delete(session.rule.id)
    this.pushSession(session)
  }

  // ─────────────────────────── local (-L) ───────────────────────────

  private startLocal(s: TunnelSession): Promise<void> {
    return new Promise((resolve, reject) => {
      const server = net.createServer(sock => this.handleForwardConn(s, sock, s.rule.dstHost!, s.rule.dstPort!))
      s.server = server
      server.once('error', err => reject(err))
      server.listen(s.rule.listenPort!, s.rule.listenHost || '127.0.0.1', () => resolve())
    })
  }

  /** local/dynamic 共用：本机 socket → forwardOut(dst) 双向 pipe */
  private handleForwardConn(s: TunnelSession, sock: net.Socket, dstHost: string, dstPort: number): void {
    s.conns.add(sock)
    sock.on('close', () => s.conns.delete(sock))
    sock.on('error', () => { /* 由 close 清理 */ })
    s.client.forwardOut(
      sock.remoteAddress || '127.0.0.1',
      sock.remotePort || 0,
      dstHost,
      dstPort,
      (err, stream) => {
        if (err || !stream) {
          sock.destroy()
          return
        }
        stream.on('close', () => sock.destroy())
        stream.on('error', () => sock.destroy())
        sock.pipe(stream)
        stream.pipe(sock)
      },
    )
  }

  // ─────────────────────────── remote (-R) ───────────────────────────

  private startRemote(s: TunnelSession): Promise<void> {
    return new Promise((resolve, reject) => {
      const bindHost = s.rule.remoteBindHost || '127.0.0.1'
      // 远端发起新连接（绑定端口有流量）→ accept 得到 ssh 通道，回连本机目标
      s.client.on('tcp connection', (info, accept, rej) => {
        try {
          const stream = accept()
          const sock = net.connect(s.rule.dstPort!, s.rule.dstHost!)
          s.conns.add(sock)
          sock.on('close', () => s.conns.delete(sock))
          sock.on('error', () => {
            try { stream.close() } catch { /* ignore */ }
          })
          stream.on('close', () => sock.destroy())
          stream.on('error', () => sock.destroy())
          stream.pipe(sock)
          sock.pipe(stream)
        } catch {
          rej()
        }
      })
      s.client.forwardIn(bindHost, s.rule.bindPort!, err => (err ? reject(err) : resolve()))
    })
  }

  // ─────────────────────────── dynamic (-D, SOCKS5) ───────────────────────────

  private startDynamic(s: TunnelSession): Promise<void> {
    return new Promise((resolve, reject) => {
      const server = net.createServer(sock => this.handleSocksConn(s, sock))
      s.server = server
      server.once('error', err => reject(err))
      server.listen(s.rule.listenPort!, s.rule.listenHost || '127.0.0.1', () => resolve())
    })
  }

  /** 极简 SOCKS5：无认证（METHODS 一律回 NO-AUTH）、仅 CONNECT 命令、无 UDP/Bind */
  private handleSocksConn(s: TunnelSession, sock: net.Socket): void {
    s.conns.add(sock)
    sock.on('close', () => s.conns.delete(sock))
    sock.on('error', () => sock.destroy())

    let stage: 'greet' | 'request' = 'greet'
    let buf = Buffer.alloc(0)

    const onData = (chunk: Buffer): void => {
      buf = Buffer.concat([buf, chunk])

      if (stage === 'greet') {
        // VER NMETHODS METHODS(1..255)
        if (buf.length < 2) return
        const ver = buf[0]
        const n = buf[1]
        if (ver !== 5 || buf.length < 2 + n) {
          if (ver !== 5) { sock.destroy() ; return }
          return // 等更多方法字节
        }
        sock.write(Buffer.from([0x05, 0x00])) // NO-AUTH
        buf = buf.subarray(2 + n)
        stage = 'request'
      }

      if (stage === 'request') {
        // VER CMD RSV ATYP ADDR PORT
        if (buf.length < 5) return
        const atyp = buf[3]
        let addrLen = 0
        if (atyp === 0x01) addrLen = 4
        else if (atyp === 0x03) addrLen = 1 + buf[4] // 1 字节长度 + domain
        else if (atyp === 0x04) addrLen = 16
        else { sock.destroy(); return }
        const need = 4 + addrLen + 2
        if (buf.length < need) return

        const cmd = buf[1]
        if (cmd !== 0x01) {
          // 仅支持 CONNECT
          sock.write(Buffer.from([0x05, 0x07, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
          sock.destroy()
          return
        }
        const addrStart = 4
        let host: string
        if (atyp === 0x01) host = [...buf.subarray(addrStart, addrStart + 4)].join('.')
        else if (atyp === 0x03) host = buf.subarray(addrStart + 1, addrStart + 1 + buf[4]).toString('ascii')
        else host = [...buf.subarray(addrStart, addrStart + 16)].join('.')
        const port = buf.readUInt16BE(addrStart + addrLen)

        buf = buf.subarray(need)
        sock.removeListener('data', onData)

        // 逐连接 forwardOut 到 CONNECT 目标
        s.client.forwardOut(
          sock.remoteAddress || '127.0.0.1',
          sock.remotePort || 0,
          host,
          port,
          (err, stream) => {
            if (err || !stream) {
              // 0x05 0x01 general failure
              sock.write(Buffer.from([0x05, 0x01, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
              sock.destroy()
              return
            }
            // 成功回包：BND.ADDR/PORT 填零（客户端只关心成功位）
            sock.write(Buffer.from([0x05, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
            stream.on('close', () => sock.destroy())
            stream.on('error', () => sock.destroy())
            sock.pipe(stream)
            stream.pipe(sock)
          },
        )
      }
    }
    sock.on('data', onData)
  }

  // ─────────────────────────── 停止 / 清理 ───────────────────────────

  async stop(id: string): Promise<TunnelStatus> {
    const s = this.sessions.get(id)
    if (!s) return { id, state: 'stopped' }
    s.closing = true
    s.state = 'stopped'
    this.teardown(s)
    this.sessions.delete(id)
    const status = this.statusOf(s)
    this.pushRaw(status)
    return status
  }

  /** 拆 listener / 销毁活动通道 / 关 SSH 连接。remote 先 best-effort unforwardIn */
  private teardown(s: TunnelSession): void {
    try { s.server?.close() } catch { /* ignore */ }
    s.server = undefined
    for (const c of s.conns) {
      try { c.destroy() } catch { /* ignore */ }
    }
    s.conns.clear()
    if (s.rule.type === 'remote' && s.client) {
      try { s.client.unforwardIn(s.rule.remoteBindHost || '127.0.0.1', s.rule.bindPort!, () => { /* ignore */ }) } catch { /* ignore */ }
    }
    try { s.client?.end() } catch { /* ignore */ }
  }

  /** 应用退出时全量清理（main.ts before-quit / window-all-closed 调用） */
  stopAll(): void {
    for (const id of [...this.sessions.keys()]) {
      void this.stop(id)
    }
  }

  // ─────────────────────────── 端口预检 ───────────────────────────

  /** true = 已被占用（保存前预检 / 启动前预检共用） */
  private probePortInUse(host: string, port: number): Promise<boolean> {
    return new Promise(resolve => {
      const srv = net.createServer()
      srv.once('error', () => resolve(true))
      srv.listen(port, host, () => {
        srv.close(() => resolve(false))
      })
    })
  }

  /** tunnel:checkPort —— 渲染层保存前调用，本机端口被占即时报（而非启动时才炸） */
  async checkPort(host: string, port: number): Promise<{ ok: boolean; message?: string }> {
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      return { ok: false, message: '端口需为 1–65535 的整数' }
    }
    const h = host || '127.0.0.1'
    const inUse = await this.probePortInUse(h, port)
    return inUse
      ? { ok: false, message: `本机端口 ${h}:${port} 已被占用` }
      : { ok: true }
  }
}
