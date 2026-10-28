import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./base', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./base')>()),
  request: vi.fn(),
  ensureApiToken: vi.fn().mockResolvedValue(undefined),
  getBaseUrl: () => 'http://127.0.0.1:7777/api',
  withApiToken: (headers: Record<string, string>) => headers,
}))

import { request } from './base'
import { botApi, streamBotChat } from './bot'

describe('cKunBot API', () => {
  beforeEach(() => vi.mocked(request).mockReset())

  it('人格更新允许清空绑定的模型', async () => {
    vi.mocked(request).mockResolvedValue({ id: 'p1' })
    await botApi.updatePersona('p1', { model_id: null })
    expect(request).toHaveBeenCalledWith('/bot/personas/p1', {
      method: 'PATCH',
      body: JSON.stringify({ model_id: null }),
    })
  })

  it('QQ 配置读取不声称返回 AppSecret', async () => {
    vi.mocked(request).mockResolvedValue({ app_id: '1', has_secret: true, persona_id: 'p1', context_id: 'c1' })
    const config = await botApi.getQqConfig()
    expect(config.has_secret).toBe(true)
    expect('app_secret' in config).toBe(false)
  })

  it('onReasoning 收到 reasoning 片段，onContent 收到正文', async () => {
    const encoder = new TextEncoder()
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('data: {"type":"reasoning","content":"想"}\n\ndata: {"type":"content","content":"好"}\n\n'))
        controller.enqueue(encoder.encode('data: {"type":"done","message":{"id":"m1","role":"assistant","content":"好"}}\n\n'))
        controller.close()
      },
    })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(stream, { status: 200 })))
    const reasoning: string[] = []
    const content: string[] = []
    await streamBotChat('c1', 'hi', { onContent: t => content.push(t), onReasoning: t => reasoning.push(t) })
    expect(reasoning).toEqual(['想'])
    expect(content).toEqual(['好'])
    vi.unstubAllGlobals()
  })

  it('本地聊天按 SSE 片段回调，完成后返回持久化消息', async () => {
    const encoder = new TextEncoder()
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('data: {"type":"content","content":"你"}\n\ndata: {"type":"content","content":"好"}\n\n'))
        controller.enqueue(encoder.encode('data: {"type":"done","message":{"id":"m1","role":"assistant","content":"你好"}}\n\n'))
        controller.close()
      },
    })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(stream, { status: 200 })))
    const chunks: string[] = []
    const result = await streamBotChat('c1', '在吗', { onContent: content => chunks.push(content) })
    expect(chunks).toEqual(['你', '好'])
    expect(result.content).toBe('你好')
    expect(fetch).toHaveBeenCalledWith('http://127.0.0.1:7777/api/bot/chat', expect.objectContaining({ method: 'POST' }))
    vi.unstubAllGlobals()
  })

  it('服务端 SSE 错误转成异常供界面重试', async () => {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('data: {"type":"error","detail":"模型暂时不可用"}\n\n'))
        controller.close()
      },
    })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(stream, { status: 200 })))
    await expect(streamBotChat('c1', '在吗', { onContent: () => {} })).rejects.toThrow('模型暂时不可用')
    vi.unstubAllGlobals()
  })

  it('请求体带上 images（无图时为空数组）', async () => {
    const makeResponse = () =>
      new Response(
        new ReadableStream<Uint8Array>({
          start(c) {
            c.enqueue(new TextEncoder().encode('data: {"type":"done","message":{"id":"m1","role":"assistant","content":"好"}}\n\n'))
            c.close()
          },
        }),
        { status: 200 },
      )
    const fetchMock = vi.fn().mockImplementation(makeResponse)
    vi.stubGlobal('fetch', fetchMock)
    await streamBotChat('c1', '看这张', { onContent: () => {}, images: ['data:image/webp;base64,AAAA'] })
    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)
    expect(body.images).toEqual(['data:image/webp;base64,AAAA'])
    await streamBotChat('c1', '你好', { onContent: () => {} })
    const bare = JSON.parse((fetchMock.mock.calls[1][1] as RequestInit).body as string)
    expect(bare.images).toEqual([])
    vi.unstubAllGlobals()
  })
})
