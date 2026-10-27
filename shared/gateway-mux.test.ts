/**
 * 帧层的正确性验收（spec 2026-10-08-tls-dataplane §1.1 / §8 M1）
 *
 * 这一层错了不会崩，只会在跑起来之后表现为「偶发的目标串台」或「整条会话自己断」，
 * 所以把三条坑逐条钉成断言，而不是靠端到端碰运气。
 */
import { describe, expect, it } from 'vitest'
import {
  allocChan,
  createMuxDecoder,
  encodeFrame,
  FRAME_TYPE,
  GLOBAL_CHAN,
  HEADER_LEN,
  MAX_PAYLOAD,
  MuxProtocolError,
  parityOf,
  splitData,
  type MuxFrame,
} from './gateway-mux'

const hex = (b: Uint8Array) => [...b].map(x => x.toString(16).padStart(2, '0')).join('')
const utf8 = (s: string) => new TextEncoder().encode(s)

/** 客户端侧解码器：本端奇数 chan，且当前没有任何存活通道 */
const clientDecoder = (used: ReadonlySet<number> = new Set()) =>
  createMuxDecoder({ localParity: 'odd', hasChannel: c => used.has(c) })

const decodeAll = (frames: MuxFrame[], as: 'odd' | 'even' = 'even', used?: ReadonlySet<number>) => {
  const d = createMuxDecoder({ localParity: as, hasChannel: c => (used ?? new Set()).has(c) })
  return frames.flatMap(f => d.push(encodeFrame(f)))
}

describe('golden 字节向量（防与 gateway/hillgw.mjs 那份自含实现漂移）', () => {
  it('DATA chan=17 "abc"', () => {
    expect(hex(encodeFrame({ type: 'data', chan: 17, payload: utf8('abc') }))).toBe(
      '0107000000110003616263',
    )
  })

  it('FIN chan=12（偶数 = 网关侧分配的 -R 通道）', () => {
    expect(hex(encodeFrame({ type: 'fin', chan: 12 }))).toBe('01080000000c0000')
  })

  it('OPEN chan=19 的 JSON 形状与键序', () => {
    expect(
      hex(encodeFrame({ type: 'open', chan: 19, payload: { host: 'db.internal', port: 5432 } })),
    ).toBe(
      '01040000001300227b22686f7374223a2264622e696e7465726e616c222c22706f7274223a353433327d',
    )
  })

  it('PING 一律写 GLOBAL_CHAN=0，即使调用方传了别的号', () => {
    const bytes = encodeFrame({ type: 'ping', chan: 4242, seq: 7 })
    expect(hex(bytes.slice(2, 6))).toBe('00000000')
    expect(bytes.length).toBe(HEADER_LEN + utf8('{"seq":7}').length)
  })

  it('bindOk chan=0（-R 的 forwardIn 对位，无 payload）', () => {
    expect(hex(encodeFrame({ type: 'bindOk', chan: 0 }))).toBe('010f000000000000')
  })

  it('BIND 的 JSON 形状与键序（含入站要回连的本机目标）', () => {
    expect(
      hex(encodeFrame({
        type: 'bind',
        chan: 0,
        payload: { bindHost: '127.0.0.1', bindPort: 8443, targetHost: 'localhost', targetPort: 6379 },
      })),
    ).toBe(
      '010d0000000000537b2262696e64486f7374223a223132372e302e302e31222c2262696e64506f7274223a383434332c22746172676574486f7374223a226c6f63616c686f7374222c22746172676574506f7274223a363337397d',
    )
  })

  it('头 8 字节：ver u8 | type u8 | chan u32 BE | len u16 BE', () => {
    const b = encodeFrame({ type: 'data', chan: 0x01020304, payload: utf8('x') })
    expect(b[0]).toBe(1)
    expect(b[1]).toBe(FRAME_TYPE.data)
    expect(hex(b.slice(2, 6))).toBe('01020304')
    expect(hex(b.slice(6, 8))).toBe('0001')
    expect(HEADER_LEN).toBe(8)
    expect(MAX_PAYLOAD).toBe(0xffff)
  })
})

describe('round-trip', () => {
  const cases: MuxFrame[] = [
    { type: 'hello', chan: 0, payload: { clientVersion: '1.4.0', deviceName: 'ckun-win', selfReportedUser: 'ckun' } },
    { type: 'auth', chan: 0, payload: { user: 'liantong', password: 's3cr3t' } },
    { type: 'helloOk', chan: 0, payload: { sessionId: 'a3f9c1', heartbeatSec: 30, maxChannels: 256, role: 'prod', policyVersion: '2026-10-08.3' } },
    { type: 'open', chan: 17, payload: { host: 'db.internal', port: 5432 } },
    { type: 'openOk', chan: 17 },
    { type: 'openErr', chan: 17, payload: { code: 'denied-by-dns', message: '解析出 169.254.169.254，不在允许集' } },
    { type: 'data', chan: 17, payload: utf8('SELECT 1') },
    { type: 'data', chan: 17, payload: new Uint8Array(0) },
    { type: 'fin', chan: 17 },
    { type: 'rst', chan: 12 },
    { type: 'ping', chan: 0, seq: 1 },
    { type: 'pong', chan: 0, seq: 1 },
    { type: 'goaway', chan: 0, payload: { reason: '网关优雅下线', lastChan: 21 } },
    { type: 'bind', chan: 0, payload: { bindHost: '0.0.0.0', bindPort: 8443, targetHost: 'localhost', targetPort: 6379 } },
    { type: 'unbind', chan: 0, payload: { bindHost: '127.0.0.1', bindPort: 8443, targetHost: 'localhost', targetPort: 6379 } },
    { type: 'bindOk', chan: 0 },
  ]
  for (const f of cases) {
    it(`${f.type} chan=${f.chan}`, () => {
      const got = decodeAll([f])
      expect(got).toHaveLength(1)
      expect(got[0]).toEqual(f)
    })
  }

  it('二进制 payload 不被 JSON 化（DATA 走裸字节）', () => {
    const raw = Uint8Array.from([0, 1, 2, 250, 251, 252, 253, 254, 255])
    const [got] = decodeAll([{ type: 'data', chan: 3, payload: raw }])
    expect(got).toEqual({ type: 'data', chan: 3, payload: raw })
    expect(got.type === 'data' && got.payload).toBeInstanceOf(Uint8Array)
  })
})

describe('粘包与分片（TCP 是字节流，帧边界不保证对齐）', () => {
  const d = clientDecoder()
  const a = encodeFrame({ type: 'data', chan: 5, payload: utf8('hello') })
  const b = encodeFrame({ type: 'fin', chan: 5 })

  it('两帧一次喂到 → 两帧都出', () => {
    const merged = new Uint8Array(a.length + b.length)
    merged.set(a, 0)
    merged.set(b, a.length)
    expect(d.push(merged).map(f => f.type)).toEqual(['data', 'fin'])
  })

  it('逐字节喂 → 前面都空，最后一字节才出帧', () => {
    const one = clientDecoder()
    const seen: string[] = []
    for (let i = 0; i < a.length; i++) {
      const out = one.push(a.subarray(i, i + 1))
      if (out.length) {
        expect(i).toBe(a.length - 1)
        seen.push(...out.map(f => f.type))
      }
    }
    expect(seen).toEqual(['data'])
  })

  it('头被劈成 3+5 两半', () => {
    const one = clientDecoder()
    expect(one.push(a.subarray(0, 3))).toEqual([])
    expect(one.push(a.subarray(3, 8))).toEqual([])
    expect(one.push(a.subarray(8))).toHaveLength(1)
  })

  it('半包期间 buffered 单调涨，成帧后归零', () => {
    const one = clientDecoder()
    one.push(b.subarray(0, 4))
    expect(one.buffered).toBe(4)
    one.push(b.subarray(4))
    expect(one.buffered).toBe(0)
  })
})

describe('致命错误：只有这四类允许杀会话', () => {
  const fatal = (bytes: Uint8Array, used?: ReadonlySet<number>) => {
    let err: unknown = null
    try {
      clientDecoder(used ?? new Set()).push(bytes)
    } catch (e) {
      err = e
    }
    expect(err).toBeInstanceOf(MuxProtocolError)
    expect((err as MuxProtocolError).fatal).toBe(true)
    return err as MuxProtocolError
  }

  it('ver 不符', () => {
    const b = encodeFrame({ type: 'fin', chan: 5 })
    const bad = Uint8Array.from(b)
    bad[0] = 2
    expect(fatal(bad).message).toMatch(/帧版本不符/)
  })

  it('未知 type', () => {
    const b = encodeFrame({ type: 'fin', chan: 5 })
    const bad = Uint8Array.from(b)
    bad[1] = 0x7f
    expect(fatal(bad).message).toMatch(/未知帧类型：0x7f/)
  })

  it('客户端侧收到奇数 chan 的 OPEN（那是本端号段，对端不该拿它发起）', () => {
    // 客户端分配奇数、网关分配偶数：到达客户端的 OPEN 必须带偶数号。
    // 带奇数号 = 对端在劫持本端的号段，或帧边界已错位 —— 两者都不能继续解。
    const bytes = encodeFrame({ type: 'open', chan: 13, payload: { host: 'a', port: 1 } })
    expect(fatal(bytes).message).toMatch(/非法 OPEN/)
  })

  it('偶数 chan 的 OPEN 到达客户端是合法方向（网关发起的 -R）', () => {
    const bytes = encodeFrame({ type: 'open', chan: 12, payload: { host: 'a', port: 1 } })
    expect(clientDecoder().push(bytes).map(f => f.type)).toEqual(['open'])
  })

  it('OPEN 撞号（对端重用了仍存活的自己的号）', () => {
    const bytes = encodeFrame({ type: 'open', chan: 12, payload: { host: 'a', port: 1 } })
    // 网关侧看偶数 OPEN 是合法方向，但 12 已被占用
    let err: unknown = null
    try {
      createMuxDecoder({ localParity: 'odd', hasChannel: c => c === 12 }).push(bytes)
    } catch (e) { err = e }
    expect(err).toBeInstanceOf(MuxProtocolError)
    expect((err as MuxProtocolError).message).toMatch(/撞号=true/)
  })

  it('JSON 坏', () => {
    const payload = utf8('{"host":')
    const b = new Uint8Array(HEADER_LEN + payload.length)
    b[0] = 1
    b[1] = FRAME_TYPE.open
    b[6] = 0
    b[7] = payload.length
    b.set(payload, HEADER_LEN)
    expect(fatal(b, new Set([1]))).toBeInstanceOf(MuxProtocolError)
  })

  it('控制帧 payload 为空 = 缺 payload', () => {
    const b = new Uint8Array(HEADER_LEN)
    b[0] = 1
    b[1] = FRAME_TYPE.hello
    expect(fatal(b).message).toMatch(/缺少 payload/)
  })
})

describe('迟到帧不是错误（§1.1 坑 1）', () => {
  it('chan 不存在的 DATA / FIN 照常解出，不 throw', () => {
    // 「通道存不存在」由持有通道表的 transport 判，解码器一律放行 ——
    // FIN 之后对端仍可能有在途 DATA，在这里杀会话就是把正常现象当协议错误。
    const d = createMuxDecoder({ localParity: 'odd', hasChannel: () => true })
    const frames = decodeAll([
      { type: 'data', chan: 999, payload: utf8('late') },
      { type: 'fin', chan: 999 },
    ])
    expect(frames.map(f => f.type)).toEqual(['data', 'fin'])
    expect(d).toBeTruthy()
  })

  it('DATA 空 payload 也算合法帧（对端只报边界）', () => {
    const [f] = decodeAll([{ type: 'data', chan: 7, payload: new Uint8Array(0) }])
    expect(f.type).toBe('data')
  })
})

describe('chan 号段与奇偶', () => {
  it('parityOf 与「客户端奇、网关偶」一致', () => {
    expect(parityOf(1)).toBe('odd')
    expect(parityOf(2)).toBe('even')
    expect(parityOf(GLOBAL_CHAN)).toBe('even')
  })

  it('allocChan 只在自家号段里走，且跳过已占用', () => {
    expect(allocChan('odd', new Set())).toBe(1)
    expect(allocChan('even', new Set())).toBe(2)
    expect(allocChan('odd', new Set([1, 3, 5]))).toBe(7)
    expect(allocChan('even', new Set([2]))).toBe(4)
  })

  it('两侧各自分配永不撞号（同一批号段互不重叠）', () => {
    const used = new Set<number>()
    const seq: number[] = []
    for (let i = 0; i < 6; i++) {
      const c = allocChan(i % 2 === 0 ? 'odd' : 'even', used)!
      used.add(c)
      seq.push(c)
    }
    expect(seq).toEqual([1, 2, 3, 4, 5, 6])
    expect(new Set(seq).size).toBe(6)
  })
})

describe('发送侧切片', () => {
  it('splitData 按 64 KiB 上限切，末片带余数', () => {
    const big = new Uint8Array(MAX_PAYLOAD + 10)
    const parts = splitData(9, big)
    expect(parts).toHaveLength(2)
    expect(parts[0].type === 'data' && parts[0].payload.length).toBe(MAX_PAYLOAD)
    expect(parts[1].type === 'data' && parts[1].payload.length).toBe(10)
  })

  it('空字节不产帧（避免零长 DATA 白占一个帧头）', () => {
    expect(splitData(9, new Uint8Array(0))).toEqual([])
  })

  it('payload 超 u16 直接拒绝编码，而不是写坏 len 字段', () => {
    const tooBig = new Uint8Array(MAX_PAYLOAD + 1)
    expect(() => encodeFrame({ type: 'data', chan: 3, payload: tooBig })).toThrow(/超过 u16 上限/)
  })
})
