/**
 * 续传旁证的落盘侧：分片元数据往返 + 超龄分片清理。
 *
 * 判据本身（decideResume）是纯函数，另有 utils/resume-policy.test.ts 钉死；
 * 这一份只管"证据有没有被如实写下来、过期有没有真的清掉"。
 */
import { mkdtempSync, readFileSync, rmSync, statSync, utimesSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

let dir = ''

vi.mock('electron', () => ({
  app: {
    getPath: (kind: string) => (kind === 'downloads' ? DownloadsRoot : join(tmpdir(), 'ckun-no-downloads')),
  },
}))

let DownloadsRoot = ''

import {
  dropPartialMeta,
  GhDownloadManager,
  partialMetaPath,
  readPartialMeta,
  writePartialMeta,
} from './gh-download'
import type { PartialMeta } from './utils/resume-policy'

const DAY = 86_400_000

/** 把 mtime 推到 agoDays 天前（清理判据读的是文件系统时间，不引入额外状态） */
function ageFile(fp: string, agoDays: number): void {
  const t = new Date(Date.now() - agoDays * DAY)
  utimesSync(fp, t, t)
}

beforeEach(() => {
  DownloadsRoot = mkdtempSync(join(tmpdir(), 'ckun-ghdl-'))
  dir = mkdtempSync(join(DownloadsRoot, 'github-accel'))
})

afterEach(() => {
  rmSync(DownloadsRoot, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 })
  vi.restoreAllMocks()
})

describe('分片元数据往返', () => {
  const partial = () => join(dir, 'app.zip.1234abcd.ckpart')

  it('写出后能原样读回', async () => {
    await writePartialMeta(partial(), { etag: 'W/"x"', lastModified: 'Mon, 01 Jan 2029 00:00:00 GMT', total: 4096 })
    expect(readFileSync(partialMetaPath(partial()), 'utf-8')).toContain('"total":4096')
    await expect(readPartialMeta(partial())).resolves.toEqual({
      etag: 'W/"x"', lastModified: 'Mon, 01 Jan 2029 00:00:00 GMT', total: 4096,
    })
  })

  it('没有元数据 → null（调用方据此放弃续传，而不是当作"和分片一致"）', async () => {
    writeFileSync(partial(), 'x'.repeat(10))
    await expect(readPartialMeta(partial())).resolves.toBeNull()
  })

  it('元数据被写坏 / 结构不对 → null，不猜', async () => {
    await writePartialMeta(partial(), { total: 10 })
    const bak = partialMetaPath(partial())
    writeFileSync(bak, '{"total": 1', 'utf-8')
    await expect(readPartialMeta(partial())).resolves.toBeNull()
    writeFileSync(bak, '[1,2]', 'utf-8')
    await expect(readPartialMeta(partial())).resolves.toBeNull()
    writeFileSync(bak, '{}', 'utf-8')
    await expect(readPartialMeta(partial())).resolves.toBeNull()
  })

  it('drop 幂等，且删不掉时不外抛（元数据只是旁证，不是主数据）', async () => {
    await dropPartialMeta(partial())
    await writePartialMeta(partial(), { total: 1 })
    await dropPartialMeta(partial())
    await expect(readPartialMeta(partial())).resolves.toBeNull()
    await expect(dropPartialMeta(partial())).resolves.toBeUndefined()
  })
})

describe('超龄分片清理', () => {
  const mgr = () => new GhDownloadManager()
  // pruneStalePartials 是编译期 private，运行时取（不为测试放开对外 API）
  const prune = (m: GhDownloadManager, d: string, now?: number) =>
    (m as unknown as { pruneStalePartials: (dir: string, now?: number) => Promise<number> })
      .pruneStalePartials(d, now)

  it('只删超龄的 .ckpart 与它的元数据，新鲜的与目录里别人的文件一律不碰', async () => {
    const stale = join(dir, 'old.zip.aaaaaaaa.ckpart')
    const fresh = join(dir, 'new.zip.bbbbbbbb.ckpart')
    const userFile = join(dir, '我自己放的说明.txt')
    for (const fp of [stale, fresh, userFile]) writeFileSync(fp, 'x'.repeat(8))
    await writePartialMeta(stale, { total: 8, etag: 'W/"old"' })
    await writePartialMeta(fresh, { total: 8, etag: 'W/"new"' })

    ageFile(stale, 8)
    ageFile(partialMetaPath(stale), 8)

    const removed = await prune(mgr(), dir)

    expect(removed).toBe(1)
    expect(statSync(fresh).size).toBe(8)
    expect(statSync(userFile).size).toBe(8)
    await expect(readPartialMeta(fresh)).resolves.toEqual({ total: 8, etag: 'W/"new"' })
    expect(() => statSync(stale)).toThrow()
    expect(() => statSync(partialMetaPath(stale))).toThrow()
  })

  it('第 7 天当天不算超龄（阈值语义是"超过"）', async () => {
    const fp = join(dir, 'edge.zip.cccccccc.ckpart')
    writeFileSync(fp, 'x')
    ageFile(fp, 6.9)
    expect(await prune(mgr(), dir)).toBe(0)
  })

  it('目录读不到时只报错给调用方，不影响下载主流程', async () => {
    await expect(prune(mgr(), join(dir, '不存在的目录'))).rejects.toThrow()
  })
})

/**
 * retry-whole（重下整份）时的进度记账。不测不可：不重置 transferred 的话，
 * 第二轮字节会继续往已下数上累加，进度条直接超过 100%，而文件其实是重下的。
 */
describe('retry-whole 后进度不重复累加', () => {
  type DownloadStatusLike = { id: string; state: string; transferred: number; total: number }
  type FetchOnce = (
    id: string, url: string, partialPath: string, resumeFrom: number,
    gunzip: boolean, meta: PartialMeta | null, redirectsLeft?: number,
  ) => Promise<'retry-whole' | 'cancelled' | 'done' | 'error'>
  type Internals = {
    active: Map<string, { status: DownloadStatusLike; cancel: () => void }>
    fetchLoop: (
      id: string, url: string, partialPath: string, resumeFrom: number,
      gunzip: boolean, meta: PartialMeta | null,
    ) => Promise<string>
    fetchOnce: FetchOnce
  }

  const seed = (transferred: number) => {
    const mgr = new GhDownloadManager()
    const it = mgr as unknown as Internals
    it.active.set('dl-1', { status: { id: 'dl-1', state: 'downloading', transferred, total: 1000 }, cancel: () => {} })
    return it
  }

  it('第二次尝试从 0 开始，且已下计数被清零', async () => {
    const it = seed(400)
    const calls: Parameters<FetchOnce>[] = []
    vi.spyOn(it, 'fetchOnce').mockImplementation((...a) => {
      calls.push(a)
      return Promise.resolve(calls.length === 1 ? 'retry-whole' : 'cancelled')
    })

    const r = await it.fetchLoop('dl-1', 'https://example.invalid/f.zip', 'C:/tmp/f.ckpart', 400, false, null)

    expect(r).toBe('cancelled')
    expect(calls.length, 'fetchOnce 应被调两轮').toBe(2)
    expect(calls[1][3], '重下时 resumeFrom 必须归零').toBe(0)
    expect(it.active.get('dl-1')!.status.transferred, '已下计数必须清零，否则进度条 >100%').toBe(0)
  })
})
