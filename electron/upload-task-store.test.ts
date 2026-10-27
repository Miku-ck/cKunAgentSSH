/**
 * UploadTaskStore 的验收。
 *
 * 记录写错不会崩，但会在最需要它的时候（闪退/断线后）给出错误答案：把「没传完」
 * 记成完成、把用户暂停的任务记成取消、或者用迟到的失败把已删记录复活。这些都是
 * 异步竞态，只能在这里用可控的 run/deferred 逐条钉住。
 */
import { mkdtempSync, rmSync, writeFileSync } from 'fs'
import { readFile, writeFile } from 'fs/promises'
import { tmpdir } from 'os'
import { join } from 'path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { parseTransferErrorCode } from '../shared/transfer-error-core'
import { UploadTaskStore } from './upload-task-store'

let dir = ''
let path = ''

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'ckun-upload-tasks-'))
  path = join(dir, 'sftp-upload-tasks.json')
})
afterEach(() => { rmSync(dir, { recursive: true, force: true }) })

/** 可控时钟：终态排序/中断时间都要可复现 */
function makeStore(at = 1000): UploadTaskStore {
  return new UploadTaskStore(path, () => at)
}

const task = (id = 't1', over: Record<string, unknown> = {}) => ({
  id,
  localPath: `/local/${id}.bin`,
  remotePath: `/remote/${id}.bin`,
  name: `${id}.bin`,
  total: 100,
  ...over,
})

const transferErr = (code: string, message = 'boom') =>
  Object.assign(new Error(message), { name: `TransferError:${code}`, code })

/** 让 trackUpload 跑到「run 已开始」再返回拒绝器 */
function deferredRun(): {
  run: () => Promise<void>
  started: Promise<void>
  fail: (err: unknown) => void
  finish: () => void
} {
  let start!: () => void
  let fail!: (err: unknown) => void
  let finish!: () => void
  const started = new Promise<void>(r => { start = r })
  const run = () => new Promise<void>((resolve, reject) => {
    fail = reject
    finish = resolve
    start()
  })
  return { run, started, fail: e => fail(e), finish: () => finish() }
}

describe('写盘与读回', () => {
  it('registerBatch → trackUpload → done，另一实例能读回终态', async () => {
    const store = makeStore()
    await store.registerBatch('c1', [task()])
    const pending = store.get('t1')!
    expect(pending.status).toBe('pending')
    expect(pending.connectionId).toBe('c1')

    await store.trackUpload('c1', 't1', '/local/t1.bin', '/remote/t1.bin', { size: 100 }, async () => {
      store.noteProgress('t1', 40, 100)
    })

    const rec = store.get('t1')!
    expect(rec.status).toBe('done')
    expect(rec.transferred).toBe(100)

    const reopened = makeStore()
    await reopened.init()
    const [persisted] = await reopened.list('c1')
    expect(persisted).toMatchObject({ id: 't1', status: 'done', total: 100, transferred: 100 })
  })

  it('进度只进内存；flushNow 后才可在磁盘读到', async () => {
    const store = makeStore()
    const d = deferredRun()
    const p = store.trackUpload('c1', 't1', '/l', '/r', { size: 100 }, d.run)
    await d.started
    store.noteProgress('t1', 42, 100)

    const before = makeStore()
    await before.init()
    expect(before.get('t1')!.transferred).toBe(0)

    await store.flushNow()
    const after = makeStore()
    await after.init()
    expect(after.get('t1')!.transferred).toBe(42)

    d.finish()
    await p
  })

  it('紧凑 JSON 不含缩进', async () => {
    const store = makeStore()
    await store.registerBatch('c1', [task()])
    const raw = await readFile(path, 'utf-8')
    expect(raw.includes('\n')).toBe(false)
    expect(JSON.parse(raw)).toHaveLength(1)
  })
})

describe('失败与状态优先级', () => {
  it('NETWORK 失败落 error 且原错误继续抛出', async () => {
    const store = makeStore()
    await store.registerBatch('c1', [task()])
    const err = transferErr('NETWORK', '连接中断')
    await expect(
      store.trackUpload('c1', 't1', '/l', '/r', { size: 100 }, async () => { throw err }),
    ).rejects.toBe(err)

    const rec = store.get('t1')!
    expect(rec.status).toBe('error')
    expect(rec.errorCode).toBe('NETWORK')
    expect(rec.error).toBe('连接中断')
  })

  it('先暂停后取消：paused 不被迟到的 CANCELLED 覆盖', async () => {
    const store = makeStore()
    await store.registerBatch('c1', [task()])
    const d = deferredRun()
    const p = store.trackUpload('c1', 't1', '/l', '/r', { size: 100 }, d.run)
    await d.started

    await store.markPaused('t1', true)
    await store.markCancelled('t1')
    expect(store.get('t1')!.status).toBe('paused')
    expect(store.get('t1')!.interrupted).toBe(true)

    const err = transferErr('CANCELLED')
    d.fail(err)
    await expect(p).rejects.toBe(err)
    expect(store.get('t1')!.status).toBe('paused')
  })

  it('运行中删除记录后，迟到的失败不得复活它', async () => {
    const store = makeStore()
    await store.registerBatch('c1', [task()])
    const d = deferredRun()
    const p = store.trackUpload('c1', 't1', '/l', '/r', { size: 100 }, d.run)
    await d.started

    await store.remove('t1')
    d.fail(transferErr('NETWORK'))
    await expect(p).rejects.toMatchObject({ code: 'NETWORK' })

    expect(store.get('t1')).toBeUndefined()
    const reopened = makeStore()
    await reopened.init()
    expect(await reopened.list('c1')).toEqual([])
  })

  it('markCancelled 只作用于未结束任务', async () => {
    const store = makeStore()
    await store.registerBatch('c1', [task('t1'), task('t2')])
    await store.markPaused('t2', false)
    await store.markCancelled('t1')
    await store.markCancelled('t2')
    expect(store.get('t1')!.status).toBe('cancelled')
    expect(store.get('t2')!.status).toBe('paused')
  })
})

describe('启动归一化', () => {
  const rawRecord = (over: Record<string, unknown>) => ({
    id: 't1',
    connectionId: 'c1',
    localPath: '/l',
    remotePath: '/r',
    name: 'a',
    total: 100,
    transferred: 30,
    status: 'running',
    createdAt: 1,
    updatedAt: 1,
    ...over,
  })

  it('磁盘上的 running/pending 归一化为 paused + interrupted；终态不动', async () => {
    await writeFile(path, JSON.stringify([
      rawRecord({ id: 'run', status: 'running' }),
      rawRecord({ id: 'pend', status: 'pending' }),
      rawRecord({ id: 'paused', status: 'paused', interrupted: true }),
      rawRecord({ id: 'done', status: 'done', transferred: 100 }),
      rawRecord({ id: 'err', status: 'error', error: 'x', errorCode: 'NETWORK' }),
    ]), 'utf-8')

    const store = makeStore(9000)
    await store.init()

    expect(store.get('run')).toMatchObject({ status: 'paused', interrupted: true, transferred: 30 })
    expect(store.get('pend')).toMatchObject({ status: 'paused', interrupted: true })
    expect(store.get('paused')).toMatchObject({ status: 'paused', interrupted: true })
    expect(store.get('done')).toMatchObject({ status: 'done' })
    expect(store.get('done')!.interrupted).toBeUndefined()
    expect(store.get('err')).toMatchObject({ status: 'error', errorCode: 'NETWORK' })

    // 归一化结果已写回磁盘，下一次启动无需再算
    const onDisk = JSON.parse(await readFile(path, 'utf-8'))
    const byId = Object.fromEntries(onDisk.map((r: { id: string }) => [r.id, r]))
    expect(byId.run.status).toBe('paused')
    expect(byId.run.interrupted).toBe(true)
    expect(byId.done.status).toBe('done')
  })

  it('损坏文件被隔离后按空历史启动，且不阻止新记录', async () => {
    await writeFile(path, '{not json', 'utf-8')
    const store = makeStore()
    await store.init()
    expect(await store.list('c1')).toEqual([])

    await store.registerBatch('c1', [task()])
    expect(store.get('t1')!.status).toBe('pending')
  })
})

describe('显式续传的源文件一致性', () => {
  it('本地文件大小与记录不符 → 清 .part、记 REMOTE_CHANGED、不执行上传', async () => {
    const local = join(dir, 'src.bin')
    writeFileSync(local, Buffer.alloc(50))
    const store = makeStore()
    await store.registerBatch('c1', [
      { id: 't1', localPath: local, remotePath: '/r/src.bin', name: 'src.bin', total: 100 },
    ])

    let discarded = 0
    let ran = false
    const err = await store.trackUpload(
      'c1', 't1', local, '/r/src.bin', { resume: true }, async () => { ran = true },
      { discardPart: async () => { discarded++ } },
    ).catch(e => e)

    expect(ran).toBe(false)
    expect(discarded).toBe(1)
    expect(parseTransferErrorCode(err?.name)).toBe('REMOTE_CHANGED')
    expect(store.get('t1')).toMatchObject({
      status: 'error',
      errorCode: 'REMOTE_CHANGED',
    })
    expect(store.get('t1')!.interrupted).toBeUndefined()
  })

  it('大小一致则放行，仍按正常生命周期走', async () => {
    const local = join(dir, 'src.bin')
    writeFileSync(local, Buffer.alloc(100))
    const store = makeStore()
    await store.registerBatch('c1', [
      { id: 't1', localPath: local, remotePath: '/r/src.bin', name: 'src.bin', total: 100 },
    ])
    let ran = false
    await store.trackUpload('c1', 't1', local, '/r/src.bin', { resume: true }, async () => { ran = true })
    expect(ran).toBe(true)
    expect(store.get('t1')!.status).toBe('done')
  })
})

describe('清除与归属', () => {
  it('clearFinished 只删该连接的终态，不动运行/暂停/其他主机', async () => {
    const store = makeStore()
    await store.registerBatch('c1', [task('run'), task('pause'), task('done')])
    await store.registerBatch('c2', [task('other')])

    const d = deferredRun()
    const p = store.trackUpload('c1', 'run', '/l', '/r', { size: 100 }, d.run)
    await d.started
    await store.markPaused('pause', false)
    await store.markCancelled('done')

    await store.clearFinished('c1')
    expect(store.get('run')!.status).toBe('running')
    expect(store.get('pause')!.status).toBe('paused')
    expect(store.get('done')).toBeUndefined()
    expect(store.get('other')!.status).toBe('pending')

    d.finish()
    await p
  })

  it('list 只返回指定连接，且返回副本', async () => {
    const store = makeStore()
    await store.registerBatch('c1', [task('a')])
    await store.registerBatch('c2', [task('b')])

    const list = await store.list('c1')
    expect(list.map(r => r.id)).toEqual(['a'])
    list[0].status = 'done'
    expect(store.get('a')!.status).toBe('pending')
  })
})
