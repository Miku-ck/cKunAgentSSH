import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { botApi, streamBotChat, type BotConversation, type BotMessage, type BotPersona, type BotRelation } from '@/api/bot'
import { toFriendlyMessage } from '@/api/base'
import { probeOutcomeText } from '@/utils/bot-probe'

export const useBotStore = defineStore('bot', () => {
  const personas = ref<BotPersona[]>([])
  const conversations = ref<BotConversation[]>([])
  const messages = ref<BotMessage[]>([])
  const activePersonaId = ref('')
  const activeConversationId = ref('')
  const streamText = ref('')
  const streamReasoning = ref('')
  const streaming = ref(false)
  /** 在场感（P4）：后端状态层给的节奏提示。'hesitate' = 欲言又止（打字→停→再打）。 */
  const pacing = ref('')
  /**
   * 刚被流式逐字演过的那一轮的落库 id。视图的投递排程靠它认「已经演过的内容」：
   * 排程只该服务没有流式过程的来源（QQ 轮询、主动推送），否则会把演完的气泡
   * 收回去重放一遍，用户看到的就是「同一条回复流式输出两次」。
   */
  const lastStreamedId = ref('')
  const error = ref('')
  const lastFailedText = ref('')
  const channel = ref<'local' | 'qq' | 'wechat'>('local')
  /** 远程渠道（QQ / 微信）的只读列表：两边同一条读取路径，只有渠道名不同。 */
  const remoteConversations = ref<BotConversation[]>([])
  const remoteMessages = ref<BotMessage[]>([])
  const remoteActiveId = ref('')
  const remoteError = ref('')
  /** 「你的头像」（Phase 4）：本地会话里你这一侧的气泡用；远端会话里你不发言不吃它。
   *  设置侧入口已按裁决下线（与聊天记录设置重叠），但历史值仍从 bot_user_avatar 读出来照常显示。 */
  const userAvatar = ref('')
  /** 「试一句」在途标记与回显：只读侧的手动探口，不进聊天错误条也不碰消息列表。 */
  const probing = ref(false)
  const probeNote = ref('')
  let abortController: AbortController | null = null
  let remoteRefreshing = false
  /** 远程列表请求序号：删除会让在途的轮询结果作废，否则刚删的行会被旧快照写回来。 */
  let remoteQuerySeq = 0

  const activePersona = computed(() => personas.value.find(p => p.id === activePersonaId.value) || null)
  // 关系状态是「人格 × 用户」维度的，不随会话变化：切换会话不该让熟悉度跳变。
  const relations = ref<Record<string, BotRelation>>({})
  const activeRelation = computed(() => relations.value[activePersonaId.value] || null)
  const filteredConversations = computed(() => conversations.value.filter(c => c.persona_id === activePersonaId.value))
  const activeConversation = computed(() => conversations.value.find(c => c.id === activeConversationId.value) || null)
  const isRemote = computed(() => channel.value !== 'local')
  /** 只读侧的渠道名：空态、错误提示、标题栏都按它说话，不再写死 QQ。 */
  const remoteChannelLabel = computed(() => (channel.value === 'wechat' ? '微信' : 'QQ'))
  const remoteActiveConversation = computed(() => remoteConversations.value.find(c => c.id === remoteActiveId.value) || null)

  function selectChannel(next: 'local' | 'qq' | 'wechat') {
    if (next === channel.value) return
    stopGenerating()
    channel.value = next
    // 切渠道先清空：上一个渠道的会话行不能在新 tab 里闪一下
    remoteConversations.value = []
    remoteMessages.value = []
    remoteActiveId.value = ''
    remoteError.value = ''
    probeNote.value = ''
  }

  /**
   * 手动探口：绕过意愿分/间隔/静音窗，真发一句到这个会话并回显结果。
   * 回显只落在会话栏那一小条上：它不是聊天记录（群主动那句本来就不落本地库），
   * 失败也不该把「回复生成失败」的错误条弹出来——这里一句回复都没生成。
   */
  async function probeProactive(id: string) {
    const provider = channel.value
    if (!id || probing.value) return
    probing.value = true
    probeNote.value = '正在生成并发送…'
    try {
      const result = await botApi.probeProactive(id)
      // 在途时切了渠道：旧渠道的结果不该写进新 tab 的回显条
      if (channel.value === provider) probeNote.value = probeOutcomeText(result)
    } catch (e) {
      if (channel.value === provider) probeNote.value = toFriendlyMessage(e, '试一句没成功')
    } finally {
      probing.value = false
    }
  }

  async function selectRemoteConversation(id: string) {
    const provider = channel.value
    remoteActiveId.value = id
    remoteMessages.value = []
    // 上一条会话的探口结论对它自己才成立：不换会话就会把旧群的原因顶在新群头上
    probeNote.value = ''
    try {
      const rows = await botApi.getMessages(id)
      if (remoteActiveId.value !== id || channel.value !== provider) return
      remoteMessages.value = rows
      remoteError.value = ''
    } catch (e) {
      if (channel.value !== provider) return
      remoteError.value = toFriendlyMessage(e, `${remoteChannelLabel.value}记录读取失败`)
    }
  }

  async function refreshRemote() {
    const provider = channel.value
    if (provider === 'local' || remoteRefreshing) return
    const label = remoteChannelLabel.value
    remoteRefreshing = true
    const seq = ++remoteQuerySeq
    try {
      const rows = await botApi.listChannelConversations(provider)
      // 请求在途时切了 tab / 删了行：这批行属于旧快照，不能写进当前列表
      if (channel.value !== provider || seq !== remoteQuerySeq) return
      remoteConversations.value = rows
      if (!rows.length) {
        remoteActiveId.value = ''
        remoteMessages.value = []
        remoteError.value = ''
        return
      }
      if (!rows.some(c => c.id === remoteActiveId.value)) remoteActiveId.value = rows[0].id
      const target = remoteActiveId.value
      const history = await botApi.getMessages(target)
      if (remoteActiveId.value === target && channel.value === provider) remoteMessages.value = history
      remoteError.value = ''
    } catch (e) {
      if (channel.value === provider) remoteError.value = toFriendlyMessage(e, `${label}记录读取失败`)
    } finally {
      remoteRefreshing = false
    }
  }

  /** 远程会话记录删除：与本地会话同一口径，删掉后自动落到下一条。 */
  async function deleteRemoteConversation(id: string) {
    await botApi.deleteConversation(id)
    remoteQuerySeq++
    remoteConversations.value = remoteConversations.value.filter(c => c.id !== id)
    if (remoteActiveId.value !== id) return
    const next = remoteConversations.value[0]?.id || ''
    remoteActiveId.value = ''
    remoteMessages.value = []
    if (next) await selectRemoteConversation(next)
  }

  /** 远端（QQ / 微信）会话本地显示覆盖：PATCH 成功后原位替换，列表不重拉——
   *  服务端保证 display 编辑不动 updated_at，重拉反而会把滚动位置弄丢。 */
  async function updateConversationDisplay(id: string, payload: { display_name?: string; display_avatar?: string }) {
    const updated = await botApi.updateConversationDisplay(id, payload)
    remoteConversations.value = remoteConversations.value.map((c) => (c.id === id ? updated : c))
    remoteError.value = ''
  }

  /** 「你的头像」读取：失败回落空占位，不阻塞初始化。 */
  async function loadUserAvatar() {
    try { userAvatar.value = (await botApi.getUserAvatar()).avatar || '' }
    catch { userAvatar.value = '' }
  }

  async function selectConversation(id: string) {
    stopGenerating()
    activeConversationId.value = id
    messages.value = []
    streamText.value = ''
    streamReasoning.value = ''
    error.value = ''
    lastFailedText.value = ''
    if (!id) return
    try {
      messages.value = await botApi.getMessages(id)
    } catch (e) {
      error.value = toFriendlyMessage(e, '聊天记录读取失败')
    }
  }

  /** 关系状态读接口：失败不该拖垮整个页面，退化成不显示徽标即可。 */
  async function refreshRelations() {
    try {
      relations.value = await botApi.getRelations()
    } catch {
      /* 忽略：关系只是展示层信息，拿不到不影响聊天 */
    }
  }

  async function init() {
    error.value = ''
    try {
      const [personaRows, conversationRows] = await Promise.all([
        botApi.listPersonas(),
        botApi.listConversations(),
        refreshRelations(),
        loadUserAvatar(),
      ])
      personas.value = personaRows
      conversations.value = conversationRows
      if (!personaRows.some(p => p.id === activePersonaId.value)) activePersonaId.value = personaRows[0]?.id || ''
      const visible = conversationRows.filter(c => c.persona_id === activePersonaId.value)
      const selected = visible.some(c => c.id === activeConversationId.value) ? activeConversationId.value : visible[0]?.id || ''
      await selectConversation(selected)
    } catch (e) {
      error.value = toFriendlyMessage(e, 'cKunBot 初始化失败')
    }
  }

  async function selectPersona(id: string) {
    if (streaming.value) return
    activePersonaId.value = id
    await selectConversation(filteredConversations.value[0]?.id || '')
  }

  async function createConversation(personaId = activePersonaId.value) {
    if (channel.value !== 'local' || !personaId || streaming.value) return
    const created = await botApi.createConversation(personaId)
    conversations.value.unshift(created)
    activePersonaId.value = personaId
    await selectConversation(created.id)
  }

  async function deleteConversation(id: string) {
    if (streaming.value && id === activeConversationId.value) stopGenerating()
    await botApi.deleteConversation(id)
    conversations.value = conversations.value.filter(c => c.id !== id)
    if (activeConversationId.value === id) await selectConversation(filteredConversations.value[0]?.id || '')
  }

  async function sendMessage(content: string, images?: string[]) {
    const text = content.trim()
    const imgs = (images || []).filter(Boolean)
    const conversationId = activeConversationId.value
    if (channel.value !== 'local' || !text || !conversationId || streaming.value) return
    streaming.value = true
    error.value = ''
    lastFailedText.value = ''
    streamText.value = ''
    streamReasoning.value = ''
    pacing.value = ''
    messages.value.push({
      id: `local-${Date.now()}`, conversation_id: conversationId, role: 'user', content: text, images: imgs, created_at: new Date().toISOString(),
    })
    abortController = new AbortController()
    // 本轮自己的 controller 必须独持一份引用：finally 里若直接写 `abortController = null`，
    // 在「停止 → 立刻再发」的时序里会把下一轮的 controller 清掉，停止按钮随即失效。
    const controller = abortController
    try {
      const saved = await streamBotChat(conversationId, text, {
        signal: controller.signal,
        images: imgs,
        onContent: chunk => { if (activeConversationId.value === conversationId) streamText.value += chunk },
        onReasoning: chunk => { if (activeConversationId.value === conversationId) streamReasoning.value += chunk },
        onPacing: hint => { if (activeConversationId.value === conversationId) pacing.value = hint },
      })
      if (activeConversationId.value === conversationId) {
        // 先记 id 再 push：视图的排程 watcher 在同一批更新里就要读到它
        lastStreamedId.value = saved.id
        messages.value.push(saved)
      }
    } catch (e) {
      if (!(e instanceof Error && e.name === 'AbortError')) {
        error.value = toFriendlyMessage(e, '回复生成失败')
        lastFailedText.value = text
      }
    } finally {
      if (activeConversationId.value === conversationId) {
        streaming.value = false
        streamText.value = ''
        streamReasoning.value = ''
      }
      if (abortController === controller) abortController = null
      // 后端在生成前就记了这轮互动，这里补一次读：关系阶段可能在刚才那一句之后变了。
      void refreshRelations()
    }
  }

  function stopGenerating() {
    abortController?.abort()
    abortController = null
    streaming.value = false
    streamText.value = ''
    streamReasoning.value = ''
  }

  return {
    personas, conversations, messages, activePersonaId, activeConversationId,
    activePersona, activeConversation, filteredConversations,
    relations, activeRelation,
    streamText, streamReasoning, streaming, error, lastFailedText, pacing, lastStreamedId,
    channel, isRemote, remoteChannelLabel,
    remoteConversations, remoteMessages, remoteActiveId, remoteError, remoteActiveConversation,
    userAvatar,
    probing, probeNote,
    init, selectPersona, selectConversation, createConversation, deleteConversation,
    sendMessage, stopGenerating, selectChannel, selectRemoteConversation, refreshRemote,
    deleteRemoteConversation, updateConversationDisplay, loadUserAvatar, probeProactive,
  }
})
