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

class MuxProtocolError extends Error {}

const textEnc = new TextEncoder()
const textDec = new TextDecoder()

function encodeFrame(type, chan, payloadObj, raw) {
  const code = FT[type]
  if (code === undefined) throw new Error(`未知帧类型：${type}`)
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

function decodePayload(type, bytes) {
  if (type === 'data') return bytes
  if (type === 'openOk' || type === 'fin' || type === 'rst' || type === 'bindOk') return null
  if (bytes.length === 0) throw new MuxProtocolError(`${type} 帧缺少 payload`)
  try {
    return JSON.parse(textDec.decode(bytes))
  } catch {
    throw new MuxProtocolError(`${type} 帧 payload 不是合法 JSON`)
  }
}

// ─────────────────────────── 授权（ZTNA 的正文） ───────────────────────────

/** 网关自己不能变成开放中继：这些目的地一律拒绝（§2.3） */
const DENY_V4_CIDR = [
  ['169.254.0.0', 16], // 云元数据
  ['127.0.0.0', 8],    // 网关本机回环
  ['0.0.0.0', 8],
]
const DENY_V6_PREFIX = ['::1', 'fe80', 'fc00', 'fd00']

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

/** true = 这个 IP 不许作为转发目标 */
export function ipDenied(ip) {
  const v = String(ip).toLowerCase()
  if (v.includes(':')) {
    const compact = v.replace(/:/g, '')
    if (compact === '1') return true // ::1
    return DENY_V6_PREFIX.some(p => v.startsWith(p.endsWith(':') ? p : `${p}:`) || v === p)
  }
  return DENY_V4_CIDR.some(cidr => inCidr(v, cidr))
}

/** ports: [5432, "8000-8100", "*"] */
function portAllowed(spec, port) {
  if (spec === '*') return true
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
  }

  send(type, obj, raw) {
    this.sess.send(type, this.chan, obj, raw)
  }

  /** 网关 → 客户端方向写数据；写不动就暂停源读取（背压，不做窗口流控） */
  writeOut(buf) {
    if (this.dead || !this.sock) return
    if (!this.sock.write(buf)) {
      this.sock.pause?.()
      this.sock.once('drain', () => this.sock.resume?.())
    }
  }

  close(rst) {
    if (this.dead) return
    this.dead = true
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
    this.pingTimer = null
    sock.on('data', chunk => this.onBytes(chunk))
    sock.on('error', err => this.kill(`socket 错误: ${err.message}`))
    sock.on('close', () => this.kill('客户端已断开'))
  }

  send(type, chan = GLOBAL_CHAN, obj, raw) {
    if (this.dead) return
    try {
      this.sock.write(encodeFrame(type, chan, obj, raw))
    } catch (err) {
      this.kill(`写帧失败: ${err.message}`)
    }
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
    for (const f of frames) this.onFrame(f)
  }

  onFrame(f) {
    switch (f.type) {
      case 'hello':
        this.hello = f.payload
        log(this, 'INFO', `HELLO ${f.payload.clientVersion || '?'} dev=${f.payload.deviceName || '?'} 自报=${f.payload.selfReportedUser || '-'}`)
        return
      case 'auth':
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
        ch.writeOut(f.payload)
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
    let addrs
    try {
      addrs = await dns.lookup(p.host, { all: true, verbatim: true })
    } catch (err) {
      this.send('openErr', chan, { code: 'target-unreachable', message: `${p.host} 解析失败：${err.code || err.message}` })
      return
    }
    const literal = ipToLong(p.host) !== null
    const bad = addrs.find(a => ipDenied(a.address) || (literal && a.address !== p.host))
    if (bad) {
      log(this, 'BAD', `OPEN chan=${chan} ${p.host}:${p.port} → denied-by-dns (${bad.address})`)
      this.send('openErr', chan, { code: 'denied-by-dns', message: `${p.host} 解析出 ${bad.address}，不在允许范围` })
      return
    }
    const ch = new Channel(this, chan)
    this.channels.set(chan, ch)
    // 连**校验过的那个 IP**，不重新解析：否则校验与连接之间还能被重绑一次
    const sock = net.connect({ host: addrs[0].address, port: p.port })
    ch.sock = sock
    sock.setNoDelay(true)
    sock.on('connect', () => this.send('openOk', chan))
    sock.on('data', buf => ch.writeOut(buf))
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
    const key = `${String(p.bindHost).toLowerCase()}:${p.bindPort}`
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
      // 方向由 chan 奇偶隐含：这是网关发起的 open（偶数号段）
      this.send('open', chan, { host: p.targetHost || 'localhost', port: p.targetPort || p.bindPort })
      sock.on('data', buf => ch.writeOut(buf))
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
    const key = `${String(p.bindHost).toLowerCase()}:${p.bindPort}`
    const server = this.listeners.get(key)
    if (server) {
      try { server.close() } catch { /* 已关 */ }
      this.listeners.delete(key)
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
function selftest() {
  const cases = [
    ['DATA chan=17', encodeFrame('data', 17, null, textEnc.encode('abc')), '0107000000110003616263'],
    ['FIN chan=12', encodeFrame('fin', 12), '01080000000c0000'],
    ['OPEN chan=19', encodeFrame('open', 19, { host: 'db.internal', port: 5432 }),
      '01040000001300227b22686f7374223a2264622e696e7465726e616c222c22706f7274223a353433327d'],
    ['BIND chan=0', encodeFrame('bind', 0, { bindHost: '127.0.0.1', bindPort: 8443 }),
      '010d0000000000287b2262696e64486f7374223a223132372e302e302e31222c2262696e64506f7274223a383434337d'],
    ['BINDOK chan=0', encodeFrame('bindOk', 0), '010f000000000000'],
    ['PING 归零 chan', encodeFrame('ping', GLOBAL_CHAN, { seq: 7 }), '010a0000000000097b22736571223a377d'],
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
    [ipDenied('fe80::1'), true, '链路本地'],
    [ipDenied('10.0.0.9'), false, '内网正常地址'],
  ]
  for (const [got, want, name] of policy) {
    console.log(`${got === want ? 'ok  ' : 'FAIL'} 策略 ${name}`)
    if (got !== want) fail++
  }
  console.log(fail ? `\n${fail} 项不通过` : '\n全部通过')
  process.exit(fail ? 1 : 0)
}

const argv = process.argv.slice(2)
if (argv.includes('--selftest')) selftest()
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
