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
