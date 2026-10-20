import { describe, expect, it } from 'vitest'
import type { AgentTraceSnapshot } from '@/api/client'
import type { BaseMessage } from '@/types/chat'
import { buildDisplayMessages, isLiveAgentRun } from './display-messages'

function trace(overrides: Partial<AgentTraceSnapshot> = {}): AgentTraceSnapshot {
  return {
    status: 'completed',
    currentState: 'completed',
    iterations: 1,
    toolCalls: [],
    reasoning: 'snapshot-reasoning',
    lastRunId: 'run-final',
    ...overrides,
  }
}

const msg = (over: Partial<BaseMessage>): BaseMessage => ({
  id: 'm1',
  role: 'user',
  content: 'x',
  created_at: '2026-10-20T00:00:00Z',
  ...over,
})

const live = (over: Partial<Parameters<typeof buildDisplayMessages>[1]> = {}) => ({
  running: true,
  content: '',
  reasoning: '',
  trace: trace(),
  ...over,
})

describe('isLiveAgentRun', () => {
  const messages = [msg({}), msg({ id: 'temp-agent-1', role: 'assistant' })]

  it('只在运行中、且是最后一条 temp 助手消息时为真', () => {
    expect(isLiveAgentRun(messages, 1, true)).toBe(true)
    expect(isLiveAgentRun(messages, 1, false)).toBe(false)
    expect(isLiveAgentRun(messages, 0, true)).toBe(false)
  })

  it('普通助手消息不算 live', () => {
    expect(isLiveAgentRun([msg({ id: 'a1', role: 'assistant' })], 0, true)).toBe(false)
    expect(isLiveAgentRun([msg({ id: undefined as unknown as string, role: 'assistant' })], 0, true)).toBe(false)
  })
})

describe('buildDisplayMessages', () => {
  it('非运行中返回原数组原引用', () => {
    const messages = [msg({ id: 'temp-agent-1', role: 'assistant' })]
    const out = buildDisplayMessages(messages, live({ running: false }))
    expect(out).toBe(messages)
  })

  it('运行中但没有 temp 占位时也返回原引用', () => {
    const messages = [msg({ role: 'assistant', content: 'done' })]
    const out = buildDisplayMessages(messages, live())
    expect(out).toBe(messages)
  })

  it('只合并最后一条 temp 助手消息，其余消息引用不变', () => {
    const first = msg({ id: 'u1' })
    const second = msg({ id: 'a1', role: 'assistant', content: 'old' })
    const last = msg({ id: 'temp-agent-9', role: 'assistant', content: 'stale' })
    const messages = [first, second, last]

    const out = buildDisplayMessages(messages, live({ content: 'live-content', reasoning: 'live-reasoning' }))

    expect(out).not.toBe(messages)
    expect(out[0]).toBe(first)
    expect(out[1]).toBe(second)
    expect(out[2]).not.toBe(last)
    expect(out[2].content).toBe('live-content')
    expect(out[2].reasoning).toBe('live-reasoning')
  })

  it('content/reasoning 按 live → 已固化 → streaming 的顺序回落', () => {
    const messages = [msg({
      id: 'temp-agent-2',
      role: 'assistant',
      content: 'fixed',
      streamingContent: 'streaming',
      reasoning: 'fixed-r',
      streamingReasoning: 'streaming-r',
    })]

    const out = buildDisplayMessages(messages, live({ content: '', reasoning: '' }))
    expect(out[0].content).toBe('fixed')
    expect(out[0].reasoning).toBe('')

    const out2 = buildDisplayMessages(
      [msg({ id: 'temp-agent-3', role: 'assistant', content: '', streamingContent: 'only-stream' })],
      live(),
    )
    expect(out2[0].content).toBe('only-stream')
  })

  it('运行中的快照清空 lastRunId：轨迹回放按钮不能拉半截 run', () => {
    const messages = [msg({ id: 'temp-agent-4', role: 'assistant' })]
    const out = buildDisplayMessages(messages, live({ trace: trace({ lastRunId: 'run-live' }) }))
    expect(out[0].agentTrace?.lastRunId).toBe('')
  })

  it('清掉 streaming 字段：外层已用合并后的 content/reasoning 渲染', () => {
    const messages = [msg({
      id: 'temp-agent-5',
      role: 'assistant',
      streamingContent: 's',
      streamingReasoning: 'sr',
    })]
    const out = buildDisplayMessages(messages, live())
    expect(out[0].streamingContent).toBeUndefined()
    expect(out[0].streamingReasoning).toBeUndefined()
  })

  it('不改输入数组与占位对象', () => {
    const last = msg({ id: 'temp-agent-6', role: 'assistant', content: 'stale' })
    const messages = [last]
    buildDisplayMessages(messages, live({ content: 'new' }))
    expect(last.content).toBe('stale')
    expect(last.agentTrace).toBeUndefined()
  })
})
