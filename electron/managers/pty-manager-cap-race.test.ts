/**
 * E-4 本地终端上限的并发回归：检查与占用之间隔着 await，就会被并发穿透。
 *
 * 原实现在 create() 开头读一次 `sessions.size >= MAX_PTY_SESSIONS`，之后要经过
 * `fsPromises.stat` 与 `loadPty()` 两个 await 才 `sessions.set`。一次恢复多个标签
 * （渲染层并发发 pty:create）时，N 个调用全部读到同一个未越限的 size，于是无界
 * spawn —— 那道"防止渲染进程无界 spawn"的门等于没有。
 *
 * 修法是在 spawn 前（await 之后、set 之前的同步段）复查。这里用 31 个预置会话 +
 * 两个并发 create 钉住这条：只允许起一个，第二个必须被上限拒掉。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('electron', () => ({
  BrowserWindow: class {},
  app: { getPath: () => '/tmp/ckun-agent-pty-test' },
}))

// create() 先 stat(cwd) 校验目录存在，再 loadPty()，两步都是 await
vi.mock('fs', () => ({
  promises: { stat: async () => ({ isDirectory: () => true }) },
}))

const pty = vi.hoisted(() => {
  const state = { spawnCalls: 0 }
  return {
    state,
    loadPty: () => import('node-pty'),
    spawn: vi.fn(() => {
      state.spawnCalls += 1
      const fake = {
        onData: () => fake,
        onExit: () => fake,
        write: () => {},
        kill: () => {},
        pid: 4242,
      }
      return fake
    }),
  }
})

vi.mock('node-pty', () => ({ spawn: pty.spawn }))

import { PtyManager } from './pty-manager'

const MAX_PTY_SESSIONS = 32

describe('pty 会话上限（E-4）', () => {
  let mgr: PtyManager
  beforeEach(() => {
    pty.state.spawnCalls = 0
    pty.spawn.mockClear()
    mgr = new PtyManager()
    mgr.setWindow({ webContents: { send: () => {} } } as never)
  })

  it('并发 create 不得穿透上限：预置 31 个会话时起 1 个、拒 1 个', async () => {
    const pool = (mgr as unknown as { sessions: Map<string, unknown> }).sessions
    for (let i = 0; i < MAX_PTY_SESSIONS - 1; i++) pool.set(`fill-${i}`, { id: `fill-${i}` })

    const settled = await Promise.allSettled([mgr.create(), mgr.create()])
    const fulfilled = settled.filter(r => r.status === 'fulfilled')
    const rejected = settled.filter(r => r.status === 'rejected')

    // 两次调用在第一个 await 之前都读到 size=31（未越限），差别只在 spawn 前的复查
    expect(fulfilled).toHaveLength(1)
    expect(rejected).toHaveLength(1)
    expect(String((rejected[0] as PromiseRejectedResult).reason)).toMatch(/数量已达上限/)
    expect(pty.state.spawnCalls).toBe(1)
    expect(pool.size).toBe(MAX_PTY_SESSIONS)
  })

  it('已到上限时直接拒绝，不再尝试 spawn', async () => {
    const pool = (mgr as unknown as { sessions: Map<string, unknown> }).sessions
    for (let i = 0; i < MAX_PTY_SESSIONS; i++) pool.set(`full-${i}`, { id: `full-${i}` })

    await expect(mgr.create()).rejects.toThrow(/数量已达上限/)
    expect(pty.state.spawnCalls).toBe(0)
  })
})
