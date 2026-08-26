/**
 * Agent 集成 Composable
 * 从 WorkView / AiChatPanel / SshAiPanel 中提取的通用 Agent 逻辑：
 * - agentMode 状态（由外部 store 提供，实现默认配置 + 持久化记忆）
 * - handleAbort（agent/chat 双分支中止）
 *
 * 注意：Agent 运行完成后的消息固化逻辑已移入 store 层（createModeChatStore /
 * chat.ts 的 watch），通过 finalizeAgentMessage 实现，不再依赖组件生命周期，
 * 避免跨模式切换时面板卸载导致 Agent 结果丢失（P3-4）。
 */
import { type Ref } from 'vue'
import { useAgentStore } from '@/stores/agent'
import type { ChatMode } from '@/types/chat'

/**
 * 固化 Agent 运行结果到会话的最后一条占位消息（id 以 'temp-agent-' 开头）。
 * 设计为在 store 层 watch 中调用，不依赖组件生命周期。
 */
/** Agent 消息的最小接口定义 */
interface AgentMessage {
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
      // 失败时 content 为空则回写 errorMessage/warnings，确保用户看到失败原因
      let finalContent = agentStore.content || ''
      if (!finalContent.trim()) {
        if (agentStore.errorMessage) {
          finalContent = `错误: ${agentStore.errorMessage}`
        } else if (agentStore.warnings.length > 0) {
          finalContent = agentStore.warnings.join('\n')
        } else {
          finalContent = '(Agent 运行失败，未产生输出)'
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

export interface UseAgentIntegrationOptions {
  /** 当前模式标识，用于过滤 agentStore 的跨模式 watch */
  mode: ChatMode
  /** 外部传入的 agentMode ref。持久化记忆由 createModeChatStore 统一实现。 */
  agentModeRef: Ref<boolean>
}

export function useAgentIntegration(options: UseAgentIntegrationOptions) {
  const agentStore = useAgentStore()
  const agentMode = options.agentModeRef

  /** 统一中止：Agent 运行中则中止 Agent，否则中止普通流式 */
  function handleAbort(chatAbort: () => void) {
    if (agentStore.isRunning) {
      agentStore.abort()
    } else {
      chatAbort()
    }
  }

  return { agentStore, agentMode, handleAbort }
}
