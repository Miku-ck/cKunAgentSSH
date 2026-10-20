/**
 * 消息展示层的唯一 live 合并口径（任务台消息流用）。
 *
 * 只有「正在跑的那一条 temp 占位」需要把 agent store 的流式 content/reasoning
 * 与实时快照叠上去；其余消息必须保持原引用 —— 否则每一帧流式输出都会让整个
 * 消息列表重建对象，Markdown 渲染器与轨迹组件跟着重挂。
 */
import type { AgentTraceSnapshot } from '@/api/client'
import type { BaseMessage } from '@/types/chat'

export function isLiveAgentRun(
  messages: readonly BaseMessage[],
  idx: number,
  running: boolean,
): boolean {
  const msg = messages[idx]
  return running &&
    idx === messages.length - 1 &&
    msg?.role === 'assistant' &&
    String(msg.id ?? '').startsWith('temp-agent-')
}

export interface LiveAgentView {
  running: boolean
  content: string
  reasoning: string
  trace: AgentTraceSnapshot
}

export function buildDisplayMessages(
  messages: readonly BaseMessage[],
  live: LiveAgentView,
): BaseMessage[] {
  const lastIdx = messages.length - 1
  if (!isLiveAgentRun(messages, lastIdx, live.running)) {
    return messages as BaseMessage[]
  }
  const last = messages[lastIdx]
  const merged: BaseMessage = {
    ...last,
    content: live.content || last.content || last.streamingContent || '',
    streamingContent: undefined,
    reasoning: live.reasoning || '',
    streamingReasoning: undefined,
    // lastRunId 必须清空：轨迹回放入口按它拉后端已固化的 run，运行中点开
    // 只会拿到半截结果并被组件缓存住。
    agentTrace: { ...live.trace, lastRunId: '' },
  }
  return [...messages.slice(0, lastIdx), merged]
}
