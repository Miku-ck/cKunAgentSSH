// useCommandDelivery.ts — 终端聚焦 CustomEvent 注册/清理
// （原「命令投递」deliverCommandToTerminal 已删除：全仓无任何代码派发
//  'ssh:deliver-command' 事件，AI 侧命令交付走 useAgentDeliver 的 tool-deliver 通道）
import { onMounted, onUnmounted } from 'vue'
import { useSSHStore } from '@/stores/ssh'

export function useCommandDelivery(terminalRefs: Record<string, { focus?: () => void }>) {
  const store = useSSHStore()

  // 聚焦活动终端（供 QuickCommandBar 发送命令后调用，让用户能按 Enter 执行）
  function focusActiveTerminal() {
    const activeId = store.activeSessionId
    if (!activeId) return
    terminalRefs[activeId]?.focus?.()
  }

  onMounted(() => {
    window.addEventListener('ssh:focus-terminal', focusActiveTerminal)
  })

  onUnmounted(() => {
    window.removeEventListener('ssh:focus-terminal', focusActiveTerminal)
  })

  return { focusActiveTerminal }
}
