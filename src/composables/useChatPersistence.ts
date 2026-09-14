// src/composables/useChatPersistence.ts
//
// SSH 模式会话持久化（固定 dual 策略：localStorage 权威 + 后端懒同步）。
// 原多模式抽象层（ChatMode 联合 / 'backend' 策略分支 / loadConversationsFromBackend
// 整体替换）随"固定 Agent 模式"重构收敛删除，仅保留 ssh-ai 实际使用的路径。
import { ref, shallowRef, computed } from 'vue'
import { api } from '@/api/client'
import type {
  ChatStorageKeys,
  ConvGroup,
  ModeConversation,
  ModeMessage,
} from '@/types/chat'

import { genId } from '@/utils/id'
// 审查修复：原文件在 deleteConversation 失败分支使用了 useToastStore 但从未导入
// （typecheck TS2304；运行时该分支一触发就抛 ReferenceError，toast 永远弹不出来）
import { useToastStore } from '@/stores/toast'

/** 后端 API 的会话模式标识（固定 Agent 模式后仅 ssh 一种） */
const API_MODE = 'ssh'

export function useChatPersistence(options: {
  config: ChatStorageKeys
  getDefaultModelId: () => Promise<string | undefined>
}) {
  const { config, getDefaultModelId } = options
  const { storageKey, modelStorageKey } = config

  function loadConversations(): ModeConversation[] {
    try {
      const raw = localStorage.getItem(storageKey)
      return raw ? JSON.parse(raw) : []
    } catch (e) {
      // m3 修复：JSON 解析失败时输出警告，便于排查数据损坏问题
      console.warn(`[useChatPersistence] 解析 ${storageKey} 失败，重置为空列表:`, e)
      return []
    }
  }

  function saveConversations(convs: ModeConversation[]) {
    localStorage.setItem(storageKey, JSON.stringify(convs))
  }

  function loadSelectedModelId(): string {
    try {
      return localStorage.getItem(modelStorageKey) || ''
    } catch {
      return ''
    }
  }

  function saveSelectedModelId(id: string) {
    localStorage.setItem(modelStorageKey, id)
  }

  /**
   * P2 修复：启动时合并后端会话列表。
   *
   * 原缺口：历史仅靠 localStorage 恢复 — 换设备/清本地后，后端已同步的
   * 会话在列表里凭空消失。
   * 合并语义：
   *   - 后端有、本地无（按 id 或 backendConvId 双键匹配）→ 追加（消息空，切换时按需拉取）
   *   - 两边都有 → 仅更新 title/updated_at（本地消息是权威，不动）
   *   - 本地有、后端无 → 保留（本地新建未同步的会话不能丢）
   * 失败时不动本地列表（后端不可达 ≠ 历史丢失），仅记警告。
   */
  async function syncBackendConversations() {
    try {
      const remote = await api.listConversations(50, 0, API_MODE)
      loadFailed.value = false
      const byLocalId = new Map(conversations.value.map((c) => [c.id, c]))
      const byBackendId = new Map(
        conversations.value.filter((c) => (c as any).backendConvId).map((c) => [(c as any).backendConvId as string, c]),
      )
      const merged = [...conversations.value]
      for (const r of remote as Array<{ id: string; title: string; created_at: string; updated_at: string }>) {
        const match = byBackendId.get(r.id) ?? byLocalId.get(r.id)
        if (match) {
          if (r.title) match.title = r.title
          if (r.updated_at) match.updated_at = r.updated_at
        } else {
          merged.push({
            id: r.id,
            title: r.title,
            messages: [] as ModeMessage[],
            backendConvId: r.id,
            created_at: r.created_at,
            updated_at: r.updated_at,
          } as ModeConversation)
        }
      }
      // 浅引用新数组触发 groupedConversations 等下游重算（原地改 title 也一并生效）
      conversations.value = merged
      saveConversations(merged)
    } catch (e) {
      console.warn(`[${API_MODE}] syncBackendConversations failed（保留本地列表）:`, e)
    }
  }

  const conversations = shallowRef<ModeConversation[]>(loadConversations())
  // 审查修复：重启后恢复上次会话（localStorage 含完整消息，仅恢复选中态）。
  // 原实现恒为 null，每次重启 AI 面板都显示空态，用户需手动从历史记录点回。
  const currentConvId = ref<string | null>(conversations.value[0]?.id ?? null)
  const selectedModelId = ref<string>(loadSelectedModelId())
  // B24 修复：后端会话列表加载失败标志（成功加载即复位），供 UI 展示
  const loadFailed = ref(false)

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

  const groupedConversations = computed<ConvGroup[]>(() => {
    const groups: ConvGroup[] = []
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
    // 审查修复：组内按更新时间倒序（新 → 旧），与列表顶部优先的展示顺序一致
    for (const g of groups) {
      g.items.sort((a, b) =>
        new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
      )
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
    } catch (e) {
      console.debug('[useChatPersistence] fetchDefaultModel skipped:', e)
    }
  }

  function setSelectedModel(id: string) {
    selectedModelId.value = id
    saveSelectedModelId(id)
  }

  async function createConversation(title?: string): Promise<ModeConversation> {
    const conv = {
      id: genId(),
      title: title || 'New Chat',
      messages: [] as ModeMessage[],
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    } as ModeConversation
    conversations.value = [conv, ...conversations.value]
    currentConvId.value = conv.id
    saveConversations(conversations.value)
    return conv
  }

  function switchConversation(id: string) {
    currentConvId.value = id
  }

  async function deleteConversation(id: string) {
    const conv = conversations.value.find((c) => c.id === id)
    // 后端删除失败时回滚本地状态，避免"删掉的会话又回来了"的数据不一致
    if (conv?.backendConvId) {
      try {
        await api.deleteConversation(conv.backendConvId)
      } catch (e) {
        console.error('[useChatPersistence] Backend delete failed:', e)
        const toast = useToastStore()
        toast.error('服务端删除失败，请稍后重试')
        return // 后端删除失败，不回滚本地状态
      }
    }
    conversations.value = conversations.value.filter((c) => c.id !== id)
    if (currentConvId.value === id) {
      currentConvId.value = conversations.value[0]?.id ?? null
    }
    saveConversations(conversations.value)
  }

  async function ensureBackendConversation(conv: ModeConversation): Promise<void> {
    if (conv.backendConvId) return
    try {
      const backendConv = await api.createConversation(conv.title, API_MODE)
      conv.backendConvId = backendConv.id
      saveConversations(conversations.value)
    } catch (e) {
      console.error(`[${API_MODE}] Failed to create backend conversation:`, e)
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
    loadFailed,
    fetchDefaultModel,
    setSelectedModel,
    createConversation,
    switchConversation,
    deleteConversation,
    ensureBackendConversation,
    syncBackendConversations,
    save,
  }
}
