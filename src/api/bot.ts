import { backendErrorDetail, ensureApiToken, getBaseUrl, request, toFriendlyMessage, withApiToken } from './base'

/** 表达 DNA：说话方式的可执行档位。档位文案在服务端，这里只约束取值。 */
export interface BotExpressionDna {
  sentence: 'short' | 'mixed' | 'long'
  particles: 'none' | 'soft' | 'lively' | 'dry'
  punctuation: 'plain' | 'ellipsis' | 'tilde' | 'exclaim'
  emoji: 'none' | 'low' | 'medium'
  kaomoji: 'none' | 'low' | 'medium'
  sticker: 'none' | 'low' | 'medium'
  catchphrase: string
  taboo: string[]
}

export type BotReactionStyle = 'reserved' | 'warm' | 'playful' | 'blunt'
export type BotRefusalStyle = 'soft' | 'direct' | 'playful' | 'concise'
export type BotRelationshipSpeed = 'slow' | 'normal' | 'fast'
/** 群参与档：从只 @ 才答到群里主动开口，打扰程度递增（后端 GROUP_MODES 同序）。 */
export type BotGroupMode = 'mention_only' | 'ambient' | 'proactive'

export interface BotPersona {
  id: string
  name: string
  avatar: string
  personality: string
  background: string
  greeting: string
  model_id: string | null
  reply_style: 'concise' | 'moderate' | 'long'
  enable_thinking: number
  proactive_enabled: number
  expression_dna: BotExpressionDna
  reaction_style: BotReactionStyle
  refusal_style: BotRefusalStyle
  address_user: string
  relationship_speed: BotRelationshipSpeed
  /** 群参与档：迁移前的旧人格记录里没这个字段，后端按最保守档读。 */
  group_mode?: BotGroupMode
  /** 群回复要不要 @ 提问者：1 = 照旧 @（被动回复）。旧记录缺字段同样按照旧 @ 读。 */
  mention_sender?: number
  /** 人格内核扩展四件套：可选 —— 旧人格记录里没有这四列，后端也按缺省读。 */
  likes_landmines?: string
  memory_directive?: string
  dialogue_samples?: string
  advanced_play?: string
  is_builtin: number
  created_at: string
  updated_at: string
}

export interface BotPersonaPayload {
  name: string
  avatar: string
  personality: string
  background: string
  greeting: string
  model_id: string | null
  reply_style: 'concise' | 'moderate' | 'long'
  enable_thinking: boolean
  proactive_enabled: boolean
  expression_dna: BotExpressionDna
  reaction_style: BotReactionStyle
  refusal_style: BotRefusalStyle
  address_user: string
  relationship_speed: BotRelationshipSpeed
  group_mode: BotGroupMode
  /** 群回复要不要 @ 提问者（关掉后这句改走不带 msg_id 的主动消息）。 */
  mention_sender: boolean
  /** 人格内核扩展四件套：可选 —— 局部更新时不必带全。 */
  likes_landmines?: string
  memory_directive?: string
  dialogue_samples?: string
  advanced_play?: string
}

/** 关系状态：分数只增不减，衰减在服务端读时计算，前端只展示阶段与进度。 */
export interface BotRelation {
  persona_id: string
  stage: '陌生' | '熟悉' | '亲近' | '默契'
  progress: number
  next_at: number | null
  score: number
  raw_score: number
  turns: number
  days_since: number | null
}

export interface BotOptionGroup {
  label: string
  options: { value: string; label: string }[]
}

/** 人格编辑器的档位字典：label 与可选项都由服务端给出，前端不手抄文案。 */
export type BotPersonaOptions = Record<
  | 'sentence' | 'particles' | 'punctuation' | 'emoji' | 'kaomoji' | 'sticker'
  | 'reaction_style' | 'refusal_style' | 'relationship_speed' | 'group_mode',
  BotOptionGroup
>

export interface BotConversation {
  id: string
  title: string
  persona_id: string | null
  /** 会话归属人格的展示三字段：人格被删后仍是当时的名字，头像为空则回退首字母。 */
  persona: { id: string; name: string; avatar: string }
  channel: 'local' | 'qq' | 'wechat'
  external_id: string | null
  /** 最后一条消息的内容，服务端顺带算出，侧栏可直接用作摘要。 */
  preview: string | null
  /** Phase 4 QQ 会话本地显示覆盖：空/缺省 = 回落 title 与代号占位头像；本地会话恒空。 */
  display_name?: string | null
  display_avatar?: string | null
  created_at: string
  updated_at: string
}

export interface BotMessage {
  id: string
  conversation_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  external_msg_id?: string | null
  reasoning?: string | null
  /** 本地看图回复：用户消息携带的图片 data URL 列表（助手消息恒为空）。 */
  images?: string[] | null
  created_at: string
}

export type BotChannelState = 'offline' | 'connecting' | 'online' | 'reconnecting' | 'error'

export interface BotChannelStatus {
  state: BotChannelState
  detail: string
}

export type BotProvider = 'qq' | 'wechat'

/** 一个机器人账号。一行绑一个人格；人格可改，但同一时刻只有一个。 */
export interface BotChannel {
  id: string
  provider: BotProvider
  display_name: string
  persona_id: string
  app_id: string
  /** 密钥只写不回显，接口只告诉前端「有没有」。 */
  has_secret: boolean
  /** 第三个凭据（微信 EncodingAESKey）；QQ 恒 false。 */
  has_extra_secret: boolean
  enabled: boolean
  /** 连带会被删掉的会话条数，删除确认文案要用。 */
  conversation_count: number
  status: BotChannelStatus
}

export interface BotChannelPayload {
  provider?: BotProvider
  display_name: string
  persona_id: string
  app_id: string
  app_secret?: string
  extra_secret?: string
}

export interface BotWebhook {
  base_url: string
}

/**
 * 手动探口（「试一句」）的回显：后端把结果念回同一本账本，这里只摊开给人看。
 *
 * action 就是那一行的动作：push=已发出、send=已直投本地会话、
 * queued=通道现在发不出去所以寄存、skip=平台拒了、missing=人格已不存在。
 */
export interface ProactiveProbeResult {
  conversation_id: string
  action: 'push' | 'send' | 'queued' | 'skip' | 'missing' | string
  /** 本来想开口的意愿分（0..1）：自动路径卡不卡就看过不过 0.55。 */
  desire: number
  /** 自动路径本来会被哪道闸拦下；能自动开口时为空串。 */
  gate_reason: string
  text: string
  detail: string
}

/** 头像图库条目：各处头像存自己的 data URL 副本，删图库项不影响已设头像。 */
export interface BotGalleryItem {
  id: string
  label: string
  data_url: string
  created_at: string
}

/** 一套内置表情包：id 用于 tab 切换，keys 是 [表情:键] 的键，也是图片路由参数。 */
export interface BotStickerPack {
  id: string
  name: string
  keys: string[]
}

export const botApi = {
  listPersonas: () => request<BotPersona[]>('/bot/personas'),
  getPersonaOptions: () => request<BotPersonaOptions>('/bot/persona-options'),
  /** 内置表情包清单（按包分组）：keys 既是展示名，也是 /assets/stickers/{键} 的路径参数。 */
  getStickers: () => request<BotStickerPack[]>('/bot/stickers'),
  getRelations: () => request<Record<string, BotRelation>>('/bot/relations'),
  createPersona: (payload: BotPersonaPayload) => request<BotPersona>('/bot/personas', { method: 'POST', body: JSON.stringify(payload) }),
  updatePersona: (id: string, payload: Partial<BotPersonaPayload>) => request<BotPersona>(`/bot/personas/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  copyPersona: (id: string) => request<BotPersona>(`/bot/personas/${id}/copy`, { method: 'POST' }),
  deletePersona: (id: string) => request<{ deleted: boolean }>(`/bot/personas/${id}`, { method: 'DELETE' }),
  listConversations: (personaId?: string) => request<BotConversation[]>(`/bot/conversations?channel=local${personaId ? `&persona_id=${encodeURIComponent(personaId)}` : ''}`),
  /** 远程渠道的只读会话：QQ 与微信同一条读取路径，只有渠道名不同。 */
  listChannelConversations: (channel: 'qq' | 'wechat') => request<BotConversation[]>(`/bot/conversations?channel=${channel}`),
  createConversation: (personaId: string) => request<BotConversation>('/bot/conversations', { method: 'POST', body: JSON.stringify({ persona_id: personaId, channel: 'local' }) }),
  getMessages: (id: string) => request<BotMessage[]>(`/bot/conversations/${id}/messages?limit=200`),
  deleteConversation: (id: string) => request<{ deleted: boolean }>(`/bot/conversations/${id}`, { method: 'DELETE' }),
  listChannels: () => request<BotChannel[]>('/bot/channels'),
  createChannel: (payload: BotChannelPayload) => request<BotChannel>('/bot/channels', { method: 'POST', body: JSON.stringify(payload) }),
  updateChannel: (id: string, payload: Partial<BotChannelPayload>) => request<BotChannel>(`/bot/channels/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteChannel: (id: string) => request<{ deleted: boolean; conversation_count: number }>(`/bot/channels/${id}`, { method: 'DELETE' }),
  connectChannel: (id: string) => request<BotChannelStatus>(`/bot/channels/${id}/connect`, { method: 'POST' }),
  disconnectChannel: (id: string) => request<BotChannelStatus>(`/bot/channels/${id}/disconnect`, { method: 'POST' }),
  getWebhook: () => request<BotWebhook>('/bot/webhook'),
  setWebhook: (baseUrl: string) => request<BotWebhook>('/bot/webhook', { method: 'PUT', body: JSON.stringify({ base_url: baseUrl }) }),
  // 人格记忆的查看与增删只走 Agent 记忆面板（api/agent.ts），bot 端不再单独封装一套。
  // Phase 4 头像体系：图库 CRUD、「你的头像」、QQ 会话显示覆盖
  listGallery: () => request<BotGalleryItem[]>('/bot/gallery'),
  addGalleryItem: (payload: { label?: string; data_url: string }) => request<BotGalleryItem>('/bot/gallery', { method: 'POST', body: JSON.stringify(payload) }),
  deleteGalleryItem: (id: string) => request<{ deleted: boolean }>(`/bot/gallery/${id}`, { method: 'DELETE' }),
  getUserAvatar: () => request<{ avatar: string }>('/bot/user-avatar'),
  setUserAvatar: (avatar: string) => request<{ avatar: string }>('/bot/user-avatar', { method: 'PUT', body: JSON.stringify({ avatar }) }),
  updateConversationDisplay: (id: string, payload: { display_name?: string; display_avatar?: string }) => request<BotConversation>(`/bot/conversations/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  /** 试一句：绕开意愿分与频率闸真发一次，但间隔与配额照占（失败文案由前端拼）。 */
  probeProactive: (conversationId: string) => request<ProactiveProbeResult>('/bot/proactive/probe', { method: 'POST', body: JSON.stringify({ conversation_id: conversationId }) }),
}

interface BotSseEvent {
  type: 'content' | 'reasoning' | 'pacing' | 'done' | 'error'
  content?: string
  detail?: string
  message?: BotMessage
}

export async function streamBotChat(
  conversationId: string,
  content: string,
  options: { onContent: (text: string) => void; onReasoning?: (text: string) => void; onPacing?: (hint: string) => void; images?: string[]; signal?: AbortSignal },
): Promise<BotMessage> {
  await ensureApiToken()
  let response: Response
  try {
    response = await fetch(`${getBaseUrl()}/bot/chat`, {
      method: 'POST',
      headers: withApiToken({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ conversation_id: conversationId, content, images: options.images ?? [] }),
      signal: options.signal,
    })
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') throw error
    throw new Error(toFriendlyMessage(error, '陪聊服务连接失败'))
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(backendErrorDetail(body) || response.statusText)
  }
  if (!response.body) throw new Error('陪聊服务未返回内容')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let result: BotMessage | null = null
  let failure = ''
  const consume = (line: string) => {
    if (!line.startsWith('data: ')) return
    let event: BotSseEvent
    try { event = JSON.parse(line.slice(6)) as BotSseEvent } catch { return }
    if (event.type === 'content') options.onContent(event.content || '')
    else if (event.type === 'reasoning') options.onReasoning?.(event.content || '')
    else if (event.type === 'pacing') options.onPacing?.(event.content || '')
    else if (event.type === 'error') failure = event.detail || '回复生成失败'
    else if (event.type === 'done' && event.message) result = event.message
  }
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''
      lines.forEach(consume)
    }
    buffer.split('\n').forEach(consume)
  } finally {
    reader.releaseLock()
  }
  if (failure) throw new Error(failure)
  if (!result) throw new Error('回复中断，请重试')
  return result
}
