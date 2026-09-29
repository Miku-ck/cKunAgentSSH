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
 *
 * 记忆设置字段与后端 MemorySettingsRequest（v2 六项）严格同集合：PUT 是全字段
 * 覆盖，测试里漏断言一项就等于放任面板把它静默重置回默认值。
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
  setDefaultModel: vi.fn(async () => ({ default: '', mode: 'ssh' })),
  testModelConnection: vi.fn(),
}
const agentApiMock = {
  getMemorySettings: vi.fn(),
  updateMemorySettings: vi.fn(async () => ({})),
}
/** 后端 GET /settings/memory 的 v2 响应（六字段，与 MemorySettingsRequest 同集合） */
const MEMORY_V2 = {
  enabled: true, max_items: 100, inject_budget_tokens: 1500,
  blocks_budget_tokens: 1200, search_timeout_s: 8, reconcile_llm_enabled: true,
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
    agentApiMock.getMemorySettings.mockResolvedValue(MEMORY_V2)
  })
  afterEach(() => vi.useRealTimers())

  it('回填不会把值再 POST 回后端', async () => {
    const s = useSettingsStore()
    agentApiMock.getMemorySettings.mockResolvedValue({
      ...MEMORY_V2, enabled: false, max_items: 55, inject_budget_tokens: 900,
      blocks_budget_tokens: 700, search_timeout_s: 3.5, reconcile_llm_enabled: false,
    })
    await s.loadMemorySettings()
    await nextTick()
    expect(s.settings.memory).toEqual({
      enabled: false, maxItems: 55, injectBudgetTokens: 900,
      blocksBudgetTokens: 700, searchTimeoutS: 3.5, reconcileLlmEnabled: false,
    })

    await vi.advanceTimersByTimeAsync(2000)
    expect(agentApiMock.updateMemorySettings).not.toHaveBeenCalled()
  })

  it('响应缺字段或类型不对时保留本地值', async () => {
    const s = useSettingsStore()
    const before = { ...s.settings.memory }
    agentApiMock.getMemorySettings.mockResolvedValue({
      enabled: 'yes', max_items: null, inject_budget_tokens: '1500',
      blocks_budget_tokens: NaN, reconcile_llm_enabled: 1,   // search_timeout_s 缺失
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
      enabled: true, max_items: 42, inject_budget_tokens: 1500,
      blocks_budget_tokens: 1200, search_timeout_s: 8, reconcile_llm_enabled: true,
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

/**
 * R6：sync 失败必须回滚到"上一成功同步"的快照，
 * 否则本地新值与后端旧值长期背离，用户看不到任何异常。
 */
describe('settings sync 失败回滚', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers()
    lsMap.clear()
    for (const fn of Object.values(apiMock)) fn.mockClear()
    for (const fn of Object.values(agentApiMock)) fn.mockClear()
    apiMock.getPostProcessSettings.mockResolvedValue({
      enabled: true, generate_title: true, extract_memory: true, generate_skill: true,
    })
    agentApiMock.getMemorySettings.mockResolvedValue(MEMORY_V2)
    // mockClear 不会重置 base impl，但也不会清掉 once 队列 —— 显式把默认实现装回来
    apiMock.updatePostProcessSettings.mockImplementation(async () => ({}))
    agentApiMock.updateMemorySettings.mockImplementation(async () => ({}))
  })
  afterEach(() => vi.useRealTimers())

  it('memory sync 失败回滚到 loadMemory 建立的基线', async () => {
    const s = useSettingsStore()
    await s.loadMemorySettings()
    await nextTick()
    agentApiMock.updateMemorySettings.mockClear()

    agentApiMock.updateMemorySettings.mockRejectedValueOnce(new Error('500'))
    s.settings.memory.maxItems = 77
    await vi.advanceTimersByTimeAsync(2000)

    expect(s.settings.memory.maxItems).toBe(100)
    // applyingRemoteMemory 闸门吞掉回滚写入 → 只看到 1 次原始 POST
    expect(agentApiMock.updateMemorySettings).toHaveBeenCalledTimes(1)
  })

  it('memory sync 成功后基线推进；再失败回滚到最近一次成功值', async () => {
    const s = useSettingsStore()
    await s.loadMemorySettings()
    await nextTick()
    agentApiMock.updateMemorySettings.mockClear()

    // 第一次 sync 成功：基线 100 → 42
    s.settings.memory.maxItems = 42
    await vi.advanceTimersByTimeAsync(2000)
    expect(s.settings.memory.maxItems).toBe(42)

    // 第二次 sync 失败：回滚到 42 而不是初始 100
    agentApiMock.updateMemorySettings.mockRejectedValueOnce(new Error('boom'))
    s.settings.memory.maxItems = 99
    await vi.advanceTimersByTimeAsync(2000)
    expect(s.settings.memory.maxItems).toBe(42)
    // 两次 POST（一次成功 + 一次失败）；回滚不再叠加第三次
    expect(agentApiMock.updateMemorySettings).toHaveBeenCalledTimes(2)
  })

  it('postProcess sync 失败回滚且不重发 POST', async () => {
    const s = useSettingsStore()
    await s.loadPostProcessSettings()
    await nextTick()
    apiMock.updatePostProcessSettings.mockClear()

    apiMock.updatePostProcessSettings.mockRejectedValueOnce(new Error('500'))
    s.settings.postProcess.enabled = false
    await vi.advanceTimersByTimeAsync(2000)

    expect(s.settings.postProcess.enabled).toBe(true)
    expect(apiMock.updatePostProcessSettings).toHaveBeenCalledTimes(1)
  })
})

/**
 * E4/E5：运维策略字段的默认值与脏值兜底。
 * 这两项是纯本地设置（无后端路由），坏了不会有请求报错，只会静默改变行为：
 * defaultEnvironment 变成任意字符串 → 新连接的 prod 标记不可信；
 * prodConfirm 变成 truthy 的 'no' → 用户以为关了额外确认，实际还开着。
 */
describe('settings 运维策略字段（E4/E5）', () => {
  afterEach(() => vi.useRealTimers())

  it('默认值：新连接 test、prod 额外确认开', () => {
    setActivePinia(createPinia())
    lsMap.clear()
    const s = useSettingsStore()
    expect(s.settings.agent.defaultEnvironment).toBe('test')
    expect(s.settings.agent.prodConfirm).toBe(true)
  })

  it('脏 localStorage 造不出非法环境值与非布尔 prodConfirm', async () => {
    lsMap.set('ckunagentic:settings', JSON.stringify({
      agent: { defaultEnvironment: 'staging', prodConfirm: 'no' },
    }))
    vi.useRealTimers()
    vi.resetModules()
    setActivePinia(createPinia())
    const { useSettingsStore: fresh } = await import('@/stores/settings')
    const s = fresh()
    expect(s.settings.agent.defaultEnvironment).toBe('test')
    expect(s.settings.agent.prodConfirm).toBe(true)
  })

  it('合法保存值原样读回', async () => {
    lsMap.set('ckunagentic:settings', JSON.stringify({
      agent: { defaultEnvironment: 'prod', prodConfirm: false },
    }))
    vi.resetModules()
    setActivePinia(createPinia())
    const { useSettingsStore: fresh } = await import('@/stores/settings')
    const s = fresh()
    expect(s.settings.agent.defaultEnvironment).toBe('prod')
    expect(s.settings.agent.prodConfirm).toBe(false)
  })
})

/**
 * round 22：脏值校验从「只有 agent 两个字段」扩展到全部本地设置键。
 * `??` 只挡 undefined/null，手改（或被写坏的）localStorage 里放 'no' / 0 / {}
 * 就会原样流进 settings：布尔开关变成 truthy 的 'no'（UI 显示关、实际开），
 * 枚举变成面板里没有的值（CSS class 拼不出来，主题静默失效），
 * 数值变成字符串（字号 '13px' 之类的拼接一路带到渲染层）。
 */
async function storeWithSaved(raw: unknown) {
  lsMap.set('ckunagentic:settings', JSON.stringify(raw))
  vi.useRealTimers()
  vi.resetModules()
  setActivePinia(createPinia())
  const { useSettingsStore: fresh } = await import('@/stores/settings')
  return fresh()
}

describe('settings 脏 localStorage 校验（全键）', () => {
  it('枚举键：非法值回落默认，合法值原样保留', async () => {
    const dirty = await storeWithSaved({
      chat: { sendShortcut: 'tab', enableThinking: 'yes' },
      appearance: {
        theme: 'neon', accent: 'cyan',
        themeGradientMode: 'diagonal', themeGradientDirection: 'xx',
      },
    })
    expect(dirty.settings.chat.sendShortcut).toBe('enter')
    expect(dirty.settings.chat.enableThinking).toBe('inherit')
    expect(dirty.settings.appearance.theme).toBe('dark')
    expect(dirty.settings.appearance.accent).toBe('blue')
    expect(dirty.settings.appearance.themeGradientMode).toBe('glow')
    expect(dirty.settings.appearance.themeGradientDirection).toBe('tb')

    const ok = await storeWithSaved({
      chat: { sendShortcut: 'shiftEnter', enableThinking: 'off' },
      appearance: {
        theme: 'forest', accent: 'green',
        themeGradientMode: 'linear', themeGradientDirection: 'lr',
      },
    })
    expect(ok.settings.chat.sendShortcut).toBe('shiftEnter')
    expect(ok.settings.chat.enableThinking).toBe('off')
    expect(ok.settings.appearance.theme).toBe('forest')
    expect(ok.settings.appearance.themeGradientDirection).toBe('lr')
  })

  it('布尔键：字符串/数字/对象都不算开，默认 false 的键不会被 truthy 脏值打开', async () => {
    const s = await storeWithSaved({
      chat: { markdown: 'false', autoScroll: 0, thinkingExpanded: {}, autoSkillMatch: 'no' },
      agent: { dynamicBudget: 'yes', enableIntentionGuide: 1 },
      postProcess: { enabled: 'no' },
      memory: { enabled: 0, reconcileLlmEnabled: 'true' },
      appearance: { animation: 'off', backgroundEnabled: 'yes', themeGradientEnabled: {} },
    })
    // 默认开的键：脏值 → 回到 true（而不是变成 'no' 这种 truthy 假关）
    expect(s.settings.chat.markdown).toBe(true)
    expect(s.settings.agent.dynamicBudget).toBe(true)
    expect(s.settings.postProcess.enabled).toBe(true)
    expect(s.settings.memory.enabled).toBe(true)
    // 默认关的键：脏值 → 回到 false（'yes' 绝不能把背景打开）
    expect(s.settings.appearance.backgroundEnabled).toBe(false)
    expect(s.settings.appearance.themeGradientEnabled).toBe(false)
    expect(s.settings.chat.autoScroll).toBe(true)
    expect(s.settings.appearance.animation).toBe(true)
  })

  it('数值键：非有限数与越界回默认（不钳边界），区间内合法值保留', async () => {
    const s = await storeWithSaved({
      chat: { messageFontSize: '13', contextWindow: 'abc' },
      agent: { maxIterations: '50', maxCostUsd: -1, maxTotalTokens: {} },
      memory: { maxItems: -5, injectBudgetTokens: 'x' },
      appearance: {
        uiFontSize: null, backgroundOpacity: 101, backgroundOverlay: -20,
        themeGradientStrength: 999, backgroundVersion: 'v2',
        backgroundImage: 42, themeGradientStartColor: {},
        terminal: { fontSize: 0, fontFamily: 42 },
      },
    })
    expect(s.settings.chat.messageFontSize).toBe(13)
    expect(s.settings.chat.contextWindow).toBe(20)
    expect(s.settings.agent.maxIterations).toBe(100)
    expect(s.settings.agent.maxCostUsd).toBe(0)
    expect(s.settings.memory.maxItems).toBe(100)
    expect(s.settings.memory.injectBudgetTokens).toBe(1500)
    expect(s.settings.appearance.uiFontSize).toBe(13)
    expect(s.settings.appearance.backgroundOpacity).toBe(80)
    expect(s.settings.appearance.backgroundOverlay).toBe(60)
    expect(s.settings.appearance.themeGradientStrength).toBe(30)
    expect(s.settings.appearance.backgroundVersion).toBe(0)
    // 字符串键同规矩：CSS url() 与颜色值里塞进数字/对象会让整条背景样式失效
    expect(s.settings.appearance.backgroundImage).toBe('')
    expect(s.settings.appearance.themeGradientStartColor).toBe('')
    expect(s.settings.appearance.terminal.fontSize).toBe(14)
    expect(s.settings.appearance.terminal.fontFamily).toBe('jetbrains')

    const ok = await storeWithSaved({
      chat: { messageFontSize: 14, contextWindow: 50 },
      agent: { maxIterations: 1, maxCostUsd: 2.5 },
      appearance: {
        uiFontSize: 15, backgroundOpacity: 30,
        terminal: { fontSize: 16, fontFamily: 'mono' },
      },
    })
    expect(ok.settings.chat.messageFontSize).toBe(14)
    expect(ok.settings.chat.contextWindow).toBe(50)
    expect(ok.settings.agent.maxIterations).toBe(1)
    expect(ok.settings.agent.maxCostUsd).toBe(2.5)
    expect(ok.settings.appearance.uiFontSize).toBe(15)
    expect(ok.settings.appearance.backgroundOpacity).toBe(30)
    expect(ok.settings.appearance.terminal.fontSize).toBe(16)
    expect(ok.settings.appearance.terminal.fontFamily).toBe('mono')
  })

  it('联合类型数值键（messageFontSize）只认面板真有的档位，区间内非档位值同样回落', async () => {
    // 类型是 12|13|14|15，用「区间夹紧 + as 断言」放行 13.5 就是把类型谎写进运行时：
    // 面板 select 里没有这一项，徽标显示空白、字号却真的生效了。
    const s = await storeWithSaved({
      chat: { messageFontSize: 13.5 },
      appearance: { uiFontSize: 16, terminal: { fontSize: 13.7 } },
    })
    expect(s.settings.chat.messageFontSize).toBe(13)
    // 界面字号的下拉只到 15，放行 16 会让 select 显示空白却按 16px 生效
    expect(s.settings.appearance.uiFontSize).toBe(13)
    // terminal.fontSize 类型是宽 number（面板给 12..18 连续档），区间合法就保留
    expect(s.settings.appearance.terminal.fontSize).toBe(13.7)
  })
})

/**
 * A1：卡片级"设为 SSH 默认"入口。store 负责 PATCH + 重拉列表，
 * UI 侧靠 modelConfigs 响应式派生显示，无须自己再拉一次 default。
 */
describe('settings setDefaultModel', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers()
    lsMap.clear()
    for (const fn of Object.values(apiMock)) fn.mockClear()
    for (const fn of Object.values(agentApiMock)) fn.mockClear()
    apiMock.setDefaultModel.mockImplementation(async () => ({ default: 'm2', mode: 'ssh' }))
    // vi.fn 从初始 `async () => []` 推出 never[]；显式断言绕开 mockResolvedValue 的窄化
    apiMock.getModelConfigs.mockResolvedValue([
      { id: 'm1', name: 'A', is_default_ssh: false },
      { id: 'm2', name: 'B', is_default_ssh: true },
    ] as never)
  })
  afterEach(() => vi.useRealTimers())

  it('成功后刷新列表，is_default_ssh 落到新条目', async () => {
    const s = useSettingsStore()
    const ok = await s.setDefaultModel('m2')
    expect(ok).toBe(true)
    expect(apiMock.setDefaultModel).toHaveBeenCalledWith('m2', 'ssh')
    expect(apiMock.getModelConfigs).toHaveBeenCalled()
    expect(s.modelConfigs.find((m) => m.id === 'm2')?.is_default_ssh).toBe(true)
    expect(s.modelsError).toBe('')
  })

  /**
   * round 22 / C9：全局默认（is_default）一直有活消费者（后处理、对话附件摘要等
   * 非 SSH 链路），但 UI 只能写 is_default_ssh，前端永远改不动那一档。
   * store 必须把档位透传给后端，不能吞掉。
   */
  it('global 档位原样透传给后端 API', async () => {
    const s = useSettingsStore()
    const ok = await s.setDefaultModel('m2', 'global')
    expect(ok).toBe(true)
    expect(apiMock.setDefaultModel).toHaveBeenCalledWith('m2', 'global')
    expect(apiMock.getModelConfigs).toHaveBeenCalled()
  })

  it('404 等后端拒绝仍走失败路径，不因档位不同而吞错', async () => {
    const s = useSettingsStore()
    apiMock.setDefaultModel.mockRejectedValueOnce(new Error('404 模型不存在: ghost'))
    const ok = await s.setDefaultModel('ghost', 'global')
    expect(ok).toBe(false)
    expect(s.modelsError).toContain('404')
  })

  it('失败时置 modelsError，返回 false 且列表保持原样', async () => {
    const s = useSettingsStore()
    // 先建立初始列表
    await s.fetchModelConfigs()
    expect(s.modelConfigs.length).toBe(2)
    apiMock.getModelConfigs.mockClear()

    apiMock.setDefaultModel.mockRejectedValueOnce(new Error('404 Model not found'))
    const ok = await s.setDefaultModel('missing')
    expect(ok).toBe(false)
    expect(s.modelsError).toContain('404')
    // 失败路径不再拉列表：后端没落库，重拉只会拿到旧状态误导用户
    expect(apiMock.getModelConfigs).not.toHaveBeenCalled()
  })
})

/**
 * C3：appearance.fontSize 重命名为 uiFontSize（它与 chat.messageFontSize、

 * appearance.terminal.fontSize 三套字号同名不同物）。老用户的 localStorage 里
 * 只有旧 key，读不到就等于把人家调过的字号打回 13。
 */
describe('settings 界面字号旧 key 兼容', () => {
  const readUiFontSize = async (raw: unknown): Promise<number> => {
    lsMap.set('ckunagentic:settings', JSON.stringify(raw))
    vi.useRealTimers()
    vi.resetModules()
    setActivePinia(createPinia())
    const { useSettingsStore: fresh } = await import('@/stores/settings')
    return fresh().settings.appearance.uiFontSize
  }

  afterEach(() => vi.useRealTimers())

  it('只有旧 fontSize 时按旧值读回', async () => {
    expect(await readUiFontSize({ appearance: { fontSize: 15 } })).toBe(15)
  })

  it('新旧并存时以新 key 为准', async () => {
    expect(await readUiFontSize({
      appearance: { fontSize: 12, uiFontSize: 14 },
    })).toBe(14)
  })

  it('两者都没有时落回默认 13', async () => {
    expect(await readUiFontSize({ appearance: { theme: 'dark' } })).toBe(13)
  })

  it('保存后落盘的是新 key，旧 key 不再回写', async () => {
    lsMap.set('ckunagentic:settings', JSON.stringify({ appearance: { fontSize: 15 } }))
    vi.useFakeTimers()
    vi.resetModules()
    setActivePinia(createPinia())
    const { useSettingsStore: fresh } = await import('@/stores/settings')
    const s = fresh()
    s.settings.appearance.uiFontSize = 14
    await vi.advanceTimersByTimeAsync(1000)   // debounce 300ms 后全量序列化
    const raw = lsMap.get('ckunagentic:settings') ?? ''
    expect(raw).toContain('"uiFontSize":14')
    // 全量重写：appearance 下的旧 fontSize 不会被带回来（terminal.fontSize 是另一套，照常保留）
    const appearance = (JSON.parse(raw) as { appearance: Record<string, unknown> }).appearance
    expect('fontSize' in appearance).toBe(false)
  })
})
