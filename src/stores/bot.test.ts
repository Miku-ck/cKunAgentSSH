import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { BotConversation, BotMessage, BotPersona } from '@/api/bot'

vi.mock('@/api/bot', () => ({
  botApi: {
    listPersonas: vi.fn(),
    listConversations: vi.fn(),
    createConversation: vi.fn(),
    getMessages: vi.fn(),
    deleteConversation: vi.fn(),
    getRelations: vi.fn(),
  },
  streamBotChat: vi.fn(),
}))

import { botApi, streamBotChat } from '@/api/bot'
import { useBotStore } from './bot'

const persona: BotPersona = {
  id: 'p1', name: '小刻', avatar: 'data:image/webp;base64,AAAA', personality: '温和', background: '', greeting: '你好',
  model_id: null, reply_style: 'moderate', enable_thinking: 0, is_builtin: 1, created_at: '', updated_at: '',
  expression_dna: {
    sentence: 'mixed', particles: 'soft', punctuation: 'plain', emoji: 'low', catchphrase: '', taboo: [],
  },
  reaction_style: 'warm', refusal_style: 'soft', address_user: '', relationship_speed: 'normal',
}
const conversation: BotConversation = {
  id: 'c1', title: '小刻', persona_id: 'p1',
  persona: { id: 'p1', name: '小刻', avatar: '' }, preview: '你好',
  channel: 'local', external_id: null, created_at: '', updated_at: '',
}

describe('cKunBot store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    vi.mocked(botApi.listPersonas).mockResolvedValue([persona])
    vi.mocked(botApi.listConversations).mockResolvedValue([conversation])
    vi.mocked(botApi.getMessages).mockResolvedValue([])
  })

  it('初始化后选中当前人格会话并读取历史', async () => {
    const store = useBotStore()
    await store.init()
    expect(store.activeConversationId).toBe('c1')
    expect(botApi.getMessages).toHaveBeenCalledWith('c1')
  })

  it('流式回复期间只更新临时气泡，完成后替换为服务端消息', async () => {
    const saved: BotMessage = { id: 'm2', conversation_id: 'c1', role: 'assistant', content: '我很好', created_at: '' }
    vi.mocked(streamBotChat).mockImplementation(async (_id, _text, options) => {
      options.onContent('我很')
      options.onContent('好')
      return saved
    })
    const store = useBotStore()
    store.personas = [persona]
    store.conversations = [conversation]
    store.activeConversationId = 'c1'
    await store.sendMessage('你好吗')
    expect(store.messages.map(m => m.content)).toEqual(['你好吗', '我很好'])
    expect(store.streaming).toBe(false)
    expect(store.streamText).toBe('')
  })

  it('reasoning 流累积到 streamReasoning，完成后落到消息 reasoning', async () => {
    const saved: BotMessage = { id: 'm2', conversation_id: 'c1', role: 'assistant', content: '好', reasoning: '想', created_at: '' }
    vi.mocked(streamBotChat).mockImplementation(async (_id, _text, options) => {
      options.onReasoning?.('想')
      options.onContent('好')
      return saved
    })
    const store = useBotStore()
    store.personas = [persona]
    store.conversations = [conversation]
    store.activeConversationId = 'c1'
    await store.sendMessage('在吗')
    expect(store.messages.at(-1)?.reasoning).toBe('想')
    expect(store.streamReasoning).toBe('')
  })

  it('失败后保留输入可重试，不伪造 assistant 消息', async () => {
    vi.mocked(streamBotChat).mockRejectedValue(new Error('模型暂时不可用'))
    const store = useBotStore()
    store.personas = [persona]
    store.conversations = [conversation]
    store.activeConversationId = 'c1'
    await store.sendMessage('在吗')
    expect(store.messages.map(m => m.content)).toEqual(['在吗'])
    expect(store.lastFailedText).toBe('在吗')
    expect(store.error).toContain('模型暂时不可用')
    expect(store.streaming).toBe(false)
  })

  it('sendMessage 透传 images 并乐观带图追加用户消息', async () => {
    const imgs = ['data:image/webp;base64,AAAA']
    vi.mocked(streamBotChat).mockImplementation(async (_id, _text, options) => {
      expect(options.images).toEqual(imgs)
      return { id: 'm2', conversation_id: 'c1', role: 'assistant', content: '看到了', created_at: '' }
    })
    const store = useBotStore()
    store.personas = [persona]
    store.conversations = [conversation]
    store.activeConversationId = 'c1'
    await store.sendMessage('这是什么', imgs)
    expect(store.messages[0].role).toBe('user')
    expect(store.messages[0].images).toEqual(imgs)
  })
})
