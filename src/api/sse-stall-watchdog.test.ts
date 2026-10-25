/**
 * streamAgentRun 连接停滞看门狗（src/api/sse.ts）回归 —— 黑洞连接不许把 run 永久挂在 running。
 *
 * 背景：读取循环只认 'data: ' 前缀行，后端每 15s 一发的 ': heartbeat' 注释行
 * （backend/routes/agent.py HEARTBEAT_INTERVAL=15）被静默忽略，且循环内没有
 * 「最后活动时间」判定 —— 休眠唤醒 / NAT 静默丢流这类无 RST 的黑洞连接会让
 * read() 永不返回：run 永远停在 running、审批卡挂着、计时无限涨。
 *
 * 修复口径：任何字节到达（含心跳注释行）刷新 lastActivityAt；超过 6 个心跳周期
 * （90s）无活动 → reader.cancel() + onError('连接停滞，请检查网络后重试')。
 *
 * 断言必须能失败：删掉看门狗、把心跳行不算活动、或把停滞误走「响应中断」/ onDone
 * 分支，本文件都要变红。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { streamAgentRun } from './sse'

interface ReadResult { done: boolean; value?: Uint8Array }

interface Chunk { /** 距上一个 chunk 的间隔 ms（首个为距首次 read 的间隔） */ afterMs: number; text: string }

/**
 * 按时刻表投喂 chunk 的假流：喂完即挂起（黑洞连接），cancel 把挂起的 read 以 done
 * 解开 —— 与 WHATWG 流语义一致（pending read 随 cancel resolve，而非 reject）。
 */
function scheduledReader(chunks: Chunk[]) {
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
      // 模拟黑洞：read 永不返回，直到看门狗 cancel 解锁
      return new Promise<ReadResult>(resolve => { pending.push(resolve) })
    }),
    cancel: vi.fn(async () => {
      for (const resolve of pending.splice(0)) resolve({ done: true })
    }),
    releaseLock: () => {},
  }
}

/** 假流的最小结构面（scheduledReader / completingReader 共用） */
interface FakeReader {
  read: () => Promise<ReadResult>
  cancel: () => Promise<void>
  releaseLock: () => void
}

function runStream(
  reader: FakeReader,
  hooks: { onEvent?: (e: unknown) => void; onError?: (e: string) => void; onDone?: () => void },
) {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, body: { getReader: () => reader } })))
  streamAgentRun({
    task: 't',
    conversationId: 'c1',
    modelId: 'm1',
    onEvent: hooks.onEvent ?? (() => {}),
    onError: hooks.onError ?? (() => {}),
    onDone: hooks.onDone ?? (() => {}),
  })
  return reader
}

beforeEach(() => {
  // Date 必须一起假掉：lastActivityAt 与看门狗都读 Date.now()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

/** 按时刻表投喂 chunk 后正常收尾（read → done）的假流：leftover 路径专用 */
function completingReader(chunks: Chunk[]): FakeReader {
  const encoder = new TextEncoder()
  let i = 0
  return {
    read: vi.fn(async () => {
      if (i < chunks.length) {
        const chunk = chunks[i++]!
        return { done: false, value: encoder.encode(chunk.text) }
      }
      return { done: true }
    }),
    cancel: vi.fn(async () => {}),
    releaseLock: () => {},
  }
}

/**
 * Q-F1：流末尾残留 buffer 不再整体丢弃。
 *
 * 读取循环 split('\n') 后 pop 出的最后一段（末行没有换行收尾）此前在 done 退出时
 * 被直接丢掉：后端把 `data: [DONE]` 作为最后一个不带换行的 chunk 发来、或末尾事件
 * 与流关闭挤在同一个读块里时，正常收尾被误报成「响应中断」。
 */
describe('流末尾 leftover buffer', () => {
  it('末尾 data: [DONE]（无换行收尾）：照常 onDone，不误报「响应中断」', async () => {
    const reader = completingReader([
      { afterMs: 0, text: 'data: {"type":"state","state":"thinking"}\n\n' },
      // 最后一行没有 \n：旧实现把它留在 buffer 里丢弃并报「响应中断」
      { afterMs: 0, text: 'data: [DONE]' },
    ])
    const events: Array<{ type: string }> = []
    const errors: string[] = []
    let doneCount = 0
    runStream(reader, {
      onEvent: e => events.push(e as { type: string }),
      onError: e => errors.push(e),
      onDone: () => { doneCount++ },
    })

    await vi.advanceTimersByTimeAsync(1)

    expect(doneCount).toBe(1)
    expect(errors).toEqual([])
    expect(events.map(e => e.type)).toEqual(['state'])
  })

  it('末尾完整事件（无 [DONE]）先派发，再按 B22 报响应中断 —— 真中断不被吞成收尾', async () => {
    // 第二行 JSON 完整但没有换行收尾：必须照常派发；没有 [DONE] 就不是正常结束
    const reader = completingReader([
      { afterMs: 0, text: 'data: {"type":"content","content":"partial"}\ndata: {"type":"state","state":"observing"}' },
    ])
    const events: Array<{ type: string }> = []
    const errors: string[] = []
    let doneCount = 0
    runStream(reader, {
      onEvent: e => events.push(e as { type: string }),
      onError: e => errors.push(e),
      onDone: () => { doneCount++ },
    })

    await vi.advanceTimersByTimeAsync(1)

    expect(events.map(e => e.type)).toEqual(['content', 'state'])
    expect(errors).toEqual(['响应中断'])
    expect(doneCount).toBe(0)
  })

  it('末尾半行（截断 JSON）不派发、报「响应中断」', async () => {
    const reader = completingReader([
      { afterMs: 0, text: 'data: {"type":"content","conte' },
    ])
    const events: Array<{ type: string }> = []
    const errors: string[] = []
    runStream(reader, {
      onEvent: e => events.push(e as { type: string }),
      onError: e => errors.push(e),
      onDone: () => {},
    })

    await vi.advanceTimersByTimeAsync(1)

    expect(events).toEqual([])
    expect(errors).toEqual(['响应中断'])
  })
})

describe('streamAgentRun 连接停滞看门狗', () => {
  it('只有心跳注释行且流永不结束：超时后 cancel + onError，不走 onDone', async () => {
    // 首个心跳送到后流静默（黑洞）：read 挂起永不返回
    const reader = scheduledReader([{ afterMs: 0, text: ': heartbeat\n\n' }])
    const errors: string[] = []
    let doneCount = 0
    runStream(reader, { onError: e => errors.push(e), onDone: () => { doneCount++ } })

    // 让 ensureApiToken → fetch → 首次 read 全部落定（微任务 + 0ms 定时器）
    await vi.advanceTimersByTimeAsync(1)
    expect(reader.read).toHaveBeenCalled()

    // 心跳每 15s 一发、90s 内 6 个周期全部静默丢失：看门狗必须在 90s + 一个巡检周期内出手
    await vi.advanceTimersByTimeAsync(95_000)

    expect(reader.cancel).toHaveBeenCalled()
    expect(errors).toEqual(['连接停滞，请检查网络后重试'])
    expect(doneCount).toBe(0)
  })

  it('心跳持续到达的活跃流不误伤：正常事件与 [DONE] 照常派发', async () => {
    // 7 个心跳横跨 105s（> 90s 看门狗阈值）—— 每个都刷新 lastActivityAt，看门狗不得出手
    const chunks: Chunk[] = [
      { afterMs: 0, text: 'data: {"type":"state","state":"thinking"}\n\n' },
      ...Array.from({ length: 7 }, () => ({ afterMs: 15_000, text: ': heartbeat\n\n' })),
      { afterMs: 15_000, text: 'data: [DONE]\n\n' },
    ]
    const reader = scheduledReader(chunks)
    const events: Array<{ type: string }> = []
    const errors: string[] = []
    let doneCount = 0
    runStream(reader, {
      onEvent: e => events.push(e as { type: string }),
      onError: e => errors.push(e),
      onDone: () => { doneCount++ },
    })

    await vi.advanceTimersByTimeAsync(121_000)

    expect(errors).toEqual([])
    expect(doneCount).toBe(1)
    expect(events.map(e => e.type)).toEqual(['state'])
  })
})
