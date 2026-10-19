/**
 * 主机密钥通知监听的清理必须真的登记上。
 *
 * `init()` 是 action，`_setupHostKeyNotifications()` 排在它两个 await 之后。
 * Pinia 只在 store setup 的**同步段** `scope.run(...)`，await 之后 activeEffectScope
 * 早就还原了 —— 那时裸调 onScopeDispose 会：
 *   ① 冒 "[Vue warn] onScopeDispose() is called when there is no active effect scope"
 *      （每次进 SSH 页 init 一次，只在有 electronAPI 时出现，所以浏览器 dev 看不到）；
 *   ② 更要紧：清理函数**不登记**，三条 hostKey IPC 监听在 $dispose/HMR 后永远摘不掉。
 * 修法：在 setup 同步段抓住 scope，登记时用它 run() 回去（run 会把 scope 重新设为活跃）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

// 测试跑在 node 环境（vitest.config.ts），补 window/localStorage 最小垫片
const offNew = vi.fn()
const offMismatch = vi.fn()
const offUnpinned = vi.fn()
const onHostKeyNew = vi.fn(() => offNew)
const onHostKeyMismatch = vi.fn(() => offMismatch)
const onHostKeyUnpinned = vi.fn(() => offUnpinned)
const sshListConnections = vi.fn(async () => [])

const g = globalThis as unknown as Record<string, unknown>
g.window = {
  electronAPI: { onHostKeyNew, onHostKeyMismatch, onHostKeyUnpinned, sshListConnections },
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

describe('ssh store 主机密钥监听的 scope 归属', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('init() 注册三条监听', async () => {
    const store = useSSHStore()
    await store.init()
    expect(onHostKeyNew).toHaveBeenCalledTimes(1)
    expect(onHostKeyMismatch).toHaveBeenCalledTimes(1)
    expect(onHostKeyUnpinned).toHaveBeenCalledTimes(1)
  })

  it('$dispose() 把三条监听都摘掉', async () => {
    const store = useSSHStore()
    await store.init()
    store.$dispose()
    expect(offNew).toHaveBeenCalledTimes(1)
    expect(offMismatch).toHaveBeenCalledTimes(1)
    expect(offUnpinned).toHaveBeenCalledTimes(1)
  })

  it('init() 之后不再有"无活跃 effect scope"告警', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const store = useSSHStore()
      await store.init()
      store.$dispose()
      expect(warn.mock.calls.flat().join(' ')).not.toMatch(/no active effect scope/)
    } finally {
      warn.mockRestore()
    }
  })
})
