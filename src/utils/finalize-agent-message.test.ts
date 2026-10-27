/**
 * finalizeAgentMessage 单测 — Agent 运行结果固化到占位消息（round 12）
 *
 * 覆盖：未回填的工具调用在固化时收敛成明确读数（而 result='' 的合法空结果不受影响）、
 * 中止/无输出时正文的回退文案，以及 runId/状态的落盘。
 */
import { describe, it, expect } from 'vitest'
import type { useAgentStore } from '@/stores/agent'
import type { AgentTraceSnapshot } from '@/api/client'
import { finalizeAgentMessage, type AgentMessage } from './finalize-agent-message'

type Store = ReturnType<typeof useAgentStore>

function makeStore(over: Record<string, unknown> = {}): Store {
  const base = {
    content: '答案',
    reasoning: '',
    lastRunId: 'run-1',
    costInfo: null,
    warnings: [] as string[],
    errorMessage: '',
    aborted: false,
    snapshot: (): AgentTraceSnapshot => ({
      status: 'completed',
      currentState: 'completed',
      iterations: 1,
      duration: 2,
      toolCalls: [
        // 从未回填：result/success 双 undefined（内联轨迹的「进行中」判据）
        { id: 'a', tool: 'shell_exec', args: {}, timestamp: 1 },
        // 回填过但结果为空串：空串是合法结果，不能被当成「没跑完」
        { id: 'b', tool: 'file_read', args: {}, timestamp: 2, result: '', success: true },
      ],
      reasoning: '',
      lastRunId: 'run-1',
    }),
  }
  return { ...base, ...over } as unknown as Store
}

function makeConv(): { messages: AgentMessage[] } {
  return {
    messages: [
      { role: 'user', id: 'u1', content: '问题', created_at: '' },
      { role: 'assistant', id: 'temp-agent-1', content: '', created_at: '' },
    ],
  }
}

function traceOf(conv: { messages: AgentMessage[] }): AgentTraceSnapshot {
  return conv.messages[1].agentTrace as AgentTraceSnapshot
}

describe('finalizeAgentMessage', () => {
  it('把从未回填的工具调用落成明确的未回写状态，空串结果原样保留', () => {
    const conv = makeConv()
    finalizeAgentMessage(conv, makeStore())
    const [pending, answered] = traceOf(conv).toolCalls
    expect(pending.success).toBe(false)
    expect(pending.result).toContain('未回写')
    expect(answered.result).toBe('')
    expect(answered.success).toBe(true)
  })

  it('中止且无输出：正文回退写「已中止」', () => {
    const conv = makeConv()
    finalizeAgentMessage(conv, makeStore({ content: '', aborted: true }))
    // status 由 store.snapshot() 自己决定（中止→failed），那条判据在 agent.test.ts 里锁
    expect(conv.messages[1].content).toBe('(已中止)')
  })

  it('正常完成：正文取 content，轨迹状态 completed，runId 回写', () => {
    const conv = makeConv()
    finalizeAgentMessage(conv, makeStore())
    expect(conv.messages[1].content).toBe('答案')
    expect(traceOf(conv).status).toBe('completed')
    expect(conv.messages[1].runId).toBe('run-1')
  })

  it('固化后换掉 temp 前缀的 id：占位语义只在跑的过程中成立', () => {
    const conv = makeConv()
    finalizeAgentMessage(conv, makeStore())
    // `useChatPersistence.loadConversations` 把「assistant + temp-agent- 前缀」判成
    // 永远等不到回填的占位并在启动时丢弃。固化后的真回复若继续带着这个前缀，
    // 下一次启动就会被当成占位吞掉 —— 任务台上只剩用户气泡。
    expect(String(conv.messages[1].id)).not.toMatch(/^temp-agent-/)
    expect(conv.messages[1].id).not.toBe(conv.messages[0].id)
  })
})
