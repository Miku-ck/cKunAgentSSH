#!/usr/bin/env node
/**
 * hillgw.mjs — cKunAgentSSH 的 TLS 接入网关（单文件 · 零依赖 · 只用 node 内置模块）
 *
 * 设计：docs/superpowers/specs/2026-10-08-tls-dataplane-design.md §4
 * 定位：交付到内网自己跑的运维侧进程（`node hillgw.mjs --config gw.json` + systemd），
 *      刻意不进 Electron 打包链 —— 客户端已有签名/更新链路，拖上网关只会互相拖累。
 *
 * 帧格式与 shared/gateway-mux.ts 是**两份实现**（网关必须自含单文件，没法 import TS）。
 * 防漂移靠 --selftest 里那组 golden 字节：它与 TS 测试里的字面量一一对应，
 * 改了任何一边而没改另一边，`node hillgw.mjs --selftest` 就红。
 *
 * 用法：
 *   node hillgw.mjs --config gw.json          启动
 *   node hillgw.mjs --hash <口令>              产出 users[].passwordSha256（配置里不留明文）
 *   node hillgw.mjs --selftest                帧编解码 + 策略匹配自比对
 *
 * 前置：服务端证书与私钥由运维提供（openssl 或既有 PKI）。Node 的 crypto 只能**解析**
 * X.509、不能签发，所以这里不内置 CA —— 客户端证书（mTLS 第二因子）因此不在本版，
 * V1 的身份面是「TLS 通道内口令」。
 */
import * as tls from 'node:tls'
import * as net from 'node:net'
import * as fs from 'node:fs'
import * as dns from 'node:dns'
import * as crypto from 'node:crypto'
import process from 'node:process'

// ─────────────────────────── 帧（与 shared/gateway-mux.ts 对齐） ───────────────────────────

const VERSION = 1
const HEADER_LEN = 8
const MAX_PAYLOAD = 0xffff
const GLOBAL_CHAN = 0

const FT = {
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
  bind: 0x0d,
  unbind: 0x0e,
  bindOk: 0x0f,
}
const FT_NAMES = new Map(Object.entries(FT).map(([k, v]) => [v, k]))
/** 认证前唯一放行的帧类型（其余一律视为劫持/越权） */
const PRE_AUTH = new Set(['hello', 'auth', 'ping', 'pong', 'goaway'])

class MuxProtocolError extends Error {}

const textEnc = new TextEncoder()
const textDec = new TextDecoder()

function encodeFrame(type, chan, payloadObj, raw) {
  const code = FT[type]
  if (code === undefined) throw new Error(`未知帧类型：${type}`)
  // 心跳不占通道号：与 TS 侧 encodeFrame 同一归一，否则两份实现写出的字节不同
  if (type === 'ping' || type === 'pong') chan = GLOBAL_CHAN
  let payload
  if (type === 'data') payload = raw ?? new Uint8Array(0)
  else if (type === 'openOk' || type === 'fin' || type === 'rst' || type === 'bindOk') payload = new Uint8Array(0)
  else payload = textEnc.encode(JSON.stringify(payloadObj))
  if (payload.length > MAX_PAYLOAD) throw new Error(`payload ${payload.length} 超过 u16 上限`)
  const out = Buffer.alloc(HEADER_LEN + payload.length)
  out[0] = VERSION
  out[1] = code
  out.writeUInt32BE(chan >>> 0, 2)
  out.writeUInt16BE(payload.length, 6)
  Buffer.from(payload.buffer, payload.byteOffset, payload.length).copy(out, HEADER_LEN)
  return out
}

/**
 * 增量解码器。网关侧 localParity='even'（本机分配偶数 chan），
 * 所以到达网关的 OPEN 必须带偶数号；带奇数号 = 劫持客户端号段 = 致命。
 */
function createDecoder(localParity, hasChannel) {
  let buf = Buffer.alloc(0)
  const peerParity = localParity === 'odd' ? 'even' : 'odd'
  return {
    push(chunk) {
      if (chunk.length) buf = Buffer.concat([buf, chunk])
      const frames = []
      let at = 0
      while (buf.length - at >= HEADER_LEN) {
        const ver = buf[at]
        const code = buf[at + 1]
        const chan = buf.readUInt32BE(at + 2)
        const len = buf.readUInt16BE(at + 6)
        if (ver !== VERSION) throw new MuxProtocolError(`帧版本不符：期望 ${VERSION}，收到 ${ver}`)
        const type = FT_NAMES.get(code)
        if (!type) throw new MuxProtocolError(`未知帧类型：0x${code.toString(16).padStart(2, '0')}`)
        if (buf.length - at - HEADER_LEN < len) break
        const payload = buf.subarray(at + HEADER_LEN, at + HEADER_LEN + len)
        at += HEADER_LEN + len
        if (type === 'open' && ((chan % 2 === 1 ? 'odd' : 'even') !== peerParity || hasChannel(chan))) {
          throw new MuxProtocolError(`非法 OPEN：chan ${chan}`)
        }
        frames.push({ type, chan, payload: decodePayload(type, payload) })
      }
      if (at > 0) buf = buf.subarray(at)
      return frames
    },
  }
}

/**
 * JSON 帧的 payload 必须解成普通对象 —— 与 shared/gateway-mux.ts 的 parseJson 同一条规则，
 * 两侧任一放宽都会让另一侧的 `p.user` / `f.payload.code` 在 'data' 监听器里抛出去。
 * `null` 是合法 JSON：未认证的 hello/auth 带一个 `null` body 就能打死网关进程本身
 * （所有租户一起掉线），所以形状错与 JSON 坏同级处理。
 */
function decodePayload(type, bytes) {
  if (type === 'data') return bytes
  if (type === 'openOk' || type === 'fin' || type === 'rst' || type === 'bindOk') return null
  if (bytes.length === 0) throw new MuxProtocolError(`${type} 帧缺少 payload`)
  let parsed
  try {
    parsed = JSON.parse(textDec.decode(bytes))
  } catch {
    throw new MuxProtocolError(`${type} 帧 payload 不是合法 JSON`)
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new MuxProtocolError(`${type} 帧 payload 不是 JSON 对象`)
  }
  return parsed
}

// ─────────────────────────── 授权（ZTNA 的正文） ───────────────────────────

/** 网关自己不能变成开放中继：这些目的地一律拒绝（§2.3） */
const DENY_V4_CIDR = [
  ['169.254.0.0', 16], // 云元数据
  ['127.0.0.0', 8],    // 网关本机回环
  ['0.0.0.0', 8],
]
/** fe80::/10 链路本地（含 fe80–febf，只比 'fe80' 前缀会漏）；fc00::/7 一段就盖住 fc00 与 fd00 */
const DENY_V6 = [
  { pfx: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1], bits: 128 },
  { pfx: [0xfe, 0x80, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], bits: 10 },
  { pfx: [0xfc, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], bits: 7 },
]

const ipToLong = (ip) => {
  const parts = ip.split('.').map(Number)
  if (parts.length !== 4 || parts.some(n => !Number.isInteger(n) || n < 0 || n > 255)) return null
  return ((parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]) >>> 0
}

function inCidr(ip, [base, bits]) {
  const a = ipToLong(ip)
  const b = ipToLong(base)
  if (a === null || b === null) return false
  if (bits === 0) return true
  const mask = (0xffffffff << (32 - bits)) >>> 0
  return (a & mask) === (b & mask)
}

/** 前 bits 位逐字节比对 */
function inPrefix(bytes, pfx, bits) {
  for (let i = 0; i < 16; i++) {
    if (bits <= 0) return true
    if (bits >= 8) {
      if (bytes[i] !== pfx[i]) return false
      bits -= 8
      continue
    }
    const mask = (0xff << (8 - bits)) & 0xff
    return (bytes[i] & mask) === (pfx[i] & mask)
  }
  return true
}

/**
 * IPv6 展开成 16 字节，处理 '::' 压缩与 IPv4-mapped 尾段（::ffff:127.0.0.1）。
 * 拿字符串比前缀会漏掉 fe9x–febf 这类同段地址，也会让 ::ffff:127.0.0.1 绕开 127/8 ——
 * 拒绝清单被绕一次，网关就成了开放中继。
 */
function v6ToBytes(ip) {
  const parts = String(ip).toLowerCase().split('::')
  if (parts.length > 2) return null
  const expand = str => {
    const list = str ? str.split(':') : []
    const out = []
    for (let i = 0; i < list.length; i++) {
      const g = list[i]
      if (g.includes('.')) {
        const v4 = ipToLong(g)
        if (v4 === null || i !== list.length - 1) return null
        out.push((v4 >>> 16) & 0xffff, v4 & 0xffff)
        continue
      }
      if (!/^[0-9a-f]{1,4}$/.test(g)) return null
      out.push(Number.parseInt(g, 16))
    }
    return out
  }
  const head = expand(parts[0])
  const tail = parts.length === 2 ? expand(parts[1]) : null
  if (head === null || (parts.length === 2 && tail === null)) return null
  if (parts.length === 1) return head.length === 8 ? toBytes(head) : null
  if (head.length + tail.length > 7) return null // '::' 至少代表一组零
  return toBytes([...head, ...Array(8 - head.length - tail.length).fill(0), ...tail])
}

const toBytes = groups => {
  const out = new Uint8Array(16)
  groups.forEach((g, i) => { out[i * 2] = g >>> 8; out[i * 2 + 1] = g & 0xff })
  return out
}

/** true = 这个 IP 不许作为转发目标 */
export function ipDenied(ip) {
  const v = String(ip).toLowerCase()
  if (v.includes(':')) {
    const bytes = v6ToBytes(v)
    if (!bytes) return true // 读不懂的 IPv6 字面量一律拒：宁可拒错也不放行
    const front = bytes.slice(0, 10).every(b => b === 0)
    const v4Tail = front && ((bytes[10] === 0 && bytes[11] === 0) || (bytes[10] === 0xff && bytes[11] === 0xff))
    if (v4Tail && ipDenied(`${bytes[12]}.${bytes[13]}.${bytes[14]}.${bytes[15]}`)) return true
    return DENY_V6.some(({ pfx, bits }) => inPrefix(bytes, pfx, bits))
  }
  return DENY_V4_CIDR.some(cidr => inCidr(v, cidr))
}

const isPort = v => Number.isInteger(v) && v >= 1 && v <= 65535

/** ports: [5432, "8000-8100", "*"] */
function portAllowed(spec, port) {  if (spec === '*') return true
  if (typeof spec === 'number') return spec === port
  const m = /^(\d+)-(\d+)$/.exec(String(spec))
  if (m) return port >= Number(m[1]) && port <= Number(m[2])
  return Number(spec) === port
}

/** 字面 host + 端口段匹配（V1 不做 CIDR，见 spec §2.3 的理由） */
export function matchAllow(allow, host, port) {
  const h = String(host).toLowerCase()
  return allow.some(e => String(e.host).toLowerCase() === h && (e.ports || ['*']).some(p => portAllowed(p, port)))
}

// ─────────────────────────── 会话 ───────────────────────────

const now = () => Date.now()

function log(sess, level, msg) {
  const tag = `${new Date().toISOString().slice(11, 23)} ${sess?.sid || '-'.padEnd(6)} ${level.padEnd(5)}`
  console.log(`${tag} ${msg}`)
}

class Channel {
  constructor(sess, chan) {
    this.sess = sess
    this.chan = chan
    this.sock = null
    this.dead = false
    this.peerFin = false
    this.outFin = false
    /** 各方向独立记水位：重复 pause 会往同一 socket 上叠 drain 监听器 */
    this.slowTarget = false
    this.slowClient = false
    this.onTargetDrain = null
    this.onClientDrain = null
  }

  send(type, obj, raw) {
    this.sess.send(type, this.chan, obj, raw)
  }

  /** 写进本机侧 socket（-L 是本机目标，-R 是本地服务连接）；写不动就压住会话读 */
  writeSock(buf) {
    if (this.dead || !this.sock) return
    if (this.sock.write(buf) || this.slowTarget) return
    this.slowTarget = true
    this.sess.pauseFor(this)
    // 解除必须走 resumeFor：会话读是整条连接共享的，一条通道 drain 不能替别的通道解禁
    this.onTargetDrain = () => {
      this.slowTarget = false
      this.sess.resumeFor(this)
    }
    this.sock.once('drain', this.onTargetDrain)
  }

  /**
   * 本机侧 → 客户端：编成 DATA 帧发出；会话写不动就压住本机侧 socket 读。
   * 方向不能与 writeSock 混用 —— 写回 ch.sock 就是把目标的回包倒回目标自己，
   * 客户端一个字节都收不到（--selftest 的「DATA 回程」用例钉的就是这条）。
   */
  toClient(buf) {
    if (this.dead || !this.sock) return
    if (this.sess.write('data', this.chan, null, buf) || this.slowClient) return
    this.slowClient = true
    this.sock.pause()
    this.onClientDrain = () => {
      this.slowClient = false
      try { this.sock.resume() } catch { /* 通道已关 */ }
    }
    this.sess.sock.once('drain', this.onClientDrain)
  }

  close(rst) {
    if (this.dead) return
    this.dead = true
    // 背压登记要在这里清掉：destroy 之后 'drain' 永不到来，
    // 少了这一步会话读就带着一个再没人解除的 pause 结束 —— 心跳照发，
    // 看上去会话还活着，实际一个字节也不再流动，只能等连续 3 次 PING 无 PONG 才判死。
    if (this.slowTarget) {
      try { this.sock?.off('drain', this.onTargetDrain) } catch { /* 已断开 */ }
      this.slowTarget = false
      this.sess.resumeFor(this)
    }
    if (this.slowClient) {
      try { this.sess.sock.off('drain', this.onClientDrain) } catch { /* 已断开 */ }
      this.slowClient = false
    }
    try { this.sock?.destroy() } catch { /* 已断开 */ }
    this.sess.channels.delete(this.chan)
    if (!this.outFin) {
      this.outFin = true
      this.send(rst ? 'rst' : 'fin')
    }
  }
}

class Session {
  constructor(sock, cfg, remoteAddr) {
    this.sock = sock
    this.cfg = cfg
    this.remote = remoteAddr
    this.sid = crypto.randomBytes(3).toString('hex')
    this.user = null
    this.role = null
    this.channels = new Map()
    this.nextChan = 2          // 网关侧偶数号段
    this.listeners = new Map() // bindHost:bindPort → net.Server
    this.bound = 0
    this.dead = false
    this.startedAt = now()
    this.missedPong = 0
    this.decoder = createDecoder('even', c => this.channels.has(c))
    this.slowChannels = new Set() // 正压着会话读的通道
    this.pingTimer = null
    sock.on('data', chunk => this.onBytes(chunk))
    sock.on('error', err => this.kill(`socket 错误: ${err.message}`))
    sock.on('close', () => this.kill('客户端已断开'))
  }

  /** 写一帧并回报 socket 的反馈（false = 缓冲已满，调用方据此压住来源） */
  write(type, chan = GLOBAL_CHAN, obj, raw) {
    if (this.dead) return true
    try {
      return this.sock.write(encodeFrame(type, chan, obj, raw))
    } catch (err) {
      this.kill(`写帧失败: ${err.message}`)
      return true
    }
  }

  send(type, chan = GLOBAL_CHAN, obj, raw) {
    this.write(type, chan, obj, raw)
  }

  /**
   * 会话读是共享资源，pause/resume 必须引用计数。
   * 每条压住会话读的通道各自等自己本机侧 socket 的 'drain'；
   * 谁先 drain 就解除整条会话的暂停，会把还在排队的另一条通道的 write 缓冲放成无上限增长。
   */
  pauseFor(ch) {
    this.slowChannels.add(ch)
    this.sock.pause()
  }

  resumeFor(ch) {
    this.slowChannels.delete(ch)
    if (this.slowChannels.size === 0 && !this.dead) this.sock.resume()
  }

  onBytes(chunk) {
    let frames
    try {
      frames = this.decoder.push(chunk)
    } catch (err) {
      // 帧已失同步：继续解只会把别人的数据当帧，整条会话作废（§1.1 坑 1）
      log(this, 'FATAL', err.message)
      this.kill(err.message)
      return
    }
    // onFrame 必须逐帧包起来：解码器交出的是合法 JSON，字段形状却由对端说了算。
    // 帧失同步（decoder.push 抛错）才整条会话作废；形状错只作废这一帧的发送方 ——
    // 但绝不能吃异常：异常从 net/tls 的 'data' 监听器里冒出去就是 uncaughtException，
    // 一台网关上所有租户的会话一起没。未认证即可达的 hello/auth 正是这个形状。
    for (const f of frames) {
      try {
        this.onFrame(f)
      } catch (err) {
        log(this, 'FATAL', `帧处理异常 ${f.type}: ${err.message}`)
        this.kill(`帧处理异常: ${err.message}`)
        return
      }
    }
  }

  onFrame(f) {
    // 认证之前只放行握手/心跳/下线。少这一道，未登录的对端就能让我在任意
    // bindHost:bindPort 上 listen（OPEN 只是侥幸被 role=null→allow:[] 兜住），
    // 网关当场变成开放中继。
    if (!this.user && !PRE_AUTH.has(f.type)) return this.kill(`未认证即发 ${f.type}，会话作废`)
    switch (f.type) {
      case 'hello': {
        // payload 由对端给，`null` 是合法 JSON（decodePayload 只在空字节时抛）
        const h = f.payload || {}
        this.hello = h
        log(this, 'INFO', `HELLO ${h.clientVersion || '?'} dev=${h.deviceName || '?'} 自报=${h.selfReportedUser || '-'}`)
        return
      }
      case 'auth':
        if (!f.payload) return this.kill('auth 帧 payload 为空')
        return this.doAuth(f.payload)
      case 'ping':
        return this.send('pong', GLOBAL_CHAN, { seq: f.payload?.seq })
      case 'pong':
        this.missedPong = 0
        return
      case 'open':
        return this.doOpen(f.chan, f.payload)
      case 'data': {
        const ch = this.channels.get(f.chan)
        // 迟到帧（通道已 FIN/RST）丢弃即可，不是协议错误
        if (!ch || ch.dead || f.payload.length === 0) return
        ch.writeSock(f.payload)
        return
      }
      case 'fin': {
        const ch = this.channels.get(f.chan)
        if (!ch) return
        ch.peerFin = true
        try { ch.sock?.end() } catch { /* 已断开 */ }
        return
      }
      case 'rst': {
        const ch = this.channels.get(f.chan)
        if (ch) ch.close(true)
        return
      }
      case 'bind':
        return this.doBind(f.payload)
      case 'unbind':
        return this.doUnbind(f.payload)
      case 'goaway':
        log(this, 'INFO', `客户端 GOAWAY ${f.payload?.reason || ''}`)
        return this.kill('客户端优雅下线')
      default:
        return
    }
  }

  doAuth(p) {
    if (!this.user) {
      const rec = (this.cfg.users || []).find(u => u.user === p.user)
      const ok = rec && timingSafeHex(sha256hex(p.password || ''), rec.passwordSha256 || '')
      if (!ok) {
        log(this, 'BAD', `AUTH 失败 user=${p.user || '-'}`)
        this.send('openErr', GLOBAL_CHAN, { code: 'malformed-request', message: '用户名或口令不正确' })
        return this.kill('认证失败')
      }
      this.user = rec.user
      this.role = rec.role
    }
    // 重放的 AUTH 不能再起一个心跳：pingTimer 只存最后一个句柄，前一个 setInterval
    // 从此没人 clearInterval，会话 kill 之后照发 PING（写已销毁的 socket）。
    if (!this.pingTimer) this.startHeartbeat()
    const role = (this.cfg.roles || {})[this.role] || { allow: [] }
    this.send('helloOk', GLOBAL_CHAN, {
      sessionId: this.sid,
      heartbeatSec: this.cfg.heartbeatSec || 30,
      maxChannels: this.cfg.maxChannels || 256,
      role: this.role,
      policyVersion: this.cfg.policyVersion || 'unversioned',
    })
    log(this, 'OK', `AUTH 通过 user=${this.user} role=${this.role}`)
    this.startHeartbeat()
  }

  startHeartbeat() {
    const sec = this.cfg.heartbeatSec || 30
    let seq = 0
    this.pingTimer = setInterval(() => {
      if (this.dead) return
      // 心跳兼做 NAT 空闲保活；连续 3 次无 PONG 判死（§1.2）
      if (++this.missedPong > 3) return this.kill('连续 3 次 PING 无 PONG')
      this.send('ping', GLOBAL_CHAN, { seq: ++seq })
    }, sec * 1000)
    this.pingTimer.unref?.()
  }

  async doOpen(chan, p) {
    const role = (this.cfg.roles || {})[this.role] || { allow: [] }
    if (!p || typeof p.host !== 'string' || !Number.isInteger(p.port)) {
      this.send('openErr', chan, { code: 'malformed-request', message: 'OPEN 缺少 host 或 port' })
      return
    }
    if (this.channels.size >= (this.cfg.maxChannels || 256)) {
      this.send('openErr', chan, { code: 'quota', message: `并发通道已达上限 ${this.cfg.maxChannels || 256}` })
      return
    }
    if (!matchAllow(role.allow || [], p.host, p.port)) {
      log(this, 'BAD', `OPEN chan=${chan} ${p.host}:${p.port} → denied-by-policy (role=${this.role})`)
      this.send('openErr', chan, { code: 'denied-by-policy', message: `目标 ${p.host}:${p.port} 不在角色 ${this.role} 的允许集内` })
      return
    }
    // 逐 IP 校验：否则允许集里的域名可以被一次 DNS 变更指到元数据地址（§2.3）
    const ch = new Channel(this, chan)
    // 占位必须在这次 await 之前：否则同 chan 的第二个 OPEN 会在解析窗口里通过撞号检查
    // （解码器的 hasChannel 此刻还说没有），覆盖表项后两条目标连接共用一个通道号 —— 数据串台，
    // 而 maxChannels 也数不准。失败路径各自摘掉。
    this.channels.set(chan, ch)
    let addrs
    try {
      addrs = await dns.lookup(p.host, { all: true, verbatim: true })
    } catch (err) {
      this.channels.delete(chan)
      this.send('openErr', chan, { code: 'target-unreachable', message: `${p.host} 解析失败：${err.code || err.message}` })
      return
    }
    const literal = ipToLong(p.host) !== null
    const bad = addrs.find(a => ipDenied(a.address) || (literal && a.address !== p.host))
    if (bad) {
      this.channels.delete(chan)
      log(this, 'BAD', `OPEN chan=${chan} ${p.host}:${p.port} → denied-by-dns (${bad.address})`)
      this.send('openErr', chan, { code: 'denied-by-dns', message: `${p.host} 解析出 ${bad.address}，不在允许范围` })
      return
    }
    // 连**校验过的那个 IP**，不重新解析：否则校验与连接之间还能被重绑一次
    // await 期间会话可能已经 kill（客户端断开 / 帧错误）：kill 里 close 通道时
    // ch.sock 还是 null，什么都关不到，所以必须在这里补一次存活检查再连，
    // 否则留下一条通往内网目标的孤儿连接，且没有任何句柄能把它关掉。
    if (this.dead) return
    const sock = net.connect({ host: addrs[0].address, port: p.port })
    ch.sock = sock
    sock.setNoDelay(true)
    sock.on('connect', () => this.send('openOk', chan))
    sock.on('data', buf => ch.toClient(buf))
    sock.on('end', () => {
      if (!ch.outFin) { ch.outFin = true; ch.send('fin', chan) }
    })
    sock.on('error', err => {
      if (!ch.outFin && !ch.peerFin) this.send('openErr', chan, { code: 'target-unreachable', message: `连不上 ${p.host}:${p.port}（${err.code || err.message}）` })
      ch.close(true)
    })
    sock.on('close', () => ch.close(false))
  }

  doBind(p) {
    // BIND 的入参来自对端，边界上就验掉：listen 到 undefined:NaN 会让进程在 net 里抛
    if (!p || typeof p.bindHost !== 'string' || !isPort(p.bindPort) || !isPort(p.targetPort)) {
      this.send('openErr', GLOBAL_CHAN, { code: 'malformed-request', message: 'BIND 需要 bindHost、bindPort 与 targetPort' })
      return
    }
    const key = `${p.bindHost.toLowerCase()}:${p.bindPort}`
    if (this.listeners.has(key)) {
      this.send('openErr', GLOBAL_CHAN, { code: 'malformed-request', message: `绑定 ${key} 已存在` })
      return
    }
    const server = net.createServer(sock => {
      const chan = this.allocChan()
      if (chan === null) { sock.destroy(); return }
      const ch = new Channel(this, chan)
      this.channels.set(chan, ch)
      ch.sock = sock
      sock.setNoDelay(true)
      // 方向由 chan 奇偶隐含：这是网关发起的 open（偶数号段）。
      // 回带 bind 身份：一条会话上挂着多条 -R 规则，客户端要按它派发而不是广播给每条规则
      this.send('open', chan, {
        host: p.targetHost || 'localhost',
        port: p.targetPort,
        bindHost: p.bindHost,
        bindPort: p.bindPort,
      })
      sock.on('data', buf => ch.toClient(buf))
      sock.on('end', () => { if (!ch.outFin) { ch.outFin = true; ch.send('fin', chan) } })
      sock.on('error', () => ch.close(true))
      sock.on('close', () => ch.close(false))
    })
    server.on('error', err => {
      this.listeners.delete(key)
      this.send('openErr', GLOBAL_CHAN, { code: 'target-unreachable', message: `绑定 ${key} 失败：${err.code || err.message}` })
    })
    server.listen(p.bindPort, p.bindHost, () => {
      this.listeners.set(key, server)
      this.send('bindOk', GLOBAL_CHAN)
      log(this, 'OK', `BIND ${key}`)
    })
  }

  doUnbind(p) {
    const b = p || {}
    const key = `${String(b.bindHost).toLowerCase()}:${b.bindPort}`
    const server = this.listeners.get(key)
    if (server) {
      try { server.close() } catch { /* 已关 */ }
      this.listeners.delete(key)
      log(this, 'OK', `UNBIND ${key}`)
    } else {
      // 认不出来的 UNBIND 要说出来：静默 bindOk 会让客户端以为规则已摘，
      // 而网关上的 listener 还占着那个端口，以后重加同端口只会以「已存在」失败
      log(this, 'BAD', `UNBIND ${key} 无对应绑定`)
    }
    this.send('bindOk', GLOBAL_CHAN)
  }

  allocChan() {
    for (let i = 0; i < 0x10000; i++) {
      const c = this.nextChan
      this.nextChan = this.nextChan + 2 > 0xfffffffe ? 2 : this.nextChan + 2
      if (!this.channels.has(c)) return c
    }
    return null
  }

  kill(reason) {
    if (this.dead) return
    this.dead = true
    clearInterval(this.pingTimer)
    for (const ch of [...this.channels.values()]) ch.close(true)
    for (const s of this.listeners.values()) { try { s.close() } catch { /* 已关 */ } }
    this.listeners.clear()
    try { this.sock.destroy() } catch { /* 已断开 */ }
    log(this, 'END', `${reason} · 存活 ${Math.round((now() - this.startedAt) / 1000)}s`)
  }
}

const sha256hex = (s) => crypto.createHash('sha256').update(s, 'utf8').digest('hex')

function timingSafeHex(a, b) {
  const x = Buffer.from(String(a), 'hex')
  const y = Buffer.from(String(b), 'hex')
  return x.length === y.length && x.length > 0 && crypto.timingSafeEqual(x, y)
}

// ─────────────────────────── 入口 ───────────────────────────

function start(cfgPath) {
  const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf8'))
  for (const u of cfg.users || []) {
    if ('password' in u) throw new Error('users[].password 不接受明文，请改用 --hash 生成 passwordSha256')
    if (!u.user || !u.passwordSha256 || !u.role) throw new Error('users[] 需要 user / passwordSha256 / role 三个字段')
  }
  // 策略写错要在启动时炸，不要等第一条 OPEN 在 async 路径里抛 TypeError 打死进程：
  // ports 写成裸数字（漏了数组）就是这种写法，matchAllow 里的 .some 当场没得调
  for (const [name, role] of Object.entries(cfg.roles || {})) {
    if (!Array.isArray(role.allow)) throw new Error(`roles.${name}.allow 必须是数组`)
    for (const e of role.allow) {
      if (!e || typeof e.host !== 'string' || !e.host.trim()) throw new Error(`roles.${name}.allow[] 需要 host 字符串`)
      if (e.ports !== undefined && !Array.isArray(e.ports)) throw new Error(`roles.${name}.allow[] 的 ports 必须是数组，如 [5432] 或 ["8000-8100"]`)
      for (const p of e.ports || []) {
        if (p !== '*' && !isPort(p) && !/^(\d+(-\d+)?)$/.test(String(p))) {
          throw new Error(`roles.${name}.allow[] 的端口只能是数字、"*" 或 "a-b"，收到 ${JSON.stringify(p)}`)
        }
      }
    }
  }
  const server = tls.createServer(
    { cert: fs.readFileSync(cfg.tls.cert), key: fs.readFileSync(cfg.tls.key), minVersion: 'TLSv1.2' },
    sock => {
      const sess = new Session(sock, cfg, `${sock.remoteAddress}:${sock.remotePort}`)
      log(sess, 'INFO', `TLS 建立 ${sess.remote} · ${sock.getProtocol()} ${sock.getCipher()?.name || '?'}`)
    },
  )
  server.on('tlsClientError', err => console.log(`tlsClientError ${err.code || err.message}`))
  server.listen(cfg.listen?.port || 4433, cfg.listen?.host || '0.0.0.0', () => {
    console.log(`hillgw 监听 ${cfg.listen?.host || '0.0.0.0'}:${cfg.listen?.port || 4433} · 用户 ${(cfg.users || []).length} 个 · 角色 ${Object.keys(cfg.roles || {}).join(',')}`)
  })
  const bye = (sig) => {
    console.log(`${sig}：停止接受新连接`)
    server.close(() => process.exit(0))
  }
  process.on('SIGINT', () => bye('SIGINT'))
  process.on('SIGTERM', () => bye('SIGTERM'))
}

/** 自比对：帧字节与策略判定必须和 TS 侧一字不差 */
async function selftest() {
  const cases = [
    ['DATA chan=17', encodeFrame('data', 17, null, textEnc.encode('abc')), '0107000000110003616263'],
    ['FIN chan=12', encodeFrame('fin', 12), '01080000000c0000'],
    ['OPEN chan=19', encodeFrame('open', 19, { host: 'db.internal', port: 5432 }),
      '01040000001300227b22686f7374223a2264622e696e7465726e616c222c22706f7274223a353433327d'],
    ['BIND chan=0', encodeFrame('bind', 0, { bindHost: '127.0.0.1', bindPort: 8443, targetHost: 'localhost', targetPort: 6379 }),
      '010d0000000000537b2262696e64486f7374223a223132372e302e302e31222c2262696e64506f7274223a383434332c22746172676574486f7374223a226c6f63616c686f7374222c22746172676574506f7274223a363337397d'],
    ['BINDOK chan=0', encodeFrame('bindOk', 0), '010f000000000000'],
    ['PING 归零 chan', encodeFrame('ping', GLOBAL_CHAN, { seq: 7 }), '010a0000000000097b22736571223a377d'],
    // 传进来的 chan 非零也必须归零 —— 两侧 encodeFrame 的这条归一只有这一处锚点
    ['PING 外部 chan 被归零', encodeFrame('ping', 17, { seq: 7 }), '010a0000000000097b22736571223a377d'],
  ]
  let fail = 0
  for (const [name, got, want] of cases) {
    const hex = Buffer.from(got).toString('hex')
    const ok = hex === want
    if (!ok) fail++
    console.log(`${ok ? 'ok  ' : 'FAIL'} ${name}  ${hex}${ok ? '' : `  ≠ ${want}`}`)
  }
  // 半包：一次喂 5 字节不该出帧，补齐才出
  const full = encodeFrame('data', 5, null, textEnc.encode('hello'))
  const d = createDecoder('even', () => false)
  const a = d.push(full.subarray(0, 5))
  const b = d.push(full.subarray(5))
  console.log(`${a.length === 0 && b.length === 1 ? 'ok  ' : 'FAIL'} 半包分片`)
  // 奇偶劫持：网关侧收到偶数 OPEN 必须致命
  let fatal = false
  try { createDecoder('even', () => false).push(encodeFrame('open', 12, { host: 'a', port: 1 })) } catch (e) { fatal = e instanceof MuxProtocolError }
  console.log(`${fatal ? 'ok  ' : 'FAIL'} 偶数 OPEN 到达网关判为劫持`)
  const policy = [
    [matchAllow([{ host: 'db.internal', ports: [5432] }], 'DB.INTERNAL', 5432), true, 'host 大小写不敏感'],
    [matchAllow([{ host: 'a', ports: ['8000-8100'] }], 'a', 8101), false, '端口段右开外'],
    [matchAllow([{ host: 'a', ports: ['*'] }], 'a', 1), true, '端口通配'],
    [ipDenied('169.254.169.254'), true, '云元数据'],
    [ipDenied('127.0.0.1'), true, '回环'],
    [ipDenied('::1'), true, 'IPv6 回环'],
    [ipDenied('0:0:0:0:0:0:0:1'), true, 'IPv6 回环未压缩写法'],
    [ipDenied('::ffff:127.0.0.1'), true, 'IPv4-mapped 绕回环'],
    [ipDenied('fe80::1'), true, '链路本地'],
    [ipDenied('febf::1'), true, '链路本地 /10 段尾'],
    [ipDenied('fec0::1'), false, '站点本地不在清单内'],
    [ipDenied('fd12::1'), true, 'ULA fc00::/7 段内'],
    [ipDenied('2001:db8::1'), false, '可路由 v6'],
    [ipDenied('10.0.0.9'), false, '内网正常地址'],
    [ipDenied('fe80:zz::1'), true, '读不懂的 v6 字面量按拒处理'],
  ]
  for (const [got, want, name] of policy) {
    console.log(`${got === want ? 'ok  ' : 'FAIL'} 策略 ${name}`)
    if (got !== want) fail++
  }
  fail += await selftestLoopback()
  fail += await selftestUnauth()
  console.log(fail ? `\n${fail} 项不通过` : '\n全部通过')
  process.exit(fail ? 1 : 0)
}

/**
 * 对端模拟器：一条 socket 上收帧/发帧。parity 传本端号段（客户端面用 'odd'，
 * 于是到达本端的 OPEN 必须是网关分配的偶数号）。
 */
function makePeer(sock, parity) {
  const dec = createDecoder(parity, () => false)
  const frames = []
  sock.on('data', chunk => frames.push(...dec.push(chunk)))
  return {
    send: (type, chan, obj, raw) => sock.write(encodeFrame(type, chan, obj, raw)),
    /** 轮询等一个满足条件的帧；超时返回 null —— 自比对要的是「红」，不是挂死 */
    until: async (pred, ms = 1500) => {
      const t0 = Date.now()
      while (Date.now() - t0 < ms) {
        const hit = frames.find(pred)
        if (hit) return hit
        await new Promise(r => setTimeout(r, 10))
      }
      return null
    },
  }
}

const say = (ok, name) => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${name}`)
  return ok ? 0 : 1
}

/** 拿一个刚释放的端口：selftest 里 bind 不能撞别人的监听口 */
async function freePort() {
  return new Promise(resolve => {
    const probe = net.createServer()
    probe.listen(0, '127.0.0.1', () => {
      const port = probe.address().port
      probe.close(() => resolve(port))
    })
  })
}

/**
 * -R 全链路字节往返：AUTH → BIND → 服务连进来 → 两个方向都要到得了对端。
 * 这条是「网关把目标回包写回目标自己」的回归锚点 —— 只用假对端的单测抓不到它，
 * 而 loopback 目标按 §2.3 一律被 doOpen 拒掉，所以走 bind 这条路测真接线。
 */
async function selftestLoopback() {
  const cfg = {
    users: [{ user: 't', passwordSha256: sha256hex('pw'), role: 'r' }],
    roles: { r: { allow: [] } },
    heartbeatSec: 3600,
  }
  const bindPort = await freePort()
  let sess = null
  const gwSide = net.createServer(sock => { sess = new Session(sock, cfg, 'selftest') })
  await new Promise(r => gwSide.listen(0, '127.0.0.1', r))
  const cli = net.connect(gwSide.address().port, '127.0.0.1')
  await new Promise(r => cli.once('connect', r))
  while (!sess) await new Promise(r => setTimeout(r, 10))
  const peer = makePeer(cli, 'odd')
  let fail = 0
  try {
    peer.send('auth', GLOBAL_CHAN, { user: 't', password: 'pw' })
    fail += say(!!(await peer.until(f => f.type === 'helloOk')), '端到端 AUTH 换到 HELLO_OK')
    peer.send('bind', GLOBAL_CHAN, { bindHost: '127.0.0.1', bindPort, targetHost: 'svc.local', targetPort: 6379 })
    fail += say(!!(await peer.until(f => f.type === 'bindOk')), `端到端 BIND 127.0.0.1:${bindPort}`)

    const svc = net.connect(bindPort, '127.0.0.1')
    await new Promise(r => svc.once('connect', r))
    const opened = await peer.until(f => f.type === 'open')
    // 方向由 chan 奇偶隐含：网关发起的 open 必须带偶数号，并回带 BIND 声明的回连目标
    fail += say(
      !!opened && opened.chan % 2 === 0 && opened.payload.host === 'svc.local' && opened.payload.port === 6379,
      '端到端 网关发起 OPEN（偶数 chan + 回连目标原样带回）',
    )
    if (!opened) return fail

    let svcSeen = ''
    svc.on('data', b => { svcSeen += b.toString() })
    peer.send('data', opened.chan, null, textEnc.encode('up'))
    const t0 = Date.now()
    while (!svcSeen.includes('up') && Date.now() - t0 < 1500) await new Promise(r => setTimeout(r, 10))
    fail += say(svcSeen.includes('up'), '端到端 客户端→本机服务 的 DATA 落地')

    svc.write('down')
    const down = await peer.until(f => f.type === 'data' && f.chan === opened.chan)
    fail += say(!!down && textDec.decode(down.payload) === 'down', '端到端 本机服务→客户端 的 DATA 回程')
    svc.destroy()
  } finally {
    sess.kill('selftest 收尾')
    cli.destroy()
    gwSide.close()
  }
  return fail
}

/** 未认证就能让网关在任意端口 listen，是把网关变成开放中继 —— 必须硬停会话 */
async function selftestUnauth() {
  const cfg = {
    users: [{ user: 't', passwordSha256: sha256hex('pw'), role: 'r' }],
    roles: { r: { allow: [] } },
    heartbeatSec: 3600,
  }
  const bindPort = await freePort()
  let sess = null
  const gwSide = net.createServer(sock => { sess = new Session(sock, cfg, 'selftest') })
  await new Promise(r => gwSide.listen(0, '127.0.0.1', r))
  const cli = net.connect(gwSide.address().port, '127.0.0.1')
  await new Promise(r => cli.once('connect', r))
  while (!sess) await new Promise(r => setTimeout(r, 10))
  const peer = makePeer(cli, 'odd')
  let closed = false
  cli.on('close', () => { closed = true })
  try {
    peer.send('bind', GLOBAL_CHAN, { bindHost: '127.0.0.1', bindPort, targetHost: 'x', targetPort: 1 })
    const t0 = Date.now()
    while (!closed && Date.now() - t0 < 1500) await new Promise(r => setTimeout(r, 10))
    // 会话被打死之外，监听口一个都没起来才算数
    return say(closed && sess.listeners.size === 0, '未认证即 BIND 被硬停且没建监听')
  } finally {
    cli.destroy()
    gwSide.close()
  }
}

const argv = process.argv.slice(2)
if (argv.includes('--selftest')) selftest().catch(err => { console.error(err); process.exit(1) })
else if (argv.includes('--hash')) {
  const i = argv.indexOf('--hash')
  const pw = argv[i + 1]
  if (!pw) { console.error('用法：node hillgw.mjs --hash <口令>'); process.exit(2) }
  console.log(sha256hex(pw))
} else if (argv.includes('--config')) {
  start(argv[argv.indexOf('--config') + 1])
} else {
  console.log('用法：node hillgw.mjs --config gw.json | --hash <口令> | --selftest')
  process.exit(2)
}
