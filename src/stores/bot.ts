import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { botApi, streamBotChat, type BotConversation, type BotMessage, type BotPersona } from '@/api/bot'
import { toFriendlyMessage } from '@/api/base'

export const useBotStore = defineStore('bot', () => {
  const personas = ref<BotPersona[]>([])
  const conversations = ref<BotConversation[]>([])
  const messages = ref<BotMessage[]>([])
  const activePersonaId = ref('')
  const activeConversationId = ref('')
  const streamText = ref('')
  const streamReasoning = ref('')
  const streaming = ref(false)
  const loading = ref(false)
  const error = ref('')
  const lastFailedText = ref('')
  const channel = ref<'local' | 'qq'>('local')
  const qqConversations = ref<BotConversation[]>([])
  const qqMessages = ref<BotMessage[]>([])
  const qqActiveId = ref('')
  const qqError = ref('')
  let abortController: AbortController | null = null
  let qqRefreshing = false

  const activePersona = computed(() => personas.value.find(p => p.id === activePersonaId.value) || null)
  const filteredConversations = computed(() => conversations.value.filter(c => c.persona_id === activePersonaId.value))
  const activeConversation = computed(() => conversations.value.find(c => c.id === activeConversationId.value) || null)
  const isQq = computed(() => channel.value === 'qq')
  const qqActiveConversation = computed(() => qqConversations.value.find(c => c.id === qqActiveId.value) || null)

  function selectChannel(next: 'local' | 'qq') {
    if (next === channel.value) return
    stopGenerating()
    channel.value = next
  }

  async function selectQqConversation(id: string) {
    qqActiveId.value = id
    qqMessages.value = []
    try {
      const rows = await botApi.getMessages(id)
      if (qqActiveId.value === id) qqMessages.value = rows
      qqError.value = ''
    } catch (e) {
      qqError.value = toFriendlyMessage(e, 'QQ 记录读取失败')
    }
  }

  async function refreshQq() {
    if (qqRefreshing) return
    qqRefreshing = true
    try {
      const rows = await botApi.listQqConversations()
      qqConversations.value = rows
      if (!rows.length) {
        qqActiveId.value = ''
        qqMessages.value = []
        qqError.value = ''
        return
      }
      if (!rows.some(c => c.id === qqActiveId.value)) qqActiveId.value = rows[0].id
      const target = qqActiveId.value
      const history = await botApi.getMessages(target)
      if (qqActiveId.value === target) qqMessages.value = history
      qqError.value = ''
    } catch (e) {
      qqError.value = toFriendlyMessage(e, 'QQ 记录读取失败')
    } finally {
      qqRefreshing = false
    }
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

  async function init() {
    loading.value = true
    error.value = ''
    try {
      const [personaRows, conversationRows] = await Promise.all([
        botApi.listPersonas(),
        botApi.listConversations(),
      ])
      personas.value = personaRows
      conversations.value = conversationRows
      if (!personaRows.some(p => p.id === activePersonaId.value)) activePersonaId.value = personaRows[0]?.id || ''
      const visible = conversationRows.filter(c => c.persona_id === activePersonaId.value)
      const selected = visible.some(c => c.id === activeConversationId.value) ? activeConversationId.value : visible[0]?.id || ''
      await selectConversation(selected)
    } catch (e) {
      error.value = toFriendlyMessage(e, 'cKunBot 初始化失败')
    } finally {
      loading.value = false
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

  async function sendMessage(content: string) {
    const text = content.trim()
    const conversationId = activeConversationId.value
    if (channel.value !== 'local' || !text || !conversationId || streaming.value) return
    streaming.value = true
    error.value = ''
    lastFailedText.value = ''
    streamText.value = ''
    streamReasoning.value = ''
    messages.value.push({
      id: `local-${Date.now()}`, conversation_id: conversationId, role: 'user', content: text, created_at: new Date().toISOString(),
    })
    abortController = new AbortController()
    try {
      const saved = await streamBotChat(conversationId, text, {
        signal: abortController.signal,
        onContent: chunk => { if (activeConversationId.value === conversationId) streamText.value += chunk },
        onReasoning: chunk => { if (activeConversationId.value === conversationId) streamReasoning.value += chunk },
      })
      if (activeConversationId.value === conversationId) messages.value.push(saved)
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
      abortController = null
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
    streamText, streamReasoning, streaming, loading, error, lastFailedText,
    channel, isQq, qqConversations, qqMessages, qqActiveId, qqError, qqActiveConversation,
    init, selectPersona, selectConversation, createConversation, deleteConversation,
    sendMessage, stopGenerating, selectChannel, selectQqConversation, refreshQq,
  }
})
