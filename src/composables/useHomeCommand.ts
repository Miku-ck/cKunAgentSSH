/**
 * 首页指令卡的下达逻辑
 *
 * spec 2026-09-29-home-command-panel · 接缝 S3。刻意只做"下达"：
 * 发送一律走 useSshAiStore().sendMessage（唯一通路，内含模型有效性校验、会话
 * 创建、终端上下文采集与 resolveRunModes 装配），审批档与姿态都读 store 而不是
 * 自己存一份 —— 首页与侧栏必须是同一档，否则用户在一处选了什么，另一处不算。
 */
import { computed, ref } from 'vue'
import { useSshAiStore } from '@/stores/ssh-ai'
import { useAgentStore } from '@/stores/agent'
import { useToastStore } from '@/stores/toast'

export function useHomeCommand() {
  const sshAi = useSshAiStore()
  const agent = useAgentStore()
  const toast = useToastStore()

  const draft = ref('')
  const canSend = computed(() => draft.value.trim().length > 0 && !agent.isRunning)

  /** 返回 false = 没发出去（空草稿 / 运行中 / 后端早退），草稿原样留着 */
  async function submit(): Promise<boolean> {
    const text = draft.value.trim()
    if (!text) return false
    // 与侧栏同一口径：运行中给出明确反馈，不静默吞掉用户刚打的字
    if (agent.isRunning) {
      toast.info('AI 正在运行中，请等待完成')
      return false
    }
    // 审批档与姿态都取自 store：首页 ＋ 菜单点亮的东西必须真的随这次下达出去，
    // 否则它只是首页的装饰（侧栏那条通路 SshAiPanel.vue:116 早就是这么传的）。
    if (!await sshAi.sendMessage(text, {
      approval: sshAi.approvalMode,
      mode: sshAi.posture ?? undefined,
    })) return false
    draft.value = ''
    return true
  }

  return { draft, canSend, submit }
}
