/**
 * 通用模式聊天 Store 工厂
 * 将 SSH 模式中 90% 重复的对话管理逻辑抽取为可复用的工厂函数。
 * 通过 mode + storageKey + modelStorageKey 实现会话/模型/历史完全隔离。
 */
import { computed, triggerRef, watch, ref } from 'vue'
import type { Ref, ComputedRef, ShallowRef } from 'vue'
import { defineStore } from 'pinia'
import { api } from '@/api/client'
import type { ModelConfig } from '@/api/client'
import type { ModeConfig, ChatMode, BaseMessage, BaseConversation } from '@/types/chat'
import { useChatPersistence } from '@/composables/useChatPersistence'
import { useChatStream } from '@/composables/useChatStream'
import { useAppStore } from './app'
import { useToastStore } from './toast'
import { useAgentStore } from './agent'
import { useSettingsStore } from './settings'
import { useSkillStore } from './skills'
import { finalizeAgentMessage } from '@/composables/useAgentIntegration'

/**
 * 工厂内部依赖 chatStore 的模型列表能力。为避免与 chat.ts 形成循环导入
 * （chat.ts 又导入本工厂），改用注册模式：chat.ts 在模块加载时把 useChatStore
 * 注册进来，本工厂仅在运行时通过 _chatStoreGetter 惰性获取。
 */
type ChatStoreGetter = () => ChatStoreLike
let _chatStoreGetter: ChatStoreGetter | null = null
export function registerChatStore(fn: ChatStoreGetter) {
  _chatStoreGetter = fn
}

/** 工厂内部依赖 chatStore 的模型列表相关能力（显式接口打破循环类型推断） */
interface ChatStoreLike {
  models: ModelConfig[]
  fetchModels: () => Promise<void>
}

// ── 通用数据模型 ──

export interface ModeMessage extends BaseMessage {
  /** 模式特有的上下文对象 */
  context?: any
}

export interface ModeConversation extends BaseConversation<ModeMessage> {
}

// ── 配置接口 ──

export interface ModeChatConfig<
  TContext = unknown,
  TMessage extends ModeMessage = ModeMessage,
  TConversation extends BaseConversation<TMessage> = BaseConversation<TMessage>,
> extends ModeConfig<TContext, TMessage, TConversation> {
  /**
   * Agent 运行完成后（completed/failed）的回调钩子，由调用方注入模式特有能力。
   * 通用消息固化（finalizeAgentMessage）已在工厂内部 watch 中统一处理，
   * 此钩子仅用于差异化的后续动作（如 Work 模式延迟刷新会话列表以回填后端生成的标题）。
   */
  onAgentComplete?: (conv: TConversation) => void | Promise<void>
}

// ── 辅助函数 ──

import { genId } from '@/utils/id'

// ── 公共接口 ──

export interface ConvGroup {
  label: string
  items: Array<{ id: string; title: string; updated_at: string }>
}

// ── 工厂函数 ──

export interface ModeChatStore<
  TContext = unknown,
  TMessage extends ModeMessage = ModeMessage,
  TConversation extends BaseConversation<TMessage> = BaseConversation<TMessage>,
> {
  conversations: ShallowRef<TConversation[]>
  currentConvId: Ref<string | null>
  currentConversation: ComputedRef<TConversation | null>
  messages: ComputedRef<TMessage[]>
  streaming: Ref<boolean>
  selectedModelId: Ref<string>
  selectedModel: ComputedRef<ModelConfig | null>
  groupedConversations: ComputedRef<ConvGroup[]>
  persistence: {
    conversations: ShallowRef<TConversation[]>
    currentConvId: Ref<string | null>
    currentConversation: ComputedRef<TConversation | null>
    messages: ComputedRef<TMessage[]>
    selectedModelId: Ref<string>
    groupedConversations: ComputedRef<ConvGroup[]>
    fetchDefaultModel: () => Promise<void>
    setSelectedModel: (id: string) => void
    createConversation: (title?: string) => Promise<TConversation>
    switchConversation: (id: string, loadMessages?: (conv: TConversation) => Promise<void>) => Promise<void>
    deleteConversation: (id: string) => Promise<void>
    ensureBackendConversation: (conv: TConversation) => Promise<void>
    loadConversationsFromBackend: (limit?: number, offset?: number) => Promise<void>
    save: () => void
  }
  fetchDefaultModel: () => Promise<void>
  setSelectedModel: (id: string) => void
  createConversation: (title?: string) => Promise<TConversation>
  switchConversation: (id: string, loadMessages?: (conv: TConversation) => Promise<void>) => Promise<void>
  deleteConversation: (id: string) => Promise<void>
  sendMessage: (content: string, options?: { context?: TContext; systemPrompt?: string; skillIds?: string[] }) => Promise<void>
  abortStream: () => void
  /** 当前 Agent 模式开关（全局状态，影响新消息发送路径） */
  agentMode: Ref<boolean>
  /** 设置当前 Agent 模式，并同步到当前会话的 agentMode 字段 */
  setAgentMode: (val: boolean) => void
  /** 手动触发会话列表响应式更新（shallowRef 内部对象原地变更后调用，如 Agent 模式直接 push 消息） */
  notifyConversationsChanged: () => void
}

const LAST_AGENT_MODE_KEY = 'ckun-last-agent-mode'

function loadLastAgentMode(): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(LAST_AGENT_MODE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw)
      if (parsed && typeof parsed === 'object') return parsed
    }
  } catch {
    // ignore
  }
  return {}
}

function saveLastAgentMode(map: Record<string, boolean>) {
  try {
    localStorage.setItem(LAST_AGENT_MODE_KEY, JSON.stringify(map))
  } catch {
    // ignore
  }
}

export function createModeChatStore<
  TContext = unknown,
  TMessage extends ModeMessage = ModeMessage,
  TConversation extends BaseConversation<TMessage> = BaseConversation<TMessage>,
>(config: ModeChatConfig<TContext, TMessage, TConversation>): ModeChatStore<TContext, TMessage, TConversation> {
  const { mode, buildContext, buildSystemPrompt, createMessage, afterStreamDone } = config
  // 兼容现有调用方：未传 persistence 时默认 dual
  const fullConfig = {
    ...config,
    persistence: config.persistence ?? ('dual' as const),
  } as ModeConfig<TContext, TMessage, TConversation>

  // M5 修复：发送锁，防止快速连点绕过 streaming 守卫导致并发流写入同一会话
  let _sendLock = false

  let persistence: ReturnType<typeof useChatPersistence<TContext, TMessage, TConversation>>
  async function getDefaultModelId(): Promise<string | undefined> {
    const chatStore: ChatStoreLike = _chatStoreGetter!()
    if (chatStore.models.length === 0) await chatStore.fetchModels()
    const sid = persistence.selectedModelId.value
    if (sid && chatStore.models.some((m: ModelConfig) => m.id === sid)) {
      return sid
    }
    try {
      const def = await api.getDefaultModel(mode)
      if (def?.id) return def.id
    } catch { /* ignore */ }
    return chatStore.models[0]?.id
  }
  persistence = useChatPersistence<TContext, TMessage, TConversation>({
    config: fullConfig,
    getDefaultModelId,
  })

  const stream = useChatStream({
    mode: mode as ChatMode,
    getModel: () => selectedModel.value,
  })

  const settingsStore = useSettingsStore()
  // 计算初始值：持久化记忆（开启时） > 全局默认
  const lastMap = loadLastAgentMode()
  const startAgentMode = settingsStore.settings.agent.rememberAgentMode && mode in lastMap
    ? lastMap[mode]
    : settingsStore.settings.agent.defaultAgentMode
  const agentMode = ref<boolean>(startAgentMode)

  /** 设置当前 Agent 模式，并同步保存到当前会话 */
  function setAgentMode(val: boolean) {
    agentMode.value = val
  }

  // 监听 Agent 模式变化，自动同步到当前会话并持久化 per-mode 记忆
  watch(
    agentMode,
    (val) => {
      const conv = persistence.currentConversation.value
      if (conv && conv.agentMode !== val) {
        conv.agentMode = val
        conv.updated_at = new Date().toISOString()
        notifyConversationsChanged()
      }
      if (settingsStore.settings.agent.rememberAgentMode) {
        const map = loadLastAgentMode()
        map[mode] = val
        saveLastAgentMode(map)
      }
    },
    { immediate: false },
  )

  /** 当前选中的模型配置（从 chatStore.models 中查找） */
  const selectedModel = computed<ModelConfig | null>(() => {
    const chatStore: ChatStoreLike = _chatStoreGetter!()
    return chatStore.models.find((m: ModelConfig) => m.id === persistence.selectedModelId.value) ?? chatStore.models[0] ?? null
  })

  // ── 模型选择 ──

  /** 加载该模式的默认模型（独立于其他模式） */
  async function fetchDefaultModel() {
    const chatStore: ChatStoreLike = _chatStoreGetter!()
    if (chatStore.models.length === 0) await chatStore.fetchModels()
    await persistence.fetchDefaultModel()
  }

  function setSelectedModel(id: string) {
    persistence.setSelectedModel(id)
  }

  // ── 会话 CRUD ──

  async function createConversation(title?: string): Promise<TConversation> {
    const conv = await persistence.createConversation(title)
    // 新会话继承当前 Agent 模式开关
    conv.agentMode = agentMode.value
    notifyConversationsChanged()
    return conv
  }

  async function switchConversation(id: string) {
    if (stream.streaming.value) abortStream()
    await persistence.switchConversation(id)
    const conv = persistence.currentConversation.value
    // 切换会话后恢复该会话上次使用的 Agent 模式
    if (conv && typeof conv.agentMode === 'boolean') {
      agentMode.value = conv.agentMode
    }
  }

  async function deleteConversation(id: string) {
    if (stream.streaming.value) abortStream()
    await persistence.deleteConversation(id)
  }

  // ── 发送消息 ──

  async function sendMessage(content: string, options?: {
    context?: TContext
    systemPrompt?: string
    skillIds?: string[]
  }) {
    // M5 修复：原子检查+加锁，消除 streaming 守卫与 startStream 之间的竞态窗口
    if (_sendLock || stream.streaming.value || !content.trim()) return
    _sendLock = true
    try {
      const appStore = useAppStore()
      const toast = useToastStore()
      if (appStore.backendStatus !== 'ready') {
        toast.error('后端未就绪，请稍后重试')
        return
      }

      const chatStore: ChatStoreLike = _chatStoreGetter!()
      if (!persistence.selectedModelId.value || !chatStore.models.some((m: ModelConfig) => m.id === persistence.selectedModelId.value)) {
        await fetchDefaultModel()
      }
      if (chatStore.models.length === 0) {
        toast.error('无可用模型')
        return
      }
      if (!persistence.selectedModelId.value) {
        toast.error('请先选择模型')
        return
      }

      let conv = persistence.currentConversation.value
      if (!conv) {
        const title = content.slice(0, 30) + (content.length > 30 ? '...' : '')
        conv = await persistence.createConversation(title)
      }
      if (!conv) return

      try {
        await persistence.ensureBackendConversation(conv)
      } catch {
        toast.error('创建会话失败')
        return
      }

      const ctx = options?.context ?? (await buildContext?.())
      const baseSystemPrompt = options?.systemPrompt ?? (buildSystemPrompt ? buildSystemPrompt(ctx) : undefined)

      // ── 技能注入：用户显式选择的 skillIds + autoSkillMatch 开启时按触发词智能匹配 ──
      // 与 Work 模式 chat.ts 行为一致：合并去重 → getSkillContext 拼接 → 注入 systemPrompt
      let systemPrompt = baseSystemPrompt
      try {
        const userSkillIds = options?.skillIds ?? []
        const skillStore = useSkillStore()
        const autoMatched = settingsStore.settings.chat.autoSkillMatch
          ? skillStore.matchSkills(content, mode)
          : []
        const allSkillIds = [...new Set([...userSkillIds, ...autoMatched.map(s => s.id)])]
        if (allSkillIds.length > 0) {
          const skillContext = skillStore.getSkillContext(allSkillIds)
          if (skillContext) {
            systemPrompt = systemPrompt ? `${systemPrompt}\n\n${skillContext}` : skillContext
          }
        }
      } catch (e) {
        console.warn('[createModeChatStore] 技能上下文构建失败（降级为无技能）:', e)
      }

      const userMsg: TMessage = createMessage
        ? createMessage('user', content, ctx)
        : {
            id: genId(),
            role: 'user',
            content,
            context: ctx,
            created_at: new Date().toISOString(),
          } as TMessage
      conv.messages.push(userMsg)

      const assistantMsg: TMessage = createMessage
        ? createMessage('assistant', '', ctx)
        : {
            id: genId(),
            role: 'assistant',
            content: '',
            streamingContent: '',
            streamingReasoning: '',
            created_at: new Date().toISOString(),
          } as TMessage
      if (assistantMsg.streamingContent === undefined) assistantMsg.streamingContent = ''
      if (assistantMsg.streamingReasoning === undefined) assistantMsg.streamingReasoning = ''
      conv.messages.push(assistantMsg)

      // 仅 dual 模式在本地做临时标题占位；backend 模式由后端 post_process 统一生成核心摘要标题，
      // 避免本地抢先写入导致后端跳过标题生成。
      if (fullConfig.persistence !== 'backend' && conv.messages.length <= 2 && conv.title === 'New Chat') {
        conv.title = content.slice(0, 30) + (content.length > 30 ? '...' : '')
      }

      conv.updated_at = new Date().toISOString()
      triggerRef(persistence.conversations)
      persistence.save()

      // M1 修复：requestAnimationFrame 节流，将多个 token 合并为单次渲染，避免高 token 速率下的卡顿
      let rafScheduled = false
      const scheduleRender = () => {
        if (rafScheduled) return
        rafScheduled = true
        requestAnimationFrame(() => {
          rafScheduled = false
          triggerRef(persistence.conversations)
        })
      }

      const ok = await stream.startStream<TMessage, TConversation>({
        content,
        conversation: conv,
        systemPrompt,
        onContent: (msg, token) => {
          msg.streamingContent = (msg.streamingContent || '') + token
          scheduleRender()
        },
        onReasoning: (msg, token) => {
          msg.streamingReasoning = (msg.streamingReasoning || '') + token
          scheduleRender()
        },
        onError: (msg, err, duration) => {
          msg.duration = duration
          msg.content = msg.streamingContent || ''
          msg.reasoning = msg.streamingReasoning || undefined
          msg.streamingContent = undefined
          msg.streamingReasoning = undefined
          if (!msg.content) {
            msg.content = `错误: ${err}`
          }
          conv.updated_at = new Date().toISOString()
          triggerRef(persistence.conversations)
          persistence.save()
        },
        onDone: async (msg, duration) => {
          msg.duration = duration
          msg.content = msg.streamingContent || ''
          msg.reasoning = msg.streamingReasoning || undefined
          msg.streamingContent = undefined
          msg.streamingReasoning = undefined
          conv.updated_at = new Date().toISOString()
          triggerRef(persistence.conversations)
          persistence.save()
          await afterStreamDone?.(conv)
        },
      })

      if (!ok) {
        toast.error('启动流式失败')
      }
    } finally {
      // Optimization 修复：所有早返回路径（后端未就绪/无模型/创建失败等）均走此 finally，防止锁永久占用
      _sendLock = false
    }
  }

  // ── 中止流 ──

  function abortStream() {
    stream.abortStream()
    const conv = persistence.currentConversation.value
    if (conv) {
      const last = conv.messages[conv.messages.length - 1]
      if (last && last.role === 'assistant') {
        last.content = last.streamingContent || '(已中止)'
        last.reasoning = last.streamingReasoning || undefined
        last.streamingContent = undefined
        last.streamingReasoning = undefined
      }
      conv.updated_at = new Date().toISOString()
      triggerRef(persistence.conversations)
      persistence.save()
    }
  }

  // ── 返回通用 API ──

  /** 手动触发会话列表响应式更新（shallowRef 内部对象原地变更后调用，如 Agent 模式直接 push 消息） */
  function notifyConversationsChanged() {
    triggerRef(persistence.conversations)
    persistence.save()
  }

  // ── Agent 完成固化（store 层 watch，不依赖组件生命周期）──
  // 修复 P3-4：跨模式切换时面板卸载不会丢失 Agent 结果。
  const agentStore = useAgentStore()
  watch(() => agentStore.status, (newStatus, oldStatus) => {
    if (agentStore.currentMode !== mode) return
    if (oldStatus === 'running' && (newStatus === 'completed' || newStatus === 'failed')) {
      const conv = persistence.currentConversation.value
      if (!conv) return
      finalizeAgentMessage(conv, agentStore)
      notifyConversationsChanged()
      config.onAgentComplete?.(conv)
    }
  })

  return {
    // State
    ...persistence,
    persistence,
    streaming: stream.streaming,
    selectedModel,
    // Model
    fetchDefaultModel,
    setSelectedModel,
    // Conversation
    createConversation,
    switchConversation,
    deleteConversation,
    // Message
    sendMessage,
    abortStream,
    agentMode,
    setAgentMode,
    notifyConversationsChanged,
  }
}
