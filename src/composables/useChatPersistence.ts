// src/composables/useChatPersistence.ts
import { ref, shallowRef, computed } from 'vue'
import { api } from '@/api/client'
import type {
  BaseConversation,
  BaseMessage,
  ModeConfig,
  PersistenceStrategy,
} from '@/types/chat'

import { genId } from '@/utils/id'

export interface UseChatPersistenceOptions<
  TContext = unknown,
  TMessage extends BaseMessage = BaseMessage,
  TConversation extends BaseConversation<TMessage> = BaseConversation<TMessage>,
> {
  config: ModeConfig<TContext, TMessage, TConversation>
  getDefaultModelId: () => Promise<string | undefined>
}

export function useChatPersistence<
  TContext = unknown,
  TMessage extends BaseMessage = BaseMessage,
  TConversation extends BaseConversation<TMessage> = BaseConversation<TMessage>,
>(options: UseChatPersistenceOptions<TContext, TMessage, TConversation>) {
  const { config, getDefaultModelId } = options
  const { persistence, storageKey, modelStorageKey, mode } = config

  function loadConversations(): TConversation[] {
    if (persistence !== 'dual') return []
    try {
      const raw = localStorage.getItem(storageKey)
      return raw ? JSON.parse(raw) : []
    } catch (e) {
      // m3 修复：JSON 解析失败时输出警告，便于排查数据损坏问题
      console.warn(`[useChatPersistence] 解析 ${storageKey} 失败，重置为空列表:`, e)
      return []
    }
  }

  function saveConversations(convs: TConversation[]) {
    if (persistence !== 'dual') return
    localStorage.setItem(storageKey, JSON.stringify(convs))
  }

  function loadSelectedModelId(): string {
    if (persistence !== 'dual') return ''
    try {
      return localStorage.getItem(modelStorageKey) || ''
    } catch {
      return ''
    }
  }

  function saveSelectedModelId(id: string) {
    if (persistence !== 'dual') return
    localStorage.setItem(modelStorageKey, id)
  }

  async function loadConversationsFromBackend(limit = 50, offset = 0) {
    try {
      const convs = await api.listConversations(limit, offset, mode)
      conversations.value = convs.map((c: any) => ({
        id: c.id,
        title: c.title,
        messages: [] as TMessage[],
        backendConvId: c.id,
        created_at: c.created_at,
        updated_at: c.updated_at,
      })) as TConversation[]
    } catch (e) {
      console.error(`[${mode}] loadConversationsFromBackend failed:`, e)
      conversations.value = []
    }
  }

  const conversations = shallowRef<TConversation[]>(loadConversations())
  const currentConvId = ref<string | null>(null)
  const selectedModelId = ref<string>(loadSelectedModelId())

  const currentConversation = computed(() =>
    conversations.value.find((c) => c.id === currentConvId.value) ?? null
  )

  // 注意：必须直接依赖 conversations（shallowRef），而非 currentConversation。
  // 因为 currentConversation 的 find 返回同一对象引用，computed 缓存命中后不会触发下游。
  // triggerRef(conversations) 需要直接传播到这里，且返回新数组引用（[...msg]）让下游检测到变化。
  const messages = computed(() => {
    const conv = conversations.value.find((c) => c.id === currentConvId.value)
    return conv ? [...conv.messages] : []
  })

  const groupedConversations = computed(() => {
    const groups: Array<{ label: string; items: Array<{ id: string; title: string; updated_at: string }> }> = []
    const now = new Date()
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
    const yesterday = new Date(today.getTime() - 86400000)

    for (const conv of conversations.value) {
      const d = new Date(conv.updated_at)
      let label: string
      if (d >= today) label = '今天'
      else if (d >= yesterday) label = '昨天'
      else label = d.toLocaleDateString('zh-CN', { month: 'long', day: 'numeric' })

      const group = groups.find(g => g.label === label)
      if (group) group.items.push(conv)
      else groups.push({ label, items: [conv] })
    }
    return groups
  })

  async function fetchDefaultModel() {
    if (selectedModelId.value) return
    try {
      const id = await getDefaultModelId()
      if (id) {
        selectedModelId.value = id
        saveSelectedModelId(id)
      }
    } catch { /* ignore */ }
  }

  function setSelectedModel(id: string) {
    selectedModelId.value = id
    saveSelectedModelId(id)
  }

  async function createConversation(title?: string): Promise<TConversation> {
    if (persistence === 'backend') {
      try {
        const backendConv = await api.createConversation(title || 'New Chat', mode)
        const conv = {
          id: backendConv.id,
          title: backendConv.title,
          messages: [] as TMessage[],
          backendConvId: backendConv.id,
          created_at: backendConv.created_at,
          updated_at: backendConv.updated_at,
        } as TConversation
        conversations.value = [conv, ...conversations.value]
        currentConvId.value = conv.id
        return conv
      } catch (e) {
        console.error(`[${mode}] createConversation backend failed:`, e)
        throw e
      }
    }

    // dual 策略保持原有逻辑
    const conv = {
      id: genId(),
      title: title || 'New Chat',
      messages: [] as TMessage[],
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    } as TConversation
    conversations.value = [conv, ...conversations.value]
    currentConvId.value = conv.id
    saveConversations(conversations.value)
    return conv
  }

  async function switchConversation(id: string, loadMessages?: (conv: TConversation) => Promise<void>) {
    currentConvId.value = id
    if (persistence === 'backend' && loadMessages) {
      const conv = currentConversation.value
      if (conv) await loadMessages(conv)
    }
  }

  async function deleteConversation(id: string) {
    const conv = conversations.value.find((c) => c.id === id)
    if (conv?.backendConvId) {
      try { await api.deleteConversation(conv.backendConvId) } catch { /* ignore */ }
    }
    conversations.value = conversations.value.filter((c) => c.id !== id)
    if (currentConvId.value === id) {
      currentConvId.value = conversations.value[0]?.id ?? null
    }
    saveConversations(conversations.value)
  }

  async function ensureBackendConversation(conv: TConversation): Promise<void> {
    if (conv.backendConvId) return
    try {
      const backendConv = await api.createConversation(conv.title, mode)
      conv.backendConvId = backendConv.id
      saveConversations(conversations.value)
    } catch (e) {
      console.error(`[${mode}] Failed to create backend conversation:`, e)
      throw e
    }
  }

  /** 手动触发本地持久化（shallowRef 内部对象变更后需要显式保存） */
  function save() {
    saveConversations(conversations.value)
  }

  return {
    conversations,
    currentConvId,
    currentConversation,
    messages,
    selectedModelId,
    groupedConversations,
    fetchDefaultModel,
    setSelectedModel,
    createConversation,
    switchConversation,
    deleteConversation,
    ensureBackendConversation,
    loadConversationsFromBackend,
    save,
  }
}
