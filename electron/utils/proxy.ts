/**
 * 出站代理探测与隧道（探测链路一致性）。
 *
 * 为什么需要：镜像/DoH 探测走 Node 的 https.get，它**不读系统代理**；而用户
 * 实际 clone 走 git（读 http.proxy / 环境变量），浏览器走系统代理。开着代理
 * 的机器上，直连探测的结论和真实体验是两回事（常见表现：探测说镜像全死、
 * 实际 clone 正常，或反过来）。
 *
 * 这里只做两件事：
 * 1. 探测「当前机器有几个代理入口」（环境变量 → git 配置 → Windows 系统代理）；
 * 2. HTTP(S) 代理走 CONNECT 隧道（https.Agent 子类），让探测走和 git 同一条路。
 * SOCKS 不做隧道（要额外实现握手），只上报给 UI 提示"探测未走代理"。
 */
import net from 'net'
import tls from 'tls'
import https from 'https'
import { execFile } from 'child_process'

export type ProxyProtocol = 'http' | 'socks' | 'unknown'

export interface ProxySpec {
  /** 原始值（UI 展示用，可能含账号但已脱敏处理由调用方负责） */
  raw: string
  protocol: ProxyProtocol
  host: string
  port: number
  /** `user:pass`，已 decode，仅用于 CONNECT 的 Proxy-Authorization */
  auth?: string
}

/** 解析代理字符串；支持 host:port、http(s)://host:port、socks5://host:port */
export function parseProxySpec(raw: string | null | undefined): ProxySpec | null {
  const s = (raw ?? '').trim()
  if (!s) return null
  const withScheme = /^[a-z0-9+.-]+:\/\//i.test(s) ? s : `http://${s}`
  let u: URL
  try {
    u = new URL(withScheme)
  } catch {
    return null
  }
  const proto = u.protocol.replace(':', '').toLowerCase()
  const protocol: ProxyProtocol = proto === 'http' || proto === 'https'
    ? 'http'
    : proto.startsWith('socks') ? 'socks' : 'unknown'
  // 不认识的协议（ftp 等）既建不了隧道也不该在 UI 里冒充“探测已走代理”
  if (protocol === 'unknown') return null
  const port = Number(u.port || (protocol === 'http' ? 80 : 0))
  if (!u.hostname || !Number.isFinite(port) || port <= 0) return null
  const auth = u.username
    ? `${decodeURIComponent(u.username)}:${decodeURIComponent(u.password)}`
    : undefined
  return { raw: s, protocol, host: u.hostname, port, auth }
}

/** 解析 CONNECT 应答首行（`HTTP/1.1 200 Connection established`） */
export function parseConnectResponse(head: string): { ok: boolean; status: number } | null {
  const m = /^HTTP\/1\.[01]\s+(\d{3})/i.exec((head || '').trim())
  if (!m) return null
  const status = Number(m[1])
  return { ok: status >= 200 && status < 300, status }
}

/**
 * Windows 系统代理（WinINET）的 ProxyServer 值可能是
 * `host:port` 或 `http=h:p;https=h:p;ftp=...`，取 https 优先。
 */
export function pickProxyServerValue(value: string | null | undefined): string | null {
  const v = (value ?? '').trim()
  if (!v) return null
  if (!v.includes('=')) return v
  const map = new Map<string, string>()
  for (const part of v.split(';')) {
    const i = part.indexOf('=')
    if (i === -1) continue
    map.set(part.slice(0, i).trim().toLowerCase(), part.slice(i + 1).trim())
  }
  return map.get('https') ?? map.get('http') ?? null
}

/** 单个 CONNECT 隧道的空闲超时 */
const TUNNEL_IDLE_TIMEOUT_MS = 12000

const agentCache = new Map<string, https.Agent>()

/**
 * 给 https.get/request 用的 CONNECT 隧道 Agent。
 * 返回的 socket 是「隧道之上再握手」的 TLS socket，证书按目标 host 的 SNI 校验。
 */
export function tunnelAgentFor(spec: ProxySpec): https.Agent {
  const key = `${spec.host}:${spec.port}:${spec.auth ?? ''}`
  const hit = agentCache.get(key)
  if (hit) return hit

  class TunnelAgent extends https.Agent {
    constructor() {
      super({ keepAlive: false })
    }

    // https.Agent 期望这里返回 TLS socket；先 CONNECT 再在隧道上握手
    createConnection(options: any, cb: any): any {
      const targetHost = String(options.host ?? '')
      const targetPort = Number(options.port) || 443
      const sock = net.connect(spec.port, spec.host)
      let done = false
      const fail = (e: Error) => {
        if (done) return
        done = true
        sock.destroy()
        cb(e)
      }
      sock.setTimeout(TUNNEL_IDLE_TIMEOUT_MS, () => fail(new Error('代理 CONNECT 超时')))
      sock.once('error', fail)
      sock.once('connect', () => {
        const authHeader = spec.auth
          ? `Proxy-Authorization: Basic ${Buffer.from(spec.auth).toString('base64')}\r\n`
          : ''
        sock.write(
          `CONNECT ${targetHost}:${targetPort} HTTP/1.1\r\n`
          + `Host: ${targetHost}:${targetPort}\r\n`
          + authHeader
          + 'Proxy-Connection: keep-alive\r\n\r\n',
        )
        let buf = ''
        const onData = (chunk: Buffer) => {
          buf += chunk.toString('latin1')
          const idx = buf.indexOf('\r\n\r\n')
          if (idx === -1) {
            if (buf.length > 8192) fail(new Error('代理应答过长'))
            return
          }
          sock.removeListener('data', onData)
          const parsed = parseConnectResponse(buf.slice(0, idx))
          if (!parsed || !parsed.ok) {
            fail(new Error(`代理 CONNECT 失败（HTTP ${parsed?.status ?? '?'}）`))
            return
          }
          // 应答头之后可能已经带上 TLS 首包，必须放回流里
          const leftover = Buffer.from(buf.slice(idx + 4), 'latin1')
          if (leftover.length) sock.unshift(leftover)
          sock.setTimeout(0)
          done = true
          const secure = tls.connect({ socket: sock, servername: targetHost })
          cb(null, secure)
        }
        sock.on('data', onData)
      })
    }
  }

  const agent = new TunnelAgent()
  agentCache.set(key, agent)
  return agent
}

export interface ProxyDetection {
  spec: ProxySpec
  source: 'env' | 'git' | 'system'
}

function run(cmd: string, args: string[]): Promise<string> {
  return new Promise((resolvePromise) => {
    execFile(cmd, args, { timeout: 3000, windowsHide: true }, (err, stdout) => {
      resolvePromise(err ? '' : String(stdout ?? ''))
    })
  })
}

/** 读取 Windows 系统代理（ProxyEnable=1 才算） */
async function readWinInetProxy(): Promise<string | null> {
  if (process.platform !== 'win32') return null
  const key = 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings'
  const enabled = await run('reg', ['query', key, '/v', 'ProxyEnable'])
  if (!/ProxyEnable\s+REG_DWORD\s+0x1\b/i.test(enabled)) return null
  const server = await run('reg', ['query', key, '/v', 'ProxyServer'])
  const m = /ProxyServer\s+REG_SZ\s+(.+)/i.exec(server)
  return pickProxyServerValue(m?.[1] ?? null)
}

/**
 * 探测当前机器的出站代理。优先级与 git 大体一致：环境变量 → git 全局配置 →
 * 系统代理；全部失败返回 null（= 直连）。任何一步失败都静默降级。
 */
export async function detectSystemProxy(gitExe: string | null): Promise<ProxyDetection | null> {
  const envRaw = process.env.HTTPS_PROXY || process.env.https_proxy
    || process.env.HTTP_PROXY || process.env.http_proxy || process.env.ALL_PROXY || process.env.all_proxy
  const fromEnv = parseProxySpec(envRaw)
  if (fromEnv) return { spec: fromEnv, source: 'env' }

  const exe = gitExe || 'git'
  for (const key of ['https.proxy', 'http.proxy']) {
    const out = (await run(exe, ['config', '--global', '--get', key])).trim()
    const spec = parseProxySpec(out)
    if (spec) return { spec, source: 'git' }
  }

  const winInet = parseProxySpec(await readWinInetProxy())
  if (winInet) return { spec: winInet, source: 'system' }
  return null
}

/** 脱敏展示（`user:pass@host:port` → `***@host:port`） */
export function maskProxy(spec: ProxySpec): string {
  return spec.auth ? `${spec.host}:${spec.port}（含认证）` : `${spec.host}:${spec.port}`
}
