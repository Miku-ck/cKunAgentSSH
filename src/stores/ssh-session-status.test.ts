/** 会话状态层：重连事件映射、活动标记、MRU 栈 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

type ReconnectCb = (d: { id: string; phase: string; attempt: number; maxAttempts: number; reason?: string }) => void
let reconnectCb: ReconnectCb | null = null
const onSshReconnect = vi.fn((cb: ReconnectCb) => { reconnectCb = cb; return () => {} })
const sshListConnections = vi.fn(async () => [])

const g = globalThis as unknown as Record<string, unknown>
g.window = {
  electronAPI: {
    onSshReconnect,
    sshListConnections,
    onHostKeyNew: () => () => {},
    onHostKeyMismatch: () => () => {},
    onHostKeyUnpinned: () => () => {},
  },
}
g.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} }

vi.mock('./toast', () => ({
  useToastStore: () => ({ info: vi.fn(), warning: vi.fn(), error: vi.fn(), success: vi.fn() }),
}))
vi.mock('@/api/client', () => ({
  toFriendlyMessage: (e: unknown) => String(e),
  unwrapIpcErrorEnvelope: (s: string) => s,
}))

import { useSSHStore } from './ssh'

function seed(store: ReturnType<typeof useSSHStore>, ...ids: string[]) {
  for (const id of ids) {
    store.sessions.push({ id, name: id, type: 'local', shell: 'CMD' })
  }
}

describe('会话状态层', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    reconnectCb = null
  })

  it('init() 注册重连跟踪监听', async () => {
    const store = useSSHStore()
    await store.init()
    expect(onSshReconnect).toHaveBeenCalledTimes(1)
  })

  it('重连事件映射：reconnecting/retry→reconnect 对象；success→清空；gave-up(exhausted)→reconnectGaveUp', async () => {
    const store = useSSHStore()
    await store.init()
    seed(store, 's1')
    reconnectCb!({ id: 's1', phase: 'reconnecting', attempt: 1, maxAttempts: 3 })
    expect(store.sessions[0].reconnect).toEqual({ attempt: 1, maxAttempts: 3 })
    expect(store.sessions[0].reconnectGaveUp).toBeFalsy()
    reconnectCb!({ id: 's1', phase: 'gave-up', attempt: 3, maxAttempts: 3, reason: 'exhausted' })
    expect(store.sessions[0].reconnect).toBeNull()
    expect(store.sessions[0].reconnectGaveUp).toBe(true)
    reconnectCb!({ id: 's1', phase: 'reconnecting', attempt: 1, maxAttempts: 3 })
    reconnectCb!({ id: 's1', phase: 'success', attempt: 1, maxAttempts: 3 })
    expect(store.sessions[0].reconnect).toBeNull()
    expect(store.sessions[0].reconnectGaveUp).toBe(false)
  })

  it('gave-up 的 cancelled/config-missing 清状态不置 gaveUp；未知会话静默丢弃', async () => {
    const store = useSSHStore()
    await store.init()
    seed(store, 's1')
    reconnectCb!({ id: 'ghost', phase: 'reconnecting', attempt: 1, maxAttempts: 3 }) // 不炸
    reconnectCb!({ id: 's1', phase: 'reconnecting', attempt: 1, maxAttempts: 3 })
    reconnectCb!({ id: 's1', phase: 'gave-up', attempt: 0, maxAttempts: 3, reason: 'cancelled' })
    expect(store.sessions[0].reconnect).toBeNull()
    expect(store.sessions[0].reconnectGaveUp).toBe(false)
  })

  it('markActivity 置位；switchSession 清零并 touch MRU 栈顶', async () => {
    const store = useSSHStore()
    await store.init()
    seed(store, 'a', 'b', 'c')
    store.markActivity('b')
    expect(store.sessions.find(s => s.id === 'b')!.hasActivity).toBe(true)
    store.switchSession('a')
    store.switchSession('c')
    store.switchSession('b')
    expect(store.mruIds.slice(0, 3)).toEqual(['b', 'c', 'a'])
    expect(store.sessions.find(s => s.id === 'b')!.hasActivity).toBe(false)
  })

  it('removeSession 同步剔除 MRU 栈', async () => {
    const store = useSSHStore()
    await store.init()
    seed(store, 'a', 'b')
    store.switchSession('a')
    store.switchSession('b')
    store.removeSession('b')
    expect(store.mruIds).not.toContain('b')
    expect(store.mruIds).toContain('a')
  })
})
