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
 * - remote(-R)：transport.bind(bind 身份 + 回连目标) + onInbound(同一身份) → net.connect 回连本机。
 *   网关是一条会话服务多条规则，所以订阅按 bind 身份键控，停止时必须摘掉。
 * - dynamic(-D)：内置极简 SOCKS5 server（无认证 / 仅 CONNECT，约百行，不引依赖），
 *   按 CONNECT 目标逐连接 dial。安全边界：listenHost 强制 127.0.0.1
 *   （tunnel-store 已钳制，这里只消费）。
 * - 状态机 starting → active | error，stop 幂等；状态每次变更经 safeSend 推 tunnel:status。
 * - 已知限制：数据面断开后隧道**不会**自己回来。SSH 侧不重连；网关侧虽然
 *   GatewayManager 会退避重拨会话，但 onTransportDead 已经把这条会话上的隧道
 *   拆掉并从表里摘了（state=error），会话恢复后还得由用户重新点启动。
 *   「网关回来就自动重启隧道」是没做的产品决定，不是这里的实现细节。
 */
import * as net from 'net'
import type { Duplex } from 'stream'
import type { BrowserWindow } from 'electron'
import type { RemoteBind, TunnelTransport } from './tunnel-transport'
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
  /** 摘掉这条会话在数据面上挂的死亡回调（共享会话上不停就会漏闭包与 session 对象） */
  offDead?: () => void
  state: TunnelStatus['state']
  message?: string
  startedAt?: number
  /** 主动 stop / 清理已开始：之后数据面迟到的 close/error 不再推 error */
  closing: boolean
}

/** 每条隧道允许的最大并发活动通道数；超出后新连接直接拒掉，防止单条隧道
 *  被海量并发连接（如 SOCKS5 代理被滥用）拖垮主进程事件循环与远端会话 */
const MAX_CONNS_PER_TUNNEL = 256

/** 按规则决定用哪条数据面并建好它。凭据由实现方自取，渲染层全程不碰明文。 */
type TransportFactory = (rule: TunnelRule) => Promise<TunnelTransport>

/**
 * 监听失败的中文文案，按 error code 分别说。
 * 原先任何 'error' 都翻译成「本机端口 … 已被占用」：1024 以下端口的 EACCES 和
 * 非法监听地址同样走这条路，于是用户对着一个换多少个端口都填不好的权限问题
 * 反复试 —— 占用只是其中一种，误报其余的都是白折腾。
 */
function listenFailureText(code: string, host: string, port: number): string {
  if (code === 'EADDRINUSE') return `本机端口 ${host}:${port} 已被占用`
  if (code === 'EACCES') return `本机无法监听 ${host}:${port}：该端口需要管理员权限（1024 以下）`
  return `本机无法监听 ${host}:${port}：${code}`
}

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
      // 脏行要在监听之前就拒掉：validateRule 只守写侧（保存那一刻），手改过的
      // tunnels.json 或旧版本残留可以带着 listenPort=undefined 走到这里，
      // 而 listen(undefined) 是「绑一个随机高位端口」并且照样报 active ——
      // 用户以为代理在 1080，实际上什么都没有。越界值同样拒绝，不夹到边界。
      const port = rule.listenPort
      if (typeof port !== 'number' || !Number.isInteger(port) || port < 1 || port > 65535) {
        const status: TunnelStatus = { id: rule.id, state: 'error', message: '本地监听端口需为 1–65535 的整数' }
        this.pushRaw(status)
        return status
      }
      // -D 的 127.0.0.1 边界在读侧再申一次：写侧钳的是「保存时」的事实，
      // 不是「监听时」的，脏行能把 SOCKS 开成局域网开放代理。
      const host = rule.type === 'dynamic' ? '127.0.0.1' : rule.listenHost || '127.0.0.1'
      const busy = await this.probePortInUse(host, port)
      if (busy) {
        const status: TunnelStatus = { id: rule.id, state: 'error', message: listenFailureText(busy, host, port) }
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
      session.offDead = transport.onDead(reason => this.onTransportDead(session, reason))

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
    if (s.conns.size >= MAX_CONNS_PER_TUNNEL) { sock.destroy(); return }
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

  /** -R 规则的完整绑定描述：对端开口 + 入站要回连的本机目标 */
  private remoteBind(s: TunnelSession): RemoteBind {
    const r = s.rule
    return {
      bindHost: r.remoteBindHost || '127.0.0.1',
      bindPort: r.bindPort!,
      targetHost: r.dstHost!,
      targetPort: r.dstPort!,
    }
  }

  private async startRemote(s: TunnelSession): Promise<void> {
    const b = this.remoteBind(s)
    // 先订阅入站再 bind：反过来会漏掉对端刚起来就打进来的第一条连接
    s.transport.onInbound(b, stream => this.handleInbound(s, stream))
    try {
      await s.transport.bind(b)
    } catch (err) {
      // bind 没成就留下订阅，等于在共享会话上挂一个永远等不到连接的回调
      s.transport.removeInbound(b)
      throw err
    }
  }

  /** 对端绑定端口上有新连接 → 回连本机目标 */
  private handleInbound(s: TunnelSession, stream: Duplex): void {
    if (s.conns.size >= MAX_CONNS_PER_TUNNEL) { stream.destroy(); return }
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
    if (s.conns.size >= MAX_CONNS_PER_TUNNEL) { sock.destroy(); return }
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
    s.offDead?.()
    s.offDead = undefined
    try { s.server?.close() } catch { /* ignore */ }
    s.server = undefined
    for (const c of s.conns) {
      try { c.destroy() } catch { /* ignore */ }
    }
    s.conns.clear()
    if (!s.transport) return
    if (s.rule.type === 'remote') {
      const b = this.remoteBind(s)
      // 共享会话上摘掉自己这条 bind 的订阅，否则停掉的规则还会被新连接唤醒
      s.transport.removeInbound(b)
      // unbind 现在与 bind 同走一条串行链并等 BIND_OK（原先是 delete 掉在途 BIND 的
      // 结算句柄，把整条链焊死）。失败不需要上报：这条数据面本来就在拆。
      s.transport.unbind(b).catch(() => {})
    }
    // 共享会话（网关一条会话服务多条规则）不能被单条隧道的停止关掉
    if (!s.transport.shared) s.transport.close()
  }

  /** 应用退出时全量清理（main.ts before-quit / window-all-closed 调用） */
  stopAll(): void {
    for (const id of [...this.sessions.keys()]) {
      void this.stop(id)
    }
  }

  /**
   * 网关被主动断开或删除时联动停掉挂在它上的隧道（spec §11.4 的后半）。
   * 被动断开由 onDead 拆；主动 close() 按定义不推 onDead，不联动的结果是本机监听口继续
   * accept、每条进来的连接只得到一句「网关会话未建立」—— 状态显示活动而流量全失败。
   */
  stopByGateway(gatewayId: string): void {
    for (const s of [...this.sessions.values()]) {
      if (s.rule.via !== 'gateway' || s.rule.gatewayId !== gatewayId) continue
      s.closing = true
      s.state = 'stopped'
      s.message = '所在网关已断开'
      this.teardown(s)
      this.sessions.delete(s.rule.id)
      this.pushSession(s)
    }
  }

  // ─────────────────────────── 端口预检 ───────────────────────────

  /** null = 可用；否则返回监听失败的 error code（占用 / 权限 / 非法地址是三回事） */
  private probePortInUse(host: string, port: number): Promise<string | null> {
    return new Promise(resolve => {
      const srv = net.createServer()
      srv.once('error', (err: NodeJS.ErrnoException) => resolve(err.code || 'EADDRINUSE'))
      srv.listen(port, host, () => {
        srv.close(() => resolve(null))
      })
    })
  }

  /** tunnel:checkPort —— 渲染层保存前调用，本机端口被占即时报（而非启动时才炸） */
  async checkPort(host: string, port: number): Promise<{ ok: boolean; message?: string }> {
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      return { ok: false, message: '端口需为 1–65535 的整数' }
    }
    const h = host || '127.0.0.1'
    const busy = await this.probePortInUse(h, port)
    return busy
      ? { ok: false, message: listenFailureText(busy, h, port) }
      : { ok: true }
  }
}
