/**
 * Tunnel Manager — 隧道（端口转发）编排
 *
 * 设计（spec: docs/superpowers/specs/2026-10-08-ssh-tunnel-design.md，
 *      数据面抽象见 docs/superpowers/specs/2026-10-08-tls-dataplane-design.md §3.1）：
 * - 每条隧道持有独立数据面（TunnelTransport：SSH 通道 或 自家 TLS 网关），不进 sessions
 *   池、不走终端的自动重连 —— 终端 tab 关闭/重连生命周期与隧道解耦。
 * - 本文件只管**编排**：本机 listener、SOCKS5 解析、通道记账、状态机。数据面怎么来的
 *   （forwardOut / 帧复用）一律经 TunnelTransport，不出现 ssh2 类型。
 * - local(-L)：本机 net.Server → transport.dial(dst) 双向 pipe。
 * - remote(-R)：transport.bind(bindHost:bindPort) + onInbound → net.connect(本机 dst) 回连。
 * - dynamic(-D)：内置极简 SOCKS5 server（无认证 / 仅 CONNECT，约百行，不引依赖），
 *   按 CONNECT 目标逐连接 dial。安全边界：listenHost 强制 127.0.0.1
 *   （tunnel-store 已钳制，这里只消费）。
 * - 状态机 starting → active | error，stop 幂等；状态每次变更经 safeSend 推 tunnel:status。
 * - 已知限制：SSH 数据面断开后隧道不自动重连（网关数据面自带重连，见 GatewayTransport）。
 */
import * as net from 'net'
import type { Duplex } from 'stream'
import type { BrowserWindow } from 'electron'
import type { TunnelTransport } from './tunnel-transport'
import type { TunnelRule } from './tunnel-store'

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
  transport: TunnelTransport
  /** local/dynamic 的本机 TCP listener；remote 为空 */
  server?: net.Server
  /** 活动转发通道（local/dynamic: 本机 socket；remote: 回连 socket），stop 时逐个销毁 */
  conns: Set<net.Socket>
  state: TunnelStatus['state']
  message?: string
  startedAt?: number
  /** 主动 stop / 清理已开始：之后数据面迟到的 close/error 不再推 error */
  closing: boolean
}

/** 按规则决定用哪条数据面并建好它。凭据由实现方自取，渲染层全程不碰明文。 */
type TransportFactory = (rule: TunnelRule) => Promise<TunnelTransport>

export class TunnelManager {
  private sessions = new Map<string, TunnelSession>()
  private mainWindow: BrowserWindow | null = null
  private transportFactory: TransportFactory | null = null

  setWindow(win: BrowserWindow): void {
    this.mainWindow = win
  }

  /** main.ts 注入：SSH 规则走 sshManager.openRawClient，网关规则走 GatewayTransport */
  setTransportFactory(fn: TransportFactory): void {
    this.transportFactory = fn
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

    // 本机端口预检（remote 的绑定端口在对端，本地探不了 → 由 bind() 报错兜底）
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
      transport: null as unknown as TunnelTransport,
      conns: new Set(),
      state: 'starting',
      closing: false,
    }
    this.sessions.set(rule.id, session)
    this.pushSession(session)

    try {
      if (!this.transportFactory) throw new Error('TunnelManager 未初始化 transportFactory')
      // 凭据/网关配置由工厂自取（渲染层全程无明文）；缺配置即 throw 中文文案，
      // 经下方 catch 落进 status.message
      const transport = await this.transportFactory(rule)
      session.transport = transport
      transport.onDead(reason => this.onTransportDead(session, reason))

      if (rule.type === 'local') await this.startLocal(session)
      else if (rule.type === 'remote') await this.startRemote(session)
      else await this.startDynamic(session)

      // 建连期间数据面已死：onTransportDead 已经把会话拆掉并从表里摘了，
      // 这里再置 active 就是给一个已经不在的会话盖上「活动」——UI 会显示一条
      // 永远停不下来的假活动隧道。
      if (session.closing || !this.sessions.has(rule.id)) return this.statusOf(session)

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

  /** 数据面死亡（被动断开）→ 隧道置 error。SSH 数据面不自动重连，UI 手动重启。 */
  private onTransportDead(session: TunnelSession, message: string): void {
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
      // 原先这里手写 `client.once('error', reject)` 桥接防 IPC 挂死；现在 dial/bind
      // 自己登记在途 promise 并在数据面死亡时统一 reject（见 SshTransport.kill），
      // 桥接收进那一处，不再每个 start* 复制一遍。
      const server = net.createServer(sock => void this.handleForwardConn(s, sock, s.rule.dstHost!, s.rule.dstPort!))
      s.server = server
      server.once('error', err => reject(err))
      server.listen(s.rule.listenPort!, s.rule.listenHost || '127.0.0.1', () => resolve())
    })
  }

  /** local/dynamic 共用：本机 socket → dial(dst) 双向 pipe */
  private async handleForwardConn(s: TunnelSession, sock: net.Socket, dstHost: string, dstPort: number): Promise<void> {
    s.conns.add(sock)
    sock.on('close', () => s.conns.delete(sock))
    sock.on('error', () => { /* 由 close 清理 */ })
    let stream: Duplex
    try {
      stream = await s.transport.dial(
        dstHost,
        dstPort,
        sock.remoteAddress || '127.0.0.1',
        sock.remotePort || 0,
      )
    } catch {
      sock.destroy()
      return
    }
    this.pipeBoth(sock, stream)
  }

  // ─────────────────────────── remote (-R) ───────────────────────────

  private async startRemote(s: TunnelSession): Promise<void> {
    // 先订阅入站再 bind：反过来会漏掉对端刚起来就打进来的第一条连接
    s.transport.onInbound(stream => this.handleInbound(s, stream))
    await s.transport.bind(s.rule.remoteBindHost || '127.0.0.1', s.rule.bindPort!)
  }

  /** 对端绑定端口上有新连接 → 回连本机目标 */
  private handleInbound(s: TunnelSession, stream: Duplex): void {
    const sock = net.connect(s.rule.dstPort!, s.rule.dstHost!)
    s.conns.add(sock)
    sock.on('close', () => s.conns.delete(sock))
    sock.on('error', () => {
      try { stream.destroy() } catch { /* 已断开 */ }
    })
    this.pipeBoth(sock, stream)
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

        // 逐连接 dial 到 CONNECT 目标
        void this.dialSocksTarget(s, sock, host, port)
      }
    }
    sock.on('data', onData)
  }

  private async dialSocksTarget(s: TunnelSession, sock: net.Socket, host: string, port: number): Promise<void> {
    let stream: Duplex
    try {
      stream = await s.transport.dial(host, port, sock.remoteAddress || '127.0.0.1', sock.remotePort || 0)
    } catch {
      // 0x05 0x01 general failure
      sock.write(Buffer.from([0x05, 0x01, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
      sock.destroy()
      return
    }
    // 成功回包：BND.ADDR/PORT 填零（客户端只关心成功位）
    sock.write(Buffer.from([0x05, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
    this.pipeBoth(sock, stream)
  }

  /**
   * 双向搬运，任一侧结束就把另一侧拆掉。三处转发（-L / -D / -R）共用，
   * 所以「一侧 error 不销毁另一侧」这类不对称只会有一个修法要记。
   */
  private pipeBoth(a: Duplex, b: Duplex): void {
    a.on('close', () => b.destroy())
    a.on('error', () => b.destroy())
    b.on('close', () => a.destroy())
    b.on('error', () => a.destroy())
    a.pipe(b)
    b.pipe(a)
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

  /** 拆 listener / 销毁活动通道 / 关数据面。remote 先 best-effort unbind */
  private teardown(s: TunnelSession): void {
    try { s.server?.close() } catch { /* ignore */ }
    s.server = undefined
    for (const c of s.conns) {
      try { c.destroy() } catch { /* ignore */ }
    }
    s.conns.clear()
    if (!s.transport) return
    if (s.rule.type === 'remote') {
      void s.transport.unbind(s.rule.remoteBindHost || '127.0.0.1', s.rule.bindPort!)
    }
    s.transport.close()
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
