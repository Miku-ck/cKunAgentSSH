import { defineStore } from 'pinia'
import { nextTick, ref, watch } from 'vue'
import { api, agentApi, toFriendlyMessage, type ModelConfig, type ModelConfigPayload } from '@/api/client'
// 外观枚举只在 useAppearance 里定义一次（store 与设置面板都从这里取，抄两份会漂）
import type { Accent, GradientDirection, GradientMode, Theme } from '@/composables/useAppearance'
import { useToastStore } from './toast'

/**
 * 惰性获取 chatStore 并刷新模型列表
 * 设置面板增删改模型后，同步刷新对话中的模型选择器
 * 使用模块缓存避免重复动态导入导致的竞态条件
 */
let _modelsStorePromise: Promise<typeof import('./models')> | null = null
function refreshChatModels() {
  if (!_modelsStorePromise) {
    _modelsStorePromise = import('./models').catch((err) => {
      _modelsStorePromise = null // 失败时重置，允许重试
      throw err
    })
  }
  _modelsStorePromise.then(({ useModelsStore }) => {
    useModelsStore().fetchModels()
  }).catch(() => { /* 模块未就绪时忽略 */ })
}

export type SettingCategory =
  | 'models'
  | 'chat'
  | 'appearance'
  | 'skills'
  | 'agent'

export interface AppSettings {
  chat: {
    sendShortcut: 'enter' | 'shiftEnter'
    thinkingExpanded: boolean
    autoScroll: boolean
    messageFontSize: 12 | 13 | 14 | 15
    markdown: boolean
    contextWindow: number
    autoSkillMatch: boolean
  }
  agent: {
    maxIterations: number
    dynamicBudget: boolean
    /** 单次 run 成本熔断上限（USD），0 = 使用后端默认 */
    maxCostUsd: number
    /** 单次 run token 熔断上限，0 = 使用后端默认 */
    maxTotalTokens: number
  }
  postProcess: {
    enabled: boolean
    generateTitle: boolean
    extractMemory: boolean
    generateSkill: boolean
  }
  memory: {
    enabled: boolean
    maxItems: number
    injectBudgetTokens: number
    blocksBudgetTokens: number
    searchTimeoutS: number
    reconcileLlmEnabled: boolean
  }
  appearance: {
    theme: Theme
    accent: Accent
    fontSize: number
    animation: boolean
    backgroundEnabled: boolean
    backgroundImage: string
    backgroundVersion: number
    backgroundOpacity: number
    backgroundOverlay: number
    /** 主题渐变色：背景加入强调色氛围渐变 */
    themeGradientEnabled: boolean
    /** 主题渐变强度（0-100） */
    themeGradientStrength: number
    /** 渐变形态：glow=顶部微光 / linear=线性渐变 / radial=放射渐变 */
    themeGradientMode: GradientMode
    /** 渐变方向（linear/radial 生效） */
    themeGradientDirection: GradientDirection
    /** 渐变起始色（空 = 跟随强调色） */
    themeGradientStartColor: string
    /** 渐变结束色（空 = 跟随主题底色） */
    themeGradientEndColor: string
    terminal: {
      fontSize: number
      fontFamily: string
      aiPredictionEnabled: boolean
    }
  }
}

const LS_KEY = 'ckunagentic:settings'

function loadFromLocalStorage(): Partial<AppSettings> | null {
  try {
    const raw = localStorage.getItem(LS_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

/**
 * 本地持久化的数值不可信：旧版本存过 strategy，手工改过 localStorage 更常见。
 * 越界或 NaN 一律退到默认值 —— 后端 Field(ge/le) 对越界值直接 422，
 * 一次脏读数会让记忆设置整条同步通道静默失败。
 */
function clampNum(v: unknown, min: number, max: number, dflt: number): number {
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? Math.min(max, Math.max(min, n)) : dflt
}

const saved = loadFromLocalStorage()

export const useSettingsStore = defineStore('settings', () => {
  const settings = ref<AppSettings>({
    chat: {
      sendShortcut: saved?.chat?.sendShortcut ?? 'enter',
      thinkingExpanded: saved?.chat?.thinkingExpanded ?? true,
      autoScroll: saved?.chat?.autoScroll ?? true,
      messageFontSize: saved?.chat?.messageFontSize ?? 13,
      markdown: saved?.chat?.markdown ?? true,
      contextWindow: saved?.chat?.contextWindow ?? 20,
      autoSkillMatch: saved?.chat?.autoSkillMatch ?? true,
    },
    agent: {
      maxIterations: saved?.agent?.maxIterations ?? 100,
      dynamicBudget: saved?.agent?.dynamicBudget ?? true,
      maxCostUsd: saved?.agent?.maxCostUsd ?? 0,
      maxTotalTokens: saved?.agent?.maxTotalTokens ?? 0,
    },
    postProcess: {
      enabled: saved?.postProcess?.enabled ?? true,
      generateTitle: saved?.postProcess?.generateTitle ?? true,
      extractMemory: saved?.postProcess?.extractMemory ?? true,
      generateSkill: saved?.postProcess?.generateSkill ?? true,
    },
    memory: {
      enabled: saved?.memory?.enabled ?? true,
      maxItems: clampNum(saved?.memory?.maxItems, 10, 500, 100),
      injectBudgetTokens: clampNum(saved?.memory?.injectBudgetTokens, 200, 8000, 1500),
      blocksBudgetTokens: clampNum(saved?.memory?.blocksBudgetTokens, 200, 8000, 1200),
      searchTimeoutS: clampNum(saved?.memory?.searchTimeoutS, 0.5, 30, 8),
      reconcileLlmEnabled: saved?.memory?.reconcileLlmEnabled ?? true,
    },
    appearance: {
      theme: saved?.appearance?.theme ?? 'dark',
      accent: saved?.appearance?.accent ?? 'blue',
      fontSize: saved?.appearance?.fontSize ?? 13,
      animation: saved?.appearance?.animation ?? true,
      backgroundEnabled: saved?.appearance?.backgroundEnabled ?? false,
      backgroundImage: saved?.appearance?.backgroundImage ?? '',
      backgroundVersion: saved?.appearance?.backgroundVersion ?? 0,
      backgroundOpacity: saved?.appearance?.backgroundOpacity ?? 80,
      backgroundOverlay: saved?.appearance?.backgroundOverlay ?? 60,
      themeGradientEnabled: saved?.appearance?.themeGradientEnabled ?? false,
      themeGradientStrength: saved?.appearance?.themeGradientStrength ?? 30,
      themeGradientMode: saved?.appearance?.themeGradientMode ?? 'glow',
      themeGradientDirection: saved?.appearance?.themeGradientDirection ?? 'tb',
      themeGradientStartColor: saved?.appearance?.themeGradientStartColor ?? '',
      themeGradientEndColor: saved?.appearance?.themeGradientEndColor ?? '',
      terminal: {
        fontSize: saved?.appearance?.terminal?.fontSize ?? 14,
        fontFamily: saved?.appearance?.terminal?.fontFamily ?? 'jetbrains',
        aiPredictionEnabled: saved?.appearance?.terminal?.aiPredictionEnabled ?? false,
      },
    },
  })

  // 防抖持久化：persistSettings 本来就是全量序列化，按分区各开一个定时器并无意义
  let saveTimer: number | undefined
  watch(settings, () => {
    clearTimeout(saveTimer)
    saveTimer = window.setTimeout(() => persistSettings(), 300)
  }, { deep: true })

  function persistSettings() {
    localStorage.setItem(LS_KEY, JSON.stringify(settings.value))
  }

  // debounce 的代价：改完设置立刻关窗（<300ms）这次改动就永久丢了 ——
  // 外观/对话/运行预算这些都是纯本地设置，丢了用户完全无感，只会以为没保存过。
  // pagehide 是 Chromium 渲染进程里最可靠的收尾点（beforeunload 需返回值才触发）。
  // 可选链：单测的 window 垫片只有 setTimeout/clearTimeout。
  window.addEventListener?.('pagehide', () => {
    clearTimeout(saveTimer)
    persistSettings()
  })

  // 记忆设置变更时同步到后端
  let memorySyncTimer: number | undefined
  // 后端回填不算用户改动：loadMemorySettings 写这几个字段会惊动 deep watch，
  // 于是启动时把刚读到的值原样 POST 回去（白写一次）；响应缺字段时更会把
  // undefined 落进本地、再推给后端 —— 一次异常响应就污染设置。
  let applyingRemoteMemory = false
  watch(
    () => settings.value.memory,
    (mem) => {
      if (applyingRemoteMemory) return
      clearTimeout(memorySyncTimer)
      memorySyncTimer = window.setTimeout(() => {
        agentApi.updateMemorySettings({
          enabled: mem.enabled,
          max_items: mem.maxItems,
          inject_budget_tokens: mem.injectBudgetTokens,
          blocks_budget_tokens: mem.blocksBudgetTokens,
          search_timeout_s: mem.searchTimeoutS,
          reconcile_llm_enabled: mem.reconcileLlmEnabled,
        }).catch((e) => {
          // 后端同步失败时提示用户，避免静默失败导致设置丢失
          const toast = useToastStore()
          const msg = e instanceof Error ? e.message : String(e)
          toast.error(`记忆设置同步失败: ${msg}`, 5000)
        })
      }, 500)
    },
    { deep: true }
  )

  // 初始化时从后端加载记忆设置（只接受形状正确的字段，缺失即保留本地值）
  async function loadMemorySettings() {
    applyingRemoteMemory = true
    try {
      const data = await agentApi.getMemorySettings()
      if (typeof data.enabled === 'boolean') settings.value.memory.enabled = data.enabled
      if (Number.isFinite(data.max_items)) settings.value.memory.maxItems = data.max_items
      if (Number.isFinite(data.inject_budget_tokens)) settings.value.memory.injectBudgetTokens = data.inject_budget_tokens
      if (Number.isFinite(data.blocks_budget_tokens)) settings.value.memory.blocksBudgetTokens = data.blocks_budget_tokens
      if (Number.isFinite(data.search_timeout_s)) settings.value.memory.searchTimeoutS = data.search_timeout_s
      if (typeof data.reconcile_llm_enabled === 'boolean') settings.value.memory.reconcileLlmEnabled = data.reconcile_llm_enabled
      await nextTick()  // 等本轮 watcher 冲刷完再放开闸门
    } catch { /* 后端未就绪时使用 localStorage 默认值 */ }
    finally { applyingRemoteMemory = false }
  }

  // 后处理设置变更时同步到后端
  let postProcessSyncTimer: number | undefined
  // 同 applyingRemoteMemory：后端回填不回写、且只接受布尔字段
  let applyingRemotePostProcess = false
  watch(
    () => settings.value.postProcess,
    (pp) => {
      if (applyingRemotePostProcess) return
      clearTimeout(postProcessSyncTimer)
      postProcessSyncTimer = window.setTimeout(() => {
        api.updatePostProcessSettings({
          enabled: pp.enabled,
          generate_title: pp.generateTitle,
          extract_memory: pp.extractMemory,
          generate_skill: pp.generateSkill,
        }).catch((e) => {
          // 后端同步失败时提示用户，避免静默失败导致设置丢失
          // C35 修复：用 toFriendlyMessage 转换，不再暴露原始 e.message（技术细节）
          const toast = useToastStore()
          toast.error(toFriendlyMessage(e, '后处理设置同步失败'), 5000)
        })
      }, 500)
    },
    { deep: true }
  )

  // 初始化时从后端加载后处理设置（字段缺失/类型不对就保留本地值，不写回后端）
  async function loadPostProcessSettings() {
    applyingRemotePostProcess = true
    try {
      const data = await api.getPostProcessSettings()
      const pp = settings.value.postProcess
      if (typeof data.enabled === 'boolean') pp.enabled = data.enabled
      if (typeof data.generate_title === 'boolean') pp.generateTitle = data.generate_title
      if (typeof data.extract_memory === 'boolean') pp.extractMemory = data.extract_memory
      if (typeof data.generate_skill === 'boolean') pp.generateSkill = data.generate_skill
      await nextTick()
    } catch { /* 后端未就绪时使用 localStorage 默认值 */ }
    finally { applyingRemotePostProcess = false }
  }

  // ── 模型配置（来自后端 DB）──
  const modelConfigs = ref<ModelConfig[]>([])
  const modelsLoading = ref(false)
  const modelsError = ref('')

  async function fetchModelConfigs() {
    modelsLoading.value = true
    modelsError.value = ''
    try {
      const data = await api.getModelConfigs()
      modelConfigs.value = data
    } catch (e: unknown) {
      console.error('[SettingsStore] Error fetching models:', e)
      modelsError.value = e instanceof Error ? e.message : String(e)
    } finally {
      modelsLoading.value = false
    }
  }

  async function createModelConfig(payload: ModelConfigPayload): Promise<ModelConfig | null> {
    try {
      const data = await api.createModelConfig(payload)
      // 重新拉取完整列表，确保与数据库一致
      await fetchModelConfigs()
      // 持久化校验：刚创建的 id 必须出现在列表里，否则说明后端 INSERT 未真正落库（db 路径/迁移异常）
      if (data && !modelConfigs.value.some(m => m.id === data.id)) {
        console.error('[SettingsStore] created model not in fetched list — DB persistence may be broken:',
          { createdId: data.id, fetchedIds: modelConfigs.value.map(m => m.id) })
        modelsError.value = '模型已创建但未在数据库中持久化，重启后可能丢失。请检查后端 DB 路径日志。'
        // 仍返回 data 让 UI 关闭对话框，但 toast 已由上层基于 modelsError 提示
      }
      refreshChatModels()
      return data
    } catch (e: unknown) {
      modelsError.value = e instanceof Error ? e.message : String(e)
      return null
    }
  }

  async function updateModelConfig(id: string, payload: Partial<ModelConfigPayload>): Promise<ModelConfig | null> {
    try {
      const data = await api.updateModelConfig(id, payload)
      // 重新拉取完整列表，确保与数据库一致
      await fetchModelConfigs()
      refreshChatModels()
      return data
    } catch (e: unknown) {
      modelsError.value = e instanceof Error ? e.message : String(e)
      return null
    }
  }

  async function deleteModelConfig(id: string): Promise<boolean> {
    try {
      await api.deleteModelConfig(id)
      // 重新拉取完整列表，确保与数据库一致
      await fetchModelConfigs()
      refreshChatModels()
      return true
    } catch (e: unknown) {
      modelsError.value = e instanceof Error ? e.message : String(e)
      return false
    }
  }

  async function testModelConnection(
    id: string,
    onStatus: (status: 'testing' | 'ok' | 'error', detail?: string, latencyMs?: number) => void,
  ) {
    onStatus('testing')
    try {
      const result = await api.testModelConnection(id)
      if (result?.status === 'ok') {
        onStatus('ok', undefined, result.latency_ms)
      } else {
        // 业务层失败（HTTP 200 但 body.status=error）：保留后端 detail，避免泛化"未知错误"丢失真实原因
        const detail = result?.detail || `后端返回未知状态: ${JSON.stringify(result)}`
        console.warn('[STORE testModelConnection] business error:', detail)
        onStatus('error', detail)
      }
    } catch (e: unknown) {
      console.error('[STORE testModelConnection] exception:', e)
      onStatus('error', e instanceof Error ? e.message : String(e))
    }
  }

  return {
    settings,
    loadMemorySettings,
    loadPostProcessSettings,
    modelConfigs,
    modelsLoading,
    modelsError,
    fetchModelConfigs,
    createModelConfig,
    updateModelConfig,
    deleteModelConfig,
    testModelConnection,
  }
})
