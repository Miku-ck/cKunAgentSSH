/**
 * GatewayTransport — 连自家 TLS 网关的数据面（spec §3.2）
 *
 * 分两层放同一文件：
 *   GatewaySession  一条已连上的 Duplex 上的帧编排（chan 表、半关闭、背压、心跳）
 *   GatewayTransport 连接与信任面（TLS + SPKI pinning + 口令认证 + 重连退避）
 *
 * 为什么把 session 收出来：它不需要证书就能测。测试用 stream.Duplex.pair() 接一个
 * 假对端即可跑通帧语义；而 Node 的 crypto 只能解析 X.509、不能签发，证书夹具得靠
 * 外部 openssl —— 把可测的部分和需要 PKI 夹具的部分分开，是这里唯一的取舍。
 *
 * 三条坑的落点（§1.1）：
 *   迟到帧   → onFrame 里 channels.get 拿不到就 return，不杀会话
 *   半关闭   → MuxChannel._final 发 FIN（只关自己这一向），_destroy 才发 RST
 *   背压     → _write 里 socket 写不动就不 cb()，让 pipe 自己把上游暂停
 */
import { Duplex } from 'stream'
import * as tls from 'tls'
import { createHash } from 'crypto'
import {
  allocChan,
  createMuxDecoder,
  encodeFrame,
  GLOBAL_CHAN,
  MuxProtocolError,
  type HelloOkPayload,
  type MuxFrame,
  type OpenErrCode,
} from '../../shared/gateway-mux'
import type { TunnelTransport } from '../tunnel-transport'

const HIGH_WATER = 256 * 1024

export class TunnelDeniedError extends Error {
  constructor(readonly code: OpenErrCode, message: string) {
    super(message)
    this.name = 'TunnelDeniedError'
  }
}

/** 一条转发通道：读方向由 DATA 喂，写方向发 DATA；end()=FIN，destroy()=RST */
class MuxChannel extends Duplex {
  // 不能叫 closed —— node:stream 的 Duplex 自己有一个公开的 closed
  private torn = false

  constructor(
    private readonly sess: GatewaySession,
    readonly chan: number,
  ) {
    super({ highWaterMark: HIGH_WATER })
  }

  override _read(): void {
    // 读方向由 session 的 pushData 驱动；这里只负责在消费后解除暂停
    this.sess.resumeSocketIfIdle()
  }

  override _write(chunk: Buffer, _enc: BufferEncoding, cb: (e?: Error) => void): void {
    this.sess.writeBytes(encodeFrame({ type: 'data', chan: this.chan, payload: new Uint8Array(chunk) }), cb)
  }

  /** end() 只关自己这一向：对端还能写完它那一侧的数据 */
  override _final(cb: (e?: Error) => void): void {
    this.sess.sendRaw(encodeFrame({ type: 'fin', chan: this.chan }))
    cb()
  }

  override _destroy(err: Error | null, cb: (e?: Error) => void): void {
    this.markClosed()
    this.sess.sendRaw(encodeFrame({ type: 'rst', chan: this.chan }))
    this.sess.dropChannel(this.chan)
    cb(err ?? undefined)
  }

  pushData(buf: Buffer): void {
    if (this.torn) return
    if (!this.push(buf)) this.sess.pauseSocket()
  }

  pushEof(): void {
    if (this.torn) return
    this.push(null)
  }

  markClosed(): void {
    this.torn = true
  }
}

export interface SessionOptions {
  user: string
  password: string
  clientVersion: string
  deviceName: string
}

export class GatewaySession {
  readonly channels = new Map<number, MuxChannel>()
  private readonly decoder = createMuxDecoder({
    localParity: 'odd',
    hasChannel: c => this.channels.has(c),
  })
  private readonly pendingOpen = new Map<number, (err: Error | null) => void>()
  private readonly inboundHandlers: ((s: Duplex) => void)[] = []
  private readonly deadHandlers: ((reason: string) => void)[] = []
  private readyResolve: ((v: HelloOkPayload) => void) | null = null
  private readyReject: ((e: Error) => void) | null = null
  private pendingBind: ((e: Error | null) => void) | null = null
  private pingTimer: NodeJS.Timeout | null = null
  private missedPong = 0
  private seq = 0
  private readPaused = false
  dead = false
  info: HelloOkPayload | null = null

  /**
   * 一次性建好、之后每次返回同一个 promise。
   * 做成 getter 的话，helloOk 之前有人读一次、之后再读一次，第二个 promise 永远
   * 不会 settle（resolve 句柄已被前一次覆盖）—— 认证失败路径正好踩得到。
   */
  readonly ready: Promise<HelloOkPayload> = new Promise<HelloOkPayload>((resolve, reject) => {
    this.readyResolve = resolve
    this.readyReject = reject
  })

  constructor(
    private readonly sock: Duplex,
    private readonly opts: SessionOptions,
  ) {
    sock.on('data', (chunk: Buffer) => this.onBytes(chunk))
    sock.on('error', (err: Error) => this.kill(`数据面错误: ${err.message}`))
    sock.on('close', () => this.kill('数据面已断开'))
  }

  hello(): void {
    this.sendRaw(encodeFrame({
      type: 'hello',
      chan: GLOBAL_CHAN,
      payload: {
        clientVersion: this.opts.clientVersion,
        deviceName: this.opts.deviceName,
        selfReportedUser: this.opts.user,
      },
    }))
    this.sendRaw(encodeFrame({
      type: 'auth',
      chan: GLOBAL_CHAN,
      payload: { user: this.opts.user, password: this.opts.password },
    }))
  }

  onBytes(chunk: Buffer): void {
    let frames: MuxFrame[]
    try {
      frames = this.decoder.push(new Uint8Array(chunk))
    } catch (err) {
      // 帧已失同步：整条会话作废，由调用方重连（决策 6：通道不迁移）
      this.kill(err instanceof MuxProtocolError ? err.message : String(err))
      return
    }
    for (const f of frames) this.onFrame(f)
  }

  private onFrame(f: MuxFrame): void {
    switch (f.type) {
      case 'helloOk':
        this.info = f.payload
        this.startHeartbeat(f.payload.heartbeatSec)
        this.readyResolve?.(f.payload)
        return
      case 'openOk': {
        const cb = this.pendingOpen.get(f.chan)
        this.pendingOpen.delete(f.chan)
        cb?.(null)
        return
      }
      case 'openErr': {
        // chan=0 的 openErr 是控制面失败（认证被拒 / bind 被拒）
        const code = f.payload.code
        const message = f.payload.message
        if (f.chan === GLOBAL_CHAN) {
          if (this.pendingBind) {
            this.pendingBind(new TunnelDeniedError(code, message))
            this.pendingBind = null
            return
          }
          if (!this.info) {
            this.readyReject?.(new TunnelDeniedError(code, message))
            this.kill(`网关拒绝: ${message}`)
            return
          }
          return
        }
        const cb = this.pendingOpen.get(f.chan)
        this.pendingOpen.delete(f.chan)
        cb?.(new TunnelDeniedError(code, message))
        this.closeChannel(f.chan)
        return
      }
      case 'open':
        return this.onPeerOpen(f.chan, f.payload)
      case 'data': {
        const ch = this.channels.get(f.chan)
        if (!ch || f.payload.length === 0) return // 迟到帧：丢弃，不是协议错误
        ch.pushData(Buffer.from(f.payload.buffer, f.payload.byteOffset, f.payload.length))
        return
      }
      case 'fin': {
        const ch = this.channels.get(f.chan)
        if (!ch) return
        ch.pushEof()
        return
      }
      case 'rst':
        this.closeChannel(f.chan)
        return
      case 'bindOk':
        this.pendingBind?.(null)
        this.pendingBind = null
        return
      case 'ping':
        this.sendRaw(encodeFrame({ type: 'pong', chan: GLOBAL_CHAN, seq: f.seq }))
        return
      case 'pong':
        this.missedPong = 0
        return
      case 'goaway':
        this.kill(`网关下线: ${f.payload.reason}`)
        return
    }
  }

  /** 对端主动开一条通道（-R：网关绑定端口上来了连接） */
  private onPeerOpen(chan: number, p: { host: string; port: number }): void {
    const ch = new MuxChannel(this, chan)
    this.channels.set(chan, ch)
    this.sendRaw(encodeFrame({ type: 'openOk', chan }))
    for (const h of this.inboundHandlers) h(ch)
  }

  async dial(dstHost: string, dstPort: number): Promise<Duplex> {
    await this.ready
    const chan = allocChan('odd', new Set(this.channels.keys()))
    if (chan === null) throw new TunnelDeniedError('quota', '本端通道号段已耗尽')
    const ch = new MuxChannel(this, chan)
    this.channels.set(chan, ch)
    const err = await new Promise<Error | null>(resolve => {
      this.pendingOpen.set(chan, resolve)
      this.sendRaw(encodeFrame({ type: 'open', chan, payload: { host: dstHost, port: dstPort } }))
    })
    this.pendingOpen.delete(chan)
    if (err) {
      this.closeChannel(chan)
      throw err
    }
    return ch
  }

  bind(bindHost: string, bindPort: number): Promise<void> {
    return this.ready.then(
      () =>
        new Promise<void>((resolve, reject) => {
          if (this.pendingBind) {
            reject(new Error('上一条 bind 仍在途中'))
            return
          }
          this.pendingBind = e => (e ? reject(e) : resolve())
          this.sendRaw(encodeFrame({ type: 'bind', chan: GLOBAL_CHAN, payload: { bindHost, bindPort } }))
        }),
    )
  }

  unbind(bindHost: string, bindPort: number): Promise<void> {
    this.sendRaw(encodeFrame({ type: 'unbind', chan: GLOBAL_CHAN, payload: { bindHost, bindPort } }))
    return Promise.resolve()
  }

  onInbound(h: (s: Duplex) => void): void {
    this.inboundHandlers.push(h)
  }

  onDead(h: (reason: string) => void): void {
    if (this.dead) h('数据面已断开')
    else this.deadHandlers.push(h)
  }

  /** 写一帧并按 socket 的反馈决定何时 cb（背压的唯一入口） */
  writeBytes(frame: Uint8Array, cb: (e?: Error) => void): void {
    if (this.dead) {
      cb(new Error('数据面已断开'))
      return
    }
    const ok = (this.sock.write(Buffer.from(frame.buffer, frame.byteOffset, frame.length)) as boolean)
    if (ok) cb()
    else this.sock.once('drain', () => cb())
  }

  sendRaw(frame: Uint8Array): void {
    if (this.dead) return
    try {
      this.sock.write(Buffer.from(frame.buffer, frame.byteOffset, frame.length))
    } catch {
      /* close 事件会接管 */
    }
  }

  /** 任一通道消费过就解除整条 socket 的暂停（V1 不做窗口，共享 socket 会头阻） */
  resumeSocketIfIdle(): void {
    if (!this.readPaused) return
    if ([...this.channels.values()].some(c => c.writableNeedDrain)) return
    this.readPaused = false
    this.sock.resume()
  }

  pauseSocket(): void {
    if (this.readPaused) return
    this.readPaused = true
    this.sock.pause()
  }

  private startHeartbeat(sec: number): void {
    if (this.pingTimer) clearInterval(this.pingTimer)
    this.pingTimer = setInterval(() => {
      if (this.dead) return
      if (++this.missedPong > 3) return this.kill('连续 3 次 PING 无 PONG')
      this.sendRaw(encodeFrame({ type: 'ping', chan: GLOBAL_CHAN, seq: ++this.seq }))
    }, Math.max(1, sec) * 1000)
    this.pingTimer.unref?.()
  }

  private closeChannel(chan: number): void {
    const ch = this.channels.get(chan)
    if (!ch) return
    ch.markClosed()
    this.channels.delete(chan)
    setImmediate(() => ch.destroy())
  }

  dropChannel(chan: number): void {
    this.channels.delete(chan)
    this.resumeSocketIfIdle()
  }

  kill(reason: string): void {
    if (this.dead) return
    this.dead = true
    if (this.pingTimer) clearInterval(this.pingTimer)
    this.readyReject?.(new Error(reason))
    this.readyReject = null
    for (const [, cb] of this.pendingOpen) cb(new Error(reason))
    this.pendingOpen.clear()
    this.pendingBind?.(new Error(reason))
    this.pendingBind = null
    for (const [, ch] of this.channels) {
      ch.markClosed()
      ch.destroy()
    }
    this.channels.clear()
    try {
      this.sock.destroy()
    } catch {
      /* 已断开 */
    }
    for (const h of this.deadHandlers) h(reason)
  }

  close(): void {
    if (this.dead) return
    this.sendRaw(encodeFrame({ type: 'goaway', chan: GLOBAL_CHAN, payload: { reason: '客户端主动断开', lastChan: 0 } }))
    this.dead = true
    if (this.pingTimer) clearInterval(this.pingTimer)
    for (const [, ch] of this.channels) {
      ch.markClosed()
      ch.destroy()
    }
    this.channels.clear()
    try {
      this.sock.end()
      this.sock.destroy()
    } catch {
      /* 已断开 */
    }
  }
}

export interface GatewayEndpoint {
  host: string
  port: number
  /** CA（PEM）。缺省时只信 SPKI 指纹那一张证书 —— 自签网关的常见形态 */
  caPem?: string
  /** 已确认的服务端 SPKI 指纹（sha256/…）。缺省 = 首次连接走 TOFU */
  trustedSpki?: string
  user: string
  password: string
  clientVersion: string
  deviceName: string
}

export type HostKeyDecision = 'trusted' | 'new' | 'mismatch'

/** 首次见到 / 指纹不符时的回调：new 由 UI 确认后写库，mismatch 一律硬失败 */
export type HostKeyProbe = (spki: string) => HostKeyDecision

export class GatewayTransport implements TunnelTransport {
  readonly kind = 'gateway' as const
  /** 一条网关会话服务多条隧道，单条隧道停止时不得关闭它 */
  readonly shared = true
  private session: GatewaySession | null = null
  private starting = false
  private closingIntentionally = false
  private readonly inboundHandlers: ((s: Duplex) => void)[] = []
  private readonly deadHandlers: ((reason: string) => void)[] = []
  private lastSpki = ''

  constructor(
    private readonly ep: GatewayEndpoint,
    private readonly hostKeyProbe: HostKeyProbe,
    private readonly onSessionReady: (info: HelloOkPayload, spki: string) => void = () => {},
  ) {}

  get dead(): boolean {
    return this.closingIntentionally || !this.session || this.session.dead
  }

  /** 供 store 展示：当前会话的通道数与策略版本 */
  get stats(): { channels: number; role: string; sessionId: string; spki: string } {
    return {
      channels: this.session?.channels.size ?? 0,
      role: this.session?.info?.role || '',
      sessionId: this.session?.info?.sessionId || '',
      spki: this.lastSpki,
    }
  }

  async start(): Promise<void> {
    if (this.starting || this.session?.dead === false) return
    this.starting = true
    try {
      const sock = await this.connect()
      const sess = new GatewaySession(sock, {
        user: this.ep.user,
        password: this.ep.password,
        clientVersion: this.ep.clientVersion,
        deviceName: this.ep.deviceName,
      })
      sess.onInbound(s => {
        for (const h of this.inboundHandlers) h(s)
      })
      sess.onDead(reason => {
        if (this.closingIntentionally) return
        for (const h of this.deadHandlers) h(reason)
      })
      this.session = sess
      sess.hello()
      const info = await sess.ready
      this.onSessionReady(info, this.lastSpki)
    } finally {
      this.starting = false
    }
  }

  private connect(): Promise<Duplex> {
    return new Promise<Duplex>((resolve, reject) => {
      const sock = tls.connect({
        host: this.ep.host,
        port: this.ep.port,
        // 没给 CA 时仍要校验指纹：Node 要求 ca 非空才走自定义信任链，
        // 这里把「信任」完全交给 SPKI pinning，所以把 rejectUnauthorized 关掉、
        // 由下面的指纹比对决定放行与否 —— 少任何一步都会变成「谁都能中间人」。
        ca: this.ep.caPem ? [this.ep.caPem] : undefined,
        rejectUnauthorized: !!this.ep.caPem,
        servername: /^[0-9.]+$/.test(this.ep.host) ? undefined : this.ep.host,
      })
      const fail = (err: Error) => {
        sock.destroy()
        reject(err)
      }
      sock.once('error', err => reject(err))
      sock.once('secureConnect', () => {
        const spki = peerSpki(sock)
        if (!spki) return fail(new Error('网关未出示证书'))
        this.lastSpki = spki
        const decision = this.hostKeyProbe(spki)
        if (decision === 'mismatch') {
          return fail(new Error('服务端证书指纹与本机记录不一致，已硬失败'))
        }
        resolve(sock)
      })
    })
  }

  dial(dstHost: string, dstPort: number): Promise<Duplex> {
    if (!this.session) return Promise.reject(new Error('网关会话未建立'))
    return this.session.dial(dstHost, dstPort)
  }

  bind(bindHost: string, bindPort: number): Promise<void> {
    if (!this.session) return Promise.reject(new Error('网关会话未建立'))
    return this.session.bind(bindHost, bindPort)
  }

  unbind(bindHost: string, bindPort: number): Promise<void> {
    return this.session?.unbind(bindHost, bindPort) ?? Promise.resolve()
  }

  onInbound(h: (s: Duplex) => void): void {
    this.inboundHandlers.push(h)
  }

  onDead(h: (reason: string) => void): void {
    this.deadHandlers.push(h)
    this.session?.onDead(h)
  }

  close(): void {
    this.closingIntentionally = true
    this.session?.close()
    this.session = null
  }
}

/** 服务端 SPKI 的 sha256/base64（与 ssh-manager 的 known_hosts 同一种指纹形状） */
export function peerSpki(sock: tls.TLSSocket): string {
  const cert = sock.getPeerCertificate(true) as { pubkey?: Buffer } | null
  if (!cert?.pubkey) return ''
  return `sha256/${createHash('sha256').update(cert.pubkey).digest('base64')}`
}
