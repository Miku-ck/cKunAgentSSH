/**
 * shared/gateway-mux.ts — 自研 TLS 数据面的帧编解码（纯函数，零 node / DOM 依赖）
 *
 * 为什么在 shared/：electron 主进程与前端分属两个 tsconfig 程序，而 vitest 的
 * include 只扫 src/**，纯逻辑放 electron/ 就测不到（与 ssh-error-core.ts 同一个理由）。
 *
 * 帧格式（spec: docs/superpowers/specs/2026-10-08-tls-dataplane-design.md §1）：
 *
 *   byte  0      1      2 3 4 5    6 7      8 …
 *        +------+------+----------+------+-----------+
 *        | ver  | type |  chan    | len  | payload   |
 *        | u8   | u8   | u32 BE   | u16  | ≤65535 B  |
 *        +------+------+----------+------+-----------+
 *
 * 方向不占字节：chan 由发起方分配，客户端奇、网关偶（SSH channel id 同法）。
 * 于是 OPEN 天然双向，-R 不需要第二套帧；而对端发来**本端奇偶性**的 OPEN
 * 当场可判为协议错误 —— 这是把 dir 字节省下来的那笔账。
 *
 * 致命错误的边界（§1.1 坑 1）：只有「ver 不符」「未知 type」「OPEN 奇偶非法/撞号」
 * 「JSON 解析失败」这四类杀整条会话，因为它们意味着帧已失同步或版本不兼容。
 * 迟到帧（chan 不存在）**不是**错误：FIN/RST 之后对端仍可能有在途 DATA，
 * 那由持有通道表的 transport 丢弃，不在解码器里判（解码器不该知道通道生命周期）。
 */

export const MUX_VERSION = 1
/** 定长头字节数。gateway/hillgw.mjs 里有一份同值常量，改这里必须同步改它。 */
export const HEADER_LEN = 8
/** len 字段 u16，故单帧 payload 上限天然就是 65535 —— 不需要「超限」检查（见 decode 注释）。 */
export const MAX_PAYLOAD = 0xffff
/** 控制帧的全局通道号 */
export const GLOBAL_CHAN = 0

export const FRAME_TYPE = {
  hello: 0x01,
  auth: 0x02,
  helloOk: 0x03,
  open: 0x04,
  openOk: 0x05,
  openErr: 0x06,
  data: 0x07,
  fin: 0x08,
  rst: 0x09,
  ping: 0x0a,
  pong: 0x0b,
  goaway: 0x0c,
  /** 让对端在 bindHost:bindPort 上监听（-R 的 forwardIn 对位）。失败复用 openErr + chan=0 */
  bind: 0x0d,
  unbind: 0x0e,
  bindOk: 0x0f,
} as const

export type FrameTypeName = keyof typeof FRAME_TYPE

const TYPE_NAMES = new Map<number, FrameTypeName>(
  Object.entries(FRAME_TYPE).map(([name, code]) => [code, name as FrameTypeName]),
)

/** OPEN_ERR 的失败码。文案由网关生成，客户端只透传展示（改文案不必发客户端版本）。 */
export type OpenErrCode =
  | 'denied-by-policy'
  | 'denied-by-dns'
  | 'target-unreachable'
  | 'quota'
  | 'malformed-request'
  | 'draining'

export interface HelloPayload {
  clientVersion: string
  deviceName: string
  /** 仅用于展示与审计；策略只认客户端证书 CN 映射出的身份（§2.2） */
  selfReportedUser?: string
}

/**
 * 认证载荷。身份面是「TLS 通道内口令」，与 Hillstone Secure Connect 的实际形态一致
 * （它的连接表单就是 用户名 + 密码 + 记住密码）。
 * token 字段留给「一次性令牌换长期客户端证书」那条路，本版未实现，所以恒缺省。
 */
export interface AuthPayload {
  user: string
  /** 只在已建立并pin过的 TLS 会话内发送；服务端存 sha256，比对走 timingSafeEqual */
  password: string
  token?: string
}

export interface HelloOkPayload {
  sessionId: string
  heartbeatSec: number
  maxChannels: number
  role: string
  policyVersion: string
}

export interface OpenPayload {
  host: string
  port: number
  proto?: 'tcp'
  /** 预留给「按进程授权」：V1 恒缺省，加驱动时不改帧格式（§2.4） */
  app?: string
}

export interface OpenErrPayload {
  code: OpenErrCode
  message: string
}

export interface GoawayPayload {
  reason: string
  lastChan: number
}

export interface BindPayload {
  bindHost: string
  bindPort: number
}

export type MuxFrame =
  | { type: 'hello'; chan: number; payload: HelloPayload }
  | { type: 'auth'; chan: number; payload: AuthPayload }
  | { type: 'helloOk'; chan: number; payload: HelloOkPayload }
  | { type: 'open'; chan: number; payload: OpenPayload }
  | { type: 'openOk'; chan: number }
  | { type: 'openErr'; chan: number; payload: OpenErrPayload }
  | { type: 'data'; chan: number; payload: Uint8Array }
  | { type: 'fin'; chan: number }
  | { type: 'rst'; chan: number }
  | { type: 'ping'; chan: number; seq: number }
  | { type: 'pong'; chan: number; seq: number }
  | { type: 'goaway'; chan: number; payload: GoawayPayload }
  | { type: 'bind'; chan: number; payload: BindPayload }
  | { type: 'unbind'; chan: number; payload: BindPayload }
  | { type: 'bindOk'; chan: number }

/** 帧已不可继续解析（版本不符 / 未知 type / 奇偶非法 / JSON 坏）。调用方必须销毁整条会话。 */
export class MuxProtocolError extends Error {
  readonly fatal = true
  constructor(message: string) {
    super(message)
    this.name = 'MuxProtocolError'
  }
}

export type Parity = 'odd' | 'even'

/** 客户端分配奇数 chan、网关分配偶数 chan */
export const parityOf = (chan: number): Parity => (chan % 2 === 1 ? 'odd' : 'even')

const enc = new TextEncoder()
const dec = new TextDecoder()

function readU32(b: Uint8Array, at: number): number {
  return (b[at] << 24) | (b[at + 1] << 16) | (b[at + 2] << 8) | b[at + 3]
}

function writeU32(b: Uint8Array, at: number, v: number): void {
  b[at] = (v >>> 24) & 0xff
  b[at + 1] = (v >>> 16) & 0xff
  b[at + 2] = (v >>> 8) & 0xff
  b[at + 3] = v & 0xff
}

function jsonBytes(v: unknown): Uint8Array {
  return enc.encode(JSON.stringify(v))
}

/**
 * 帧 → 字节。DATA 的 payload 是原始字节，其余控制帧是 JSON。
 * 控制帧走 JSON 的权衡：它们小而稀少，可观测性优先；DATA 每帧省下的开销才重要。
 */
export function encodeFrame(f: MuxFrame): Uint8Array {
  const code = FRAME_TYPE[f.type]
  if (code === undefined) throw new Error(`未知帧类型：${String(f.type)}`)
  let chan = f.chan
  if (f.type === 'ping' || f.type === 'pong') chan = GLOBAL_CHAN

  let payload: Uint8Array
  switch (f.type) {
    case 'data':
      payload = f.payload
      break
    case 'openOk':
    case 'fin':
    case 'rst':
    case 'bindOk':
      payload = EMPTY
      break
    case 'ping':
    case 'pong':
      payload = jsonBytes({ seq: f.seq })
      break
    default:
      payload = jsonBytes(f.payload)
  }

  if (payload.length > MAX_PAYLOAD) {
    throw new Error(`单帧 payload ${payload.length} 超过 u16 上限 ${MAX_PAYLOAD}`)
  }
  const out = new Uint8Array(HEADER_LEN + payload.length)
  out[0] = MUX_VERSION
  out[1] = code
  writeU32(out, 2, chan >>> 0)
  out[6] = (payload.length >>> 8) & 0xff
  out[7] = payload.length & 0xff
  out.set(payload, HEADER_LEN)
  return out
}

const EMPTY = new Uint8Array(0)

/** 把 DATA 切成不超过 maxChunk 的若干帧：发送侧对齐 64 KiB 上限，避免每帧自己再判 */
export function splitData(chan: number, bytes: Uint8Array, maxChunk = MAX_PAYLOAD): MuxFrame[] {
  if (bytes.length === 0) return []
  const out: MuxFrame[] = []
  for (let at = 0; at < bytes.length; at += maxChunk) {
    out.push({ type: 'data', chan, payload: bytes.subarray(at, at + maxChunk) })
  }
  return out
}

export interface DecoderOptions {
  /** 本端的 chan 奇偶性。对端 OPEN 必须是**对端**那一侧的奇偶，否则协议错误。 */
  localParity: Parity
  /** 本端已分配且仍存活的 chan（判 OPEN 撞号）。返回 true = 已存在 = 非法。 */
  hasChannel: (chan: number) => boolean
}

export interface MuxDecoder {
  /**
   * 喂入任意分片的字节，返回**已完整**的帧。攒不够一帧就返回空数组。
   * @throws MuxProtocolError 帧已不可继续解析，调用方必须销毁会话。
   */
  push(bytes: Uint8Array): MuxFrame[]
  /** 已缓冲但未成帧的字节数（粘包/半包的观测面） */
  readonly buffered: number
}

/**
 * 增量解码器。单条 TLS 连接一个实例。
 *
 * 任何一处解析错位都意味着帧已失同步，继续解只会把别人的数据当帧 —— 所以四类
 * 致命错误一律 throw，由调用方销毁会话；而「chan 不存在」在这里不判（迟到帧是
 * 正常现象），那是持有通道表的 transport 的职责。
 */
export function createMuxDecoder(opts: DecoderOptions): MuxDecoder {
  let buf = new Uint8Array(0)
  const peerParity: Parity = opts.localParity === 'odd' ? 'even' : 'odd'

  return {
    get buffered() {
      return buf.length
    },
    push(chunk: Uint8Array): MuxFrame[] {
      if (chunk.length) {
        const merged = new Uint8Array(buf.length + chunk.length)
        merged.set(buf, 0)
        merged.set(chunk, buf.length)
        buf = merged
      }
      const frames: MuxFrame[] = []
      let at = 0

      while (buf.length - at >= HEADER_LEN) {
        const ver = buf[at]
        const code = buf[at + 1]
        const chan = readU32(buf, at + 2) >>> 0
        const len = (buf[at + 6] << 8) | buf[at + 7]

        if (ver !== MUX_VERSION) {
          throw new MuxProtocolError(`帧版本不符：期望 ${MUX_VERSION}，收到 ${ver}`)
        }
        const typeName = TYPE_NAMES.get(code)
        if (!typeName) {
          // 未知 type 无法安全跳过（不认识它的语义），且这基本等于版本不兼容
          throw new MuxProtocolError(`未知帧类型：0x${code.toString(16).padStart(2, '0')}`)
        }
        // len 是 u16，天然不可能超上限 —— 所以这里没有「超限」检查，只有成帧完整性检查
        if (buf.length - at - HEADER_LEN < len) break

        const payload = buf.slice(at + HEADER_LEN, at + HEADER_LEN + len)
        at += HEADER_LEN + len

        if (typeName === 'open' && (parityOf(chan) !== peerParity || opts.hasChannel(chan))) {
          throw new MuxProtocolError(
            `非法 OPEN：chan ${chan}（本端奇偶 ${opts.localParity}，对端应为 ${peerParity}；撞号=${opts.hasChannel(chan)}）`,
          )
        }
        frames.push(decodeBody(typeName, chan, payload))
      }

      if (at > 0) buf = buf.subarray(at)
      return frames
    },
  }
}

function parseJson<T>(typeName: string, bytes: Uint8Array): T {
  if (bytes.length === 0) throw new MuxProtocolError(`${typeName} 帧缺少 payload`)
  try {
    return JSON.parse(dec.decode(bytes)) as T
  } catch {
    // JSON 坏 = 帧边界已经错位，继续解没有意义
    throw new MuxProtocolError(`${typeName} 帧 payload 不是合法 JSON`)
  }
}

function decodeBody(typeName: FrameTypeName, chan: number, payload: Uint8Array): MuxFrame {
  switch (typeName) {
    case 'data':
      return { type: 'data', chan, payload }
    case 'openOk':
    case 'fin':
    case 'rst':
    case 'bindOk':
      return { type: typeName, chan }
    case 'bind':
      return { type: 'bind', chan, payload: parseJson<BindPayload>('bind', payload) }
    case 'unbind':
      return { type: 'unbind', chan, payload: parseJson<BindPayload>('unbind', payload) }
    case 'ping':
      return { type: 'ping', chan, seq: parseJson<{ seq: number }>('ping', payload).seq }
    case 'pong':
      return { type: 'pong', chan, seq: parseJson<{ seq: number }>('pong', payload).seq }
    case 'hello':
      return { type: 'hello', chan, payload: parseJson<HelloPayload>('hello', payload) }
    case 'auth':
      return { type: 'auth', chan, payload: parseJson<AuthPayload>('auth', payload) }
    case 'helloOk':
      return { type: 'helloOk', chan, payload: parseJson<HelloOkPayload>('helloOk', payload) }
    case 'open':
      return { type: 'open', chan, payload: parseJson<OpenPayload>('open', payload) }
    case 'openErr':
      return { type: 'openErr', chan, payload: parseJson<OpenErrPayload>('openErr', payload) }
    case 'goaway':
      return { type: 'goaway', chan, payload: parseJson<GoawayPayload>('goaway', payload) }
  }
}

/**
 * 分配下一个可用 chan。跳过已占用号，一轮到底即耗尽（调用方按 quota 拒绝新通道）。
 * 起始值：奇偶各从 1 / 2 起，GLOBAL_CHAN=0 不参与分配。
 */
export function allocChan(parity: Parity, used: ReadonlySet<number>): number | null {
  const start = parity === 'odd' ? 1 : 2
  // u32 上界的一半才是本端可用的号段空间；这里取一个足够真实的上限，
  // 真正的并发上限由网关下发的 maxChannels 决定，不是这里。
  for (let i = 0; i < 0x10000; i++) {
    const chan = start + i * 2
    if (chan > 0xffffffff - 1) return null
    if (!used.has(chan)) return chan
  }
  return null
}
