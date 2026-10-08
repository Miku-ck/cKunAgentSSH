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
import { GatewayTransport, type HostKeyDecision } from './transports/gateway-transport'
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
  role?: string
  sessionId?: string
  spki?: string
  sinceAt?: number
  /** 第几次重连（0 = 未断过） */
  attempt?: number
}

interface Entry {
  rec: GatewayRecord
  transport: GatewayTransport | null
  state: GatewayState
  message?: string
  sinceAt?: number
  attempt: number
  retryTimer: NodeJS.Timeout | null
  /** 主动断开 / 删除中：迟到的 close/error 不再触发重连 */
  closing: boolean
}

const MAX_BACKOFF_MS = 30_000

export class GatewayManager {
  private readonly entries = new Map<string, Entry>()
  private win: BrowserWindow | null = null

  constructor(
    private readonly store: GatewayStore,
    private readonly push: (channel: string, data: unknown) => void,
  ) {}

  setWindow(win: BrowserWindow): void {
    this.win = win
  }

  statusAll(): GatewayStatus[] {
    return [...this.entries.values()].map(e => this.statusOf(e))
  }

  /** 供 transportFactory 取：未在线返回 null */
  transportOf(id: string): GatewayTransport | null {
    const e = this.entries.get(id)
    if (!e || e.state !== 'online' || !e.transport) return null
    return e.transport
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

  async connect(id: string): Promise<GatewayStatus> {
    const existing = this.entries.get(id)
    if (existing && (existing.state === 'online' || existing.state === 'connecting')) {
      return this.statusOf(existing)
    }
    const rec = await this.store.getWithSecret(id)
    if (!rec) throw new Error('网关记录不存在')
    const e: Entry = existing ?? {
      rec, transport: null, state: 'idle', attempt: 0, retryTimer: null, closing: false,
    }
    e.rec = rec
    e.closing = false
    this.entries.set(id, e)
    await this.dial(e)
    return this.statusOf(e)
  }

  private async dial(e: Entry): Promise<void> {
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
        trustedSpki: e.rec.trustedSpki,
        user: e.rec.user,
        password: e.rec.password,
        clientVersion: app.getVersion(),
        deviceName: app.name,
      },
      spki => this.decideHostKey(e, spki),
      (info, spki) => this.onReady(e, info, spki),
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
      e.message = (err as Error)?.message || String(err)
      this.emit(e)
      // 指纹不符与认证失败是配置问题，退避重试只会把网关日志刷满（§3.3）
      if (!isFatalAuthError(e.message)) this.scheduleRetry(e)
    }
  }

  private decideHostKey(e: Entry, spki: string): HostKeyDecision {
    if (!e.rec.trustedSpki) {
      // TOFU：首次见到就记下来并交给 UI 确认，与 SSH 首次连接同一套交互
      e.rec.trustedSpki = spki
      void this.store.setSpki(e.rec.id, spki)
      this.push('gateway:host-key-new', { id: e.rec.id, name: e.rec.name, host: e.rec.host, port: e.rec.port, spki })
      return 'new'
    }
    return spki === e.rec.trustedSpki ? 'trusted' : 'mismatch'
  }

  private onReady(e: Entry, info: HelloOkPayload, spki: string): void {
    e.rec.trustedSpki = e.rec.trustedSpki || spki
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
    e.transport?.close()
    e.transport = null
    e.state = 'idle'
    e.message = undefined
    e.attempt = 0
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

/** 口令错 / 指纹不符 / 被策略拒 → 不重连；其余（网络、超时）→ 重连 */
function isFatalAuthError(message: string): boolean {
  return /指纹|口令不正确|用户名或|denied-by-policy|硬失败/.test(message)
}
