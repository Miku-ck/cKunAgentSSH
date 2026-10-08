/**
 * Gateway Manager — 按网关维护一条共享的 TLS 接入会话
 *
 * 为什么需要它（spec §3.1 的补充）：SSH 数据面是「每条隧道各开一条连接」，因为对端是
 * 用户自己的 SSH 服务器、没有会话概念；而 TLS 网关侧的身份是**登录出来的**（口令认证 →
 * 角色 → 允许集），一条会话服务多条规则才是它的形状 —— 界面上「连接/断开」的对象
 * 也是网关，不是某条转发规则。所以这里持 gatewayId → GatewayTransport 一张表，
 * 隧道规则经 transportFactory 搭这条共享会话；网关没在线时规则启动即报「未连接」。
 *
 * 重连在这一层（不是 tunnel-manager）：会话断了要把网关卡从「在线」改回「重连中」，
 * 而每条隧道各自退避会打出 N 倍的重连风暴。退避成功后在途通道一律不迁移（决策 6）。
 */
import { app } from 'electron'
import { GatewayStore, type GatewayRecord } from './gateway-store'
import {
  GatewayHostKeyPendingError,
  GatewayTransport,
  TunnelDeniedError,
  type ChannelRow,
  type HostKeyDecision,
} from './transports/gateway-transport'
import type { HelloOkPayload } from '../shared/gateway-mux'

export type GatewayState = 'idle' | 'connecting' | 'online' | 'reconnecting' | 'error'

export interface GatewayStatus {
  id: string
  name: string
  host: string
  port: number
  state: GatewayState
  message?: string
  /** 当前活动转发通道数 */
  channels?: number
  /** 网关协商的心跳间隔（秒），未连接时为空 */
  heartbeatSec?: number
  role?: string
  sessionId?: string
  spki?: string
  sinceAt?: number
  /** 第几次重连（0 = 未断过） */
  attempt?: number
}

/** 首次见到网关证书时推给 UI 的确认事件（与 ssh:host-key-new 同一形状） */
export interface GatewayHostKeyEvent {
  id: string
  name: string
  host: string
  port: number
  spki: string
}

interface Entry {
  rec: GatewayRecord
  transport: GatewayTransport | null
  state: GatewayState
  message?: string
  sinceAt?: number
  attempt: number
  /** 网关协商下来的心跳秒数，仅在 HELLO_OK 之后有值 */
  heartbeatSec?: number
  retryTimer: NodeJS.Timeout | null
  /** 主动断开 / 删除中：迟到的 close/error 不再触发重连 */
  closing: boolean
}

const MAX_BACKOFF_MS = 30_000

export class GatewayManager {
  private readonly entries = new Map<string, Entry>()
  /** 首见指纹待确认：id → 这次握手实际收到的 spki（渲染层不能自填，§2.1） */
  private readonly pendingSpki = new Map<string, string>()
  /** 同一条网关的并发 connect 只拨一次 */
  private readonly connecting = new Map<string, Promise<GatewayStatus>>()
  /** 网关被主动断开 / 删除时通知隧道层（main.ts 注入，避免构造顺序耦合） */
  private onGatewayDown: (id: string) => void = () => {}

  constructor(
    private readonly store: GatewayStore,
    private readonly push: (channel: string, data: unknown) => void,
  ) {}

  setOnGatewayDown(fn: (id: string) => void): void {
    this.onGatewayDown = fn
  }

  statusAll(): GatewayStatus[] {
    return [...this.entries.values()].map(e => this.statusOf(e))
  }

  /** 待确认指纹（UI 弹窗与 trustHostKey 校验都读这里，渲染层无从自填） */
  pendingSpkiOf(id: string): string | undefined {
    return this.pendingSpki.get(id)
  }

  /** 供 transportFactory 取：未在线返回 null */
  transportOf(id: string): GatewayTransport | null {
    const e = this.entries.get(id)
    if (!e || e.state !== 'online' || !e.transport) return null
    return e.transport
  }

  /** 「通道」页签的数据源：帧层的可观测面是这条数据面唯一能自证的地方 */
  channelsOf(id: string): ChannelRow[] {
    return this.entries.get(id)?.transport?.channelRows() ?? []
  }

  private statusOf(e: Entry): GatewayStatus {
    const t = e.transport
    return {
      id: e.rec.id,
      name: e.rec.name,
      host: e.rec.host,
      port: e.rec.port,
      state: e.state,
      message: e.message,
      channels: t ? t.stats.channels : 0,
      heartbeatSec: e.state === 'online' ? e.heartbeatSec : undefined,
      role: t?.stats.role,
      sessionId: t?.stats.sessionId,
      spki: t?.stats.spki || e.rec.trustedSpki,
      sinceAt: e.sinceAt,
      attempt: e.attempt,
    }
  }

  private emit(e: Entry): void {
    this.push('gateway:status', this.statusOf(e))
  }

  /**
   * 连接某条网关。并发调用（用户连点、或重连定时器与手动点击撞在一起）只拨一次：
   * 前一条 transport 还没落到表里就返回，会出现两条 TLS 会话、其中一条成无人持有的孤儿。
   */
  connect(id: string): Promise<GatewayStatus> {
    const inflight = this.connecting.get(id)
    if (inflight) return inflight
    const run = this.doConnect(id).finally(() => this.connecting.delete(id))
    this.connecting.set(id, run)
    return run
  }

  private async doConnect(id: string): Promise<GatewayStatus> {
    const existing = this.entries.get(id)
    if (existing && (existing.state === 'online' || existing.state === 'connecting')) {
      return this.statusOf(existing)
    }
    const rec = await this.store.getWithSecret(id)
    if (!rec) throw new Error('网关记录不存在')
    const e: Entry = existing ?? {
      rec, transport: null, state: 'idle', attempt: 0, retryTimer: null, closing: false,
    }
    // 用户在退避期间点「连接」= 现在就试：掐掉定时器，否则两条 dial 会并行
    if (e.retryTimer) {
      clearTimeout(e.retryTimer)
      e.retryTimer = null
    }
    e.rec = rec
    e.closing = false
    this.entries.set(id, e)
    await this.dial(e)
    return this.statusOf(e)
  }

  private async dial(e: Entry): Promise<void> {
    // 每次拨号都重读记录。gateway:save 只落库、不会通知这一层，沿用内存里的旧 e.rec
    // 意味着用户改完 host/port/口令后，自动重连照旧打旧地址、拿旧口令试（界面上已经
    // 显示新值）；而 store 在身份变更时会清掉 trustedSpki，内存里那份旧钉值还会让
    // decideHostKey 判成「指纹不一致」直接硬失败。读不到（记录被删）就沿用当前值，
    // 由随后 transport 的失败路径收场 —— 这里不额外造第三种状态。
    const fresh = await this.store.getWithSecret(e.rec.id).catch(() => undefined)
    if (fresh) e.rec = fresh
    if (!e.rec.password) {
      e.state = 'error'
      e.message = '本机没有可用的口令（系统钥匙串不可用或未重新输入），请在弹窗里再填一次'
      this.emit(e)
      return
    }
    e.state = e.attempt > 0 ? 'reconnecting' : 'connecting'
    e.message = undefined
    this.emit(e)
    const transport = new GatewayTransport(
      {
        host: e.rec.host,
        port: e.rec.port,
        caPem: e.rec.caPem,
        user: e.rec.user,
        password: e.rec.password,
        clientVersion: app.getVersion(),
        deviceName: app.name,
      },
      spki => this.decideHostKey(e, spki),
      info => this.onReady(e, info),
    )
    transport.onDead(reason => this.onDead(e, reason))
    try {
      await transport.start()
      if (e.closing) {
        transport.close()
        return
      }
      e.transport = transport
      e.state = 'online'
      e.sinceAt = Date.now()
      e.attempt = 0
      this.emit(e)
    } catch (err) {
      e.transport = null
      e.state = 'error'
      if (err instanceof GatewayHostKeyPendingError) {
        // 首见指纹：会话在发出 AUTH 之前就被拆掉，口令没有外发（§2.1）。
        // 这里只记账并推事件，落盘等用户点「信任并连接」。
        this.pendingSpki.set(e.rec.id, err.spki)
        e.message = '首次连接：需确认服务端证书指纹'
        this.push('gateway:host-key-new', {
          id: e.rec.id, name: e.rec.name, host: e.rec.host, port: e.rec.port, spki: err.spki,
        })
      } else {
        e.message = (err as Error)?.message || String(err)
      }
      this.emit(e)
      // 口令错 / 指纹不符 / 被策略拒是配置问题，退避重试只会把网关日志刷满（§3.3）
      if (!isFatalAuthError(err)) this.scheduleRetry(e)
    }
  }

  private decideHostKey(e: Entry, spki: string): HostKeyDecision {
    // 这里只判定、不写库：写库的时机是用户确认之后（trustHostKey）
    if (!e.rec.trustedSpki) return 'new'
    return spki === e.rec.trustedSpki ? 'trusted' : 'mismatch'
  }

  /**
   * 用户点头才落盘。提交的 spki 必须等于本次握手实际收到的那份：渲染层递什么就写什么，
   * 等于把 pinning 的决定权交给能注入 JS 的一侧。
   */
  async trustHostKey(id: string, spki: string): Promise<void> {
    const pending = this.pendingSpki.get(id)
    if (!pending) throw new Error('这条网关没有待确认的指纹，请重新点连接')
    if (pending !== spki) throw new Error('提交的指纹与本次握手收到的不一致，已拒绝')
    await this.store.setSpki(id, spki)
    this.pendingSpki.delete(id)
    const e = this.entries.get(id)
    if (e) e.rec = { ...e.rec, trustedSpki: spki }
  }

  /** 用户取消：待确认记录清掉，指纹不落库 —— 下次连接重新问一遍 */
  rejectHostKey(id: string): void {
    this.pendingSpki.delete(id)
  }

  private onReady(e: Entry, info: HelloOkPayload): void {
    // 心跳间隔是网关协商出来的，不能只活在 message 文案里：UI 要按字段显示，
    // 抄一份「30 s」进模板就是把可变的运行时事实写死。
    e.heartbeatSec = info.heartbeatSec
    e.message = `角色 ${info.role} · 心跳 ${info.heartbeatSec}s`
  }

  private onDead(e: Entry, reason: string): void {
    if (e.closing) return
    e.transport = null
    e.state = 'reconnecting'
    e.message = reason
    this.emit(e)
    this.scheduleRetry(e)
  }

  private scheduleRetry(e: Entry): void {
    if (e.retryTimer) return
    e.attempt += 1
    // 指数退避 + ±20% jitter：多条网关同时断线时不会一起回头打服务器
    const base = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** Math.min(e.attempt - 1, 5))
    const wait = Math.round(base * (0.8 + Math.random() * 0.4))
    e.retryTimer = setTimeout(() => {
      e.retryTimer = null
      if (e.closing) return
      void this.dial(e)
    }, wait)
    e.retryTimer.unref?.()
  }

  async disconnect(id: string): Promise<GatewayStatus> {
    const e = this.entries.get(id)
    if (!e) return { id, name: '', host: '', port: 0, state: 'idle' }
    e.closing = true
    if (e.retryTimer) clearTimeout(e.retryTimer)
    e.retryTimer = null
    this.pendingSpki.delete(id)
    e.transport?.close()
    e.transport = null
    e.state = 'idle'
    e.message = undefined
    e.attempt = 0
    // 主动 close() 不推 onDead，挂在它上的隧道得由这里联动停掉（§11.4）
    this.onGatewayDown(id)
    this.emit(e)
    return this.statusOf(e)
  }

  /** 记录被删：先停会话再摘表 */
  async drop(id: string): Promise<void> {
    await this.disconnect(id)
    this.entries.delete(id)
  }

  stopAll(): void {
    for (const id of [...this.entries.keys()]) void this.disconnect(id)
  }
}

/**
 * 口令错 / 指纹不符 / 被策略拒 / 等着确认 → 不重连；其余（网络、超时）→ 重连。
 * 优先看错误类型：网关的 OPEN_ERR 自带 code（TunnelDeniedError），文案会变而码不会，
 * 拿中文正则当判据会随措辞调整而误停或漏停。
 */
function isFatalAuthError(err: unknown): boolean {
  if (err instanceof GatewayHostKeyPendingError) return true
  if (err instanceof TunnelDeniedError) return err.code === 'malformed-request' || err.code === 'denied-by-policy'
  return /指纹|口令|硬失败/.test(String((err as Error)?.message ?? err))
}
