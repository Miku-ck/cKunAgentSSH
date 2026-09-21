/**
 * settings store — 后端回填与本地持久化的契约测试（本轮修复防回归）
 *
 * 后端 loadXxxSettings 把值写进 settings 会被 deep watch 当成"用户改动"再 POST
 * 回去；响应缺字段时更会把 undefined 落进本地设置并推给后端。三条约束：
 * - 回填不回写（updateMemorySettings / updatePostProcessSettings 不被调用）
 * - 形状不对的字段被丢弃，本地值原样保留
 * - 用户真改动仍然 debounce 同步 + 落 localStorage
 *
 * 测试跑在 node 环境，故先补 localStorage / window 两个最小垫片。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const lsMap = new Map<string, string>()
const g = globalThis as unknown as Record<string, unknown>
g.localStorage = {
  getItem: (k: string) => lsMap.get(k) ?? null,
  setItem: (k: string, v: string) => { lsMap.set(k, v) },
  removeItem: (k: string) => { lsMap.delete(k) },
}
g.window = {
  setTimeout: (fn: () => void, ms: number) => setTimeout(fn, ms),
  clearTimeout: (id: unknown) => clearTimeout(id as number),
}

const apiMock = {
  getPostProcessSettings: vi.fn(),
  updatePostProcessSettings: vi.fn(async () => ({})),
  getModelConfigs: vi.fn(async () => []),
  createModelConfig: vi.fn(),
  updateModelConfig: vi.fn(),
  deleteModelConfig: vi.fn(),
  testModelConnection: vi.fn(),
}
const agentApiMock = {
  getMemorySettings: vi.fn(),
  updateMemorySettings: vi.fn(async () => ({})),
}

vi.mock('@/api/client', () => ({
  api: apiMock,
  agentApi: agentApiMock,
  toFriendlyMessage: (_e: unknown, fallback: string) => fallback,
}))
vi.mock('@/stores/toast', () => ({
  useToastStore: () => ({ error: vi.fn(), warning: vi.fn(), success: vi.fn() }),
}))

const { useSettingsStore } = await import('@/stores/settings')

describe('settings 后端回填', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers()
    lsMap.clear()
    for (const fn of Object.values(apiMock)) fn.mockClear()
    for (const fn of Object.values(agentApiMock)) fn.mockClear()
    apiMock.getPostProcessSettings.mockResolvedValue({
      enabled: true, generate_title: true, extract_memory: true, generate_skill: true,
    })
    agentApiMock.getMemorySettings.mockResolvedValue({
      enabled: true, strategy: 'auto', max_items: 100,
    })
  })
  afterEach(() => vi.useRealTimers())

  it('回填不会把值再 POST 回后端', async () => {
    const s = useSettingsStore()
    agentApiMock.getMemorySettings.mockResolvedValue({
      enabled: false, strategy: 'manual', max_items: 55,
    })
    await s.loadMemorySettings()
    await nextTick()
    expect(s.settings.memory).toEqual({ enabled: false, strategy: 'manual', maxItems: 55 })

    await vi.advanceTimersByTimeAsync(2000)
    expect(agentApiMock.updateMemorySettings).not.toHaveBeenCalled()
  })

  it('响应缺字段或类型不对时保留本地值', async () => {
    const s = useSettingsStore()
    const before = { ...s.settings.memory }
    agentApiMock.getMemorySettings.mockResolvedValue({
      enabled: 'yes', strategy: 'telepathy', max_items: null,
    })
    await s.loadMemorySettings()
    await nextTick()
    expect(s.settings.memory).toEqual(before)

    await vi.advanceTimersByTimeAsync(2000)
    expect(agentApiMock.updateMemorySettings).not.toHaveBeenCalled()
  })

  it('用户改动仍照常同步到后端并落本地', async () => {
    const s = useSettingsStore()
    await s.loadMemorySettings()
    await nextTick()
    agentApiMock.updateMemorySettings.mockClear()

    s.settings.memory.maxItems = 42
    await vi.advanceTimersByTimeAsync(2000)
    expect(agentApiMock.updateMemorySettings).toHaveBeenCalledWith({
      enabled: true, strategy: 'auto', max_items: 42,
    })
    expect(lsMap.get('ckunagentic:settings')).toContain('"maxItems":42')
  })

  it('后处理回填同样不回写，且接受合法值', async () => {
    const s = useSettingsStore()
    apiMock.getPostProcessSettings.mockResolvedValue({
      enabled: false, generate_title: true, extract_memory: false, generate_skill: true,
    })
    await s.loadPostProcessSettings()
    await nextTick()
    expect(s.settings.postProcess).toEqual({
      enabled: false, generateTitle: true, extractMemory: false, generateSkill: true,
    })

    await vi.advanceTimersByTimeAsync(2000)
    expect(apiMock.updatePostProcessSettings).not.toHaveBeenCalled()
  })

  it('响应缺字段时后处理保留本地值', async () => {
    const s = useSettingsStore()
    const before = { ...s.settings.postProcess }
    apiMock.getPostProcessSettings.mockResolvedValue({ enabled: 'nope' })
    await s.loadPostProcessSettings()
    await nextTick()
    expect(s.settings.postProcess).toEqual(before)
  })
})
