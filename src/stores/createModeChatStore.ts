/**
 * 通用模式聊天 Store 工厂
 * 将 SSH 模式中 90% 重复的对话管理逻辑抽取为可复用的工厂函数。
 * 通过 mode + storageKey + modelStorageKey 实现会话/模型/历史完全隔离。
 */
import { computed, triggerRef, watch, ref } from 'vue'
import type { Ref, ComputedRef, ShallowRef } from 'vue'
import { defineStore } from 'pinia'
import { api, type ChatAttachment } from '@/api/client'
import type { ModelConfig } from '@/api/client'
import type { ModeConfig, ChatMode, BaseMessage, BaseConversation } from '@/types/chat'
import { useChatPersistence } from '@/composables/useChatPersistence'
import { useChatStream } from '@/composables/useChatStream'
import { useAppStore } from './app'
import { useToastStore } from './toast'
import { useAgentStore } from './agent'
import { useSettingsStore } from './settings'
import { useSkillStore } from './skills'
import { finalizeAgentMessage, type AgentMessage } from '@/composables/useAgentIntegration'

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
  // M18：返回 Promise<boolean>（true=已接受入会话，false=早退拒绝），供组件回滚草稿
  sendMessage: (content: string, options?: { context?: TContext; systemPrompt?: string; skillIds?: string[]; attachments?: ChatAttachment[] }) => Promise<boolean>
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
  // 计算初始值：代码默认值 > 持久化记忆。
  // 修复：原实现 lastMap[mode] 覆盖 defaultAgentMode — 用户 localStorage 中
  // 保存了旧版 defaultAgentMode=true 的记忆，即使代码已改为 false，
  // rememberAgentMode 仍从 lastMap 恢复 true，导致所有消息走 /agent/run，
  // /chat/stream 从未被调用，普通对话完全无反应。现在 AND 双重校验：
  // 只有代码默认值也为 true 时才信任持久化记忆，否则回退到代码默认值。
  const lastMap = loadLastAgentMode()
  const savedMode = settingsStore.settings.agent.rememberAgentMode && mode in lastMap
    ? lastMap[mode]
    : undefined
  const startAgentMode = savedMode !== undefined
    ? savedMode && settingsStore.settings.agent.defaultAgentMode
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
    // 不再从会话恢复 agentMode — 用户的显式切换优先。
    // 原实现：切换到 Agent 模式创建的旧会话时 agentMode 被强制恢复为 true，
    // 用户明明已切到普通模式，消息却仍走 /agent/run，/chat/stream 从未被调用。
  }

  async function deleteConversation(id: string) {
    if (stream.streaming.value) abortStream()
    await persistence.deleteConversation(id)
  }

  // ── 发送消息 ──

  // M18：返回是否被接受 — true=消息已入会话（草稿可清空），false=早退拒绝
  // （后端未就绪/无模型/未选模型/建会话失败），供组件回滚草稿，避免长文/附件丢失
  async function sendMessage(content: string, options?: {
    context?: TContext
    systemPrompt?: string
    skillIds?: string[]
    attachments?: ChatAttachment[]
  }): Promise<boolean> {
    // M5 修复：原子检查+加锁，消除 streaming 守卫与 startStream 之间的竞态窗口
    if (_sendLock || stream.streaming.value) {
      console.log('[factory] sendMessage BLOCKED: _sendLock=', _sendLock, 'streaming=', stream.streaming.value)
      return false
    }
    // 允许"仅附件"的空消息（attachments 已注入上下文时）
    if (!content.trim() && !(options?.attachments && options.attachments.length > 0)) return false
    _sendLock = true
    try {
      const appStore = useAppStore()
      const toast = useToastStore()
      if (appStore.backendStatus !== 'ready') {
        toast.error('后端未就绪，请稍后重试')
        return false
      }

      const chatStore: ChatStoreLike = _chatStoreGetter!()
      // 竞态修复：chat.ts 的 fetchModels 可能尚未完成（两个 store 的
      // backendStatus watcher 并发触发），主动确保模型列表已加载
      if (chatStore.models.length === 0) {
        await chatStore.fetchModels()
      }
      if (!persistence.selectedModelId.value || !chatStore.models.some((m: ModelConfig) => m.id === persistence.selectedModelId.value)) {
        await fetchDefaultModel()
      }
      if (chatStore.models.length === 0) {
        toast.error('无可用模型，请在设置中配置模型')
        return false
      }
      if (!persistence.selectedModelId.value) {
        toast.error('请先选择模型')
        return false
      }

      let conv = persistence.currentConversation.value
      if (!conv) {
        const title = content.slice(0, 30) + (content.length > 30 ? '...' : '')
        try {
          conv = await persistence.createConversation(title)
        } catch (e) {
          // createConversation（backend 策略走 api.createConversation）失败时
          // 原实现异常直接穿透 → handleSend 的 await 未捕获 → 无气泡、无请求、
          // 无提示，表现为"普通对话没有反应"。与下方 ensureBackendConversation
          // 的兜底语义对齐：toast + 早退。
          console.error('[factory] createConversation failed:', e)
          toast.error('创建会话失败')
          return false
        }
      }
      if (!conv) return false

      try {
        await persistence.ensureBackendConversation(conv)
      } catch {
        toast.error('创建会话失败')
        return false
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

      let ok = false
      try {
        ok = await stream.startStream<TMessage, TConversation>({
        content,
        conversation: conv,
        systemPrompt,
        attachments: options?.attachments,
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
          // 审查修复：流中断时保留已生成的部分内容并追加错误说明；
          // 原实现在有部分输出时丢弃错误原因，用户无法知道回答为何中断
          const partial = msg.streamingContent || ''
          msg.content = partial
            ? `${partial}\n\n⚠️ 响应中断: ${err}`
            : `错误: ${err}`
          msg.reasoning = msg.streamingReasoning || undefined
          msg.streamingContent = undefined
          msg.streamingReasoning = undefined
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
      } catch (e) {
        // startStream 抛异常（如 backendConvId 缺失、getModel 抛错）时兜底：
        // 原实现异常直接变成 unhandled rejection — streaming 卡 true、无 toast、
        // 后续发送全被 props.streaming 拦截，用户表现为"普通对话没有反应"。
        console.error('[factory] startStream threw:', e)
        ok = false
        if (stream.streaming.value) stream.streaming.value = false
      }

      if (!ok) {
        // 安全兜底：startStream 返回 false 时若 streaming 仍为 true（边界竞态），
        // 强制重置，避免后续所有发送被 SideChatPanel 的 props.streaming 拦截
        if (stream.streaming.value) stream.streaming.value = false
        toast.error('启动流式失败')
      }
      // 消息已入会话（userMsg/assistantMsg 已 push 并持久化）— 即便流启动失败，
      // 内容也保留在历史中，草稿无需回填（回填会重复），返回 true 清空草稿
      return true
    } finally {
      // Optimization 修复：所有早返回路径（后端未就绪/无模型/创建失败等）均走此 finally，防止锁永久占用
      _sendLock = false
    }
  }

  // ── 中止流 ──

  function abortStream() {
    // 审查修复：仅当流确实在运行时才固化"已中止"占位。
    // 原实现无条件重写最后一条助手消息 —— closeAiPanel()/中止按钮在
    // 无流时也会调用本方法，导致已完成的回答被覆盖成 "(已中止)" 并持久化，
    // 破坏历史消息。
    const wasStreaming = stream.streaming.value
    stream.abortStream()
    if (!wasStreaming) return
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
      // 同一 mode 存在多个 store 实例（chat.ts / ssh-ai.ts 各建一个），
      // 共享同一个 agentStore，Agent 结束时所有实例的 watch 都会触发。
      // 非发起方实例的 persistence 列表里没有占位消息（占位只存在于发起方
      // 实时列表），兜底会退到该实例【自己的当前会话】执行固化/清理 →
      // 误固化到错误会话、甚至误删用户当前会话。
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
