import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import net from 'net'
import { tcpLatency, TCP_TEST_TIMEOUT_MS } from './tcp-latency'

// 本机起一个临时监听端口，避免测试依赖外网：连得上/连不上/超时三条路径都可控。
describe('tcpLatency', () => {
  let server: net.Server
  let port: number

  beforeAll(async () => {
    server = net.createServer(sock => sock.destroy())
    await new Promise<void>(resolvePromise => server.listen(0, '127.0.0.1', resolvePromise))
    port = (server.address() as net.AddressInfo).port
  })

  afterAll(async () => {
    await new Promise<void>(resolvePromise => server.close(() => resolvePromise()))
  })

  it('连得上返回毫秒数', async () => {
    const ms = await tcpLatency('127.0.0.1', port)
    expect(ms).not.toBeNull()
    expect(ms!).toBeGreaterThanOrEqual(0)
    expect(ms!).toBeLessThan(TCP_TEST_TIMEOUT_MS)
  })

  it('端口关闭返回 null（不抛错）', async () => {
    // 先关服务再连同一端口；监听后未 bind 的端口大概率 ECONNREFUSED
    const closed = net.createServer()
    await new Promise<void>(resolvePromise => closed.listen(0, '127.0.0.1', resolvePromise))
    const closedPort = (closed.address() as net.AddressInfo).port
    await new Promise<void>(resolvePromise => closed.close(() => resolvePromise()))
    await expect(tcpLatency('127.0.0.1', closedPort, 1000)).resolves.toBeNull()
  })

  it('不可达地址走超时，返回 null 而非挂住', async () => {
    // 10.255.255.1 是保留网段，正常环境不会被路由；超时设短，测试不可长期挂住
    const started = Date.now()
    const ms = await tcpLatency('10.255.255.1', 443, 300)
    expect(ms).toBeNull()
    expect(Date.now() - started).toBeLessThan(3000)
  })
})
