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
import type { TunnelTransport } from '../tunnel-transport'

type RejectPending = (reason: string) => void

export class SshTransport implements TunnelTransport {
  readonly kind = 'ssh' as const
  private deadFlag = false
  private deadReason = 'SSH 连接已断开'
  private readonly pending = new Set<RejectPending>()
  private readonly inboundHandlers: ((stream: Duplex) => void)[] = []
  private readonly deadHandlers: ((reason: string) => void)[] = []

  constructor(private readonly client: Client) {
    client.on('error', (err: Error) => this.kill(`SSH 连接错误: ${err.message}`))
    client.on('close', () => this.kill('SSH 连接已断开'))
    // 远端绑定端口上有新连接 → accept 出通道交给订阅者（-R）
    client.on('tcp connection', (info, accept, reject) => {
      try {
        const stream = accept()
        for (const h of this.inboundHandlers) h(stream)
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
    for (const h of this.deadHandlers) h(reason)
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

  bind(bindHost: string, bindPort: number): Promise<void> {
    if (this.deadFlag) return Promise.reject(new Error(this.deadReason))
    return new Promise<void>((resolve, reject) => {
      const settleDead: RejectPending = reason => reject(new Error(reason))
      this.pending.add(settleDead)
      this.client.forwardIn(bindHost, bindPort, err => {
        this.pending.delete(settleDead)
        if (err) reject(err)
        else resolve()
      })
    })
  }

  unbind(bindHost: string, bindPort: number): Promise<void> {
    return new Promise<void>(resolve => {
      try {
        this.client.unforwardIn(bindHost, bindPort, () => resolve())
      } catch {
        resolve()
      }
    })
  }

  onInbound(handler: (stream: Duplex) => void): void {
    this.inboundHandlers.push(handler)
  }

  onDead(handler: (reason: string) => void): void {
    if (this.deadFlag) {
      handler(this.deadReason)
      return
    }
    this.deadHandlers.push(handler)
  }

  /** 主动关闭不触发 onDead —— 那是拆除，不是故障 */
  close(): void {
    this.deadFlag = true
    this.pending.clear()
    try {
      this.client.end()
    } catch {
      /* 已断开 */
    }
  }
}
