/**
 * 同 URL 并发下载去重：两个同 URL 任务并发时，双方都会打开同一个 .ckpart
 * 按 'a' 追加写，字节交错、rename 后会产出损坏的"完成"文件。守三点：
 * 在途时同 URL 二次 start 被拒且不留僵尸任务；同名不同 URL 不被误拦（分片名带
 * URL 指纹，本就不共用文件）；任务到终态（done/cancelled）后同 URL 可重新开始。
 */
import { EventEmitter } from 'node:events'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { PassThrough } from 'node:stream'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('electron', () => ({
  app: {
    getPath: (kind: string) => (kind === 'downloads' ? DownloadsRoot : join(tmpdir(), 'ckun-no-downloads')),
  },
}))

let DownloadsRoot = ''

// https 请求不真发：只捕获调用，由用例决定响应节奏（不回应 = 任务停在 downloading）
vi.mock('https', () => ({
  default: {
    get: (url: string, _opts: unknown, cb: (res: FakeRes) => void) => {
      const req = new FakeReq()
      capturedGets.push({ url, cb, req })
      return req
    },
  },
}))

/** https.get 的返回值：gh-download 只用到 once（挂 timeout/error）与 destroy */
class FakeReq extends EventEmitter {
  destroy(): void { /* 真实请求会掐断 socket，这里没有 socket 可掐 */ }
}

/** 响应对象：借 PassThrough 的 pipe/data 通路走完真实下载循环 */
class FakeRes extends PassThrough {
  statusCode = 200
  headers: Record<string, string | undefined> = {}
}

type CapturedGet = { url: string; cb: (res: FakeRes) => void; req: FakeReq }
const capturedGets: CapturedGet[] = []

import { GhDownloadManager } from './gh-download'
import type { DownloadStatus } from './gh-download'

const URL_A = 'https://mirror.example/a/repo1/main.zip'
// 与 URL_A 同名不同 URL：正是分片名带指纹要保住"互不串档"的场景
const URL_B = 'https://mirror.example/a/repo2/main.zip'

beforeEach(() => {
  DownloadsRoot = mkdtempSync(join(tmpdir(), 'ckun-ghdl-conc-'))
  capturedGets.length = 0
})

afterEach(() => {
  rmSync(DownloadsRoot, { recursive: true, force: true })
  vi.restoreAllMocks()
})

/** 开管理器并收下终态推送（done/error/cancelled），downloading 的不记 */
function makeMgr() {
  const mgr = new GhDownloadManager()
  const terminal: DownloadStatus[] = []
  mgr.setSender(s => { if (s.state !== 'downloading') terminal.push(s) })
  return { mgr, terminal }
}

/** start() 到真正发请求隔着 mkdir/stat 等异步步，轮询等它到齐 */
async function waitForGets(n: number): Promise<void> {
  await vi.waitFor(() => {
    if (capturedGets.length < n) throw new Error(`已捕获 ${capturedGets.length} 个请求，期望 ≥ ${n}`)
  }, { timeout: 3000, interval: 20 })
}

/** 200 + 全量 body 回应（content-length 与 body 对齐，run() 的完整性校验才放行） */
function respondFull(get: CapturedGet, body: string): void {
  const res = new FakeRes()
  res.headers['content-length'] = String(Buffer.byteLength(body))
  get.cb(res)
  res.end(body)
}

describe('同 URL 并发下载去重', () => {
  it('在途时同 URL 二次 start 被拒，且不产生第二个任务', async () => {
    const { mgr } = makeMgr()
    await mgr.start(URL_A)
    await waitForGets(1) // 不回应，任务停在 downloading

    await expect(mgr.start(URL_A)).rejects.toThrow('该文件已在下载中')
    expect(mgr.list().filter(s => s.state === 'downloading'), '被拒的 start 不得留下任务').toHaveLength(1)

    await expect(mgr.start(URL_B)).resolves.toMatchObject({ state: 'downloading' })
    await waitForGets(2)
    expect(capturedGets[1]!.url).toBe(URL_B)
  })

  it('首任务完成后同 URL 可重新开始（终态条目仍留在表里供回看，但不再拦截）', async () => {
    const { mgr, terminal } = makeMgr()
    const first = await mgr.start(URL_A)
    await waitForGets(1)
    respondFull(capturedGets[0]!, 'hello-part-body')
    await vi.waitFor(() => expect(terminal.map(s => s.state)).toContain('done'))

    const again = await mgr.start(URL_A)
    expect(again.id).not.toBe(first.id)
    await waitForGets(2)
    expect(capturedGets[1]!.url).toBe(URL_A)
  })

  it('取消后同 URL 可重新开始', async () => {
    const { mgr } = makeMgr()
    const first = await mgr.start(URL_A)
    await waitForGets(1)
    mgr.cancel(first.id)
    expect(mgr.statusOf(first.id)?.state).toBe('cancelled')

    await expect(mgr.start(URL_A)).resolves.toMatchObject({ state: 'downloading' })
  })
})

/**
 * Q-E4 回归：出站 host 校验与并发任务数上限。
 *
 * assertUrl 在仅校验 https 之外，请求前 new URL 解析并校验 host：拒绝 localhost、
 * 环回、私有与保留地址（IPv4/IPv6 字面量与非常规编码写法——WHATWG 归一化后统一
 * 判定）；GitHub 下载链路域名白名单直接放行，自定义镜像的公网域放行。302 每一跳
 * 复检同一校验。并发上限 16：超限拒绝并回执，取消任务后名额释放。
 */
describe('出站 host 校验（Q-E4）', () => {
  it.each([
    ['https://localhost/x.zip', '环回名'],
    ['https://LOCALHOST/x.zip', '环回名（大小写）'],
    ['https://127.0.0.1/x.zip', 'IPv4 环回'],
    ['https://2130706433/x.zip', 'IPv4 十进制整数写法'],
    ['https://0x7f000001/x.zip', 'IPv4 十六进制写法'],
    ['https://127.1/x.zip', 'IPv4 缺段简写'],
    ['https://10.1.2.3/x.zip', '私有 10/8'],
    ['https://192.168.1.1/x.zip', '私有 192.168/16'],
    ['https://172.16.0.9/x.zip', '私有 172.16/12'],
    ['https://169.254.169.254/x.zip', '链路本地（云元数据）'],
    ['https://100.64.1.1/x.zip', 'CGNAT 100.64/10'],
    ['https://224.0.0.1/x.zip', '组播 224/4'],
    ['https://[::1]/x.zip', 'IPv6 环回'],
    ['https://[::]/x.zip', 'IPv6 未指定'],
    ['https://[fc00::1]/x.zip', 'IPv6 ULA'],
    ['https://[fe80::1]/x.zip', 'IPv6 链路本地'],
    ['https://[::ffff:7f00:1]/x.zip', 'IPv4-mapped IPv6（归一化形态）'],
    ['https://[2002:7f00:1::]/x.zip', '6to4 内嵌环回'],
    ['http://mirror.example/x.zip', '非 https 协议'],
    ['file:///etc/hosts', 'file 协议'],
    ['not-a-url', '畸形 URL'],
  ])('拒绝 %s（%s）', async (url) => {
    const { mgr } = makeMgr()
    await expect(mgr.start(url)).rejects.toThrow()
    expect(mgr.list(), '被拒的 start 不得留下任务').toHaveLength(0)
    expect(capturedGets, '不得发起任何出站请求').toHaveLength(0)
  })

  it.each([
    ['https://github.com/u/r/releases/download/v1.2.3/f.zip', 'github.com'],
    ['https://objects.githubusercontent.com/sha/f.zip', 'objects.githubusercontent.com'],
    ['https://codeload.github.com/u/r/zip/refs/heads/main', 'codeload.github.com'],
    ['https://mirror.example/a/repo/main.zip', '自定义镜像公网域'],
  ])('放行 %s（%s）', async (url) => {
    const { mgr } = makeMgr()
    await expect(mgr.start(url)).resolves.toMatchObject({ state: 'downloading' })
    await waitForGets(1)
    expect(capturedGets[0]!.url).toBe(url)
  })

  it('302 重定向到内网地址被拒：每一跳复检同一 host 校验', async () => {
    const { mgr, terminal } = makeMgr()
    await mgr.start(URL_A)
    await waitForGets(1)
    const res = new FakeRes()
    res.statusCode = 302
    res.headers.location = 'https://169.254.169.254/latest/meta-data/'
    capturedGets[0]!.cb(res)
    res.resume()
    await vi.waitFor(() => expect(terminal.map(s => s.state)).toContain('error'))
    expect(terminal.find(s => s.state === 'error')?.error).toContain('不允许的下载地址')
  })
})

describe('并发任务数上限（Q-E4）', () => {
  it('第 17 个在途任务被拒且不留僵尸条目，取消一个后名额释放', async () => {
    const { mgr } = makeMgr()
    const started: DownloadStatus[] = []
    for (let i = 0; i < 16; i++) {
      started.push(await mgr.start(`https://mirror.example/bulk/file-${i}.zip`))
    }
    await waitForGets(16)
    await expect(mgr.start('https://mirror.example/bulk/overflow.zip'))
      .rejects.toThrow('下载任务数已达上限')
    expect(mgr.list().filter(s => s.state === 'downloading'), '被拒的 start 不得留下任务').toHaveLength(16)

    // 取消一个（终态）→ 名额释放，新任务可进
    mgr.cancel(started[0]!.id)
    expect(mgr.statusOf(started[0]!.id)?.state).toBe('cancelled')
    await expect(mgr.start('https://mirror.example/bulk/overflow.zip'))
      .resolves.toMatchObject({ state: 'downloading' })
  })
})
