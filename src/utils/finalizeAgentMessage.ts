/**
 * 固化 Agent 运行结果到会话的最后一条占位消息（id 以 'temp-agent-' 开头）。
 * 设计为在 store 层 watch 中调用，不依赖组件生命周期。
 */
import type { useAgentStore } from '@/stores/agent'

/** Agent 消息的最小接口定义（导出供 store 层泛型边界断言使用，审查 M-19） */
export interface AgentMessage {
  role: string
  id: string
  content?: string
  reasoning?: string
  runId?: string
  fileChanges?: unknown
  tokens_used?: number
  duration?: number
  [key: string]: unknown
}

export function finalizeAgentMessage(
  conv: { messages: AgentMessage[] } | null | undefined,
  agentStore: ReturnType<typeof useAgentStore>,
) {
  if (!conv) return
  const msgs = conv.messages
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === 'assistant' && msgs[i].id.startsWith('temp-agent-')) {
      const trace = agentStore.snapshot()
      // 失败/中止时 content 为空则回写 errorMessage/warnings，确保用户看到原因
      let finalContent = agentStore.content || ''
      if (!finalContent.trim()) {
        if (agentStore.errorMessage) {
          finalContent = `错误: ${agentStore.errorMessage}`
        } else if (agentStore.warnings.length > 0) {
          finalContent = agentStore.warnings.join('\n')
        } else if (agentStore.aborted) {
          // M17：用户主动中止且无部分输出 — 标注"(已中止)"而非误报失败
          finalContent = '(已中止)'
        } else {
          finalContent = '(AI 运行失败，未产生输出)'
        }
      }
      msgs[i].content = finalContent
      msgs[i].reasoning = agentStore.reasoning || undefined
      msgs[i].runId = agentStore.lastRunId || undefined
      msgs[i].fileChanges = agentStore.fileChanges || undefined
      msgs[i].tokens_used = agentStore.costInfo?.total_tokens || 0
      if (trace.duration && trace.duration > 0) msgs[i].duration = trace.duration
      msgs[i].agentTrace = trace
      msgs[i].streamingContent = undefined
      msgs[i].streamingReasoning = undefined
      break
    }
  }
}
