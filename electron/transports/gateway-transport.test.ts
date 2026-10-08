/**
 * GatewaySession 的帧语义验收（spec §8 M2）
 *
 * 假对端用自写的内存 Duplex（本机 Node 24 没有 stream.Duplex.pair()），全程不碰 TLS：
 * Node 的 crypto 只能解析 X.509 不能签发，真证书夹具要外部 openssl。把「帧语义」和
 * 「需要 PKI 夹具的连接面」切开，前者在这里全量跑；网关侧的字节往返由
 * gateway/hillgw.mjs --selftest 的 loopback 用例覆盖。
 */
import { Duplex } from 'stream'
import { describe, expect, it, vi } from 'vitest'
import { GatewaySession, TunnelDeniedError } from './gateway-transport'
import type { RemoteBind } from '../tunnel-transport'
import {
  createMuxDecoder,
  encodeFrame,
  GLOBAL_CHAN,
  type HelloOkPayload,
  type MuxFrame,
} from '../../shared/gateway-mux'

const HELLO_OK: HelloOkPayload = {
  sessionId: 'a3f9c1',
  heartbeatSec: 30,
  maxChannels: 256,
  role: 'prod',
  policyVersion: '2026-10-08.3',
}

const text = (s: string) => new TextEncoder().encode(s)

/**
 * 内存里的双向 Duplex。
 *
 * 不用 stream.Duplex.pair()：本机 Node 24 上它不存在（typeof === 'undefined'），
 * 而这里要的是「一端 write 等于另一端 data」，二十行自己接更可靠。
 */
function duplexPair(): [Duplex, Duplex] {
  const a = new Duplex({ read(): void {}, write(c: Buffer, _e: BufferEncoding, cb: () => void): void { b.push(c); cb() } })
  const b = new Duplex({ read(): void {}, write(c: Buffer, _e: BufferEncoding, cb: () => void): void { a.push(c); cb() } })
  return [a, b]
}

/** 假网关：localParity=even（客户端号段是奇数，所以到达这里的 OPEN 必须是奇数） */
class FakePeer {
  readonly frames: MuxFrame[] = []
  readonly echo = new Map<number, boolean>()
  private readonly dec = createMuxDecoder({ localParity: 'even', hasChannel: () => false })

  constructor(
    readonly sock: Duplex,
    onFrame: (peer: FakePeer, f: MuxFrame) => void,
  ) {
    sock.on('data', (chunk: Buffer) => {
      for (const f of this.dec.push(new Uint8Array(chunk))) {
        this.frames.push(f)
        onFrame(this, f)
      }
    })
  }

  /** 认证一律放行；OPEN 回 openOk 并开启回显；BIND 回 bindOk */
  autoRespond(f: MuxFrame): void {
    if (f.type === 'auth') this.send({ type: 'helloOk', chan: GLOBAL_CHAN, payload: HELLO_OK })
    else if (f.type === 'open') {
      this.send({ type: 'openOk', chan: f.chan })
      this.echo.set(f.chan, true)
    } else if (f.type === 'data' && this.echo.get(f.chan)) {
      this.send({ type: 'data', chan: f.chan, payload: f.payload })
    } else if (f.type === 'bind') this.send({ type: 'bindOk', chan: GLOBAL_CHAN })
  }

  /** 只放行认证，其余帧留给用例自己应答 */
  authOnly(f: MuxFrame): void {
    if (f.type === 'auth') this.send({ type: 'helloOk', chan: GLOBAL_CHAN, payload: HELLO_OK })
  }

  send(f: MuxFrame): void {
    this.sock.write(Buffer.from(encodeFrame(f)))
  }

  of(type: string): MuxFrame[] {
    return this.frames.filter(f => f.type === type)
  }

  last(type: string): MuxFrame | undefined {
    return this.of(type).at(-1)
  }
}

function pair(mode: 'auto' | 'auth' | 'manual' = 'auto'): { sess: GatewaySession; peer: FakePeer } {
  const [clientEnd, peerEnd] = duplexPair()
  const sess = new GatewaySession(clientEnd, {
    user: 'liantong',
    password: 'pw',
    clientVersion: '1.0.0',
    deviceName: 'test',
  })
  const peer = new FakePeer(peerEnd, (p, f) => {
    if (mode === 'auto') p.autoRespond(f)
    else if (mode === 'auth') p.authOnly(f)
  })
  sess.hello()
  return { sess, peer }
}

const readAll = (s: Duplex, n: number): Promise<Buffer> =>
  new Promise(resolve => {
    const chunks: Buffer[] = []
    let size = 0
    const onData = (c: Buffer): void => {
      chunks.push(c)
      size += c.length
      if (size >= n) {
        s.removeListener('data', onData)
        resolve(Buffer.concat(chunks))
      }
    }
    s.on('data', onData)
  })

describe('会话建立', () => {
  it('auth → helloOk，ready 拿到网关下发的角色与心跳周期', async () => {
    const { sess, peer } = pair()
    const info = await sess.ready
    expect(info).toEqual(HELLO_OK)
    expect(peer.of('hello')).toHaveLength(1)
    expect(peer.of('auth')[0]).toMatchObject({ chan: GLOBAL_CHAN })
    sess.close()
  })

  it('hello 之前 dial 会等 ready（不会把 OPEN 抢在认证前面发出去）', async () => {
    const { sess, peer } = pair()
    const dialing = sess.dial('db.internal', 5432, '127.0.0.1', 51000)
    await expect(dialing).resolves.toBeTruthy()
    const order = peer.frames.map(f => f.type)
    expect(order.indexOf('auth')).toBeLessThan(order.indexOf('open'))
    sess.close()
  })

  it('认证被拒（chan=0 的 openErr）→ ready 落错且会话判死', async () => {
    const { sess, peer } = pair('manual')
    peer.send({
      type: 'openErr',
      chan: GLOBAL_CHAN,
      payload: { code: 'malformed-request', message: '用户名或口令不正确' },
    })
    await expect(sess.ready).rejects.toBeInstanceOf(TunnelDeniedError)
    expect(sess.dead).toBe(true)
  })
})

describe('数据搬运', () => {
  it('dial → OPEN(奇数号) → openOk → 双向字节对得上', async () => {
    const { sess, peer } = pair()
    const ch = await sess.dial('db.internal', 5432, '127.0.0.1', 51000)
    expect(peer.last('open')).toMatchObject({ chan: 1, payload: { host: 'db.internal', port: 5432 } })

    const echoed = readAll(ch, 6)
    ch.write(Buffer.from('SELECT'))
    expect((await echoed).toString()).toBe('SELECT')
    sess.close()
  })

  it('两条通道互不串台（chan 分流正确）', async () => {
    const { sess } = pair()
    const a = await sess.dial('db-a', 5432, '127.0.0.1', 51001)
    const b = await sess.dial('db-b', 5433, '127.0.0.1', 51002)
    expect(a).not.toBe(b)
    const ra = readAll(a, 3)
    const rb = readAll(b, 3)
    a.write(Buffer.from('aaa'))
    b.write(Buffer.from('bbb'))
    expect((await ra).toString()).toBe('aaa')
    expect((await rb).toString()).toBe('bbb')
    sess.close()
  })

  it('OPEN 被拒 → dial 抛带 code 的拒绝，通道不留存', async () => {
    const { sess, peer } = pair('auth')
    const dialing = sess.dial('metadata', 80, '127.0.0.1', 51003)
    await vi.waitFor(() => expect(peer.of('open')).toHaveLength(1))
    peer.send({
      type: 'openErr',
      chan: 1,
      payload: { code: 'denied-by-dns', message: '解析出 169.254.169.254，不在允许范围' },
    })
    await expect(dialing).rejects.toMatchObject({ code: 'denied-by-dns' })
    expect(sess.channels.size).toBe(0)
    expect(sess.dead).toBe(false) // 单条通道被拒不等于整条会话作废
    sess.close()
  })
})

/** 走完 ready → OPEN → openOk，交回一条可用通道（号段固定 1） */
async function openChan(): Promise<{ sess: GatewaySession; peer: FakePeer; ch: Duplex }> {
  const { sess, peer } = pair('auth')
  await sess.ready
  const dialing = sess.dial('db.internal', 5432, '127.0.0.1', 51000)
  await vi.waitFor(() => expect(peer.of('open')).toHaveLength(1))
  peer.send({ type: 'openOk', chan: 1 })
  return { sess, peer, ch: await dialing }
}

describe('半关闭（§1.1 坑 2）', () => {
  it('end() 只发 FIN：不销毁通道，且读方向之后仍收得到数据', async () => {
    const { sess, peer, ch } = await openChan()

    ch.end()
    expect(peer.of('fin')).toHaveLength(1)
    expect(peer.of('rst')).toHaveLength(0)
    expect(ch.destroyed).toBe(false)

    // 本端写完不代表对端写完 —— 这正是 pipe 那版会提前断的场景
    const rest = readAll(ch, 3)
    peer.send({ type: 'data', chan: 1, payload: text('ACK') })
    expect((await rest).toString()).toBe('ACK')

    const eof = new Promise<void>(r => ch.once('end', () => r()))
    peer.send({ type: 'fin', chan: 1 })
    await expect(eof).resolves.toBeUndefined()
    sess.close()
  })

  it('destroy() 发 RST 并摘掉通道', async () => {
    const { sess, peer, ch } = await openChan()
    ch.destroy()
    await vi.waitFor(() => expect(peer.of('rst')).toHaveLength(1))
    expect(peer.of('fin')).toHaveLength(0)
    expect(sess.channels.size).toBe(0)
    sess.close()
  })
})

describe('迟到帧（§1.1 坑 1）', () => {
  it('给不存在的 chan 喂 DATA 不杀会话', async () => {
    const { sess, peer } = pair()
    await sess.ready
    peer.send({ type: 'data', chan: 999, payload: text('late') })
    expect(sess.dead).toBe(false)
    // 会话仍然可用：再开一条通道照样通
    const ch = await sess.dial('db', 1, '127.0.0.1', 51004)
    expect(ch).toBeTruthy()
    sess.close()
  })

  it('帧版本不符 → 会话作废（帧已失同步，不能继续解）', async () => {
    const { sess, peer } = pair()
    await sess.ready
    const bad = Buffer.from(encodeFrame({ type: 'fin', chan: 1 }))
    bad[0] = 9
    peer.sock.write(bad)
    await vi.waitFor(() => expect(sess.dead).toBe(true))
  })
})

const BIND_PUBLIC: RemoteBind = { bindHost: '0.0.0.0', bindPort: 8443, targetHost: 'localhost', targetPort: 6379 }
const BIND_SSH: RemoteBind = { bindHost: '0.0.0.0', bindPort: 22, targetHost: 'localhost', targetPort: 22 }

describe('-R 双向', () => {
  it('bind → BIND 帧（带要回连的本机目标）→ bindOk 兑现', async () => {
    const { sess, peer } = pair()
    await sess.bind(BIND_PUBLIC)
    expect(peer.last('bind')).toMatchObject({
      chan: GLOBAL_CHAN,
      payload: { bindHost: '0.0.0.0', bindPort: 8443, targetHost: 'localhost', targetPort: 6379 },
    })
    sess.close()
  })

  it('bind 被拒 → bind() 抛带 code 的错误', async () => {
    const { sess, peer } = pair('auth')
    const binding = sess.bind(BIND_SSH)
    await vi.waitFor(() => expect(peer.of('bind')).toHaveLength(1))
    peer.send({ type: 'openErr', chan: GLOBAL_CHAN, payload: { code: 'denied-by-policy', message: '22 端口不允许绑定' } })
    await expect(binding).rejects.toMatchObject({ code: 'denied-by-policy' })
    sess.close()
  })

  it('两条 bind 串行发起：BIND_OK 不回带身份，所以不能并发糊在一起', async () => {
    const { sess, peer } = pair()
    const p1 = sess.bind(BIND_PUBLIC)
    const p2 = sess.bind(BIND_SSH)
    await p1
    // 第一个 bindOk 之前第二条根本没发出去；应答完它才轮到第二条
    expect(peer.of('bind')).toHaveLength(1)
    await p2
    await vi.waitFor(() => expect(peer.of('bind')).toHaveLength(2))
    sess.close()
  })

  it('网关主动 OPEN（偶数号）→ 按 bind 身份派发到正确的那条订阅', async () => {
    const { sess, peer } = pair('auth')
    await sess.ready
    const hit = new Promise<Duplex>(r => sess.onInbound(BIND_PUBLIC, r))
    const other = vi.fn()
    sess.onInbound(BIND_SSH, other)
    peer.send({ type: 'open', chan: 12, payload: { host: 'localhost', port: 6379, bindHost: '0.0.0.0', bindPort: 8443 } })
    const ch = await hit
    expect(other).not.toHaveBeenCalled()
    expect(peer.last('openOk')).toMatchObject({ chan: 12 })
    ch.write(Buffer.from('PING'))
    await vi.waitFor(() => {
      expect(peer.of('data').at(-1)).toMatchObject({ chan: 12 })
    })
    sess.close()
  })

  it('没人认领的 bind 身份 → 回 RST，不留悬空通道', async () => {
    const { sess, peer } = pair('auth')
    await sess.ready
    const got = vi.fn()
    sess.onInbound(BIND_PUBLIC, got)
    peer.send({ type: 'open', chan: 14, payload: { host: 'localhost', port: 1, bindHost: '0.0.0.0', bindPort: 9999 } })
    await vi.waitFor(() => expect(peer.of('rst')).toHaveLength(1))
    expect(got).not.toHaveBeenCalled()
    expect(peer.of('openOk')).toHaveLength(0)
    expect(sess.channels.size).toBe(0)
    sess.close()
  })

  it('removeInbound 之后同一条 bind 的连接不再唤醒已停的规则', async () => {
    const { sess, peer } = pair('auth')
    await sess.ready
    const got = vi.fn()
    const off = () => sess.removeInbound(BIND_PUBLIC)
    sess.onInbound(BIND_PUBLIC, got)
    off()
    peer.send({ type: 'open', chan: 16, payload: { host: 'localhost', port: 6379, bindHost: '0.0.0.0', bindPort: 8443 } })
    await vi.waitFor(() => expect(peer.of('rst')).toHaveLength(1))
    expect(got).not.toHaveBeenCalled()
    sess.close()
  })
})

describe('大块写与主动关闭（审查回填）', () => {
  it('单块 >64KiB 的写切成若干 DATA 帧，字节拼回来一字不差', async () => {
    const { sess, peer, ch } = await openChan()
    const payload = Buffer.alloc(70 * 1024, 0x61)
    ch.write(payload)
    await vi.waitFor(() => expect(peer.of('data').length).toBeGreaterThanOrEqual(2))
    const frames = peer.of('data').filter(f => f.chan === 1) as Array<Extract<MuxFrame, { type: 'data' }>>
    const total = frames.reduce((n, f) => n + f.payload.length, 0)
    expect(total).toBe(payload.length)
    expect(frames.every(f => f.payload.length <= 0xffff)).toBe(true)
    expect(Buffer.concat(frames.map(f => Buffer.from(f.payload)))).toEqual(payload)
    sess.close()
  })

  it('主动 close() 也要把在途的 dial 落回：调用方是 await，不 settle 就是一次挂死的 IPC', async () => {
    const { sess, peer } = pair('auth')
    await sess.ready
    const dialing = sess.dial('db.internal', 5432, '127.0.0.1', 51005)
    await vi.waitFor(() => expect(peer.of('open')).toHaveLength(1))
    sess.close()
    await expect(dialing).rejects.toThrow(/已主动关闭/)
  })

  it('close() 把在途的 bind 一起落回', async () => {
    const { sess, peer } = pair('auth')
    const binding = sess.bind(BIND_PUBLIC)
    await vi.waitFor(() => expect(peer.of('bind')).toHaveLength(1))
    sess.close()
    await expect(binding).rejects.toThrow(/已主动关闭/)
  })

  it('onDead 返回的取消订阅真的能摘掉（共享会话上停一条规则不能留闭包）', async () => {
    const [clientEnd, peerEnd] = duplexPair()
    const sess = new GatewaySession(clientEnd, { user: 'u', password: 'p', clientVersion: '1', deviceName: 't' })
    const peer = new FakePeer(peerEnd, (p, f) => p.authOnly(f))
    sess.hello()
    await sess.ready
    const a = vi.fn()
    const b = vi.fn()
    sess.onDead(a)
    const offB = sess.onDead(b)
    offB()
    // 触发死亡用「坏帧」而不是 destroy()：内存假对端没有真 socket 的 close 传播
    const bad = Buffer.from(encodeFrame({ type: 'fin', chan: 1 }))
    bad[0] = 9
    peer.sock.write(bad)
    await vi.waitFor(() => expect(a).toHaveBeenCalled())
    expect(b).not.toHaveBeenCalled()
  })
})

describe('心跳（§1.2）', () => {
  it('连续 4 次无 PONG → 会话判死', async () => {
    vi.useFakeTimers()
    try {
      const [clientEnd, peerEnd] = duplexPair()
      const sess = new GatewaySession(clientEnd, {
        user: 'u', password: 'p', clientVersion: '1', deviceName: 't',
      })
      const peer = new FakePeer(peerEnd, (p, f) => {
        if (f.type === 'auth') p.send({ type: 'helloOk', chan: GLOBAL_CHAN, payload: { ...HELLO_OK, heartbeatSec: 1 } })
      })
      sess.hello()
      await vi.advanceTimersByTimeAsync(0)
      await expect(sess.ready).resolves.toMatchObject({ heartbeatSec: 1 })
      expect(sess.dead).toBe(false)
      await vi.advanceTimersByTimeAsync(4000)
      // 3 个 PING 都没人答 → 第 4 个 tick 判死，所以发出去的是 3 个
      expect(peer.of('ping')).toHaveLength(3)
      expect(sess.dead).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })
})
