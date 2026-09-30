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
  /** ＋ 菜单选完文件到挂上 chip 之间的那段：菜单项靠它显示"上传中"并禁用，
   *  否则双击会发两次上传（与侧栏 uploading 同一个防御） */
  const uploading = ref(false)

  /** 本地附件：走 store 既有的 uploadFiles → /chat/upload（后端真读真返），
   *  成功项追加进待发送附件；单个文件失败由 uploadFiles 内部逐个 toast，
   *  这里只兜它整体抛出的非预期异常 —— 静默失败比报错更难查。 */
  async function attachFiles(files: File[]): Promise<void> {
    if (!files.length || uploading.value) return
    uploading.value = true
    try {
      const results = await sshAi.uploadFiles(files)
      if (results.length) sshAi.setPendingAttachments([...sshAi.pendingAttachments, ...results])
    } catch (err: unknown) {
      toast.error(err instanceof Error ? err.message : String(err))
    } finally {
      uploading.value = false
    }
  }

  function removeAttachment(fileId: string) {
    sshAi.setPendingAttachments(sshAi.pendingAttachments.filter(f => f.file_id !== fileId))
  }

  /** 返回 false = 没发出去（空草稿 / 运行中 / 后端早退），草稿原样留着 */
  async function submit(): Promise<boolean> {
    const text = draft.value.trim()
    if (!text) return false
    // 与侧栏同一口径：运行中给出明确反馈，不静默吞掉用户刚打的字
    if (agent.isRunning) {
      toast.info('AI 正在运行中，请等待完成')
      return false
    }
    // 审批档、姿态、技能都取自 store：＋ 菜单点亮的东西必须真的随这次下达出去，
    // 否则它只是首页的装饰（侧栏那条通路 SshAiPanel.vue:116 早就是这么传的）。
    if (!await sshAi.sendMessage(text, {
      approval: sshAi.approvalMode,
      mode: sshAi.posture ?? undefined,
      skillIds: [...sshAi.selectedSkillIds],
      attachments: [...sshAi.pendingAttachments],
    })) return false
    // 技能与附件是"这一条消息带什么"，不是常驻档：发完即清，否则下一条静默复用
    // 同一份技能目录 / 再把整份文件重注入一遍（侧栏在 emit 时清、被拒时由
    // restoreDraft 放回，这里是同一语义）。
    sshAi.setSelectedSkillIds([])
    sshAi.setPendingAttachments([])
    draft.value = ''
    return true
  }

  return { draft, canSend, uploading, attachFiles, removeAttachment, submit }
}
