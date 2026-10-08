/**
 * SshTransport — 用 ssh2 的 channel 转发实现 TunnelTransport
 *
 * 这一层是**行为搬迁**，不是重写：原来散在 tunnel-manager 里的 forwardOut / forwardIn /
 * 'tcp connection' / unforwardIn 调用原样搬进来。搬的同时把「等待点必须被 settle」这件事
 * 收到这里统一处理 —— 原来每个 start* 都要手写一次 `client.once('error', reject)` 桥接
 * （tunnel-manager 旧 :196-199 记的那次 IPC 挂死），漏一处就是一次永不返回的 invoke。
 * 现在 dial/bind 自己登记在途 promise，死亡时统一 reject，调用方不再需要桥接。
 */
import { Duplex } from 'node:stream'
import type { Client } from 'ssh2'
import type { RemoteBind, TunnelTransport } from '../tunnel-transport'

type RejectPending = (reason: string) => void

export class SshTransport implements TunnelTransport {
  readonly kind = 'ssh' as const
  /** 每条 SSH 隧道各持一条连接，停止时可以关 */
  readonly shared = false
  private deadFlag = false
  private deadReason = 'SSH 连接已断开'
  private readonly pending = new Set<RejectPending>()
  private inbound: ((stream: Duplex) => void) | undefined
  private readonly deadHandlers: ((reason: string) => void)[] = []

  constructor(private readonly client: Client) {
    client.on('error', (err: Error) => this.kill(`SSH 连接错误: ${err.message}`))
    client.on('close', () => this.kill('SSH 连接已断开'))
    // 远端绑定端口上有新连接 → accept 出通道交给订阅者（-R）
    client.on('tcp connection', (info, accept, reject) => {
      try {
        const stream = accept()
        this.inbound?.(stream)
      } catch {
        try { reject() } catch { /* 已断开 */ }
      }
    })
  }

  get dead(): boolean {
    return this.deadFlag
  }

  private kill(reason: string): void {
    if (this.deadFlag) return
    this.deadFlag = true
    this.deadReason = reason
    // 在途的 dial/bind 永不回调是 SSH 客户端死亡时的真实形状，不 settle 就是 IPC 挂死
    for (const reject of [...this.pending]) reject(reason)
    this.pending.clear()
    // 快照后再通知：回调里可能摘掉自己，迭代活数组会跳过后面的订阅者
    for (const h of [...this.deadHandlers]) h(reason)
  }

  dial(dstHost: string, dstPort: number, originHost: string, originPort: number): Promise<Duplex> {
    if (this.deadFlag) return Promise.reject(new Error(this.deadReason))
    return new Promise<Duplex>((resolve, reject) => {
      const settleDead: RejectPending = reason => reject(new Error(reason))
      this.pending.add(settleDead)
      this.client.forwardOut(originHost, originPort, dstHost, dstPort, (err, stream) => {
        this.pending.delete(settleDead)
        if (err || !stream) {
          reject(err ?? new Error(`SSH 通道建立失败: ${dstHost}:${dstPort}`))
          return
        }
        resolve(stream)
      })
    })
  }

  bind(b: RemoteBind): Promise<void> {
    if (this.deadFlag) return Promise.reject(new Error(this.deadReason))
    return new Promise<void>((resolve, reject) => {
      const settleDead: RejectPending = reason => reject(new Error(reason))
      this.pending.add(settleDead)
      this.client.forwardIn(b.bindHost, b.bindPort, err => {
        this.pending.delete(settleDead)
        if (err) reject(err)
        else resolve()
      })
    })
  }

  unbind(b: RemoteBind): Promise<void> {
    return new Promise<void>(resolve => {
      try {
        this.client.unforwardIn(b.bindHost, b.bindPort, () => resolve())
      } catch {
        resolve()
      }
    })
  }

  /**
   * SSH 每条隧道各持一条连接，一个订阅者就够，所以忽略 bind 身份（接口为共享会话而设）。
   * 'tcp connection' 事件也不带「是哪条 forwardIn 收进来的」信息 —— 无从按身份过滤。
   */
  onInbound(_b: RemoteBind, handler: (stream: Duplex) => void): void {
    this.inbound = handler
  }

  removeInbound(_b: RemoteBind): void {
    this.inbound = undefined
  }

  onDead(handler: (reason: string) => void): () => void {
    if (this.deadFlag) {
      handler(this.deadReason)
      return () => {}
    }
    this.deadHandlers.push(handler)
    return () => {
      const i = this.deadHandlers.indexOf(handler)
      if (i >= 0) this.deadHandlers.splice(i, 1)
    }
  }

  /**
   * 主动关闭不触发 onDead —— 那是拆除，不是故障。
   * 但在途的 dial/bind 必须落回：调用方是 `await transport.bind(...)`，
   * 不 settle 就是一次永不返回的 IPC invoke（与 kill() 同一形状）。
   */
  close(): void {
    this.deadFlag = true
    for (const reject of [...this.pending]) reject('本端已主动关闭隧道数据面')
    this.pending.clear()
    try {
      this.client.end()
    } catch {
      /* 已断开 */
    }
  }
}
