/**
 * 聊天 Pinia Store — 消息、会话、模型选择（SSH 模式）
 *
 * 核心对话逻辑委托给 createModeChatStore 工厂，本文件保留 SSH 模式特有功能：
 * 模型列表、技能上下文等。
 */
import { ref, computed, watch } from 'vue'
import type { Ref } from 'vue'
import { defineStore } from 'pinia'
import {
  api,
  toFriendlyMessage,
  type Conversation,
  type Message,
  type ModelConfig,
  type ChatAttachment,
  type AgentFileChangesSummary,
  type AgentTraceSnapshot,
} from '@/api/client'
import { useAppStore } from './app'

import { useToastStore } from './toast'
import { createModeChatStore, registerChatStore } from './createModeChatStore'
import type { ModeMessage, ModeConversation, ModeChatStore } from './createModeChatStore'

export interface ChatMessage extends Message {
  /** 流式输出时实时累积的内容 */
  streamingContent?: string
  /** 流式输出时的推理内容 */
  streamingReasoning?: string
  /** 流式耗时（秒，保留1位小数） */
  duration?: number
  /** Agent 模式产生的 assistant 消息会带上 runId，用于打开 TraceViewer 查看轨迹 */
  runId?: string
  /** C3: Agent 模式产生的 assistant 消息会带上文件变更摘要，供操作栏徽标展示 */
  fileChanges?: AgentFileChangesSummary
  /** Agent 模式产生的 assistant 消息会带上执行过程快照，使历史会话也能显示过程摘要 */
  agentTrace?: AgentTraceSnapshot
}

export const useChatStore = defineStore('chat', () => {
  const appStore = useAppStore()
  const toast = useToastStore()

  // ── 核心对话能力（委托给工厂，SSH 模式后端持久化） ──
  const base: ModeChatStore<unknown, ModeMessage, ModeConversation> = createModeChatStore({
    mode: 'ssh',
    persistence: 'backend',
    storageKey: '',
    modelStorageKey: '',
    buildSystemPrompt: () => '',
    createMessage: (role, content) => ({
      id: `temp-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
      role,
      content,
      tokens_used: 0,
      created_at: new Date().toISOString(),
    } as ModeMessage),
    afterStreamDone: async (conv) => {
      // 先刷新会话列表（标题/排序可能因后端 post_process 更新），
      // 再重新加载当前会话消息体——避免 fetchConversations 把列表接口不返回的 messages 清空
      await fetchConversations()
      if (currentConversationId.value === conv.id) {
        await loadMessages(conv.id)
      }
      // 后端标题生成是异步后处理，延迟再刷新一次以回填生成的标题
      scheduleDelayedRefresh(1800)
    },
    // Agent 完成后延迟刷新会话列表（后端 post_process_after_turn 异步生成标题），
    // 并重新加载消息体。固化逻辑已在工厂 store 层 watch 中完成，此处仅补标题回填。
    onAgentComplete: async (conv) => {
      scheduleDelayedRefresh(5000, conv.id)
    },
  })

  // ── State ──
  const conversations = base.conversations as unknown as Ref<Conversation[]>
  const currentConversationId = base.currentConvId
  // C-review NIT：原 computed setter 无调用方（死代码）— 写路径都在工厂层
  // 直接操作 base.conversations，改为只读 computed
  const messages = computed<ChatMessage[]>(() => base.messages.value as ChatMessage[])
  const streaming = base.streaming
  const models = ref<ModelConfig[]>([])
  const selectedModelId = base.selectedModelId
  const agentMode = base.agentMode
  const setAgentMode = base.setAgentMode

  // ── Computed ──
  const selectedModel = base.selectedModel

  // ── Actions ──

  // in-flight 守卫：并发触发只下发一次 api.getModels()，消除启动阶段的重复请求。
  let fetchModelsPromise: Promise<void> | null = null
  async function fetchModels() {
    if (fetchModelsPromise) return fetchModelsPromise
    fetchModelsPromise = (async () => {
      try {
        const result = await api.getModels()
        models.value = result

        // 如果尚未选择模型，或者当前选的模型已不在列表里，则自动选 SSH 模式默认模型
        if (selectedModelId.value && models.value.some(m => m.id === selectedModelId.value)) {
          return
        }
        try {
          const defaultModel = await api.getDefaultModel('ssh')
          if (defaultModel?.id) {
            selectedModelId.value = defaultModel.id
          }
        } catch {
          // 取不到默认模型则选列表第一个
          if (models.value.length > 0) {
            selectedModelId.value = models.value[0].id
          }
        }
      } catch (e) {
        console.error('[chatStore] fetchModels failed:', e)
        models.value = []
      } finally {
        fetchModelsPromise = null
      }
    })()
    return fetchModelsPromise
  }

  // 后端就绪后自动初始化
  let initPromise: Promise<void> | null = null
  function ensureLoaded() {
    if (initPromise) return initPromise
    if (appStore.backendStatus !== 'ready') return
    initPromise = (async () => {
      if (models.value.length === 0) await fetchModels()
      if (conversations.value.length === 0) await fetchConversations()
    })()
    return initPromise
  }
  // 后端就绪后拉取模型和会话（ensureLoaded 已覆盖 fetchModels，无需重复 watch）
  watch(() => appStore.backendStatus, ensureLoaded, { immediate: true })

  async function fetchConversations() {
    // 拉取 SSH 模式的会话
    await base.persistence.loadConversationsFromBackend(50, 0)
  }

  async function createConversation(title?: string): Promise<Conversation> {
    // 工厂 createConversation 处理 API 调用 + conversations 插入 + currentConvId 设置
    const conv = await base.createConversation(title)
    await fetchConversations()
    // 返回 API 格式的 Conversation 对象
    return {
      id: conv.id,
      title: conv.title,
      status: 'completed' as const,
      mode: 'ssh',
      created_at: conv.created_at,
      updated_at: conv.updated_at,
    }
  }

  async function switchConversation(id: string) {
    // 流式中切换会话先中止，避免旧流继续向已不是当前的会话写入
    if (streaming.value) abortStream()
    base.currentConvId.value = id
    await loadMessages(id)
  }

  async function loadMessages(conversationId: string) {
    const targetId = conversationId
    try {
      // C-review MINOR-11：分页拉全量历史 — 原实现只取最近 50 条，长会话的
      // 更早消息被静默截断且无任何提示。循环 offset 翻页（新→旧窗口），
      // 更早的页 prepend 到前面；上限 1000 条，触顶时明确提示。
      const PAGE_SIZE = 50
      const MAX_MESSAGES = 1000
      let rawMessages: Message[] = []
      let offset = 0
      while (offset < MAX_MESSAGES) {
        const page = await api.getMessages(targetId, PAGE_SIZE, offset)
        rawMessages = [...page, ...rawMessages]
        if (page.length < PAGE_SIZE) break
        offset += PAGE_SIZE
      }
      if (offset >= MAX_MESSAGES && base.currentConvId.value === targetId) {
        toast.warning(`该会话消息超过 ${MAX_MESSAGES} 条，仅加载最近 ${MAX_MESSAGES} 条`)
      }
      // Optimization 修复：await 后校验当前会话未改变，避免快速切换时消息串台
      if (base.currentConvId.value !== targetId) return
      // 映射后端的 snake_case 字段到前端 ChatMessage 的 camelCase 字段
      const mapped: ChatMessage[] = rawMessages.map((msg) => ({
        ...msg,
        runId: msg.run_id,
        agentTrace: msg.agent_trace,
        // 从持久化的 agent_trace 还原结构化字段，避免 5s 回填后文件变更/耗时丢失
        fileChanges: msg.agent_trace?.fileChanges ?? undefined,
        duration: msg.agent_trace?.duration ?? undefined,
      }))
      base.conversations.value = base.conversations.value.map((c) =>
        c.id === targetId ? { ...c, messages: mapped as ModeMessage[] } : c
      )
    } catch (e) {
      // B15 修复：失败时保留旧 messages（不清空），并 toast 提示 —
      // 原实现把 messages 置 [] 且无任何提示，用户看到空会话无法得知原因
      if (base.currentConvId.value !== targetId) return
      console.error('[chatStore] loadMessages failed:', e)
      toast.error('消息加载失败，请重试')
    }
  }

  // ── 延迟刷新定时器管理（用于后端标题回填）──
  // 审查 M-17 修复：原实现是【模块级】函数，回调里引用的 fetchConversations /
  // currentConversationId / loadMessages 都定义在 store 闭包内 — 模块作用域根本
  // 看不到它们：typecheck 报 TS2304，且定时器真正触发时抛 ReferenceError，
  // 回调末尾的 splice 永远执行不到，_refreshTimers 数组无界增长（每次对话都泄漏）。
  // 移入闭包后引用正确，定时器登记也收敛在本 store 生命周期内。
  const _refreshTimers: ReturnType<typeof setTimeout>[] = []
  function scheduleDelayedRefresh(delay: number, convId?: string) {
    const timer = setTimeout(async () => {
      try {
        await fetchConversations()
        const targetId = convId ?? currentConversationId.value
        if (targetId) {
          await loadMessages(targetId)
        }
      } catch (e) {
        // 刷新失败不影响主流程（下次对话会再触发），但必须捕获，
        // 否则 unhandled rejection 且定时器登记无法清理
        console.warn('[chatStore] 延迟刷新失败:', e)
      } finally {
        const idx = _refreshTimers.indexOf(timer)
        if (idx >= 0) _refreshTimers.splice(idx, 1)
      }
    }, delay)
    _refreshTimers.push(timer)
  }

  async function deleteConversation(id: string) {
    // B23 修复：失败时 toast 提示（原实现静默失败，调用方还误以为删掉了）
    try {
      await api.deleteConversation(id)
    } catch (e) {
      console.error('[chatStore] deleteConversation failed:', e)
      toast.error(toFriendlyMessage(e, '删除会话失败，请重试'))
      return
    }
    const prevCurrent = base.currentConvId.value
    base.conversations.value = base.conversations.value.filter((c) => c.id !== id)
    // 与 IDE/SSH 工厂逻辑保持一致：删除当前会话后跳到列表第一条，无会话则回空态
    if (base.currentConvId.value === id) {
      base.currentConvId.value = base.conversations.value[0]?.id ?? null
    }
    // B23 修复：current 变化后重新加载新 current 会话的消息
    // （原实现切换后消息区仍是已删除会话的旧内容）
    const nextCurrent = base.currentConvId.value
    if (nextCurrent && nextCurrent !== prevCurrent) {
      await loadMessages(nextCurrent)
    }
  }

  async function updateConversationTitle(id: string, title: string) {
    const updated = await api.updateConversationTitle(id, title)
    base.conversations.value = base.conversations.value.map((c) =>
      c.id === id
        ? { ...c, title: updated.title, updated_at: updated.updated_at }
        : c
    )
  }

  /** 发送消息 (完整的流式对话流程) */
  async function sendMessage(content: string, skillIds: string[] = [], attachments: ChatAttachment[] = []): Promise<void> {
    if (streaming.value) return
    // 允许"仅附件"的空消息
    if (!content.trim() && attachments.length === 0) return

    // 后端就绪检查
    const toast = useToastStore()
    if (appStore.backendStatus !== 'ready') {
      toast.error('后端未就绪，请稍后重试')
      return
    }
    // 模型检查
    if (!selectedModelId.value) {
      toast.error('请先选择模型')
      return
    }

    if (!currentConversationId.value) {
      // Optimization 修复：自动创建会话失败时捕获错误，避免变为未捕获 rejection
      try {
        // 新建会话时不传标题，让后端 post_process 在第一轮完成后自动生成核心摘要标题
        await createConversation()
      } catch (e) {
        console.error('[chatStore] auto-create conversation failed:', e)
        toast.error('创建会话失败，请重试')
        return
      }
    }

    // 技能注入统一由 createModeChatStore 工厂完成（显式 skillIds + autoSkillMatch 自动匹配，
    // 合并去重后单次注入 systemPrompt）。此处不再重复 matchSkills/getSkillContext，
    // 避免最终 systemPrompt 中出现两份技能上下文。
    await base.sendMessage(content, {
      skillIds: [...new Set(skillIds)],
      attachments,
    })
  }

  /** 创建新会话并发送消息，返回新会话 ID */
  async function sendMessageToNewConversation(content: string, title?: string): Promise<string> {
    base.currentConvId.value = null
    const convTitle = title || undefined
    // 若未选择模型，先尝试加载并自动选中默认/第一个模型
    if (!selectedModelId.value) {
      await fetchModels()
      if (!selectedModelId.value) {
        const def = models.value.find(m => m.is_default) || models.value[0]
        if (def) selectedModelId.value = def.id
      }
    }
    const conv = await createConversation(convTitle)
    base.currentConvId.value = conv.id
    await sendMessage(content)
    return conv.id
  }

  function abortStream() {
    base.abortStream()
  }

  return {
    conversations,
    currentConversationId,
    messages,
    streaming,
    models,
    selectedModelId,
    selectedModel,
    fetchModels,
    fetchConversations,
    loadMessages,
    createConversation,
    switchConversation,
    deleteConversation,
    updateConversationTitle,
    sendMessage,
    sendMessageToNewConversation,
    abortStream,
    agentMode,
    setAgentMode,
    notifyConversationsChanged: base.notifyConversationsChanged,
  }
})

// 向模式工厂注册本 store，打破与 createModeChatStore 的循环导入（见工厂内 registerChatStore 说明）
registerChatStore(useChatStore)
