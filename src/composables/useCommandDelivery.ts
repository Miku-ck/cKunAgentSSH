// useCommandDelivery.ts — 终端聚焦 + 命令投递 + CustomEvent 注册/清理
import { onMounted, onUnmounted } from 'vue'
import { useSSHStore } from '@/stores/ssh'
import { useToastStore } from '@/stores/toast'
import { toFriendlyMessage } from '@/api/client'

export function useCommandDelivery(terminalRefs: Record<string, { focus?: () => void }>) {
  const store = useSSHStore()
  const toast = useToastStore()

  // 聚焦活动终端（供 QuickCommandBar 发送命令后调用，让用户能按 Enter 执行）
  function focusActiveTerminal() {
    const activeId = store.activeSessionId
    if (!activeId) return
    terminalRefs[activeId]?.focus?.()
  }

  // 将命令写入活动终端（由 AI 助手通过 CustomEvent 触发）
  function deliverCommandToTerminal(e: Event) {
    const detail = (e as CustomEvent).detail as { command?: string } | undefined
    const command = detail?.command
    const sessionId = store.activeSessionId
    if (!command || !sessionId) {
      toast.warning(sessionId ? '没有可投递的命令' : '请先打开一个终端')
      return
    }
    const session = store.sessions.find(s => s.id === sessionId)
    if (!session) return
    const api = window.electronAPI
    if (!api) {
      toast.error('Electron API 未就绪，无法投递命令')
      return
    }
    const writer = session.type === 'local' ? api.ptyWrite : api.sshWrite
    writer(sessionId, command).catch((err: any) => {
      console.error('[SshView] deliver command failed:', err)
      toast.error(toFriendlyMessage(err, '命令投递失败'))
    })
    focusActiveTerminal()
  }

  onMounted(() => {
    window.addEventListener('ssh:focus-terminal', focusActiveTerminal)
    window.addEventListener('ssh:deliver-command', deliverCommandToTerminal)
  })

  onUnmounted(() => {
    window.removeEventListener('ssh:focus-terminal', focusActiveTerminal)
    window.removeEventListener('ssh:deliver-command', deliverCommandToTerminal)
  })

  return { focusActiveTerminal, deliverCommandToTerminal }
}
