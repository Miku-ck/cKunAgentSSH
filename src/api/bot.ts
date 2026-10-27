import { backendErrorDetail, ensureApiToken, getBaseUrl, request, toFriendlyMessage, withApiToken } from './base'

export interface BotPersona {
  id: string
  name: string
  avatar: string
  personality: string
  background: string
  greeting: string
  model_id: string | null
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
}

export interface BotConversation {
  id: string
  title: string
  persona_id: string | null
  persona_snapshot: string
  channel: 'local' | 'qq'
  external_id: string | null
  created_at: string
  updated_at: string
}

export interface BotMessage {
  id: string
  conversation_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  external_msg_id?: string | null
  created_at: string
}

export interface BotQqConfig {
  app_id: string
  has_secret: boolean
  persona_id: string
  context_id: string
}

export interface BotQqStatus {
  state: 'offline' | 'connecting' | 'online' | 'error'
  detail: string
}

export const botApi = {
  listPersonas: () => request<BotPersona[]>('/bot/personas'),
  createPersona: (payload: BotPersonaPayload) => request<BotPersona>('/bot/personas', { method: 'POST', body: JSON.stringify(payload) }),
  updatePersona: (id: string, payload: Partial<BotPersonaPayload>) => request<BotPersona>(`/bot/personas/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  copyPersona: (id: string) => request<BotPersona>(`/bot/personas/${id}/copy`, { method: 'POST' }),
  deletePersona: (id: string) => request<{ deleted: boolean }>(`/bot/personas/${id}`, { method: 'DELETE' }),
  listConversations: (personaId?: string) => request<BotConversation[]>(`/bot/conversations?channel=local${personaId ? `&persona_id=${encodeURIComponent(personaId)}` : ''}`),
  createConversation: (personaId: string) => request<BotConversation>('/bot/conversations', { method: 'POST', body: JSON.stringify({ persona_id: personaId, channel: 'local' }) }),
  getMessages: (id: string) => request<BotMessage[]>(`/bot/conversations/${id}/messages?limit=200`),
  deleteConversation: (id: string) => request<{ deleted: boolean }>(`/bot/conversations/${id}`, { method: 'DELETE' }),
  getQqConfig: () => request<BotQqConfig>('/bot/qq/config'),
  saveQqConfig: (payload: { app_id: string; app_secret?: string; persona_id: string }) => request<BotQqConfig>('/bot/qq/config', { method: 'PUT', body: JSON.stringify(payload) }),
  getQqStatus: () => request<BotQqStatus>('/bot/qq/status'),
  connectQq: () => request<BotQqStatus>('/bot/qq/connect', { method: 'POST' }),
  disconnectQq: () => request<BotQqStatus>('/bot/qq/disconnect', { method: 'POST' }),
}

interface BotSseEvent {
  type: 'content' | 'done' | 'error'
  content?: string
  detail?: string
  message?: BotMessage
}

export async function streamBotChat(
  conversationId: string,
  content: string,
  options: { onContent: (text: string) => void; signal?: AbortSignal },
): Promise<BotMessage> {
  await ensureApiToken()
  let response: Response
  try {
    response = await fetch(`${getBaseUrl()}/bot/chat`, {
      method: 'POST',
      headers: withApiToken({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ conversation_id: conversationId, content }),
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
