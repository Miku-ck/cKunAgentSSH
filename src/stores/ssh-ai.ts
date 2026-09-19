/**
 * SSH 模式 AI 运维助手 Store
 *
 * 会话/模型持久化来自 useChatPersistence（单一 ssh 模式、固定 dual 策略）。
 * 原 createModeChatStore 通用工厂仅剩本文件一个调用方，已收敛内联至此
 * （固定 Agent 模式重构的收尾清理）。
 */
import { computed, ref, triggerRef, watch } from 'vue'
import { defineStore } from 'pinia'
import { useSSHStore } from './ssh'
import { useAgentStore } from './agent'
import { useAppStore } from './app'
import { useSettingsStore } from './settings'
import { useToastStore } from './toast'
import { api, type ChatAttachment, type ModelConfig } from '@/api/client'
import { useChatPersistence } from '@/composables/useChatPersistence'
import { useModelsStore } from './models'
import { finalizeAgentMessage, type AgentMessage } from '@/utils/finalizeAgentMessage'
import type { ModeConversation, ModeMessage } from '@/types/chat'

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
  /** 终端当前工作目录（远程 OSC 7 跟踪 / 本地 pty cwd） */
  cwd?: string
  /** 用户选中并发送给 AI 的终端片段 */
  selection?: string
  /** 最近执行的命令（时间正序） */
  recentCommands?: string[]
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
  // ── 1. 通用对话能力（单一 ssh 模式，直接组合 useChatPersistence） ──

  const persistence = useChatPersistence({
    config: {
      storageKey: 'ckun-ssh-ai-conversations',
      modelStorageKey: 'ckun-ssh-ai-selected-model',
    },
    getDefaultModelId,
  })

  async function getDefaultModelId(): Promise<string | undefined> {
    const modelsStore = useModelsStore()
    if (modelsStore.models.length === 0) await modelsStore.fetchModels()
    const sid = persistence.selectedModelId.value
    if (sid && modelsStore.models.some((m: ModelConfig) => m.id === sid)) {
      return sid
    }
    try {
      const def = await api.getDefaultModel('ssh')
      if (def?.id) return def.id
    } catch { /* ignore */ }
    return modelsStore.models[0]?.id
  }

  // P2 修复：store 初始化时合并后端会话列表。
  // 历史原仅靠 localStorage 恢复，换设备/清本地后丢失；后端保存了已同步的完整列表。
  // fire-and-forget：store 工厂在 pinia setup 内执行，不阻塞面板渲染。
  void persistence.syncBackendConversations()

  /** 当前选中的模型配置（从 models store 中查找） */
  const selectedModel = computed<ModelConfig | null>(() => {
    const modelsStore = useModelsStore()
    return modelsStore.models.find((m: ModelConfig) => m.id === persistence.selectedModelId.value) ?? modelsStore.models[0] ?? null
  })

  // ── 模型选择 ──

  /** 加载默认模型 */
  async function fetchDefaultModel() {
    const modelsStore = useModelsStore()
    if (modelsStore.models.length === 0) await modelsStore.fetchModels()
    await persistence.fetchDefaultModel()
  }

  function setSelectedModel(id: string) {
    persistence.setSelectedModel(id)
  }

  // ── 会话 CRUD ──

  async function createConversation(title?: string): Promise<ModeConversation> {
    const conv = await persistence.createConversation(title)
    notifyConversationsChanged()
    return conv
  }

  async function switchConversation(id: string) {
    persistence.switchConversation(id)
    // P2 修复：启动合并进来的后端会话本地无消息缓存（messages:[]），
    // 切到时按需从后端拉取一次并固化到本地，避免"选中了却是空对话"
    const conv = persistence.currentConversation.value
    if (conv && conv.messages.length === 0 && (conv as any).backendConvId) {
      try {
        // Message[] → ModeMessage[]：后端消息结构与本地结构兼容，显式断言
        const msgs = (await api.getMessages((conv as any).backendConvId)) as unknown as ModeMessage[]
        conv.messages = msgs
        persistence.save()
        notifyConversationsChanged()
      } catch (e) {
        console.warn(`[ssh] 拉取会话消息失败（保留空列表）:`, e)
      }
    }
  }

  async function deleteConversation(id: string) {
    await persistence.deleteConversation(id)
  }

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
  // 修复 P3-4：面板卸载不会丢失 Agent 结果。
  const agentStore = useAgentStore()
  watch(() => agentStore.status, (newStatus, oldStatus) => {
    if (agentStore.currentMode !== 'ssh') return
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
      // 会话可能属于其他 store 实例/已删除，兜底会误固化到错误会话、甚至误删
      // 用户当前会话。列表里没有源会话即说明本轮不归属本 store，直接返回。
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
      // 结构上满足 AgentMessage 最小接口，显式断言（与固化函数签名对齐）
      finalizeAgentMessage(conv as unknown as { messages: AgentMessage[] }, agentStore)
      notifyConversationsChanged()
    }
  })

  // ── 2. SSH 特有 State ──
  const showAiPanel = ref(false)

  /** 终端上下文「问 AI」预填请求：终端组件选中后调用，面板打开并预填问题草稿。
   *  预填只发生一次（SshAiPanel watch 消费后置 null），用户可修改后再发送 */
  const pendingPrefill = ref<{ text: string } | null>(null)

  const appStore = useAppStore()
  const toast = useToastStore()

  const agentRunning = computed(() => agentStore.isRunning)

  // 后端就绪后自动加载模型，复用共享的 models
  let modelInitPromise: Promise<void> | null = null
  function ensureModelLoaded() {
    if (modelInitPromise) return modelInitPromise
    if (appStore.backendStatus !== 'ready') return
    modelInitPromise = (async () => {
      if (persistence.selectedModelId.value) return
      await fetchDefaultModel()
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
  // 选中片段/最近命令等"现场上下文"按需采集（问 AI 入口），避免每次 buildContext 发 IPC
  function buildContext(includeSelection: string, recentCommands?: string[]): SshAiContext | undefined {
    const sshStore = useSSHStore()
    const session = sshStore.activeSession
    if (!session) return undefined

    const base = {
      sessionType: session.type as 'local' | 'remote',
      sessionId: session.type === 'remote' ? session.id : undefined,
    }

    if (session.type === 'remote') {
      const conn = sshStore.connections.find(c => c.id === session.connectionId)
      // 连接已被删除时回退到会话自带的展示信息，避免向后端发送 undefined
      return {
        ...base,
        connectionName: conn?.name || session.name,
        host: conn?.host || 'unknown',
        username: conn?.username || 'unknown',
        environment: conn?.environment || 'test',
        selection: includeSelection || undefined,
        recentCommands,
      }
    } else {
      return {
        ...base,
        shell: session.shell,
        // 本地终端不传递 sessionId，避免 shell_exec 误判为 SSH 远程会话
        selection: includeSelection || undefined,
        recentCommands,
      }
    }
  }

  /** 活动终端现场上下文（selection 为空时不传）— 供「问 AI」入口采集。
   *  sessionId 标记快照来源终端：发送前切 tab/关会话时用于防串主机（见 sendMessage） */
  function getTerminalSnapshot(): { sessionId: string | null; selection: string; cwd: string; recent: string[] } {
    const sshStore = useSSHStore()
    const session = sshStore.activeSession
    const sel = session ? sshStore.getTerminalSelection(session.id) : ''
    const recent = session ? sshStore.getRecentCommands(session.id, 10) : []
    return {
      sessionId: session ? session.id : null,
      selection: sel || '',
      cwd: session ? sshStore.getTerminalCwd(session.id) || '' : '',
      recent,
    }
  }

  // 暂存「问 AI」时采集的现场快照：点击按钮即清空终端选区登记，若发送时重新采集
  // 会拿到空 selection；且预填草稿与快照必须绑定同一终端——sessionId 不一致时
  // （发送前切 tab / 原会话已关闭）发送端会丢弃该快照，防"主机 B 的身份 + 主机 A 的现场"。
  let pendingSnapshot: { sessionId: string | null; selection: string; cwd: string; recent: string[] } | null = null

  /** 终端「问 AI」入口：选中内容 → 预填 AI 面板问题草稿（上下文随消息自动注入） */
  function askAiAboutSelection(question?: string) {
    const snap = getTerminalSnapshot()
    const q = (question || '').trim()
    if (!q && !snap.selection) return
    const text = q || '请分析下面选中的终端输出：'
    pendingPrefill.value = { text }
    // 仅在确有选区/终端现场时暂存快照，供发送时优先消费
    pendingSnapshot = snap.selection || snap.cwd || snap.recent.length ? snap : null
    if (!showAiPanel.value) showAiPanel.value = true
  }

  /** 纯文本「问 AI」入口（不采集终端上下文）：供 Schedules 等非终端页面使用，
   *  避免混入无关终端的选区/命令历史 */
  function askAi(text: string) {
    const q = (text || '').trim()
    if (!q) return
    pendingPrefill.value = { text: q }
    pendingSnapshot = null
    if (!showAiPanel.value) showAiPanel.value = true
  }

  // ── 4. System Prompt 构建：运维专家 + SSH 环境信息 + 终端现场上下文 ──
  function buildSystemPrompt(ctx?: SshAiContext): string {
    // 模式基础 prompt（"运维助手"）由后端根据 mode='ssh' 注入
    let prompt = ''

    if (ctx?.sessionType === 'remote') {
      prompt += `用户当前已通过 SSH 连接到服务器：${ctx.connectionName || ''}（${ctx.username || ''}@${ctx.host || ''}）。`
    } else if (ctx?.sessionType === 'local') {
      prompt += `用户当前在本地终端（${ctx.shell || 'shell'}）环境。`
    }

    // 终端现场上下文（cwd / 最近命令 / 选中片段）— 由消息 context 透传，prompt 中结构化说明
    const cwd = ctx?.cwd
    const recent = ctx?.recentCommands?.length ? ctx.recentCommands : null
    if (cwd || recent) {
      prompt += '\n\n【终端现场上下文】'
      if (cwd) prompt += `\n当前工作目录: ${cwd}`
      if (recent) prompt += `\n最近执行的命令:\n${recent.map(c => `- ${c}`).join('\n')}`
    }
    if (ctx?.selection) {
      prompt += `\n\n用户在终端中选中了以下内容，问题可能与它相关:\n\`\`\`\n${ctx.selection}\n\`\`\``
    }

    if (ctx?.sessionType === 'remote') {
      prompt += '\n请基于该环境与终端现场上下文给出建议。'
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
        const att = await api.uploadChatFile(persistence.currentConvId.value || '', file)
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

    // 复用共享模型检查逻辑：确保已选模型有效
    if (!persistence.selectedModelId.value) {
      await fetchDefaultModel()
    }
    if (!persistence.selectedModelId.value) {
      toast.error('请先选择模型')
      return false
    }

    // 优先消费「问 AI」时暂存的现场快照（选区登记已被点击清除，重新采集会为空）。
    // 防串主机：快照记录来源终端 sessionId，发送时活动会话已切走（或原会话已关闭）
    // 则丢弃暂存、改现场采集——否则会拼出"主机 B 的身份 + 主机 A 的选区/命令"的矛盾上下文。
    // 无暂存（普通输入）时直接现场采集。
    let snapshot = pendingSnapshot
    if (
      !snapshot ||
      (snapshot.sessionId && snapshot.sessionId !== useSSHStore().activeSessionId)
    ) {
      snapshot = getTerminalSnapshot()
    }
    pendingSnapshot = null
    const ctx = buildContext(snapshot.selection, snapshot.recent)
    if (ctx) ctx.cwd = snapshot.cwd || undefined
    const systemPrompt = buildSystemPrompt(ctx)

    let conv = persistence.currentConversation.value as SshAiConversation | undefined
    if (!conv) {
      const title = effectiveContent.slice(0, 30) + (effectiveContent.length > 30 ? '...' : '')
      conv = await createConversation(title) as SshAiConversation
    }
    if (!conv) return false

    try {
      await persistence.ensureBackendConversation(conv)
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
    notifyConversationsChanged()

    agentStore.runAgent({
      task: effectiveContent,
      conversationId: conv.backendConvId!,
      modelId: persistence.selectedModelId.value,
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
    // ── 会话/模型持久化 API ──
    conversations: persistence.conversations,
    currentConvId: persistence.currentConvId,
    messages: persistence.messages,
    selectedModelId: persistence.selectedModelId,
    groupedConversations: persistence.groupedConversations,
    fetchDefaultModel,
    setSelectedModel,
    createConversation,
    switchConversation,
    deleteConversation,
    // ── SSH 特有 API ──
    showAiPanel,
    toggleAiPanel,
    closeAiPanel,
    pendingPrefill,
    askAiAboutSelection,
    askAi,
    sendMessage,
    uploadFiles,
  }
})
