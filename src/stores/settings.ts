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
  | 'memory'
  | 'ops'

interface AppSettings {
  chat: {
    sendShortcut: 'enter' | 'shiftEnter'
    thinkingExpanded: boolean
    autoScroll: boolean
    messageFontSize: 12 | 13 | 14 | 15
    markdown: boolean
    contextWindow: number
    autoSkillMatch: boolean
    /**
     * E2：深度思考三态。'inherit' 表示前端不发 enable_thinking，
     * 交给后端演化配置决定；'on'/'off' 为用户显式表态，覆盖演化。
     */
    enableThinking: 'inherit' | 'on' | 'off'
  }
  agent: {
    maxIterations: number
    dynamicBudget: boolean
    /** E1：意图识别 + 思考预算控制总开关（关闭则无规划阶段、无引导语注入） */
    enableIntentionGuide: boolean
    /** 单次 run 成本熔断上限（USD），0 = 使用后端默认 */
    maxCostUsd: number
    /** 单次 run token 熔断上限，0 = 使用后端默认 */
    maxTotalTokens: number
    /**
     * E4：新建 SSH 连接的默认环境标记。逐条仍可在连接表单里改，这里只定初值。
     * 默认 'test'：误标成 prod 会让每条危险命令都走"必须预登记 allowlist"，
     * 测试机上日常运维直接跑不动。
     */
    defaultEnvironment: 'test' | 'prod'
    /**
     * E5：生产环境额外确认总开关。关掉只撤掉「prod 专属」的那层提示与批量下发
     * 确认框；dangerous/hardline 判定与 prod 预授权 allowlist 闸门不受此开关控制。
     */
    prodConfirm: boolean
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
    /** 检索注入预算（每轮按相关性取的历史经验） */
    injectBudgetTokens: number
    /** 画像块预算（常驻已知事实），与注入预算分账 */
    blocksBudgetTokens: number
    /** 预检索等待上限，超时降级为按新近兜底 */
    searchTimeoutS: number
    /** 关闭后后台整理只做阈值去重，不调模型 */
    reconcileLlmEnabled: boolean
  }
  appearance: {
    theme: Theme
    accent: Accent
    /** 界面基础字号（html 根字号）。旧名 fontSize 与 chat.messageFontSize /
     *  terminal.fontSize 三套字号分不清，读取侧仍兼容旧 key */
    uiFontSize: number
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

const saved = loadFromLocalStorage()

/**
 * 重命名前的 appearance 行：`fontSize` 曾指"界面基础字号"，与 chat 消息字号、
 * 终端字号三者同名不同物。旧 key 只读兼容一次，下次全量序列化自然消失。
 */
type LegacyAppearance = Partial<AppSettings['appearance']> & { fontSize?: number }
const savedAppearance = (saved?.appearance ?? {}) as LegacyAppearance

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
      enableThinking:
        saved?.chat?.enableThinking === 'on' || saved?.chat?.enableThinking === 'off'
          ? saved?.chat?.enableThinking
          : 'inherit',
    },
    agent: {
      maxIterations: saved?.agent?.maxIterations ?? 100,
      dynamicBudget: saved?.agent?.dynamicBudget ?? true,
      enableIntentionGuide: saved?.agent?.enableIntentionGuide ?? true,
      maxCostUsd: saved?.agent?.maxCostUsd ?? 0,
      maxTotalTokens: saved?.agent?.maxTotalTokens ?? 0,
      defaultEnvironment: saved?.agent?.defaultEnvironment === 'prod' ? 'prod' : 'test',
      // 只认真布尔：手改过的 localStorage 里塞字符串/数字时，`?? true` 会原样放行，
      // 于是 prodConfirm 变成 truthy 的 'no' —— 用户以为关了，实际还开着。
      prodConfirm: typeof saved?.agent?.prodConfirm === 'boolean' ? saved.agent.prodConfirm : true,
    },
    postProcess: {
      enabled: saved?.postProcess?.enabled ?? true,
      generateTitle: saved?.postProcess?.generateTitle ?? true,
      extractMemory: saved?.postProcess?.extractMemory ?? true,
      generateSkill: saved?.postProcess?.generateSkill ?? true,
    },
    memory: {
      enabled: saved?.memory?.enabled ?? true,
      maxItems: saved?.memory?.maxItems ?? 100,
      injectBudgetTokens: saved?.memory?.injectBudgetTokens ?? 1500,
      blocksBudgetTokens: saved?.memory?.blocksBudgetTokens ?? 1200,
      searchTimeoutS: saved?.memory?.searchTimeoutS ?? 8,
      reconcileLlmEnabled: saved?.memory?.reconcileLlmEnabled ?? true,
    },
    appearance: {
      theme: saved?.appearance?.theme ?? 'dark',
      accent: saved?.appearance?.accent ?? 'blue',
      uiFontSize: savedAppearance.uiFontSize ?? savedAppearance.fontSize ?? 13,
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
    // 审查R2修复：与 useChatPersistence 同款裸 setItem —— 配额满时 QuotaExceeded
    // 会在防抖定时器/pagehide 回调里抛出未捕获异常。设置体量小，无需淘汰逻辑，
    // 捕获并记日志即可（不能让持久化失败掀翻调用栈）。
    try {
      localStorage.setItem(LS_KEY, JSON.stringify(settings.value))
    } catch (e) {
      console.warn('[settings] 设置持久化失败（localStorage 可能已满或被禁用）:', e)
    }
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
  // 上一成功同步到后端的快照：sync 失败时把本地回滚到这里，
  // 否则"UI 显示新值 / DB 仍是旧值"的静默背离会长期存在。
  let lastSyncedMemory: AppSettings['memory'] = { ...settings.value.memory }
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
        }).then(() => {
          lastSyncedMemory = { ...settings.value.memory }
        }).catch((e) => {
          const toast = useToastStore()
          const msg = e instanceof Error ? e.message : String(e)
          // 回滚本身要走 settings 响应式；先置闸门吞掉 watch，等本轮 watcher 冲刷完再放开
          applyingRemoteMemory = true
          settings.value.memory = { ...lastSyncedMemory }
          window.setTimeout(() => { applyingRemoteMemory = false }, 0)
          toast.error(`记忆设置同步失败，已回滚: ${msg}`, 5000)
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
      const mem = settings.value.memory
      if (typeof data.enabled === 'boolean') mem.enabled = data.enabled
      if (Number.isFinite(data.max_items)) mem.maxItems = data.max_items
      if (Number.isFinite(data.inject_budget_tokens)) mem.injectBudgetTokens = data.inject_budget_tokens
      if (Number.isFinite(data.blocks_budget_tokens)) mem.blocksBudgetTokens = data.blocks_budget_tokens
      if (Number.isFinite(data.search_timeout_s)) mem.searchTimeoutS = data.search_timeout_s
      if (typeof data.reconcile_llm_enabled === 'boolean') mem.reconcileLlmEnabled = data.reconcile_llm_enabled
      await nextTick()  // 等本轮 watcher 冲刷完再放开闸门
      // 后端值即"最后一次已知一致状态"，作为 sync 失败时的回滚基线
      lastSyncedMemory = { ...settings.value.memory }
    } catch { /* 后端未就绪时使用 localStorage 默认值 */ }
    finally { applyingRemoteMemory = false }
  }

  // 后处理设置变更时同步到后端
  let postProcessSyncTimer: number | undefined
  // 同 applyingRemoteMemory：后端回填不回写、且只接受布尔字段
  let applyingRemotePostProcess = false
  // 同 lastSyncedMemory：sync 失败回滚基线
  let lastSyncedPostProcess: AppSettings['postProcess'] = { ...settings.value.postProcess }
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
        }).then(() => {
          lastSyncedPostProcess = { ...settings.value.postProcess }
        }).catch((e) => {
          // C35 修复：用 toFriendlyMessage 转换，不再暴露原始 e.message（技术细节）
          const toast = useToastStore()
          applyingRemotePostProcess = true
          settings.value.postProcess = { ...lastSyncedPostProcess }
          window.setTimeout(() => { applyingRemotePostProcess = false }, 0)
          toast.error(`${toFriendlyMessage(e, '后处理设置同步失败')}，已回滚`, 5000)
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
      lastSyncedPostProcess = { ...settings.value.postProcess }
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

  /**
   * A1：把某条模型设为 SSH 模式默认。后端会原子清掉其他条的 is_default_ssh
   * （db.set_default_model），所以本地只需重新拉列表即可让卡片徽标同步。
   */
  async function setDefaultModel(id: string): Promise<boolean> {
    try {
      await api.setDefaultModel(id, 'ssh')
      await fetchModelConfigs()
      // 模型选择器与 ssh-ai store 各自缓存了默认模型的 id，切换后必须刷新，
      // 否则 ChatPanel 顶部仍显示旧模型、下一次运行仍走旧 endpoint
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
    setDefaultModel,
    testModelConnection,
  }
})
