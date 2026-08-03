/**
 * SSH 模式 AI 运维助手 Store
 * 通用对话能力委托给 createModeChatStore，本文件仅保留 SSH 特有能力。
 */
import { ref, computed, watch } from 'vue'
import { defineStore } from 'pinia'
import { useSSHStore } from './ssh'
import { useAgentStore } from './agent'
import { useAppStore } from './app'
import { useSettingsStore } from './settings'
import { useToastStore } from './toast'
import { createModeChatStore, type ModeMessage, type ModeConversation } from './createModeChatStore'

// ── SSH 特有数据模型 ──

export interface SshAiContext {
  connectionName?: string
  host?: string
  username?: string
  sessionType?: 'local' | 'remote'
  shell?: string
  /** 当前活动终端/SSH 会话 ID，Agent 模式下透传给后端供 shell_exec 远程执行 */
  sessionId?: string
}

/** SSH 消息（兼容 ModeMessage，context 强类型为 SshAiContext） */
export interface SshAiMessage extends ModeMessage {
  context?: SshAiContext
}

export interface SshAiConversation extends ModeConversation {
  messages: SshAiMessage[]
}

import { genId } from '@/utils/id'

export const useSshAiStore = defineStore('ssh-ai', () => {
  // ── 1. 通用对话能力（委托给 createModeChatStore） ──
  const base = createModeChatStore({
    mode: 'ssh',
    storageKey: 'ckun-ssh-ai-conversations',
    modelStorageKey: 'ckun-ssh-ai-selected-model',
  })

  // ── 2. SSH 特有 State ──
  const showAiPanel = ref(false)
  // Agent 模式开关由 createModeChatStore 工厂统一持有，实现默认开启与切换持久化
  const agentMode = base.agentMode
  const setAgentMode = base.setAgentMode

  const agentStore = useAgentStore()
  const appStore = useAppStore()
  const toast = useToastStore()

  const agentRunning = computed(() => agentStore.isRunning)

  // 后端就绪后自动加载模型，复用 chatStore 共享的 models
  let modelInitPromise: Promise<void> | null = null
  function ensureModelLoaded() {
    if (modelInitPromise) return modelInitPromise
    if (appStore.backendStatus !== 'ready') return
    modelInitPromise = (async () => {
      if (base.selectedModelId.value) return
      await base.fetchDefaultModel()
    })()
    return modelInitPromise
  }
  watch(() => appStore.backendStatus, ensureModelLoaded, { immediate: true })

  function toggleAiPanel() {
    showAiPanel.value = !showAiPanel.value
  }

  // ── 3. SSH 上下文构建：从当前活动 SSH 会话获取信息 ──
  function buildContext(): SshAiContext | undefined {
    const sshStore = useSSHStore()
    const session = sshStore.activeSession
    if (!session) return undefined

    if (session.type === 'remote') {
      const conn = sshStore.connections.find(c => c.id === session.connectionId)
      // 连接已被删除时回退到会话自带的展示信息，避免向后端发送 undefined
      return {
        sessionType: 'remote',
        connectionName: conn?.name || session.name,
        host: conn?.host,
        username: conn?.username,
        sessionId: session.id,
      }
    } else {
      return {
        sessionType: 'local',
        shell: session.shell,
        // 本地终端不传递 sessionId，避免 shell_exec 误判为 SSH 远程会话
      }
    }
  }

  // ── 4. System Prompt 构建：运维专家 + SSH 环境信息 ──
  function buildSystemPrompt(ctx?: SshAiContext): string {
    // 模式基础 prompt（"运维助手"）由后端根据 mode='ssh' 注入
    let prompt = ''

    if (ctx?.sessionType === 'remote') {
      prompt += `用户当前已通过 SSH 连接到服务器：${ctx.connectionName || ''}（${ctx.username || ''}@${ctx.host || ''}）。请基于该环境给出建议。`
    } else if (ctx?.sessionType === 'local') {
      prompt += `用户当前在本地终端（${ctx.shell || 'shell'}）环境。`
    }

    return prompt
  }

  // ── 5. SSH 特有 Actions ──

  /** 发送消息（SSH 模式封装：自动注入上下文 + system prompt） */
  async function sendMessage(content: string, options?: { skillIds?: string[] }) {
    if (agentMode.value) {
      await sendAgentMessage(content, options?.skillIds || [])
    } else {
      await sendChatMessage(content, options?.skillIds || [])
    }
  }

  /** 普通聊天模式 */
  async function sendChatMessage(content: string, skillIds: string[] = []) {
    const ctx = buildContext()
    const systemPrompt = buildSystemPrompt(ctx)
    await base.sendMessage(content, { context: ctx, systemPrompt, skillIds })
  }

  /** Agent 模式：多步工具调用，支持远程命令执行 */
  async function sendAgentMessage(content: string, skillIds: string[] = []) {
    if (agentStore.isRunning) {
      toast.error('Agent 正在运行中，请等待完成或先中止')
      return
    }
    if (!content.trim()) return

    if (appStore.backendStatus !== 'ready') {
      toast.error('后端未就绪，请稍后重试')
      return
    }

    // 复用 base 内部模型检查逻辑：确保已选模型有效
    if (!base.selectedModelId.value) {
      await base.fetchDefaultModel()
    }
    if (!base.selectedModelId.value) {
      toast.error('请先选择模型')
      return
    }

    const ctx = buildContext()
    const systemPrompt = buildSystemPrompt(ctx)

    let conv = base.currentConversation.value as SshAiConversation | undefined
    if (!conv) {
      const title = content.slice(0, 30) + (content.length > 30 ? '...' : '')
      conv = await base.createConversation(title) as SshAiConversation
    }
    if (!conv) return

    try {
      await base.persistence.ensureBackendConversation(conv)
    } catch {
      toast.error('创建会话失败')
      return
    }

    const userMsg: SshAiMessage = {
      id: genId(),
      role: 'user',
      content,
      context: ctx,
      created_at: new Date().toISOString(),
    }
    conv.messages.push(userMsg)

    const assistantMsg: SshAiMessage = {
      id: 'temp-agent-' + genId(),
      role: 'assistant',
      content: '',
      streamingContent: '',
      streamingReasoning: '',
      context: ctx,
      created_at: new Date().toISOString(),
    }
    conv.messages.push(assistantMsg)

    if (conv.messages.length <= 2 && conv.title === 'New Chat') {
      conv.title = content.slice(0, 30) + (content.length > 30 ? '...' : '')
    }

    conv.updated_at = new Date().toISOString()
    base.notifyConversationsChanged()

    agentStore.runAgent({
      task: content,
      conversationId: conv.backendConvId!,
      modelId: base.selectedModelId.value,
      mode: 'ssh',
      systemPrompt,
      sshSessionId: ctx?.sessionId,
      enableThinking: false,
      enableTools: true,
      maxIterations: useSettingsStore().settings.agent.maxIterations,
      enableDynamicBudget: useSettingsStore().settings.agent.dynamicBudget,
      // 用户显式选择的技能 ID（后端 SkillRein 会与自动匹配的合并去重）
      skillIds,
    })
  }

  return {
    // ── 透传通用 API ──
    conversations: base.conversations,
    currentConvId: base.currentConvId,
    currentConversation: base.currentConversation,
    messages: base.messages,
    streaming: base.streaming,
    selectedModelId: base.selectedModelId,
    selectedModel: base.selectedModel,
    groupedConversations: base.groupedConversations,
    persistence: base.persistence,
    fetchDefaultModel: base.fetchDefaultModel,
    setSelectedModel: base.setSelectedModel,
    createConversation: base.createConversation,
    switchConversation: base.switchConversation,
    deleteConversation: base.deleteConversation,
    abortStream: base.abortStream,
    save: base.persistence.save,
    notifyConversationsChanged: base.notifyConversationsChanged,
    // ── SSH 特有 API ──
    showAiPanel,
    toggleAiPanel,
    agentMode,
    setAgentMode,
    agentRunning,
    buildContext,
    buildSystemPrompt,
    sendMessage,
    sendAgentMessage,
  }
})
