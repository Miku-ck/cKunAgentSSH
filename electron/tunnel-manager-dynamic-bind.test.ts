/**
 * -D 的 127.0.0.1 边界必须落在真正 listen 的值上（安全回归）。
 *
 * start() 里那次钳制只喂给了端口预检：`const host = rule.type === 'dynamic' ?
 * '127.0.0.1' : …` 之后只用在 probePortInUse 与错误文案里，而 startDynamic 拿的是
 * `s.rule.listenHost || '127.0.0.1'` —— 脏 tunnels.json（手改或旧版本残留；写侧
 * validateRule 只在保存那一刻钳）带着 listenHost=0.0.0.0 照样能起监听，于是免认证
 * SOCKS5 直接开到局域网。
 *
 * 假 net 记录每次 listen 时区分「有没有 CONNECT 处理函数」：预检用的是裸
 * createServer()，真监听才带 handler —— 只断言最后一次等于把预检当成绑定，
 * 数据面没起来也会悄悄通过。同时钉住反方向：-L 的 listenHost 是用户意图。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

type ListenRecord = { host: string; port: number; bound: boolean }
const listens = vi.hoisted(() => [] as ListenRecord[])

vi.mock('net', () => ({
  createServer: (handler?: unknown) => {
    const srv: Record<string, unknown> = {}
    srv.once = () => srv
    srv.on = () => srv
    srv.listen = (port: number, host: string, cb?: () => void) => {
      listens.push({ host, port, bound: typeof handler === 'function' })
      cb?.()
      return srv
    }
    srv.close = (cb?: () => void) => { cb?.() }
    srv.address = () => ({ address: '127.0.0.1', port: 0 })
    return srv
  },
  connect: () => ({ on: () => undefined, destroy: () => undefined, write: () => undefined }),
}))

vi.mock('electron', () => ({
  BrowserWindow: class {},
  app: { getPath: () => '/tmp/ckun-agent-tunnel-test' },
}))

import { TunnelManager } from './tunnel-manager'
import type { TunnelRule } from './tunnel-store'

/** TunnelTransport 的最小可用替身：start() 会挂 onDead，缺一个成员就整条建连失败 */
function fakeTransport() {
  const bind = { bindHost: '127.0.0.1', bindPort: 0, targetHost: '', targetPort: 0 }
  return {
    dial: async () => ({ on: () => undefined, pipe: () => undefined, write: () => undefined, destroy: () => undefined }),
    bind: async () => undefined,
    unbind: async () => undefined,
    onInbound: () => undefined,
    removeInbound: () => undefined,
    onDead: () => () => undefined,
    close: () => undefined,
    _bind: bind,
  }
}

describe('-D 监听地址边界', () => {
  let mgr: TunnelManager
  beforeEach(() => {
    listens.length = 0
    mgr = new TunnelManager()
    mgr.setWindow({ isDestroyed: () => false, webContents: { send: () => {} } } as never)
    mgr.setTransportFactory(async () => fakeTransport() as never)
  })

  it('dynamic 规则带着脏 listenHost，真正绑定的仍是 127.0.0.1', async () => {
    const rule = {
      id: 't-dirty-dynamic',
      name: 'dirty -D',
      type: 'dynamic',
      listenHost: '0.0.0.0',
      listenPort: 47801,
    } as unknown as TunnelRule

    const status = await mgr.start(rule)
    expect(status.state).toBe('active')

    // 只认带 CONNECT 处理函数的那次 listen —— 预检那次不算绑定
    const bound = listens.filter(l => l.bound)
    expect(bound).toHaveLength(1)
    expect(bound[0]).toEqual({ host: '127.0.0.1', port: 47801, bound: true })

    mgr.stop(rule.id)
  })

  it('local 规则的 listenHost 属于用户意图，不被同一条钳制管死', async () => {
    const rule = {
      id: 't-local-host',
      name: 'explicit -L bind',
      type: 'local',
      listenHost: '0.0.0.0',
      listenPort: 47802,
      dstHost: '127.0.0.1',
      dstPort: 3306,
    } as unknown as TunnelRule

    const status = await mgr.start(rule)
    expect(status.state).toBe('active')

    const bound = listens.filter(l => l.bound)
    expect(bound).toHaveLength(1)
    expect(bound[0]).toEqual({ host: '0.0.0.0', port: 47802, bound: true })

    mgr.stop(rule.id)
  })
})
