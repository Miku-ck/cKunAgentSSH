/**
 * 更新链路的传输策略守卫（真 IO，只碰本机回环）。
 *
 * 为什么单独一份：前两个用例把网络整个注入了 —— shared/update-core.test.ts 是纯函数，
 * electron/updater.test.ts 用替身 fetchJson/downloadFile —— 于是「明文」「跳转降级」
 * 这两件事没有任何用例真的开过 socket，而它们恰恰是这条链路的信任边界：清单没有签名，
 * 走明文等于让链路上的任何人同时决定「版本号、产物地址、期望 sha256」，下载端那记
 * sha256 校验就形同虚设（期望值和包是同一个人给的）。
 *
 * 这里跑真的 defaultFetchJson：正例证明本地假清单验收（回环 http）没被一起挡掉，
 * 反例证明 https→http 的跳转降级会被逐跳拦下、且第二个请求根本没发出去。
 */
import { createServer, type ServerResponse } from 'node:http'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { Updater } from './updater'

const SHA = 'c'.repeat(64)

interface Fake {
  url: (path: string) => string
  hits: () => number
  close: () => Promise<void>
}

function serve(handler: (res: ServerResponse, hits: number) => void): Promise<Fake> {
  let hits = 0
  const server = createServer((_req, res) => {
    hits += 1
    handler(res, hits)
  })
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address() as { port: number }
      resolve({
        url: (path: string) => `http://127.0.0.1:${port}${path}`,
        hits: () => hits,
        close: () => new Promise((r) => { server.close(() => r()) }),
      })
    })
  })
}

function makeUpdater(feedUrl: string, sink: string[]) {
  const root = mkdtempSync(join(tmpdir(), 'ckun-transport-'))
  return new Updater({
    appDir: join(root, 'cKunAgentSSH'),
    userDataDir: join(root, 'userData'),
    currentVersion: '0.1.0',
    exeName: 'cKunAgentSSH.exe',
    platform: 'win32',
    arch: 'x64',
    feedUrl,
    // check() 把网络失败静默成 idle（离线不该在界面留红字），所以判据得看这条 warn
    log: (_level, message) => sink.push(message),
  })
}

describe('更新链路的传输策略（真实 defaultFetchJson）', () => {
  it('回环 http 仍然可用：本地假清单验收路径不能被一起挡掉', async () => {
    const fake = await serve((res, hits) => {
      if (hits === 1) {
        res.statusCode = 302
        res.setHeader('location', '/latest2.json')
        res.end()
        return
      }
      res.setHeader('content-type', 'application/json')
      res.end(JSON.stringify({
        version: '0.2.0',
        releases: [{ platform: 'win32', arch: 'x64', url: 'http://127.0.0.1:1/a.tar.gz', sha256: SHA }],
      }))
    })
    const logs: string[] = []
    try {
      const s = await makeUpdater(fake.url('/latest.json'), logs).check()
      expect(s.phase, logs.join('\n')).toBe('available')
      expect(s.targetVersion).toBe('0.2.0')
      expect(fake.hits()).toBe(2)
    } finally {
      await fake.close()
    }
  })

  it('清单跳转到明文公网地址：逐跳拦下，第二个请求根本没发', async () => {
    // .invalid 是 RFC 2606 保留域，永远不会被解析：判据因此能区分
    // 「我们的协议闸」（消息里有 https）与「守卫缺失时的 DNS 失败」（ENOTFOUND）
    const fake = await serve((res) => {
      res.statusCode = 302
      res.setHeader('location', 'http://mirror.example.invalid/latest.json')
      res.end()
    })
    const logs: string[] = []
    try {
      const s = await makeUpdater(fake.url('/latest.json'), logs).check()
      expect(s.phase).toBe('idle')
      expect(logs.join('\n')).toContain('https')
      expect(fake.hits(), '被拦下后不该再发第二个请求').toBe(1)
    } finally {
      await fake.close()
    }
  })
})
