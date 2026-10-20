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
import { parseBackendTs } from '@/utils/format'
import { useToastStore } from '@/stores/toast'

/** 后端 API 的会话模式标识（固定 Agent 模式后仅 ssh 一种） */
const API_MODE = 'ssh'

// 审查R2修复：localStorage 写入失败（QuotaExceeded）的告警去重标志（模块级，全会话只 toast 一次防刷屏）
let quotaWarned = false

export function useChatPersistence(options: {
  config: ChatStorageKeys
  getDefaultModelId: () => Promise<string | undefined>
}) {
  const { config, getDefaultModelId } = options
  const { storageKey, modelStorageKey } = config

  function loadConversations(): ModeConversation[] {
    try {
      const raw = localStorage.getItem(storageKey)
      const parsed = raw ? JSON.parse(raw) : []
      // 审查R2修复：JSON.parse 成功不等于形态合法——非数组值（如被写进 `{}`/字符串）
      // 或条目缺 messages 会让 messages computed / .find / 分组渲染整个 AI 面板抛错白屏。
      // 按条目形状过滤，坏数据降级为空列表。
      if (!Array.isArray(parsed)) {
        console.warn(`[useChatPersistence] ${storageKey} 内容不是数组，重置为空列表`)
        return []
      }
      // S9-18：`temp-agent-` 是运行中占位气泡的 id 前缀，只在「同一次 run 还活着」
      // 期间有意义（ssh-ai 的终态 watch 负责把它换成真结果或移除）。页面重载后那场
      // run 必然已经没了 ⇒ 复原回来的占位永远等不到回填，界面上是一只永久空白的
      // 助手气泡。读侧直接丢弃，比让它挂着再由 UI 猜要诚实。
      return parsed
        .filter((c: any) => c && typeof c === 'object' && Array.isArray(c.messages))
        .map((c: any) => ({
          ...c,
          messages: c.messages.filter(
            (m: any) => !(m?.role === 'assistant' && String(m?.id).startsWith('temp-agent-')),
          ),
        }))
    } catch (e) {
      // m3 修复：JSON 解析失败时输出警告，便于排查数据损坏问题
      console.warn(`[useChatPersistence] 解析 ${storageKey} 失败，重置为空列表:`, e)
      return []
    }
  }

  function saveConversations(convs: ModeConversation[]) {
    // 审查R2修复（a）：裸 setItem 在配额满（约 5MB）时抛 QuotaExceededError，
    // 会把异常溅进所有调用方（发送消息/删除会话/启动合并）。捕获后：
    // ① toast 告警一次（模块级标志防刷屏）；② 按 updated_at 淘汰最旧一条会话后重试一次。
    try {
      localStorage.setItem(storageKey, JSON.stringify(convs))
    } catch (e) {
      console.warn(`[useChatPersistence] 写入 ${storageKey} 失败（可能配额已满）:`, e)
      if (!quotaWarned) {
        quotaWarned = true
        try {
          useToastStore().error('本地存储已满，会话保存失败，已自动清理最旧的历史会话')
        } catch { /* 单测等无 pinia 环境忽略 */ }
      }
      if (convs.length <= 1) return
      const oldestIdx = convs.reduce(
        (acc, c, i) =>
          parseBackendTs(c.updated_at).getTime() < parseBackendTs(convs[acc].updated_at).getTime() ? i : acc,
        0,
      )
      const trimmed = convs.filter((_, i) => i !== oldestIdx)
      try {
        localStorage.setItem(storageKey, JSON.stringify(trimmed))
        // 内存侧同步淘汰（调用方传入的就是 conversations.value 时），
        // 否则下一次保存又会把被淘汰的会话写回来，反复失败
        if (conversations.value === convs) conversations.value = trimmed
      } catch (e2) {
        console.warn(`[useChatPersistence] 淘汰最旧会话后仍写入失败，放弃本次持久化:`, e2)
      }
    }
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
      // 这三个集必须在 await **之后**取。原实现在 await 之前就快照了
      // `conversations.value`，然后把那份旧快照赋回去并落盘 —— 这一句
      // 「本地新建未同步的会话不能丢」的注释因此正好说了反话：往返期间
      // 新建/发过消息的会话既从列表里消失，也被从 localStorage 里抹掉。
      const live = conversations.value
      const byLocalId = new Map(live.map((c) => [c.id, c]))
      const byBackendId = new Map(
        live.filter((c) => c.backendConvId).map((c) => [c.backendConvId as string, c]),
      )
      const merged = [...live]
      for (const r of remote as Array<{ id: string; title: string; created_at: string; updated_at: string }>) {
        const match = byBackendId.get(r.id) ?? byLocalId.get(r.id)
        if (match) {
          if (r.title) match.title = r.title
          if (r.updated_at) match.updated_at = r.updated_at
          // 只按本地 id 命中时 backendConvId 还是空的（早期版本沿用后端 id 建过条目）：
          // 补上，否则切到它会因缺 backendConvId 拉不到消息、删除时也不会同步删后端
          if (!match.backendConvId) match.backendConvId = r.id
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
      // 后端条目原样存 "YYYY-MM-DD HH:MM:SS"（UTC-naive），本地条目是 toISOString()：
      // 裸 new Date() 把前者按本地时区解析，UTC+8 下几分钟前的会话会被归到「昨天」。
      const d = parseBackendTs(conv.updated_at)
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
        parseBackendTs(b.updated_at).getTime() - parseBackendTs(a.updated_at).getTime()
      )
    }
    // 组间也要按各自最新条目倒序：conversations.value 本身不保证按 updated_at 有序
    // （syncBackendConversations 往后追加远端条目、createConversation 往前插），
    // 分组按首次遇到顺序生成会让「昨天」组排在「今天」组上面。items 已降序，
    // 故取每组 items[0] 的时间即该组最新时间。
    groups.sort((a, b) =>
      parseBackendTs(b.items[0].updated_at).getTime() - parseBackendTs(a.items[0].updated_at).getTime()
    )
    return groups
  })

  async function fetchDefaultModel() {
    // 原实现的 `if (selectedModelId.value) return` 让「已删模型留下的残留选中值」
    // 永远没有修正机会：选择器因为列表里找不到它而显示「选择模型」，发送却照旧带
    // 死 id → 后端 404 模型不存在（routes/agent.py:457），面板从此发不出东西。
    // 「这个选中值还能不能用」只有 getDefaultModelId 看得懂（它拿得到模型列表）。
    try {
      const id = await getDefaultModelId()
      if (id) {
        selectedModelId.value = id
        saveSelectedModelId(id)
      }
    } catch (e) {
      // 后端未就绪/模型列表为空都会走到这里：全静默会让"选不到模型"无从排查
      console.warn('[useChatPersistence] 取默认模型失败:', e)
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

  /**
   * 进入空白任务：只把当前指针挪开，不建条目也不落盘。
   * 会话要等首条消息发出后由 `_sendAgentMessageInner` 的懒建路径创建 ——
   * 提前建一条空会话会在历史列表里留下一行永远没内容的记录。
   */
  function beginNewTask() {
    currentConvId.value = null
  }

  async function deleteConversation(id: string) {
    const conv = conversations.value.find((c) => c.id === id)
    // 后端删不掉就保留本地条目并原样返回：本地先删会把服务端会话变成再也删不到的
    // 孤儿，下次启动又被 syncBackendConversations 合并回来（"删掉的会话又回来了"）
    if (conv?.backendConvId) {
      try {
        await api.deleteConversation(conv.backendConvId)
      } catch (e) {
        console.error('[useChatPersistence] Backend delete failed:', e)
        const toast = useToastStore()
        toast.error('服务端删除失败，请稍后重试')
        return
      }
    }
    conversations.value = conversations.value.filter((c) => c.id !== id)
    if (currentConvId.value === id) {
      currentConvId.value = conversations.value[0]?.id ?? null
    }
    saveConversations(conversations.value)
  }

  /**
   * 确保后端有这一行会话。
   *
   * `backendTitle` 是给后端看的标题，与本地列表显示的 `conv.title` 刻意分开：
   * S9-05 —— 隐式建会话时本地派生的 30 字摘要一旦落库，就永远不在
   * `post_processor._DEFAULT_TITLES` 里了，而后端起标题的门槛正是
   * 「当前标题 ∈ 默认集合」⇒ LLM 与规则兜底两条路都永久早退，
   * 会话标题就此停在摘要串上。传占位标题即可把命名权交还给后端生成。
   */
  async function ensureBackendConversation(conv: ModeConversation, backendTitle?: string): Promise<void> {
    if (conv.backendConvId) return
    try {
      const backendConv = await api.createConversation(backendTitle ?? conv.title, API_MODE)
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
    fetchDefaultModel,
    setSelectedModel,
    createConversation,
    switchConversation,
    beginNewTask,
    deleteConversation,
    ensureBackendConversation,
    syncBackendConversations,
    save,
  }
}
