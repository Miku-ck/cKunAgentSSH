/**
 * 「助手在场状态」的唯一判定：待审批 > 运行中 > 未读 > 空闲。
 *
 * 首页标签的状态点与顶栏 AI 按钮的徽章都读这一份结果。优先级写两遍就会漂移，
 * 而这两处在同一屏上，漂移一眼可见（一边警示黄、一边运行蓝，说的却是同一场 run）。
 * 纯函数单独导出，测试不需要挂 Pinia。
 */
import { computed } from 'vue'
import { useAgentStore } from '@/stores/agent'
import { useSshAiStore } from '@/stores/ssh-ai'

export type AgentPresence = 'pending' | 'running' | 'unread' | 'idle'

export function resolveAgentPresence(input: {
  pendingCount: number
  running: boolean
  unread: boolean
}): AgentPresence {
  if (input.pendingCount > 0) return 'pending'
  if (input.running) return 'running'
  if (input.unread) return 'unread'
  return 'idle'
}

export function useAgentPresence() {
  const agent = useAgentStore()
  const sshAi = useSshAiStore()

  const pendingCount = computed(() => agent.pendingPermissions.length)
  const presence = computed<AgentPresence>(() => resolveAgentPresence({
    pendingCount: pendingCount.value,
    running: agent.isRunning,
    unread: sshAi.unreadReply,
  }))

  return { presence, pendingCount }
}
