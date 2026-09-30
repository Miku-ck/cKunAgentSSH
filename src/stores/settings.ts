import { defineStore } from 'pinia'
import { nextTick, onScopeDispose, ref, watch } from 'vue'
import { api, agentApi, toFriendlyMessage, type ModelConfig, type ModelConfigPayload, type ModelDefaultMode } from '@/api/client'
// 外观枚举只在 appearance-tokens 里定义一次（store 与设置面板都从这里取，抄两份会漂）。
// 它不能住回 useAppearance.ts：本文件在模块顶层就取 ACCENT_MAP 的键当白名单，
// 而 useAppearance 要取本文件的 store —— 那是个真运行时环，细节见那个叶子模块的头注。
import type { Accent, GradientDirection, GradientMode, TerminalBgMode, Theme } from '@/utils/appearance-tokens'
import { ACCENT_MAP, isHexColor } from '@/utils/appearance-tokens'
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
     * 关闭运行预算熔断：后端把显式 0 解释为「不设上限」，而上面两个数字的 0 在
     * 本 store 里一直是「不发这个字段」—— 两种 0 不能共用一个数，所以这里单列一个
     * 布尔，由 ssh-ai 在发请求时翻成显式 0。此前"关掉熔断"根本发不出去，
     * 长批跑只能在 $2 处断掉。
     */
    breakerDisabled: boolean
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
      /** 终端背景模式：auto=跟随主题/全局背景（现状），solid=自定义纯色 */
      backgroundMode: TerminalBgMode
      /** 纯色背景 hex（仅 solid 生效；非法值回落默认） */
      backgroundColor: string
      /** 纯色背景透明度 0-100（低于 100 时与下层主题底色/背景图混合） */
      backgroundOpacity: number
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
 * localStorage 是可被手改、被旧版本写入、被扩展污染的输入源，`saved?.x ?? 默认`
 * 只挡得住 null/undefined：'no' 是 truthy、'13' 是字符串、999 越界照样流到渲染层
 * （CSS 类拼不出来、字号变成 '13pxpx'、开关显示关实际开）。所以每条本地设置都过
 * 一遍下面的闸门，脏值一律回落默认，而不是钳到边界——把 999 钳成 100 会让用户
 * 以为自己填的值生效了。
 */
function asBool(value: unknown, fallback: boolean): boolean {
  return typeof value === 'boolean' ? value : fallback
}

function asEnum<T extends string | number>(value: unknown, allowed: readonly T[], fallback: T): T {
  return (allowed as readonly unknown[]).includes(value) ? value as T : fallback
}

function asNumber(value: unknown, fallback: number, lo: number, hi: number): number {
  return typeof value === 'number' && Number.isFinite(value) && value >= lo && value <= hi
    ? value
    : fallback
}

function asString(value: unknown, fallback: string): string {
  return typeof value === 'string' ? value : fallback
}

// 枚举白名单写成 Record<Union, true> 再取键：漏一项是编译错误，
// 直接抄一份字符串数组则不会报错——正是本仓反复出现的"抄两份清单"漂移族。
const THEMES = Object.keys({
  dark: true, light: true, system: true, 'blue-screen': true,
  forest: true, mint: true, bee: true, coral: true, atelier: true,
} as Record<Theme, true>) as Theme[]
const ACCENTS = Object.keys(ACCENT_MAP) as Accent[]
const GRADIENT_MODES = Object.keys({
  glow: true, linear: true, radial: true,
} as Record<GradientMode, true>) as GradientMode[]
const GRADIENT_DIRECTIONS = Object.keys({
  tb: true, bt: true, lr: true, rl: true, tlbr: true, trbl: true,
} as Record<GradientDirection, true>) as GradientDirection[]
const TERMINAL_BG_MODES = Object.keys({
  auto: true, solid: true,
} as Record<TerminalBgMode, true>) as TerminalBgMode[]

/** 颜色输入框/历史脏值都可能塞进非 hex，<input type="color"> 拿到会直接显示黑 */
function asHexColor(value: unknown, fallback: string): string {
  return isHexColor(value) ? value : fallback
}

/**
 * 重命名前的 appearance 行：`fontSize` 曾指"界面基础字号"，与 chat 消息字号、
 * 终端字号三者同名不同物。旧 key 只读兼容一次，下次全量序列化自然消失。
 */
type LegacyAppearance = Partial<AppSettings['appearance']> & { fontSize?: number }
const savedAppearance = (saved?.appearance ?? {}) as LegacyAppearance

export const useSettingsStore = defineStore('settings', () => {
  const settings = ref<AppSettings>({
    chat: {
      sendShortcut: asEnum(saved?.chat?.sendShortcut, ['enter', 'shiftEnter'] as const, 'enter'),
      thinkingExpanded: asBool(saved?.chat?.thinkingExpanded, true),
      autoScroll: asBool(saved?.chat?.autoScroll, true),
      messageFontSize: asEnum(saved?.chat?.messageFontSize, [12, 13, 14, 15] as const, 13),
      markdown: asBool(saved?.chat?.markdown, true),
      // 下限 1 与后端 routes/agent.py 的 Field(ge=1) 同口径。原区间写 5，
      // 于是手填/历史遗留的 1-4 会被静默改回 20 —— 而这三处区间各有说法已漂过一次。
      contextWindow: asNumber(saved?.chat?.contextWindow, 20, 1, 100),
      autoSkillMatch: asBool(saved?.chat?.autoSkillMatch, true),
      enableThinking: asEnum(
        saved?.chat?.enableThinking, ['inherit', 'on', 'off'] as const, 'inherit'),
    },
    agent: {
      maxIterations: asNumber(saved?.agent?.maxIterations, 100, 1, 500),
      dynamicBudget: asBool(saved?.agent?.dynamicBudget, true),
      enableIntentionGuide: asBool(saved?.agent?.enableIntentionGuide, true),
      maxCostUsd: asNumber(saved?.agent?.maxCostUsd, 0, 0, 100000),
      maxTotalTokens: asNumber(saved?.agent?.maxTotalTokens, 0, 0, 1e12),
      breakerDisabled: asBool(saved?.agent?.breakerDisabled, false),
      defaultEnvironment: asEnum(
        saved?.agent?.defaultEnvironment, ['test', 'prod'] as const, 'test'),
      prodConfirm: asBool(saved?.agent?.prodConfirm, true),
    },
    postProcess: {
      enabled: asBool(saved?.postProcess?.enabled, true),
      generateTitle: asBool(saved?.postProcess?.generateTitle, true),
      extractMemory: asBool(saved?.postProcess?.extractMemory, true),
      generateSkill: asBool(saved?.postProcess?.generateSkill, true),
    },
    memory: {
      enabled: asBool(saved?.memory?.enabled, true),
      maxItems: asNumber(saved?.memory?.maxItems, 100, 10, 500),
      injectBudgetTokens: asNumber(saved?.memory?.injectBudgetTokens, 1500, 200, 8000),
      blocksBudgetTokens: asNumber(saved?.memory?.blocksBudgetTokens, 1200, 200, 8000),
      searchTimeoutS: asNumber(saved?.memory?.searchTimeoutS, 8, 0.5, 30),
      reconcileLlmEnabled: asBool(saved?.memory?.reconcileLlmEnabled, true),
    },
    appearance: {
      theme: asEnum(saved?.appearance?.theme, THEMES, 'dark'),
      accent: asEnum(saved?.appearance?.accent, ACCENTS, 'blue'),
      uiFontSize: asNumber(savedAppearance.uiFontSize ?? savedAppearance.fontSize, 13, 12, 15),
      animation: asBool(saved?.appearance?.animation, true),
      backgroundEnabled: asBool(saved?.appearance?.backgroundEnabled, false),
      backgroundImage: asString(saved?.appearance?.backgroundImage, ''),
      backgroundVersion: asNumber(saved?.appearance?.backgroundVersion, 0, 0, Number.MAX_SAFE_INTEGER),
      backgroundOpacity: asNumber(saved?.appearance?.backgroundOpacity, 80, 0, 100),
      backgroundOverlay: asNumber(saved?.appearance?.backgroundOverlay, 60, 0, 100),
      themeGradientEnabled: asBool(saved?.appearance?.themeGradientEnabled, false),
      themeGradientStrength: asNumber(saved?.appearance?.themeGradientStrength, 30, 0, 100),
      themeGradientMode: asEnum(saved?.appearance?.themeGradientMode, GRADIENT_MODES, 'glow'),
      themeGradientDirection: asEnum(
        saved?.appearance?.themeGradientDirection, GRADIENT_DIRECTIONS, 'tb'),
      themeGradientStartColor: asString(saved?.appearance?.themeGradientStartColor, ''),
      themeGradientEndColor: asString(saved?.appearance?.themeGradientEndColor, ''),
      terminal: {
        fontSize: asNumber(saved?.appearance?.terminal?.fontSize, 14, 12, 18),
        fontFamily: asString(saved?.appearance?.terminal?.fontFamily, 'jetbrains'),
        aiPredictionEnabled: asBool(saved?.appearance?.terminal?.aiPredictionEnabled, false),
        backgroundMode: asEnum(saved?.appearance?.terminal?.backgroundMode, TERMINAL_BG_MODES, 'auto'),
        backgroundColor: asHexColor(saved?.appearance?.terminal?.backgroundColor, '#0d1117'),
        backgroundOpacity: asNumber(saved?.appearance?.terminal?.backgroundOpacity, 92, 0, 100),
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

  // 记忆后端同步：debounce 到期与 pagehide 强 flush 共用同一实现。
  // applyingRemoteMemory：后端回填不算用户改动——loadMemorySettings 写这几个
  // 字段会惊动 deep watch，于是启动时把刚读到的值原样 POST 回去（白写一次）；
  // 响应缺字段时更会把 undefined 落进本地、再推给后端——一次异常响应就污染设置。
  // lastSyncedMemory：上一成功同步的快照，sync 失败时本地回滚到这里，
  // 否则"UI 显示新值 / DB 仍是旧值"的静默背离会长期存在。
  let memorySyncTimer: number | undefined
  let applyingRemoteMemory = false
  let lastSyncedMemory: AppSettings['memory'] = { ...settings.value.memory }
  async function syncMemoryToBackend() {
    const mem = { ...settings.value.memory }
    try {
      await agentApi.updateMemorySettings({
        enabled: mem.enabled,
        max_items: mem.maxItems,
        inject_budget_tokens: mem.injectBudgetTokens,
        blocks_budget_tokens: mem.blocksBudgetTokens,
        search_timeout_s: mem.searchTimeoutS,
        reconcile_llm_enabled: mem.reconcileLlmEnabled,
      })
      lastSyncedMemory = mem
    } catch (e) {
      const toast = useToastStore()
      const msg = e instanceof Error ? e.message : String(e)
      // 回滚本身要走 settings 响应式；先置闸门吞掉 watch，等本轮 watcher 冲刷完再放开
      applyingRemoteMemory = true
      settings.value.memory = { ...lastSyncedMemory }
      window.setTimeout(() => { applyingRemoteMemory = false }, 0)
      toast.error(`记忆设置同步失败，已回滚: ${msg}`, 5000)
    }
  }
  watch(
    () => settings.value.memory,
    () => {
      if (applyingRemoteMemory) return
      clearTimeout(memorySyncTimer)
      memorySyncTimer = window.setTimeout(() => void syncMemoryToBackend(), 500)
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

  // 后处理后端同步：与 syncMemoryToBackend 同型（闸门/基线/回滚口径一致）
  let postProcessSyncTimer: number | undefined
  let applyingRemotePostProcess = false
  let lastSyncedPostProcess: AppSettings['postProcess'] = { ...settings.value.postProcess }
  async function syncPostProcessToBackend() {
    const pp = { ...settings.value.postProcess }
    try {
      await api.updatePostProcessSettings({
        enabled: pp.enabled,
        generate_title: pp.generateTitle,
        extract_memory: pp.extractMemory,
        generate_skill: pp.generateSkill,
      })
      lastSyncedPostProcess = pp
    } catch (e) {
      // C35 修复：用 toFriendlyMessage 转换，不再暴露原始 e.message（技术细节）
      const toast = useToastStore()
      applyingRemotePostProcess = true
      settings.value.postProcess = { ...lastSyncedPostProcess }
      window.setTimeout(() => { applyingRemotePostProcess = false }, 0)
      toast.error(`${toFriendlyMessage(e, '后处理设置同步失败')}，已回滚`, 5000)
    }
  }
  watch(
    () => settings.value.postProcess,
    () => {
      if (applyingRemotePostProcess) return
      clearTimeout(postProcessSyncTimer)
      postProcessSyncTimer = window.setTimeout(() => void syncPostProcessToBackend(), 500)
    },
    { deep: true }
  )

  // debounce 的代价：改完设置立刻关窗（<300ms）这次改动就永久丢了 ——
  // 外观/对话/运行预算这些都是纯本地设置，丢了用户完全无感，只会以为没保存过。
  // pagehide 是 Chromium 渲染进程里最可靠的收尾点（beforeunload 需返回值才触发）。
  // 后端同步分区同理要 flush：否则本地 localStorage 拿新值、后端停在旧值，
  // 下次启动 load*Settings 以后端为准把改动静默冲掉（原实现只保住了 localStorage 半边）。
  // 可选链：单测的 window 垫片只有 setTimeout/clearTimeout。
  const flushOnPageHide = () => {
    clearTimeout(saveTimer)
    clearTimeout(memorySyncTimer)
    clearTimeout(postProcessSyncTimer)
    persistSettings()
    // fire-and-forget：sync 实现内部自带 catch→回滚→toast；关窗场景 toast/toFriendly
    // 均无意义，渲染进程拆除可能掐断在途 fetch，属尽力而为（比原样丢 500ms 窗口好）。
    void syncMemoryToBackend()
    void syncPostProcessToBackend()
  }
  window.addEventListener?.('pagehide', flushOnPageHide)
  onScopeDispose(() => window.removeEventListener?.('pagehide', flushOnPageHide))

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
   * A1：把某条模型设为默认。mode='ssh' 写 is_default_ssh（对话/终端/定时任务读它），
   * mode='global' 写 is_default（SSH 档未设时后处理与 terminal 的兜底链读它）。
   * 后端两档都是"原子清其他 + 置一"（db.set_default_model），所以本地只需重新拉列表
   * 即可让卡片徽标同步。
   */
  async function setDefaultModel(id: string, mode: ModelDefaultMode = 'ssh'): Promise<boolean> {
    try {
      await api.setDefaultModel(id, mode)
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
