/**
 * streamBotChat 连接停滞看门狗（src/api/bot.ts）回归 —— 黑洞连接不许把陪聊永久挂在「生成中」。
 *
 * 背景：后端 /bot/chat 现每 15s 推一行 ': heartbeat'（与 /agent/run 同构），读取循环
 * 只认 'data: ' 前缀行，注释行被静默忽略；若无「最后活动时间」判定，休眠唤醒 /
 * NAT 静默丢流这类无 RST 黑洞连接会让 read() 永不返回，UI 永久卡「生成中」。
 *
 * 修复口径：任何字节到达（含心跳注释行）刷新 lastActivityAt；90s 无活动 →
 * reader.cancel() 把挂起 read 以 done 解开 → 抛「连接停滞」，交还用户重试。
 *
 * 断言必须能失败：删掉看门狗、心跳行不算活动、或把停滞误走「回复中断」分支，都要变红。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./base', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./base')>()),
  ensureApiToken: vi.fn().mockResolvedValue(undefined),
  getBaseUrl: () => 'http://127.0.0.1:7777/api',
  withApiToken: (headers: Record<string, string>) => headers,
}))

vi.mock('./connect-timeout', () => ({
  createConnectTimeout: () => ({
    controller: new AbortController(),
    clear: () => {},
  }),
}))

import { streamBotChat } from './bot'

interface ReadResult { done: boolean; value?: Uint8Array }
interface Chunk { afterMs: number; text: string }

function scheduledReader(chunks: Chunk[], endOnDrain = false) {
  const encoder = new TextEncoder()
  let i = 0
  const pending: Array<(r: ReadResult) => void> = []
  return {
    read: vi.fn(() => {
      if (i < chunks.length) {
        const chunk = chunks[i++]!
        return new Promise<ReadResult>(resolve =>
          setTimeout(() => resolve({ done: false, value: encoder.encode(chunk.text) }), chunk.afterMs))
      }
      if (endOnDrain) return Promise.resolve({ done: true })
      return new Promise<ReadResult>(resolve => { pending.push(resolve) })
    }),
    cancel: vi.fn(async () => {
      for (const resolve of pending.splice(0)) resolve({ done: true })
    }),
    releaseLock: () => {},
  }
}

function runStream(reader: ReturnType<typeof scheduledReader>) {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, body: { getReader: () => reader } })))
  return streamBotChat('c1', 'hi', { onContent: () => {} })
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('streamBotChat 停滞看门狗', () => {
  it('90s 无任何字节 → cancel 并报「连接停滞」', async () => {
    const reader = scheduledReader([])
    const promise = runStream(reader)
    const assertion = expect(promise).rejects.toThrow('连接停滞')
    await vi.advanceTimersByTimeAsync(95_000)
    await assertion
    expect(reader.cancel).toHaveBeenCalled()
  })

  it('每 15s 一次心跳（仅注释行）能维持连接，不被误判停滞', async () => {
    const done = 'data: {"type":"done","message":{"id":"m1","conversation_id":"c1","role":"assistant","content":"在","created_at":""}}\n\n'
    const chunks: Chunk[] = [
      { afterMs: 0, text: ': heartbeat\n\n' },
      ...Array.from({ length: 7 }, () => ({ afterMs: 15_000, text: ': heartbeat\n\n' })),
      { afterMs: 15_000, text: done },
    ]
    const reader = scheduledReader(chunks, true)
    let settled: { ok: boolean; value?: unknown } | null = null
    runStream(reader).then(
      value => { settled = { ok: true, value } },
      error => { settled = { ok: false, value: error } },
    )
    // 先让 ensureApiToken → fetch → 首次 read 的微任务链落定
    await vi.advanceTimersByTimeAsync(1)
    // 7 次心跳横跨 105s（> 90s 阈值），每次都刷新活动的 lastActivityAt，看门狗不得出手
    await vi.advanceTimersByTimeAsync(8 * 15_000 + 1)
    // 尾部 buffer 解析与 return 在微任务里，推进后再 flush 几轮微任务
    await Promise.resolve()
    await Promise.resolve()
    expect(reader.read.mock.calls.length, `read 调用次数`).toBeGreaterThanOrEqual(8)
    expect(settled, `流应完成，实际=${JSON.stringify(settled)}`).toMatchObject({ ok: true })
    expect(reader.cancel).not.toHaveBeenCalled()
  })
})
