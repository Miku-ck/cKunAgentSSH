import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { BotConversation, BotMessage, BotPersona } from '@/api/bot'

vi.mock('@/api/bot', () => ({
  botApi: {
    listPersonas: vi.fn(),
    listConversations: vi.fn(),
    listChannelConversations: vi.fn(),
    createConversation: vi.fn(),
    getMessages: vi.fn(),
    deleteConversation: vi.fn(),
    getRelations: vi.fn(),
    updateConversationDisplay: vi.fn(),
    getUserAvatar: vi.fn(),
    setUserAvatar: vi.fn(),
  },
  streamBotChat: vi.fn(),
}))

import { botApi, streamBotChat } from '@/api/bot'
import { useBotStore } from './bot'

const persona: BotPersona = {
  id: 'p1', name: '小刻', avatar: 'data:image/webp;base64,AAAA', personality: '温和', background: '', greeting: '你好',
  model_id: null, reply_style: 'moderate', enable_thinking: 0, proactive_enabled: 0, is_builtin: 1, created_at: '', updated_at: '',
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
    vi.mocked(botApi.getUserAvatar).mockResolvedValue({ avatar: '' })
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

  it('updateQqDisplay 走 PATCH 并原位替换 QQ 会话', async () => {
    const qq = { ...conversation, id: 'q1', channel: 'qq' as const }
    vi.mocked(botApi.updateConversationDisplay).mockResolvedValue({ ...qq, display_name: '夜宵群' })
    const store = useBotStore()
    store.remoteConversations = [qq]
    store.remoteActiveId = 'q1'
    await store.updateQqDisplay('q1', { display_name: '夜宵群' })
    expect(botApi.updateConversationDisplay).toHaveBeenCalledWith('q1', { display_name: '夜宵群' })
    expect(store.remoteConversations[0].display_name).toBe('夜宵群')
  })

  it('远程只读列表按当前渠道拉取：微信 tab 读微信会话', async () => {
    const wechat = {
      ...conversation, id: 'w1', channel: 'wechat' as const, external_id: 'wx:openid-1',
    }
    vi.mocked(botApi.listChannelConversations).mockResolvedValue([wechat])
    vi.mocked(botApi.getMessages).mockResolvedValue([])
    const store = useBotStore()
    store.selectChannel('wechat')
    await store.refreshRemote()
    expect(botApi.listChannelConversations).toHaveBeenCalledWith('wechat')
    expect(store.remoteConversations.map(c => c.id)).toEqual(['w1'])
    expect(store.remoteActiveId).toBe('w1')
    expect(store.remoteChannelLabel).toBe('微信')
  })

  it('删除微信会话后从列表移除，并自动选中下一条', async () => {
    const a = { ...conversation, id: 'w1', channel: 'wechat' as const, external_id: 'wx:o1' }
    const b = { ...conversation, id: 'w2', channel: 'wechat' as const, external_id: 'wx:o2' }
    vi.mocked(botApi.deleteConversation).mockResolvedValue({ deleted: true })
    vi.mocked(botApi.getMessages).mockResolvedValue([])
    const store = useBotStore()
    store.selectChannel('wechat')
    store.remoteConversations = [a, b]
    store.remoteActiveId = 'w1'
    await store.deleteRemoteConversation('w1')
    expect(botApi.deleteConversation).toHaveBeenCalledWith('w1')
    expect(store.remoteConversations.map(c => c.id)).toEqual(['w2'])
    expect(store.remoteActiveId).toBe('w2')
  })

  it('删掉最后一条远程会话后回到空态，不留幽灵消息', async () => {
    const only = { ...conversation, id: 'w1', channel: 'wechat' as const, external_id: 'wx:o1' }
    vi.mocked(botApi.deleteConversation).mockResolvedValue({ deleted: true })
    const store = useBotStore()
    store.selectChannel('wechat')
    store.remoteConversations = [only]
    store.remoteActiveId = 'w1'
    store.remoteMessages = [{ id: 'm1', conversation_id: 'w1', role: 'user', content: 'hi', created_at: '' }]
    await store.deleteRemoteConversation('w1')
    expect(store.remoteConversations).toEqual([])
    expect(store.remoteActiveId).toBe('')
    expect(store.remoteMessages).toEqual([])
  })

  it('切走后返回的旧渠道响应不落进新 tab', async () => {
    const qq = { ...conversation, id: 'q1', channel: 'qq' as const }
    let resolveList: (rows: BotConversation[]) => void = () => {}
    vi.mocked(botApi.listChannelConversations).mockReturnValue(
      new Promise((resolve) => { resolveList = resolve }),
    )
    const store = useBotStore()
    store.selectChannel('qq')
    const pending = store.refreshRemote()
    store.selectChannel('wechat')
    resolveList([qq])
    await pending
    expect(store.remoteConversations).toEqual([])
  })

  it('删除时在途的刷新结果不能把刚删的行写回列表', async () => {
    const a = { ...conversation, id: 'w1', channel: 'wechat' as const, external_id: 'wx:o1' }
    const b = { ...conversation, id: 'w2', channel: 'wechat' as const, external_id: 'wx:o2' }
    let resolveList: (rows: BotConversation[]) => void = () => {}
    vi.mocked(botApi.listChannelConversations).mockReturnValue(
      new Promise((resolve) => { resolveList = resolve }),
    )
    vi.mocked(botApi.deleteConversation).mockResolvedValue({ deleted: true })
    vi.mocked(botApi.getMessages).mockResolvedValue([])
    const store = useBotStore()
    store.selectChannel('wechat')
    store.remoteConversations = [a, b]
    store.remoteActiveId = 'w1'
    const pending = store.refreshRemote()
    await store.deleteRemoteConversation('w1')
    resolveList([a, b])
    await pending
    expect(store.remoteConversations.map(c => c.id)).toEqual(['w2'])
    expect(store.remoteActiveId).toBe('w2')
  })

  it('init 读取「你的头像」；读取失败回落空占位不阻塞初始化', async () => {
    vi.mocked(botApi.getUserAvatar).mockRejectedValueOnce(new Error('boom'))
    const store = useBotStore()
    await store.init()
    expect(store.userAvatar).toBe('')
    vi.mocked(botApi.getUserAvatar).mockResolvedValueOnce({ avatar: 'data:image/webp;base64,UHJ' })
    await store.loadUserAvatar()
    expect(store.userAvatar).toBe('data:image/webp;base64,UHJ')
  })
})
