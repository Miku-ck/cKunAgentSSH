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
import { api, workflowApi, type ChatAttachment, type WorkflowSnapshot } from '@/api/client'
import { useChatPersistence } from '@/composables/useChatPersistence'
import { useModelsStore } from './models'
import { finalizeAgentMessage, type AgentMessage } from '@/utils/finalizeAgentMessage'
import { resolveRunModes } from '@/utils/run-modes'
import type { ModeConversation, ModeMessage } from '@/types/chat'

// 后端 AgentRunRequest.task 的 max_length（routes/agent.py），与聊天输入框的
// maxlength 同值：改这里必须同时改那两处，否则要么附件永远塞不进、要么 422。
const TASK_CHAR_LIMIT = 50000

// ── SSH 特有数据模型 ──

interface SshAiContext {
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
interface SshAiMessage extends ModeMessage {
  context?: SshAiContext
}

interface SshAiConversation extends ModeConversation {
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
    const list = modelsStore.models
    // 只有列表非空才谈「失效」：fetchModels 失败会把列表留空，据空列表判定失效
    // 就把用户手选的模型静默换成了后端默认值 —— 取不到列表 ≠ 模型已删除。
    const usable = (id?: string) => !list.length || list.some((m) => m.id === id)
    const sid = persistence.selectedModelId.value
    if (sid && usable(sid)) return sid
    try {
      const def = await api.getDefaultModel()
      if (def?.id && usable(def.id)) return def.id
    } catch { /* ignore */ }
    return list[0]?.id
  }

  // P2 修复：store 初始化时合并后端会话列表。
  // 历史原仅靠 localStorage 恢复，换设备/清本地后丢失；后端保存了已同步的完整列表。
  // fire-and-forget：store 工厂在 pinia setup 内执行，不阻塞面板渲染。
  void persistence.syncBackendConversations()

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

  // ── 审批档（纯审批轴） ──

  /** 原先是 SideChatPanel 的组件局部 ref，首页输入卡读不到；两处各持一档就是
   *  第二条通路。刻意不持久化 —— 与提升前一致：重启回到手动审批。 */
  const approvalMode = ref<'normal' | 'auto'>('normal')

  function setApprovalMode(mode: 'normal' | 'auto') {
    approvalMode.value = mode
  }

  // ── 会话 CRUD ──

  async function createConversation(title?: string): Promise<ModeConversation> {
    const conv = await persistence.createConversation(title)
    notifyConversationsChanged()
    // 工作流按会话存储：新会话没有工件，清掉上一会话的缓存（否则界面会显示
    // 上一个会话的 Spec/Plan/Goal，而它们并没有被注入本轮上下文）
    workflowSnapshot.value = { phase: '', spec: '', plan: '', goal: '' }
    return conv
  }

  async function switchConversation(id: string) {
    persistence.switchConversation(id)
    // 工作流随会话切换重新加载：工件按会话落盘，界面必须与之一致
    void loadWorkflow()
    // P2 修复：启动合并进来的后端会话本地无消息缓存（messages:[]），
    // 切到时按需从后端拉取一次并固化到本地，避免"选中了却是空对话"
    const conv = persistence.currentConversation.value
    if (conv && conv.messages.length === 0 && conv.backendConvId) {
      try {
        // Message[] → ModeMessage[]：后端行是 snake_case 的 agent_trace，UI 读的是
        // camelCase 的 agentTrace —— 原实现直接断言，重载后的历史消息既没有执行过程
        // 卡片也没有轨迹回放入口（数据在库里，只是没接到渲染层）
        const rows = (await api.getMessages(conv.backendConvId)) as unknown as Array<
          ModeMessage & { agent_trace?: ModeMessage['agentTrace'] }
        >
        conv.messages = rows.map(({ agent_trace, ...rest }) =>
          agent_trace ? { ...rest, agentTrace: agent_trace } : rest,
        )
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
    // M17：abort 使状态 running→idle（非 completed/failed），原 watch 漏捕 →
    // temp-agent 占位消息（空内容）永久残留历史。补 running→idle 分支，
    // 以 aborted 标志区分"用户中止"与 C10 并发保护的直接 abortFn()（后者不固化）。
    const isTerminal = oldStatus === 'running' && (newStatus === 'completed' || newStatus === 'failed')
    const isAbort = oldStatus === 'running' && newStatus === 'idle' && agentStore.aborted
    if (!isTerminal && !isAbort) return
    // run 终态（含 abort）：agent 可能在运行中经 goal_write/plan_write/spec_write
    // 落盘了工作流工件 → 刷新当前会话的阶段指示，让界面与注入保持一致。
    if (isTerminal) void loadWorkflow()
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
        // 本轮占位不在（已固化过或从未写入）：不固化，只回收彻底空的会话，
        // 避免历史列表留下一条空条目
        if (conv.messages.length === 0) {
          persistence.conversations.value = persistence.conversations.value.filter((c) => c.id !== conv.id)
          if (persistence.currentConvId.value === conv.id) {
            persistence.currentConvId.value = persistence.conversations.value[0]?.id ?? null
          }
          notifyConversationsChanged()
        }
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

  // 后端就绪后自动加载模型，复用共享的 models
  // 只缓存"进行中的那次加载"，落地即释放：一次抖动/一次删模型都能立刻重试，
  // 不必等某个固定超时
  let modelInitPromise: Promise<void> | null = null
  function ensureModelLoaded(): Promise<void> | undefined {
    if (modelInitPromise) return modelInitPromise
    if (appStore.backendStatus !== 'ready') return
    const pending = (async () => {
      if (!persistence.selectedModelId.value) await fetchDefaultModel()
    })()
    modelInitPromise = pending
    pending.finally(() => {
      if (modelInitPromise === pending) modelInitPromise = null
    }).catch(() => {})
    return pending
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
  type TerminalSnapshot = { sessionId: string | null; selection: string; cwd: string; recent: string[] }

  function getTerminalSnapshot(): TerminalSnapshot {
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

  // 暂存「问 AI」时采集的现场快照，三态语义：
  //   undefined — 未表态（面板里直接输入）→ 发送时现场采集
  //   null      — 纯文本入口刻意不要终端现场（批量下发结果 / 监控指标 / 失败诊断，
  //               问题文本已自带上下文；活动终端与问题所指主机未必同一台）
  //   对象      — 点击「问 AI」时暂存的现场（选区登记随即被清空，只能此刻采集）；
  //               发送时来源终端已切走/关闭则丢弃改现场采集，防"主机 B 身份 + 主机 A 现场"
  let pendingSnapshot: TerminalSnapshot | null | undefined

  /** 终端「问 AI」入口：选中内容 → 预填 AI 面板问题草稿（上下文随消息自动注入） */
  function askAiAboutSelection(question?: string) {
    const snap = getTerminalSnapshot()
    const q = (question || '').trim()
    if (!q && !snap.selection) return
    const text = q || '请分析下面选中的终端输出：'
    pendingPrefill.value = { text }
    // 调用方自带问题文本（监控指标「问 AI」）时不暂存现场：问题所指主机与活动
    // 终端未必同一台，拼进无关终端的选区/历史只会误导模型。
    pendingSnapshot = q ? null : snap
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
    // 模式基础 prompt（"运维助手"）由后端固定注入（mode 轴已坍缩为单一 SSH）
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
  // ── 会话工作流（Spec/Plan/Goal）──
  // 权威在后端落盘的 markdown 工件（backend/agent/workflow.py），前端只缓存与
  // 展示。内容仍由 agent 经 spec_write/plan_write/goal_write 维护；唯一的写是
  // 删除（clearWorkflowArtifact），只绑在工具条「目标」/「计划」chip 的 × 上。
  const workflowSnapshot = ref<WorkflowSnapshot>({ phase: '', spec: '', plan: '', goal: '' })
  const workflowPhase = computed(() => workflowSnapshot.value.phase)

  /** 加载当前会话工作流阶段与工件；失败/无会话按空处理（不沿用上一个会话） */
  async function loadWorkflow(): Promise<void> {
    const cid = persistence.currentConversation.value?.backendConvId
    if (!cid) {
      workflowSnapshot.value = { phase: '', spec: '', plan: '', goal: '' }
      return
    }
    try {
      const snap = await workflowApi.read(cid)
      // 审查R2修复：await 后校验会话未切换——快速切会话时旧响应晚到，
      // 无条件赋值会把上一个会话的工作流快照挂到新会话头上
      if (persistence.currentConversation.value?.backendConvId !== cid) return
      workflowSnapshot.value = snap
    } catch (e) {
      console.warn('[ssh] 加载会话工作流失败（按空处理）:', e)
      // 审查R2修复：同上，会话已切换时旧请求的失败不应清空新会话的快照
      if (persistence.currentConversation.value?.backendConvId !== cid) return
      workflowSnapshot.value = { phase: '', spec: '', plan: '', goal: '' }
    }
  }

  /**
   * 删除工件（chip 的 × 走这里），返回删除后的阶段供调用方提示。
   * null = 没做成（无会话或请求失败）：此时不伪造空串，因为空串是一个合法阶段
   * （「无任何工件」），把失败说成「已清空」会让用户以为 × 生效了。
   */
  async function clearWorkflowArtifact(
    kind: 'spec' | 'plan' | 'goal',
  ): Promise<{ phase: string } | null> {
    const cid = persistence.currentConversation.value?.backendConvId
    if (!cid) return null
    try {
      const r = await workflowApi.clearArtifact(cid, kind)
      await loadWorkflow()
      return { phase: r.phase }
    } catch (e) {
      console.warn('[ssh] 删除工作流工件失败:', e)
      return null
    }
  }

  async function sendMessage(content: string, options?: { skillIds?: string[]; attachments?: ChatAttachment[]; mode?: string; approval?: string }): Promise<boolean> {
    return sendAgentMessage(content, options?.skillIds || [], options?.attachments || [], options?.mode, options?.approval)
  }

  /** 上传文件（对话附件）：逐个调用后端 /chat/upload，失败项 toast 提示并跳过 */
  async function uploadFiles(files: File[]): Promise<ChatAttachment[]> {
    const results: ChatAttachment[] = []
    // 附件目录必须按「后端会话 id」归档：删除会话的级联清理只认这个键
    // （DELETE /chat/conversations/<backendConvId> → rmtree uploads/<backendConvId>）。
    // 之前传的是本地 id，两边永远对不上，附件文件全部成为孤儿且无法回收。
    const conv = persistence.currentConversation.value
    let backendConvId = conv?.backendConvId || ''
    if (conv && !backendConvId) {
      try {
        await persistence.ensureBackendConversation(conv)
        backendConvId = conv.backendConvId || ''
      } catch {
        // 后端会话建不出来：退回共享的 general 目录，不因此阻断上传
      }
    }
    for (const file of files) {
      try {
        const att = await api.uploadChatFile(backendConvId, file)
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
  async function sendAgentMessage(content: string, skillIds: string[] = [], attachments: ChatAttachment[] = [], mode?: string, approval?: string): Promise<boolean> {
    if (_agentSendLock) return false
    _agentSendLock = true
    try {
      return await _sendAgentMessageInner(content, skillIds, attachments, mode, approval)
    } finally {
      _agentSendLock = false
    }
  }

  async function _sendAgentMessageInner(content: string, skillIds: string[] = [], attachments: ChatAttachment[] = [], agentMode?: string, approval?: string): Promise<boolean> {
    // 附带文件拼入任务文本：Agent 走 /agent/run，直接注入内容让 LLM 读取。
    // 预算必须卡在这里：后端 AgentRunRequest.task 有 max_length=50000（与输入框
    // maxlength 同值），而每个附件各自可达 5 万字符——原样拼接会让整条消息在第一个
    // token 之前撞 422，用户只看到一句校验错误。放不下的附件整体丢弃并点名：
    // 塞半截文件比不要更容易骗人（模型会把它当全文下结论）。
    let effectiveContent = content
    if (attachments.length > 0) {
      const parts = [content]
      let budget = TASK_CHAR_LIMIT - content.length
      const dropped: string[] = []
      for (const att of attachments) {
        // 后端只注入前 50K 字符（truncated 标记）：不告诉模型「这是节选」，它会
        // 把开头当成整份文件下结论（"日志里没有 ERROR"其实是被截断了）
        const head = att.truncated ? `${att.filename}（文件过大，以下仅为开头节选）` : att.filename
        const block = `[附件: ${head}]\n${att.content}`
        if (block.length + 2 > budget) {
          dropped.push(att.filename)
          continue
        }
        parts.push(block)
        budget -= block.length + 2  // +2 = join 用的空行
      }
      effectiveContent = parts.join('\n\n')
      if (dropped.length) {
        toast.error(`问题长度已达上限，以下附件未注入：${dropped.join('、')}`, 6000)
      }
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

    // 复用共享模型检查逻辑：确保已选模型有效（含"有效性"——残留的死 id 也要在这里
    // 换成能用的，只在「没选」时兜底的话，删掉当前模型后每次发送都吃 404）
    await fetchDefaultModel()
    if (!persistence.selectedModelId.value) {
      toast.error('请先选择模型')
      return false
    }

    // 消费暂存快照（三态语义见 pendingSnapshot 定义）。纯文本入口标 null 时必须
    // 跳过现场采集——原先这里无条件重新采集，把「不采集终端上下文」的入口约定
    // 又破掉了：批量下发/AI 诊断发问时会混入此刻活跃的无关终端选区与命令历史。
    const staged = pendingSnapshot
    pendingSnapshot = undefined
    let snapshot: TerminalSnapshot =
      staged === null ? { sessionId: null, selection: '', cwd: '', recent: [] } : staged ?? getTerminalSnapshot()
    // 防串主机：暂存快照来源终端已切走（或原会话已关闭）→ 改现场采集
    if (snapshot.sessionId && snapshot.sessionId !== useSSHStore().activeSessionId) {
      snapshot = getTerminalSnapshot()
    }
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
      systemPrompt,
      // 可见终端执行模式：无论本地/远程都把活动会话 id 传给后端，
      // 后端据此把 shell_exec 交付到当前可见终端执行（而非 headless）
      sshSessionId: ctx?.sessionId || useSSHStore().activeSessionId || undefined,
      environment: ctx?.environment || 'test',
      // E2：三态思考模式。'inherit' → 省略字段让后端演化配置接管；
      // 'on'/'off' → 用户显式表态，前端把开关关掉时后端 None 分支不再触发。
      // 原实现永久钉死 undefined，用户永远压不下演化学到的 enable_thinking=true。
      enableThinking:
        useSettingsStore().settings.chat.enableThinking === 'inherit'
          ? undefined
          : useSettingsStore().settings.chat.enableThinking === 'on',
      enableTools: true,
      maxIterations: useSettingsStore().settings.agent.maxIterations,
      enableDynamicBudget: useSettingsStore().settings.agent.dynamicBudget,
      // E1：意图识别 + 思考预算控制。关闭 → 后端跳过规划阶段/引导语注入
      enableIntentionGuide: useSettingsStore().settings.agent.enableIntentionGuide,
      // 对话姿态 + 审批档 + 权限模式：三字段单一口径见 resolveRunModes。
      // 姿态=plan 时 permissionMode 恒为 'plan'（审批档休眠）；否则取审批轴
      // （'auto' 模型代判 / 其余手动）。
      ...resolveRunModes(agentMode, approval),
      // 成本/token 熔断预算（设置 → Agent 治理 → 运行预算）。
      // 数字框里的 0 = 不发这个字段（走后端默认）；「关闭熔断」才发显式 0，
      // 后端把显式 0 解释为不设上限。两种 0 语义相反，所以由独立布尔区分，
      // 不能让 `|| undefined` 把用户要的"关掉"又折回默认。
      maxCostUsd: useSettingsStore().settings.agent.breakerDisabled
        ? 0
        : (useSettingsStore().settings.agent.maxCostUsd || undefined),
      maxTotalTokens: useSettingsStore().settings.agent.breakerDisabled
        ? 0
        : (useSettingsStore().settings.agent.maxTotalTokens || undefined),
      visibleTerminal: true,
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
    // ── 审批档 ──
    approvalMode,
    setApprovalMode,
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
    // ── 会话工作流（Spec/Plan/Goal）──
    workflowSnapshot,
    workflowPhase,
    loadWorkflow,
    clearWorkflowArtifact,
  }
})
