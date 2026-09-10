import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import { api, agentApi, toFriendlyMessage, type ModelConfig, type ModelConfigPayload } from '@/api/client'
import { useToastStore } from './toast'

/**
 * 惰性获取 chatStore 并刷新模型列表
 * 设置面板增删改模型后，同步刷新对话中的模型选择器
 * 使用模块缓存避免重复动态导入导致的竞态条件
 */
let _chatStorePromise: Promise<typeof import('./chat')> | null = null
function refreshChatModels() {
  if (!_chatStorePromise) {
    _chatStorePromise = import('./chat').catch((err) => {
      _chatStorePromise = null // 失败时重置，允许重试
      throw err
    })
  }
  _chatStorePromise.then(({ useChatStore }) => {
    useChatStore().fetchModels()
  }).catch(() => { /* 模块未就绪时忽略 */ })
}

export type SettingCategory =
  | 'models'
  | 'chat'
  | 'appearance'

export interface AppSettings {
  chat: {
    sendShortcut: 'enter' | 'shiftEnter'
    streaming: boolean
    thinkingExpanded: boolean
    autoScroll: boolean
    messageFontSize: 12 | 13 | 14 | 15
    codeTheme: string
    markdown: boolean
    contextWindow: number
    autoSkillMatch: boolean
  }
  agent: {
    maxIterations: number
    dynamicBudget: boolean
    defaultAgentMode: boolean
    rememberAgentMode: boolean
  }
  postProcess: {
    enabled: boolean
    generateTitle: boolean
    extractMemory: boolean
    generateSkill: boolean
  }
  memory: {
    enabled: boolean
    userMdPath: string
    strategy: 'auto' | 'manual'
    maxItems: number
  }
  appearance: {
    theme: 'dark' | 'light' | 'system' | 'blue-screen' | 'forest' | 'mint' | 'bee' | 'coral'
    accent: 'blue' | 'purple' | 'green' | 'orange' | 'white' | 'forest' | 'mint' | 'bee' | 'coral'
    fontSize: number
    sidebarWidth: number
    compact: boolean
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
    themeGradientMode: 'glow' | 'linear' | 'radial'
    /** 渐变方向（linear/radial 生效） */
    themeGradientDirection: 'tb' | 'bt' | 'lr' | 'rl' | 'tlbr' | 'trbl'
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

const saved = loadFromLocalStorage()

export const useSettingsStore = defineStore('settings', () => {
  const settings = ref<AppSettings>({
    chat: {
      sendShortcut: saved?.chat?.sendShortcut ?? 'enter',
      streaming: saved?.chat?.streaming ?? true,
      thinkingExpanded: saved?.chat?.thinkingExpanded ?? true,
      autoScroll: saved?.chat?.autoScroll ?? true,
      messageFontSize: saved?.chat?.messageFontSize ?? 13,
      codeTheme: saved?.chat?.codeTheme ?? 'github-dark',
      markdown: saved?.chat?.markdown ?? true,
      contextWindow: saved?.chat?.contextWindow ?? 20,
      autoSkillMatch: saved?.chat?.autoSkillMatch ?? true,
    },
    agent: {
      maxIterations: saved?.agent?.maxIterations ?? 100,
      dynamicBudget: saved?.agent?.dynamicBudget ?? true,
      // 硬编码 false：旧版 localStorage 保存了 true（?? 无法覆盖），
      // 导致所有消息走 /agent/run，普通对话永远不到后端
      defaultAgentMode: false,
      rememberAgentMode: saved?.agent?.rememberAgentMode ?? true,
    },
    postProcess: {
      enabled: saved?.postProcess?.enabled ?? true,
      generateTitle: saved?.postProcess?.generateTitle ?? true,
      extractMemory: saved?.postProcess?.extractMemory ?? true,
      generateSkill: saved?.postProcess?.generateSkill ?? true,
    },
    memory: {
      enabled: saved?.memory?.enabled ?? true,
      userMdPath: saved?.memory?.userMdPath ?? '',
      strategy: saved?.memory?.strategy ?? 'auto',
      maxItems: saved?.memory?.maxItems ?? 100,
    },
    appearance: {
      theme: saved?.appearance?.theme ?? 'dark',
      accent: saved?.appearance?.accent ?? 'blue',
      fontSize: saved?.appearance?.fontSize ?? 13,
      sidebarWidth: saved?.appearance?.sidebarWidth ?? 240,
      compact: saved?.appearance?.compact ?? false,
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

  // 防抖持久化：按分区独立监听，避免高频变更（如滑块）触发全量序列化
  let saveChatTimer: number | undefined
  let saveAgentTimer: number | undefined
  let saveAppearanceTimer: number | undefined
  let saveMemoryTimer: number | undefined
  let savePostProcessTimer: number | undefined

  watch(() => settings.value.chat, () => {
    clearTimeout(saveChatTimer)
    saveChatTimer = window.setTimeout(() => persistSettings(), 300)
  }, { deep: true })

  watch(() => settings.value.agent, () => {
    clearTimeout(saveAgentTimer)
    saveAgentTimer = window.setTimeout(() => persistSettings(), 300)
  }, { deep: true })

  watch(() => settings.value.appearance, () => {
    clearTimeout(saveAppearanceTimer)
    saveAppearanceTimer = window.setTimeout(() => persistSettings(), 300)
  }, { deep: true })

  watch(() => settings.value.memory, () => {
    clearTimeout(saveMemoryTimer)
    saveMemoryTimer = window.setTimeout(() => persistSettings(), 300)
  }, { deep: true })

  watch(() => settings.value.postProcess, () => {
    clearTimeout(savePostProcessTimer)
    savePostProcessTimer = window.setTimeout(() => persistSettings(), 300)
  }, { deep: true })

  function persistSettings() {
    localStorage.setItem(LS_KEY, JSON.stringify(settings.value))
  }

  // 记忆设置变更时同步到后端
  let memorySyncTimer: number | undefined
  watch(
    () => settings.value.memory,
    (mem) => {
      clearTimeout(memorySyncTimer)
      memorySyncTimer = window.setTimeout(() => {
        agentApi.updateMemorySettings({
          enabled: mem.enabled,
          strategy: mem.strategy,
          max_items: mem.maxItems,
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

  // 初始化时从后端加载记忆设置
  async function loadMemorySettings() {
    try {
      const data = await agentApi.getMemorySettings()
      settings.value.memory.enabled = data.enabled
      settings.value.memory.strategy = data.strategy as 'auto' | 'manual'
      settings.value.memory.maxItems = data.max_items
    } catch { /* 后端未就绪时使用 localStorage 默认值 */ }
  }

  // 后处理设置变更时同步到后端
  let postProcessSyncTimer: number | undefined
  watch(
    () => settings.value.postProcess,
    (pp) => {
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

  // 初始化时从后端加载后处理设置
  async function loadPostProcessSettings() {
    try {
      const data = await api.getPostProcessSettings()
      settings.value.postProcess.enabled = data.enabled
      settings.value.postProcess.generateTitle = data.generate_title
      settings.value.postProcess.extractMemory = data.extract_memory
      settings.value.postProcess.generateSkill = data.generate_skill
    } catch { /* 后端未就绪时使用 localStorage 默认值 */ }
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
