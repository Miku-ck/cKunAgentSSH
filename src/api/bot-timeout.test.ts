/**
 * streamBotChat 连接/首字节超时回归（src/api/bot.ts）。
 *
 * 与 bot.test.ts 分开：这里要用 fake timers 把 28s 连接超时推到点，而 bot.test.ts
 * 的流式用例跑在真实 timer / 原生 Response 流上，混在一起会互相干扰。
 *
 * 口径：只覆盖 fetch 返回 headers 之前，不限制长流时长。后端 /bot/chat 不发心跳，
 * 所以这里刻意没有「读取循环停滞看门狗」—— 模型思考期长时间无字节是正常态。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./base', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./base')>()),
  request: vi.fn(),
  ensureApiToken: vi.fn().mockResolvedValue(undefined),
  getBaseUrl: () => 'http://127.0.0.1:7777/api',
  withApiToken: (headers: Record<string, string>) => headers,
}))

import { streamBotChat } from './bot'

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

/** fetch 永远 pending，只在 signal 中止时 reject —— 与真实 fetch 的 abort 语义一致 */
function pendingFetch() {
  return vi.fn((_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
    const signal = init.signal as AbortSignal
    if (signal.aborted) reject(signal.reason)
    else signal.addEventListener('abort', () => reject(signal.reason), { once: true })
  }))
}

describe('streamBotChat 连接超时', () => {
  it('fetch 不返回：28s 到点报可操作的连接超时，不留定时器', async () => {
    vi.stubGlobal('fetch', pendingFetch())
    const promise = streamBotChat('c1', 'hi', { onContent: () => {} })
    const assertion = expect(promise).rejects.toThrow('连接超时，请检查网络后重试')

    await vi.advanceTimersByTimeAsync(0) // 放行 ensureApiToken → fetch
    await vi.advanceTimersByTimeAsync(28_000)

    await assertion
    expect(vi.getTimerCount()).toBe(0)
  })

  it('调用方 signal 中止：仍以 AbortError 透传，不被超时文案吞掉', async () => {
    vi.stubGlobal('fetch', pendingFetch())
    const controller = new AbortController()
    const promise = streamBotChat('c1', 'hi', { onContent: () => {}, signal: controller.signal })
    const captured = promise.catch(e => e)

    await vi.advanceTimersByTimeAsync(0)
    controller.abort()

    const err = await captured
    expect(err.name).toBe('AbortError')
    expect(vi.getTimerCount()).toBe(0)
  })

  it('fetch 及时返回：连接定时器被清除，正常消息照常收尾', async () => {
    const encoder = new TextEncoder()
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(encoder.encode('data: {"type":"done","message":{"id":"m1","role":"assistant","content":"好"}}\n\n'))
        c.close()
      },
    })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(stream, { status: 200 })))

    const promise = streamBotChat('c1', 'hi', { onContent: () => {} })
    await vi.advanceTimersByTimeAsync(0)
    // 定时器若没清掉，这里就会在 28s 处把 signal 打掉；清掉了才应安然无事
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(28_000)

    expect((await promise).content).toBe('好')
  })
})
