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
import { api, type ChatAttachment } from '@/api/client'

// ── SSH 特有数据模型 ──

export interface SshAiContext {
  connectionName?: string
  host?: string
  username?: string
  sessionType?: 'local' | 'remote'
  shell?: string
  /** 当前活动终端/SSH 会话 ID，Agent 模式下透传给后端供 shell_exec 远程执行 */
  sessionId?: string
  /** 目标环境标记('test'|'prod')：来自 SSH 连接声明，驱动生产环境危险命令强制审批 */
  environment?: 'test' | 'prod'
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
      // 完成后清除缓存，允许后续重试
      modelInitPromise = null
    })()
    // 超时清理：30秒后清除缓存，防止永久阻塞
    let modelInitTimer: ReturnType<typeof setTimeout> | null = null
    modelInitTimer = setTimeout(() => {
      modelInitPromise = null
      modelInitTimer = null
    }, 30000)
    // promise 完成时清理超时定时器
    modelInitPromise.finally(() => {
      if (modelInitTimer) {
        clearTimeout(modelInitTimer)
        modelInitTimer = null
      }
    }).catch(() => {})
    return modelInitPromise
  }
  watch(() => appStore.backendStatus, ensureModelLoaded, { immediate: true })

  function toggleAiPanel() {
    if (showAiPanel.value) {
      closeAiPanel()
    } else {
      showAiPanel.value = true
    }
  }

  /**
   * 关闭 AI 面板：隐藏面板。
   * Agent 运行设计为可跨面板存活（store 层 watch 固化结果），关闭面板不中止 Agent。
   */
  function closeAiPanel() {
    showAiPanel.value = false
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
        host: conn?.host || 'unknown',
        username: conn?.username || 'unknown',
        sessionId: session.id,
        environment: conn?.environment || 'test',
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

  /** 发送消息（固定 Agent 路径：AI 自主意图识别并调用工具，无需手动切换模式） */
  // M18：返回是否被接受（true=已入会话，false=早退拒绝），供组件回滚草稿
  async function sendMessage(content: string, options?: { skillIds?: string[]; attachments?: ChatAttachment[] }): Promise<boolean> {
    return sendAgentMessage(content, options?.skillIds || [], options?.attachments || [])
  }

  /** 上传文件（对话附件）：逐个调用后端 /chat/upload，失败项 toast 提示并跳过 */
  async function uploadFiles(files: File[]): Promise<ChatAttachment[]> {
    const results: ChatAttachment[] = []
    for (const file of files) {
      try {
        const att = await api.uploadChatFile(base.currentConvId.value || '', file)
        results.push(att)
      } catch (e: any) {
        const detail = e?.detail || e?.message || String(e)
        toast.error(`上传 ${file.name} 失败：${detail}`)
      }
    }
    return results
  }

  /** Agent 模式：多步工具调用，支持远程命令执行 */
  // 发送锁，防止快速连点创建多个会话
  let _agentSendLock = false
  async function sendAgentMessage(content: string, skillIds: string[] = [], attachments: ChatAttachment[] = []): Promise<boolean> {
    if (_agentSendLock) return false
    _agentSendLock = true
    try {
      return await _sendAgentMessageInner(content, skillIds, attachments)
    } finally {
      _agentSendLock = false
    }
  }

  async function _sendAgentMessageInner(content: string, skillIds: string[] = [], attachments: ChatAttachment[] = []): Promise<boolean> {
    // 附带文件拼入任务文本：Agent 走 /agent/run，直接注入内容让 LLM 读取
    let effectiveContent = content
    if (attachments.length > 0) {
      const parts = [content]
      for (const att of attachments) {
        parts.push(`[附件: ${att.filename}]\n${att.content}`)
      }
      effectiveContent = parts.join('\n\n')
    }
    if (agentStore.isRunning) {
      toast.error('AI 正在运行中，请等待完成或先中止')
      return false
    }
    if (!effectiveContent.trim()) return false

    if (appStore.backendStatus !== 'ready') {
      toast.error('后端未就绪，请稍后重试')
      return false
    }

    // 复用 base 内部模型检查逻辑：确保已选模型有效
    if (!base.selectedModelId.value) {
      await base.fetchDefaultModel()
    }
    if (!base.selectedModelId.value) {
      toast.error('请先选择模型')
      return false
    }

    const ctx = buildContext()
    const systemPrompt = buildSystemPrompt(ctx)

    let conv = base.currentConversation.value as SshAiConversation | undefined
    if (!conv) {
      const title = effectiveContent.slice(0, 30) + (effectiveContent.length > 30 ? '...' : '')
      conv = await base.createConversation(title) as SshAiConversation
    }
    if (!conv) return false

    try {
      await base.persistence.ensureBackendConversation(conv)
    } catch {
      toast.error('创建会话失败')
      return false
    }

    const userMsg: SshAiMessage = {
      id: genId(),
      role: 'user',
      content: effectiveContent,
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
      conv.title = effectiveContent.slice(0, 30) + (effectiveContent.length > 30 ? '...' : '')
    }

    conv.updated_at = new Date().toISOString()
    base.notifyConversationsChanged()

    agentStore.runAgent({
      task: effectiveContent,
      conversationId: conv.backendConvId!,
      modelId: base.selectedModelId.value,
      mode: 'ssh',
      systemPrompt,
      // 可见终端执行模式：无论本地/远程都把活动会话 id 传给后端，
      // 后端据此把 shell_exec 交付到当前可见终端执行（而非 headless）
      sshSessionId: ctx?.sessionId || useSSHStore().activeSessionId || undefined,
      environment: ctx?.environment || 'test',
      enableThinking: false,
      enableTools: true,
      maxIterations: useSettingsStore().settings.agent.maxIterations,
      enableDynamicBudget: useSettingsStore().settings.agent.dynamicBudget,
      visibleTerminal: true,
      sessionType: ctx?.sessionType || 'local',
      // 用户显式选择的技能 ID（后端 SkillRein 会与自动匹配的合并去重）
      skillIds,
      // 对话设置：自动技能匹配开关 + 上下文窗口条数
      autoSkillMatch: useSettingsStore().settings.chat.autoSkillMatch,
      contextWindow: useSettingsStore().settings.chat.contextWindow,
    })
    // 消息已入会话且 Agent 已启动 — 草稿可清空
    return true
  }

  return {
    // ── 透传通用 API ──
    conversations: base.conversations,
    currentConvId: base.currentConvId,
    currentConversation: base.currentConversation,
    messages: base.messages,
    selectedModelId: base.selectedModelId,
    selectedModel: base.selectedModel,
    groupedConversations: base.groupedConversations,
    persistence: base.persistence,
    fetchDefaultModel: base.fetchDefaultModel,
    setSelectedModel: base.setSelectedModel,
    createConversation: base.createConversation,
    switchConversation: base.switchConversation,
    deleteConversation: base.deleteConversation,
    save: base.persistence.save,
    notifyConversationsChanged: base.notifyConversationsChanged,
    // ── SSH 特有 API ──
    showAiPanel,
    toggleAiPanel,
    closeAiPanel,
    agentRunning,
    buildContext,
    buildSystemPrompt,
    sendMessage,
    sendAgentMessage,
    uploadFiles,
  }
})
