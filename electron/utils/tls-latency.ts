/**
 * TLS 握手探测（hosts 优选的第二道筛）。
 *
 * 只做 TCP connect 的测速会放进「连得上但证书不对」的 IP：链路被劫持、镜像 IP
 * 指向了别的服务、或者干脆是个只回 RST/SYN-ACK 的蜜罐。这里按真实域名的 SNI
 * 做一次完整握手并校验证书，握手成功才认为这个 IP 真的在服务 github.com。
 */
import tls from 'tls'

export interface TlsProbeResult {
  ok: boolean
  latencyMs: number | null
  error?: string
}

/** 握手超时：比 TCP 测速宽一点（证书链多一次往返） */
export const TLS_PROBE_TIMEOUT_MS = 5000

export function tlsProbe(host: string, servername: string, timeoutMs = TLS_PROBE_TIMEOUT_MS): Promise<TlsProbeResult> {
  return new Promise((resolvePromise) => {
    const started = Date.now()
    let settled = false
    const done = (r: TlsProbeResult) => {
      if (settled) return
      settled = true
      socket.destroy()
      resolvePromise(r)
    }
    const socket = tls.connect({
      host,
      port: 443,
      servername,
      // 默认值就是 true，这里显式写出来：校验失败必须判失败，不能退回"能连就行"
      rejectUnauthorized: true,
    })
    socket.setTimeout(timeoutMs)
    socket.once('secureConnect', () => done({ ok: true, latencyMs: Date.now() - started }))
    socket.once('timeout', () => done({ ok: false, latencyMs: null, error: `TLS 握手超时（>${timeoutMs}ms）` }))
    socket.once('error', (e: Error) => done({ ok: false, latencyMs: null, error: e.message }))
  })
}
