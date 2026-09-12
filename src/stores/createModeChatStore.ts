/**
 * 通用模式聊天 Store 工厂
 * 将 SSH 模式中 90% 重复的对话管理逻辑抽取为可复用的工厂函数。
 * 通过 mode + storageKey + modelStorageKey 实现会话/模型/历史完全隔离。
 */
import { computed, triggerRef, watch } from 'vue'
import type { Ref, ComputedRef, ShallowRef } from 'vue'
import { defineStore } from 'pinia'
import { api, type ModelConfig } from '@/api/client'
import type { ModeConfig, BaseMessage, BaseConversation } from '@/types/chat'
import { useChatPersistence } from '@/composables/useChatPersistence'
import { useAgentStore } from './agent'
import { useModelsStore } from './models'
import { finalizeAgentMessage, type AgentMessage } from '@/utils/finalizeAgentMessage'

/** 模型列表由独立的 models store 统一持有（避免与调用方 store 循环导入） */

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
   * 此钩子仅用于差异化的后续动作（如延迟刷新会话列表以回填后端生成的标题）。
   */
  onAgentComplete?: (conv: TConversation) => void | Promise<void>
}

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
  /** 手动触发会话列表响应式更新（shallowRef 内部对象原地变更后调用，如 Agent 模式直接 push 消息） */
  notifyConversationsChanged: () => void
}

export function createModeChatStore<
  TContext = unknown,
  TMessage extends ModeMessage = ModeMessage,
  TConversation extends BaseConversation<TMessage> = BaseConversation<TMessage>,
>(config: ModeChatConfig<TContext, TMessage, TConversation>): ModeChatStore<TContext, TMessage, TConversation> {
  const { mode } = config
  // 兼容现有调用方：未传 persistence 时默认 dual
  const fullConfig = {
    ...config,
    persistence: config.persistence ?? ('dual' as const),
  } as ModeConfig<TContext, TMessage, TConversation>

  let persistence: ReturnType<typeof useChatPersistence<TContext, TMessage, TConversation>>
  async function getDefaultModelId(): Promise<string | undefined> {
    const modelsStore = useModelsStore()
    if (modelsStore.models.length === 0) await modelsStore.fetchModels()
    const sid = persistence.selectedModelId.value
    if (sid && modelsStore.models.some((m: ModelConfig) => m.id === sid)) {
      return sid
    }
    try {
      const def = await api.getDefaultModel(mode)
      if (def?.id) return def.id
    } catch { /* ignore */ }
    return modelsStore.models[0]?.id
  }
  persistence = useChatPersistence<TContext, TMessage, TConversation>({
    config: fullConfig,
    getDefaultModelId,
  })

  /** 当前选中的模型配置（从 models store 中查找） */
  const selectedModel = computed<ModelConfig | null>(() => {
    const modelsStore = useModelsStore()
    return modelsStore.models.find((m: ModelConfig) => m.id === persistence.selectedModelId.value) ?? modelsStore.models[0] ?? null
  })

  // ── 模型选择 ──

  /** 加载该模式的默认模型（独立于其他模式） */
  async function fetchDefaultModel() {
    const modelsStore = useModelsStore()
    if (modelsStore.models.length === 0) await modelsStore.fetchModels()
    await persistence.fetchDefaultModel()
  }

  function setSelectedModel(id: string) {
    persistence.setSelectedModel(id)
  }

  // ── 会话 CRUD ──

  async function createConversation(title?: string): Promise<TConversation> {
    const conv = await persistence.createConversation(title)
    notifyConversationsChanged()
    return conv
  }

  async function switchConversation(id: string) {
    await persistence.switchConversation(id)
  }

  async function deleteConversation(id: string) {
    await persistence.deleteConversation(id)
  }

  // ── 返回通用 API ──

  /** 手动触发会话列表响应式更新（shallowRef 内部对象原地变更后调用，如 Agent 模式直接 push 消息） */
  let notifySaveTimer: ReturnType<typeof setTimeout> | null = null
  function notifyConversationsChanged() {
    triggerRef(persistence.conversations)
    // 防抖写入 localStorage，避免 Agent 流式输出期间频繁序列化
    if (notifySaveTimer) clearTimeout(notifySaveTimer)
    notifySaveTimer = setTimeout(() => {
      notifySaveTimer = null
      persistence.save()
    }, 300)
  }

  // ── Agent 完成固化（store 层 watch，不依赖组件生命周期）──
  // 修复 P3-4：跨模式切换时面板卸载不会丢失 Agent 结果。
  const agentStore = useAgentStore()
  watch(() => agentStore.status, (newStatus, oldStatus) => {
    if (agentStore.currentMode !== mode) return
    // M17：abort 使状态 running→idle（非 completed/failed），原 watch 漏捕 →
    // temp-agent 占位消息（空内容）永久残留历史。补 running→idle 分支，
    // 以 aborted 标志区分"用户中止"与 C10 并发保护的直接 abortFn()（后者不固化）。
    const isTerminal = oldStatus === 'running' && (newStatus === 'completed' || newStatus === 'failed')
    const isAbort = oldStatus === 'running' && newStatus === 'idle' && agentStore.aborted
    if (!isTerminal && !isAbort) return
    {
      // 审查 M-19 修复：优先固化为本次运行的【源会话】（agentStore 在 runAgent
      // 时记录的 conversationId）。原实现取"完成时刻的当前会话" — 用户在 Agent
      // 运行中切换会话后，固化会落到错误的会话上：源会话的 temp-agent 占位消息
      // 永远停在"运行中"骨架态，结果消息丢失。
      const sourceId = agentStore.currentConversationId
      // 审查修复（P1）：移除 `|| persistence.currentConversation` 兜底 —
      // 同一 mode 若存在多个 store 实例，共享同一个 agentStore，Agent 结束时
      // 所有实例的 watch 都会触发。非发起方实例的 persistence 列表里没有占位
      // 消息（占位只存在于发起方实时列表），兜底会退到该实例【自己的当前会话】
      // 执行固化/清理 → 误固化到错误会话、甚至误删用户当前会话。
      // 发起方列表必含源会话（c.id === sourceId 或 backendConvId 双键命中），
      // 找不到即说明本实例非发起方，直接返回让发起方实例处理。
      const conv = sourceId
        ? persistence.conversations.value.find(
            (c) => c.id === sourceId || c.backendConvId === sourceId,
          )
        : null
      if (!conv) return
      // 审查修复：Agent 运行中用户切换/删除会话后，源会话可能已不在列表，
      // 占位消息（temp-agent-）将永远停在运行中骨架态。此时移除悬空占位，
      // 避免历史里出现卡死的空消息。
      const hasPlaceholder = conv.messages.some(
        (m) => m.role === 'assistant' && String(m.id).startsWith('temp-agent-'),
      )
      if (!hasPlaceholder) {
        conv.messages = conv.messages.filter(
          (m) => !(m.role === 'assistant' && String(m.id).startsWith('temp-agent-')),
        )
        if (conv.messages.length === 0) {
          // 空会话（仅剩占位）直接移除，避免历史列表出现空条目
          persistence.conversations.value = persistence.conversations.value.filter((c) => c.id !== conv.id)
          if (persistence.currentConvId.value === conv.id) {
            persistence.currentConvId.value = persistence.conversations.value[0]?.id ?? null
          }
        }
        notifyConversationsChanged()
        return
      }
      // 泛型边界：TConversation.messages 为 TMessage[]（自由类型参数），
      // 与 AgentMessage[] 不可直接互证，显式断言（TMessage extends ModeMessage
      // 结构上满足 AgentMessage 最小接口）
      finalizeAgentMessage(conv as unknown as { messages: AgentMessage[] }, agentStore)
      notifyConversationsChanged()
      // 中止不是"完成" — 仅正常终态（completed/failed）触发 onAgentComplete 钩子
      if (isTerminal) config.onAgentComplete?.(conv)
    }
  })

  return {
    // State
    ...persistence,
    persistence,
    selectedModel,
    // Model
    fetchDefaultModel,
    setSelectedModel,
    // Conversation
    createConversation,
    switchConversation,
    deleteConversation,
    notifyConversationsChanged,
  }
}
