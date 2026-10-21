import type { TerminalSession } from '@/stores/ssh'

/**
 * 标签状态点的单一判据：标签栏、溢出菜单、MRU 切换器三处共用。
 * 优先级 gave-up > reconnecting > activity；活动点在活跃标签上不亮
 * （人正看着它），重连/断连不受此限（状态比位置重要）。
 */
export function tabStatusKind(
  s: Pick<TerminalSession, 'id' | 'hasActivity' | 'reconnect' | 'reconnectGaveUp'>,
  activeId: string,
): 'gave-up' | 'reconnecting' | 'activity' | null {
  if (s.reconnectGaveUp) return 'gave-up'
  if (s.reconnect) return 'reconnecting'
  if (s.hasActivity && s.id !== activeId) return 'activity'
  return null
}
