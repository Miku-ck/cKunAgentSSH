/**
 * TunnelTransport — 隧道数据面的抽象接缝
 *
 * 设计要点（历史设计文档已退役，帧格式见 shared/gateway-mux.ts）：
 * TunnelManager 原先直接绑 ssh2（forwardOut / forwardIn / 'tcp connection' / unforwardIn），
 * 于是「数据面怎么来」和「转发怎么编排」长在一起。抽这一层后两条数据面并列：
 *   - SshTransport：借用户已有的 SSH 服务器，零部署（electron/transports/ssh-transport.ts）
 *   - GatewayTransport：连自家 TLS 网关，自研帧（electron/transports/gateway-transport.ts）
 * 这不是新旧切换而是两个语义不同的目标端，删掉任一条都少一个功能。
 *
 * 通道一律用 node:stream.Duplex：本机 socket 那侧的 pipe 编排因此完全不变，
 * 新数据面只需实现一个 Duplex 包装（读方向喂 DATA、写方向发 DATA）。
 */
import type { Duplex } from 'node:stream'

/** -R 的一条绑定：对端开的口 + 入站连接要建立回的本机目标 */
export interface RemoteBind {
  bindHost: string
  bindPort: number
  targetHost: string
  targetPort: number
}

export interface TunnelTransport {
  /**
   * 主动开到 dst 的一条通道（-L 与 -D 的 CONNECT 用）。
   * origin* 是本机发起方的地址，SSH 侧用它做日志归因；网关侧不进 OPEN 帧
   * （允许集不看 origin，多一个字段就多一处两侧必须同步的字节）。
   * @throws 目标被拒 / 建连失败 / 本端已死，message 直接进 TunnelStatus.message
   */
  dial(dstHost: string, dstPort: number, originHost: string, originPort: number): Promise<Duplex>
  /** 让对端在 bindHost:bindPort 上监听，此后入站连接经 onInbound 回来（-R 用） */
  bind(b: RemoteBind): Promise<void>
  /** 撤销一次 bind。best-effort：实现不得因它 throw 而阻断清理路径 */
  unbind(b: RemoteBind): Promise<void>
  /**
   * 订阅**这条 bind** 的入站连接。按 bind 身份派发而不是广播：网关的一条共享会话上
   * 挂着多条 -R 规则，广播会让一条入站被每条规则各自回连、同一条通道被 pipe 进两个
   * 本机目标（串台）。SSH 侧每条隧道各持一条连接，键对它无意义。
   */
  onInbound(b: RemoteBind, handler: (stream: Duplex) => void): void
  /** 撤订阅：teardown 不摘就是共享会话上越积越多的死回调，停掉的规则还会被打进来的连接唤醒 */
  removeInbound(b: RemoteBind): void
  /**
   * 订阅整条数据面死亡（含被动断开）。close() 主动关闭不触发。
   * 返回取消订阅：共享会话上每条隧道都挂一个闭包，不摘就是停一条规则漏一个 session 对象。
   */
  onDead(handler: (reason: string) => void): () => void
  close(): void
  readonly kind: 'ssh' | 'gateway'
  readonly dead: boolean
  /**
   * true = 这条数据面被多条隧道共用（网关侧一条会话服务多条规则）。
   * 单条隧道停止时不得 close 它，否则会把同网关的其他隧道一起带走。
   */
  readonly shared: boolean
}
