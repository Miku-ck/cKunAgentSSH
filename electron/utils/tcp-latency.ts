/**
 * TCP connect 延迟测量（hosts 优选与加速健康巡检共用）。
 * 只测三次握手耗时，不做 TLS/应用层探测 —— 判的是"这个端点连得上吗、有多慢"。
 */
import net from 'net'

/** 单次 connect 的超时；优选与巡检同口径 */
export const TCP_TEST_TIMEOUT_MS = 4000

/** 连得上返回毫秒，错误/超时返回 null */
export function tcpLatency(host: string, port = 443, timeoutMs = TCP_TEST_TIMEOUT_MS): Promise<number | null> {
  return new Promise((resolvePromise) => {
    const started = Date.now()
    const sock = net.connect(port, host)
    let settled = false
    const done = (v: number | null) => {
      if (settled) return
      settled = true
      sock.destroy()
      resolvePromise(v)
    }
    sock.once('connect', () => done(Date.now() - started))
    sock.once('error', () => done(null))
    sock.once('timeout', () => done(null))
    sock.setTimeout(timeoutMs)
  })
}
