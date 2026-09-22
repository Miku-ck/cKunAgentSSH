/**
 * useOpsSettings — 运维设置（G4/G5/G9/G10）的读写契约。
 *
 * 最关键的一条是第 2 例：保存超时/重试时 payload 绝不能带 proxy_url。
 * GET 只回脱敏代理（口令不可回显），一旦带上空串就等于"清除代理"，
 * 用户只想调超时会把代理口令抹掉，且要到下次请求失败才发现。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const g = globalThis as unknown as Record<string, unknown>
g.window = {
  setTimeout: (fn: () => void, ms: number) => setTimeout(fn, ms),
  clearTimeout: (id: unknown) => clearTimeout(id as number),
}

const apiMock = {
  getHttpSettings: vi.fn(),
  updateHttpSettings: vi.fn(),
  getLogLevel: vi.fn(),
  updateLogLevel: vi.fn(),
  getDiagnostics: vi.fn(),
}
const mockToast = { error: vi.fn(), warning: vi.fn(), success: vi.fn() }

vi.mock('@/api/client', () => ({
  api: apiMock,
  toFriendlyMessage: (e: unknown, fallback: string) =>
    e instanceof Error ? e.message : fallback,
}))
vi.mock('@/stores/toast', () => ({ useToastStore: () => mockToast }))

const { useOpsSettings, TIMEOUT_MAX } = await import('@/composables/useOpsSettings')

const httpView = (over = {}) => ({
  proxy_url_masked: 'http://corp-proxy:3128',
  proxy_url_set: true,
  proxy_source: 'db',
  timeout_seconds: 45,
  retry_count: 2,
  ...over,
})
const levelView = (over = {}) => ({
  level: 'warning',
  levels: ['debug', 'info', 'warning', 'error', 'critical'],
  ...over,
})

function diagView(over = {}) {
  return {
    backend: { version: '0.1.0', python: '3.12.7', platform: 'win32', pid: 4242 },
    database: { path: 'D:/x/ckun.db', size_bytes: 2097152, counts: { messages: 12 } },
    logging: levelView({ level: 'info' }),
    http: { proxy: 'socks5h://corp:1080', proxy_set: true, timeout_seconds: 60, retry_count: 1 },
    tick: { running: true, last_result: { summary: 'ok' } },
    bridge: { connected: true, present: true },
    permissions: { ttl_hours: 24, permanent_entries: 3 },
    app_settings: { 'web.tavily_api_key': 'tvly…mnop' },
    ...over,
  } as never
}

describe('useOpsSettings', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    for (const fn of Object.values(apiMock)) fn.mockReset()
    for (const fn of Object.values(mockToast)) fn.mockReset()
    apiMock.getHttpSettings.mockResolvedValue(httpView())
    apiMock.getLogLevel.mockResolvedValue(levelView())
    apiMock.updateHttpSettings.mockImplementation(async (p: Record<string, unknown>) => httpView(p))
    apiMock.updateLogLevel.mockImplementation(async (l: string) => levelView({ level: l }))
    apiMock.getDiagnostics.mockResolvedValue(diagView())
  })

  it('load 把脱敏代理与生效值一并读回', async () => {
    const o = useOpsSettings()
    await o.load()
    expect(o.proxyMasked.value).toBe('http://corp-proxy:3128')
    expect(o.proxySet.value).toBe(true)
    expect(o.timeoutDraft.value).toBe(45)
    expect(o.retryDraft.value).toBe(2)
    expect(o.logLevel.value).toBe('warning')
    expect(o.loadError.value).toBe('')
  })

  it('保存超时/重试时不带 proxy_url 字段', async () => {
    const o = useOpsSettings()
    await o.load()
    apiMock.updateHttpSettings.mockClear()
    o.timeoutDraft.value = 90
    o.retryDraft.value = 0
    expect(o.httpDirty.value).toBe(true)
    expect(await o.saveHttp()).toBe(true)
    expect(apiMock.updateHttpSettings).toHaveBeenCalledTimes(1)
    expect(apiMock.updateHttpSettings.mock.calls[0][0]).toEqual({
      timeout_seconds: 90, retry_count: 0,
    })
  })

  it('保存失败回滚到最近一次成功值，且不吞掉错误原因', async () => {
    const o = useOpsSettings()
    await o.load()
    apiMock.updateHttpSettings.mockRejectedValueOnce(new Error('后端拒绝'))
    o.timeoutDraft.value = 120
    expect(await o.saveHttp()).toBe(false)
    expect(o.timeoutDraft.value).toBe(45)
    expect(o.timeoutText.value).toBe('45')
    expect(mockToast.error).toHaveBeenCalledWith(expect.stringContaining('后端拒绝'), 5000)
  })

  it('输入框清空不等于把超时设成 0 秒', async () => {
    const o = useOpsSettings()
    await o.load()
    o.timeoutText.value = ''
    await nextTick()
    // 空串走 NaN：判定无效，保存按钮被禁用，不会误发 0
    expect(Number.isNaN(o.timeoutDraft.value)).toBe(true)
    expect(o.timeoutInvalid.value).toBe(true)
  })

  it('越界超时判定为无效（与后端 le=600 同口径）', async () => {
    const o = useOpsSettings()
    await o.load()
    o.timeoutText.value = String(TIMEOUT_MAX + 1)
    await nextTick()
    expect(o.timeoutInvalid.value).toBe(true)
  })

  it('代理草稿为空时拒绝保存并提示走清除', async () => {
    const o = useOpsSettings()
    await o.load()
    o.proxyDraft.value = '   '
    expect(await o.saveProxy()).toBe(false)
    expect(apiMock.updateHttpSettings).not.toHaveBeenCalled()
    expect(mockToast.warning).toHaveBeenCalled()
  })

  it('清除代理是显式动作，payload 为 proxy_url 空串', async () => {
    const o = useOpsSettings()
    await o.load()
    apiMock.updateHttpSettings.mockImplementation(async () => httpView({
      proxy_url_masked: '', proxy_url_set: false, proxy_source: '',
    }))
    expect(await o.clearProxy()).toBe(true)
    expect(apiMock.updateHttpSettings.mock.calls[0][0]).toEqual({ proxy_url: '' })
    expect(o.proxySet.value).toBe(false)
  })

  it('日志级别失败回滚到原值', async () => {
    const o = useOpsSettings()
    await o.load()
    apiMock.updateLogLevel.mockRejectedValueOnce(new Error('500'))
    expect(await o.setLogLevel('debug')).toBe(false)
    expect(o.logLevel.value).toBe('warning')
  })

  it('诊断快照回填生效值', async () => {
    const o = useOpsSettings()
    await o.load()
    expect(await o.refreshDiagnostics()).toBe(true)
    expect(o.logLevel.value).toBe('info')
    expect(o.timeoutDraft.value).toBe(60)
    expect(o.proxyMasked.value).toBe('socks5h://corp:1080')
    expect(o.diagnostics.value?.database.path).toBe('D:/x/ckun.db')
  })

  it('诊断失败只标记本区错误，不清空已有快照', async () => {
    const o = useOpsSettings()
    await o.refreshDiagnostics()
    apiMock.getDiagnostics.mockRejectedValueOnce(new Error('网络中断'))
    expect(await o.refreshDiagnostics()).toBe(false)
    expect(o.diagError.value).toContain('网络中断')
    expect(o.diagnostics.value).not.toBeNull()
  })

  it('后端读回脏级别时不采纳未知值', async () => {
    const o = useOpsSettings()
    apiMock.getLogLevel.mockResolvedValue(levelView({ level: 'trace' }))
    await o.load()
    expect(o.logLevel.value).toBe('warning')
  })
})
