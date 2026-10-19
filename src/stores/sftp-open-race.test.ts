/**
 * 打开远程会话的并发代次回归。
 *
 * connect / connectFromTerminal 要等一整套握手 + 取 cwd + 列目录，期间 `online` 还是
 * false，标题栏 SFTP 图标与页内「连接远程」都照常可点 ⇒ 两次打开并发。没有代次判据时
 * 落败者会：① 把自己的失败写成全局的离线（`sshSessionId = ''`），而真正活着的那条会话
 * 因此再也关不掉 —— disconnect 见空串直接跳过 sftpClose/sshDisconnect，主进程通道泄漏；
 * ② 或在更慢的那条成功落地时，把 A 机 cwd 写进 remoteCwd 而列表读的是 B 机 —— 地址栏
 * 与文件分属两台主机，随后的上传/删除全打到另一台。
 * 修法沿用本 store 既有的 remoteLoadSeq 手法：入口取代次，每个 await 之后比对。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('./toast', () => ({
  useToastStore: () => ({ info: vi.fn(), warning: vi.fn(), error: vi.fn(), success: vi.fn() }),
}))
vi.mock('@/api/client', () => ({
  toFriendlyMessage: (e: unknown) => String(e),
  unwrapIpcErrorEnvelope: (s: string) => s,
}))

import { useSftpStore } from './sftp'

type FakeApi = Record<string, ReturnType<typeof vi.fn>>

function makeApi(overrides: Record<string, unknown> = {}): FakeApi {
  const base: FakeApi = {}
  for (const [k, v] of Object.entries(overrides)) base[k] = v as ReturnType<typeof vi.fn>
  return new Proxy(base, {
    get(target, prop: string) {
      if (!(prop in target)) target[prop] = vi.fn(async () => [])
      return target[prop]
    },
  })
}

function installApi(api: FakeApi): void {
  ;(globalThis as unknown as { window: unknown }).window = { electronAPI: api }
}

describe('sftp store — 打开会话的并发代次', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('落败的那次打开不得抹掉接管者的会话身份，只回收自己那条子系统', async () => {
    let releaseA!: () => void
    const slowA = new Promise<void>(r => { releaseA = r })
    const closed: string[] = []
    installApi(makeApi({
      sftpOpen: vi.fn(async (id: string) => {
        if (id === 'A') {
          await slowA
          throw new Error('A 的子系统打开失败')
        }
      }),
      sftpClose: vi.fn(async (id: string) => { closed.push(id) }),
      sshGetCwd: vi.fn(async (id: string) => (id === 'A' ? '/from-a' : '/from-b')),
      sftpList: vi.fn(async () => []),
    }))

    const store = useSftpStore()
    const pA = store.connectFromTerminal('A', '主机A', 'a:22').catch(() => {})
    const pB = store.connectFromTerminal('B', '主机B', 'b:22')
    await pB
    releaseA()
    await pA

    // 接管者仍是当前会话：身份不被旧代的失败抹成空串，否则 disconnect 会跳过关闭
    expect(store.sshSessionId).toBe('B')
    expect(store.online).toBe(true)
    expect(store.connectionHost).toBe('b:22')
    expect(store.remoteCwd).toBe('/from-b')
    // 只回收旧代自己那条，接管者那条不能被关掉
    expect(closed).toEqual(['A'])
  })

  it('断开后在途的那次打开不得把在线态重新点亮', async () => {
    let releaseC!: () => void
    const slowC = new Promise<void>(r => { releaseC = r })
    installApi(makeApi({
      sftpOpen: vi.fn(async () => { await slowC }),
      sshGetCwd: vi.fn(async () => '/from-c'),
      sftpList: vi.fn(async () => []),
    }))

    const store = useSftpStore()
    const p = store.connectFromTerminal('C', '主机C', 'c:22').catch(() => {})
    await store.disconnect()
    releaseC()
    await p

    expect(store.online).toBe(false)
    expect(store.sshSessionId).toBe('')
    expect(store.remoteCwd).toBe('')
  })

  it('独立建连失败只清自己那条身份，不动后起会话', async () => {
    let releaseFirst!: () => void
    const slowFirst = new Promise<void>(r => { releaseFirst = r })
    installApi(makeApi({
      sshConnectSftp: vi.fn(async () => ({ id: 'conn-1', name: 'n1' })),
      sftpOpen: vi.fn(async () => { await slowFirst; throw new Error('子系统打不开') }),
      sftpList: vi.fn(async () => []),
    }))

    const store = useSftpStore()
    const p1 = store.connect({ username: 'u', host: 'h1', port: 22 } as never).catch(() => {})
    releaseFirst()
    await p1
    expect(store.online).toBe(false)
    expect(store.sshSessionId).toBe('')
  })
})
