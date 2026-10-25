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
