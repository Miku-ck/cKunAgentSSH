/**
 * 启动可靠性回归 — appStore.readyPromise 是初始数据加载的唯一就绪闸门。
 *
 * 原缺口：ssh-ai store 工厂、首页 Composer onMounted、App onMounted 都在后端还没
 * 通过健康检查时就 fire-and-forget 发 GET，port=0 让 getBaseUrl 抛错、渲染层每次首屏
 * 打 3 条"后端服务未就绪"的 warn。这些调用没有共享的就绪信号可等——主进程的
 * whenBackendReady（走 /api/health 轮询）已在 appStore 里 await，但没对外暴露。
 *
 * 钉的行为：appStore 把就绪信号收敛成一个闸门 promise，
 *   - 后端就绪 → resolve(端口)，status=ready
 *   - backend:status 事件先到 ready → 同样 resolve（事件与 IPC 双路，幂等）
 *   - 本次启动失败 → 不 reject、不锁死闸门（status=error）；崩溃自动重启成功后的 ready
 *     事件仍开启它 —— 否则"首启失败→重启成功"窄窗口会永久跳过初始加载（P2 回归）
 *   - 无 electronAPI（浏览器 dev）→ 立即 resolve，不能被 gate 卡死首屏
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useAppStore } from './app'

type StatusCb = (data: { status: string; port?: number; error?: string }) => void

function makeElectronApi(overrides: Record<string, unknown> = {}) {
  const base: Record<string, unknown> = {
    // 默认给一条可控的 whenBackendReady + 捕获 status 回调
    onBackendStatus: vi.fn((cb: StatusCb) => {
      ;(base as { __statusCb: unknown }).__statusCb = cb
      return vi.fn()
    }),
    getBackendPort: vi.fn(async () => 0),
    whenBackendReady: vi.fn(() => new Promise<number>(() => {})), // 默认挂起
  }
  return Object.assign(base, overrides) as Record<string, unknown> & {
    __statusCb?: StatusCb
  }
}

function setWindow(api: unknown) {
  ;(globalThis as unknown as { window: unknown }).window = api === undefined ? {} : { electronAPI: api }
}

const flush = () => new Promise((r) => setTimeout(r, 0))

describe('appStore.readyPromise — 初始加载的就绪闸门', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('后端就绪：whenBackendReady resolve → readyPromise 带端口 resolve、status=ready', async () => {
    let resolveReady!: (p: number) => void
    const api = makeElectronApi({
      whenBackendReady: vi.fn(() => new Promise<number>((r) => { resolveReady = r })),
    })
    setWindow(api)
    const store = useAppStore()

    expect(store.backendStatus).toBe('starting')
    resolveReady(8010)
    await expect(store.readyPromise).resolves.toBe(8010)
    expect(store.backendStatus).toBe('ready')
    expect(store.backendPort).toBe(8010)
  })

  it('本次启动失败不锁死闸门：reject 后 status=error 且闸门仍待决，自动重启的 ready 事件照样开启', async () => {
    const api = makeElectronApi({
      whenBackendReady: vi.fn(() => Promise.reject(new Error('spawn failed'))),
    })
    setWindow(api)
    const store = useAppStore()

    await flush() // 让 whenBackendReady 的 reject 微任务跑完
    expect(store.backendStatus).toBe('error')

    // 闸门未被一次性 reject 锁死：后端崩溃自动重启成功后发来 ready 事件，应开启它
    let resolvedPort = -1
    void store.readyPromise.then((p) => { resolvedPort = p })
    api.__statusCb?.({ status: 'ready', port: 8066 })
    await expect(store.readyPromise).resolves.toBe(8066)
    expect(resolvedPort).toBe(8066)
    expect(store.backendStatus).toBe('ready')
  })

  it('backend:status ready 事件先于 IPC 到达 → 同样 resolve（双路幂等）', async () => {
    const api = makeElectronApi({
      whenBackendReady: vi.fn(() => new Promise<number>(() => {})), // 永不 resolve，逼事件路径生效
    })
    setWindow(api)
    const store = useAppStore()

    api.__statusCb?.({ status: 'ready', port: 8055 })
    await expect(store.readyPromise).resolves.toBe(8055)
    expect(store.backendStatus).toBe('ready')
  })

  it('无 electronAPI（浏览器 dev）：readyPromise 立即 resolve，不卡首屏', async () => {
    setWindow(undefined)
    const store = useAppStore()
    await expect(store.readyPromise).resolves.toBeDefined()
  })
})
