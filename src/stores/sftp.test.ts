/**
 * S5-11 回归 — 复制行的「取消」不能再是静默空操作。
 *
 * 原实现把任何 running 任务的 id 直接发给 `sftpCancel`，而那条通道在主进程只通知
 * SSHManager 的传输表：本地复制（fs:copy 逐文件循环）没有句柄、远程复制（一条
 * `cp -r`）根本不受我们控制 ⇒ 点了 ✕ 什么也不会发生，行一直停在「复制中」。
 * 现在：本地复制真能被取消（先置 cancelled 再发信号，runLocalCopy 的 catch 静默收尾），
 * 远程复制把话说清楚；上传/下载同顺序本地先置 cancelled —— 自动续传的退避间隙里
 * 没有在途 IPC 可拒绝，不先置位「取消」会被吞，醒来后任务继续续传。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const toastSpy = vi.hoisted(() => ({
  info: vi.fn(),
  warning: vi.fn(),
  error: vi.fn(),
  success: vi.fn(),
}))

vi.mock('./toast', () => ({ useToastStore: () => toastSpy }))
// store 只用到这两个纯函数；真模块会拉起 axios 与后端地址解析
vi.mock('@/api/client', () => ({
  toFriendlyMessage: (e: unknown) => String(e),
  unwrapIpcErrorEnvelope: (s: string) => s,
}))

import { useSftpStore } from './sftp'

type FakeApi = Record<string, ReturnType<typeof vi.fn>>

function makeApi(overrides: Record<string, unknown> = {}): FakeApi {
  const base: FakeApi = {
    fsCopy: vi.fn(async () => undefined),
    sftpCancel: vi.fn(async () => undefined),
    fsPathJoin: vi.fn(async (...segs: string[]) => segs.join('/')),
    fsReadDir: vi.fn(async () => []),
  }
  for (const [k, v] of Object.entries(overrides)) base[k] = v as ReturnType<typeof vi.fn>
  // 未预料到的通道一律给个空实现：本组用例只关心取消这条链路
  return new Proxy(base, {
    get(target, prop: string) {
      if (!(prop in target)) target[prop] = vi.fn(async () => [])
      return target[prop]
    },
  })
}

function pushTransfer(store: ReturnType<typeof useSftpStore>, over: Record<string, unknown>): void {
  store.addTransfer({
    id: 't1', direction: 'down', kind: 'copy-local', name: 'n',
    from: '/a', to: '/b', transferred: 0, total: 0,
    status: 'running', startTime: 1, ...over,
  } as never)
}

const flush = () => new Promise((r) => setTimeout(r, 0))

describe('sftp store — 传输索引不变量（阶段2/B4）', () => {
  let api: FakeApi
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    api = makeApi()
    ;(globalThis as unknown as { window: unknown }).window = { electronAPI: api }
  })

  function mk(id: string, status = 'running') {
    return {
      id, direction: 'up', kind: 'upload', name: id, from: '/a', to: '/b',
      transferred: 0, total: 100, status, startTime: 1,
    } as never
  }

  it('addTransfer 登记后 applyProgress 命中数组里的同一对象', () => {
    const store = useSftpStore()
    store.addTransfer(mk('tA'))
    store.applyProgress({ id: 'tA', transferred: 42, total: 100 })
    expect(store.transfers[0].transferred).toBe(42)
  })

  it('removeTransfer 后对同一 id 的 applyProgress 静默无副作用（索引已删）', () => {
    const store = useSftpStore()
    store.addTransfer(mk('tA'))
    store.addTransfer(mk('tB'))
    store.removeTransfer('tA')
    // 迟到/误投的进度不得命中已删记录，也不得误改到 tB
    store.applyProgress({ id: 'tA', transferred: 999, total: 100 })
    expect(store.transfers.map(t => t.id)).toEqual(['tB'])
    expect(store.transfers[0].transferred).toBe(0)
    store.applyProgress({ id: 'tB', transferred: 7, total: 100 })
    expect(store.transfers[0].transferred).toBe(7)
  })

  it('clearCompleted 重建索引：完成项移出后不可再被 applyProgress 命中', () => {
    const store = useSftpStore()
    store.addTransfer(mk('done', 'done'))
    store.addTransfer(mk('run', 'running'))
    store.clearCompleted()
    expect(store.transfers.map(t => t.id)).toEqual(['run'])
    // done 已从数组与索引双双移除：给它的进度既不报错也不复活
    store.applyProgress({ id: 'done', transferred: 100, total: 100 })
    expect(store.transfers.find(t => t.id === 'done')).toBeUndefined()
    expect(store.transfers[0].transferred).toBe(0)
  })
})

describe('sftp store — 复制的取消语义（S5-11）', () => {
  let api: FakeApi

  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    api = makeApi()
    ;(globalThis as unknown as { window: unknown }).window = { electronAPI: api }
  })

  it('远程复制：不再把信号丢给一条管不到它的通道，而是把话说清楚', async () => {
    const store = useSftpStore()
    pushTransfer(store, { kind: 'copy-remote' })

    await store.cancelTransfer('t1')

    expect(api.sftpCancel).not.toHaveBeenCalled()
    expect(toastSpy.info).toHaveBeenCalledWith(expect.stringContaining('无法中途取消'))
    expect(store.transfers[0].status).toBe('running')
  })

  it('本地复制：先置 cancelled 再发 sftpCancel，且只发这一条腿能办到的信号', async () => {
    const store = useSftpStore()
    pushTransfer(store, { kind: 'copy-local' })

    await store.cancelTransfer('t1')

    expect(store.transfers[0].status).toBe('cancelled')
    expect(api.sftpCancel).toHaveBeenCalledWith('t1')
    expect(toastSpy.info).toHaveBeenCalledWith(expect.stringContaining('已取消复制'))
    expect(toastSpy.error).not.toHaveBeenCalled()
  })

  it('fs:copy 因取消而抛错时静默收尾，不把它报成「复制失败」', async () => {
    // 主进程的真实形态：sftp:cancel 置位 → 下一次迭代 throw「复制已取消」
    const rejecters = new Map<string, (e: Error) => void>()
    api.fsCopy = vi.fn((_src: string, _dst: string, id: string) =>
      new Promise<void>((_res, rej) => rejecters.set(id, rej))) as never
    api.sftpCancel = vi.fn(async (id: string) => {
      rejecters.get(id)?.(new Error('复制已取消'))
    }) as never

    const store = useSftpStore()
    await store.localCopy(['/src-dir'], '/dst-dir')
    await flush()
    const id = store.transfers[0].id
    expect(store.transfers[0].status).toBe('running')

    await store.cancelTransfer(id)
    await flush()

    expect(store.transfers[0].status).toBe('cancelled')
    expect(toastSpy.error).not.toHaveBeenCalled()
  })

  it('排队中的复制仍按 F-5 本地取消（主进程无记录，不发信号）', async () => {
    const store = useSftpStore()
    pushTransfer(store, { status: 'pending' })

    await store.cancelTransfer('t1')

    expect(store.transfers[0].status).toBe('cancelled')
    expect(api.sftpCancel).not.toHaveBeenCalled()
  })

  it('上传/下载：与复制同顺序 —— 本地先置 cancelled 再发 sftpCancel（退避间隙取消不落空）', async () => {
    const store = useSftpStore()
    pushTransfer(store, { kind: 'upload', direction: 'up' })

    await store.cancelTransfer('t1')

    expect(store.transfers[0].status).toBe('cancelled')
    expect(api.sftpCancel).toHaveBeenCalledWith('t1')
  })
})

describe('sftp store — 上传记录（跨重启保留 / 手动继续）', () => {
  let api: FakeApi

  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    api = makeApi()
    ;(globalThis as unknown as { window: unknown }).window = { electronAPI: api }
  })

  const record = (over: Record<string, unknown> = {}) => ({
    id: 't1',
    connectionId: 'conn-1',
    localPath: '/local/a.bin',
    remotePath: '/remote/a.bin',
    name: 'a.bin',
    total: 100,
    transferred: 40,
    status: 'paused',
    interrupted: true,
    createdAt: 100,
    updatedAt: 200,
    ...over,
  })

  function pushUpload(store: ReturnType<typeof useSftpStore>, over: Record<string, unknown> = {}) {
    pushTransfer(store, {
      kind: 'upload', direction: 'up', name: 'a.bin',
      from: '/local/a.bin', to: '/remote/a.bin', total: 100, transferred: 40,
      status: 'paused', ...over,
    })
  }

  it('queueStats 的字节只算未结束任务：永久保留的完成行不把百分比钉死', () => {
    const store = useSftpStore()
    store.addTransfer({
      id: 'old', direction: 'up', kind: 'upload', name: 'old', from: '/a', to: '/b',
      transferred: 900, total: 900, status: 'done', startTime: 1,
    })
    store.addTransfer({
      id: 'run', direction: 'up', kind: 'upload', name: 'run', from: '/c', to: '/d',
      transferred: 50, total: 100, status: 'running', startTime: 1,
    })
    expect(store.queueStats.percent).toBe(50)
  })

  it('clearCompleted 同时清主进程里本主机的终态记录（离线也能清）', () => {
    const store = useSftpStore()
    store.activeConnectionId = 'conn-1'
    store.addTransfer({
      id: 'u', direction: 'up', kind: 'upload', name: 'u', from: '/a', to: '/b',
      transferred: 1, total: 1, status: 'done', startTime: 1,
    })
    store.clearCompleted()
    expect(api.sftpClearUploadTasks).toHaveBeenCalledWith('conn-1')
  })

  it('删除上传行会删持久记录；删除下载行不会', () => {
    const store = useSftpStore()
    pushUpload(store)
    pushTransfer(store, { id: 'd1', kind: 'download', direction: 'down' })

    store.removeTransfer('t1')
    expect(api.sftpRemoveUploadTask).toHaveBeenCalledWith('t1')

    store.removeTransfer('d1')
    expect(api.sftpRemoveUploadTask).not.toHaveBeenCalledWith('d1')
  })

  it('上传成功后不自动消失（记录承诺）；下载仍按原逻辑自动移除', async () => {
    vi.useFakeTimers()
    try {
      api.fsStat = vi.fn(async () => ({ isDirectory: false, size: 100 })) as never
      const store = useSftpStore()
      store.sshSessionId = 'sid'
      store.online = true

      await store.uploadFiles(['/local/a.bin'], '/remote')
      // 让登记 + 上传的微任务全部跑完
      await vi.advanceTimersByTimeAsync(0)
      await vi.advanceTimersByTimeAsync(0)
      const row = store.transfers.find(t => t.kind === 'upload')!
      expect(row.status).toBe('done')

      await vi.advanceTimersByTimeAsync(4000)
      expect(store.transfers.find(t => t.id === row.id)).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })

  it('断开连接：上传行转 paused+interrupted 保留，下载行清空', async () => {
    const store = useSftpStore()
    store.sshSessionId = 'sid'
    store.online = true
    pushUpload(store, { status: 'running', interrupted: undefined })
    pushTransfer(store, { id: 'd1', kind: 'download', direction: 'down', status: 'running' })

    await store.disconnect()

    const up = store.transfers.find(t => t.id === 't1')!
    expect(up.status).toBe('paused')
    expect(up.interrupted).toBe(true)
    expect(store.transfers.find(t => t.id === 'd1')).toBeUndefined()
    expect(api.sftpPauseUploadTask).toHaveBeenCalledWith('t1', true)
    expect(api.sftpCancel).toHaveBeenCalledWith('t1')
  })

  it('离线时继续只提示，不发传输', () => {
    const store = useSftpStore()
    store.online = false
    pushUpload(store)

    store.resumeTransfer('t1')

    expect(api.sftpUpload).not.toHaveBeenCalled()
    expect(store.transfers[0].status).toBe('paused')
    expect(toastSpy.warning).toHaveBeenCalledWith(expect.stringContaining('请先连接远程主机'))
  })

  it('在线继续：走并发门并以显式 resume 调用上传', async () => {
    const store = useSftpStore()
    store.online = true
    store.sshSessionId = 'sid'
    pushUpload(store)

    store.resumeTransfer('t1')
    await flush()

    expect(api.sftpUpload).toHaveBeenCalledTimes(1)
    const call = api.sftpUpload.mock.calls[0]
    expect(call[0]).toBe('t1')
    expect(call[4]).toMatchObject({ resume: true })
    expect(store.transfers[0].status).toBe('done')
  })

  it('连接后恢复记录按 id 幂等，重复恢复不产重复行', async () => {
    api.sftpGetSessionConnectionId = vi.fn(async () => 'conn-1') as never
    api.sshGetCwd = vi.fn(async () => '/home/u') as never
    api.sftpListUploadTasks = vi.fn(async () => [
      record({ id: 't1', status: 'paused' }),
      record({ id: 't2', status: 'done', transferred: 100 }),
    ]) as never

    const store = useSftpStore()
    await store.connectFromTerminal('sid', 'host', 'u@h:22')
    expect(store.activeConnectionId).toBe('conn-1')
    expect(store.transfers.filter(t => t.kind === 'upload').map(t => t.id)).toEqual(['t1', 't2'])

    await store.restoreUploads()
    expect(store.transfers.filter(t => t.kind === 'upload')).toHaveLength(2)
  })

  it('主进程仍在 running（渲染层重载）：恢复为可继续并先停掉孤儿传输', async () => {
    api.sftpGetSessionConnectionId = vi.fn(async () => 'conn-1') as never
    api.sshGetCwd = vi.fn(async () => '/home/u') as never
    api.sftpListUploadTasks = vi.fn(async () => [
      record({ id: 't9', status: 'running', interrupted: undefined }),
    ]) as never

    const store = useSftpStore()
    await store.connectFromTerminal('sid', 'host', 'u@h:22')

    const row = store.transfers.find(t => t.id === 't9')!
    expect(row.status).toBe('paused')
    expect(row.interrupted).toBe(true)
    expect(api.sftpPauseUploadTask).toHaveBeenCalledWith('t9', true)
    expect(api.sftpCancel).toHaveBeenCalledWith('t9')
  })

  it('恢复往返期间刚发起的在跑上传不被历史记录覆写（计数器会撞历史 id）', async () => {
    const store = useSftpStore()
    store.activeConnectionId = 'conn-1'
    let release!: (records: unknown[]) => void
    api.sftpListUploadTasks = vi.fn(
      () => new Promise<unknown>(res => { release = res as (r: unknown[]) => void }),
    ) as never

    const restoring = store.restoreUploads()
    await flush()
    // 列表还没回来，用户已拖进一个文件开始传：新行 id 从 t1 起，与历史记录同名
    store.addTransfer({
      id: 't1', direction: 'up', kind: 'upload', name: 'fresh.bin',
      from: '/new.bin', to: '/remote/new.bin', transferred: 5, total: 10,
      status: 'running', startTime: 1,
    })
    release([record({ id: 't1', name: 'a.bin', status: 'paused', interrupted: true })])
    await restoring

    const row = store.transfers.find(t => t.id === 't1')!
    expect(row.name).toBe('fresh.bin')
    expect(row.to).toBe('/remote/new.bin')
    expect(row.status).toBe('running')
    expect(row.interrupted).toBeUndefined()
  })
})
