/**
 * 长任务结构化编排（spec 2026-10-24-task-structuring · 接缝 S5）
 *
 * 只做"发送前的可选旁路"这一件事的编排：算目标 → 调 /agent/structure →
 * 把结构稿呈现给用户 → 用户显式采用才写回输入框、显式回退才还原。
 *
 * 三条不可让的边界（与后端护栏同源）：
 * - 绝不静默替换：open() 只固化回退基准，sharedDraft 一直到 accept() 都不动。
 * - 主机字段只读：来源永远是当前 active target，不接受结构稿里的任何主机改动。
 * - 新增写步骤：采用前必须逐条确认；含这些步骤时把 auto 审批回落 normal。
 */
import { computed, ref } from 'vue'
import { agentApi } from '@/api/agent'
import type { StructureResponse } from '@/utils/task-structure-core'
import {
  hasAddedWriteSteps,
  resolveApprovalOnAccept,
  shouldOfferStructuring,
  willDowngradeApproval,
} from '@/utils/task-structure-core'
import { useSshAiStore } from '@/stores/ssh-ai'
import { useAgentStore } from '@/stores/agent'
import { useSSHStore } from '@/stores/ssh'
import { useToastStore } from '@/stores/toast'

export function useTaskStructuring() {
  const sshAi = useSshAiStore()
  const agent = useAgentStore()
  const ssh = useSSHStore()
  const toast = useToastStore()

  const drawerOpen = ref(false)
  const loading = ref(false)
  const error = ref('')
  const structure = ref<StructureResponse | null>(null)
  /** 已被用户勾选确认的"新增·需确认"步骤 id。 */
  const confirmedAdded = ref<string[]>([])

  const canTrigger = computed(() =>
    shouldOfferStructuring(sshAi.sharedDraft, agent.isRunning),
  )

  /** 当前 active target → 结构化请求的主机/环境/白名单口径（与发送落点同源）。 */
  function targetPayload() {
    const s = ssh.activeSession
    const isRemote = s?.type === 'remote'
    const conn = isRemote ? ssh.connections.find(c => c.id === s?.connectionId) : undefined
    const host = isRemote ? (conn?.host || s?.shell || '') : ''
    return {
      host,
      session_type: (isRemote ? 'remote' : 'local') as 'remote' | 'local',
      environment: (conn?.environment || 'test') as 'test' | 'prod',
      // 白名单：已连接的远程主机地址都算"合法目标"，其余主机字面量被后端标为需确认。
      allowed_hosts: ssh.sessions
        .filter(x => x.type === 'remote')
        .map(x => x.shell)
        .filter(Boolean),
    }
  }

  async function open() {
    if (loading.value || agent.isRunning) return
    const rawText = sshAi.sharedDraft.trim()
    if (!rawText) return
    // 只固化回退基准，sharedDraft 不动 —— 用户在弹窗里看不到自己的原文被改。
    sshAi.beginStructuring()
    loading.value = true
    error.value = ''
    structure.value = null
    confirmedAdded.value = []
    try {
      const resp = await agentApi.structureTask({
        raw_text: rawText,
        active_target: { host: targetPayload().host, session_type: targetPayload().session_type },
        environment: targetPayload().environment,
        posture: sshAi.posture ?? undefined,
        allowed_hosts: targetPayload().allowed_hosts,
        model_id: sshAi.selectedModelId || undefined,
      })
      structure.value = resp
      drawerOpen.value = true
    } catch (e: unknown) {
      // 失败诚实降级：不打开弹窗、还原基准，原文原样留在输入框可直接发送。
      error.value = e instanceof Error ? e.message : String(e)
      sshAi.clearStructuringState()
      toast.info('结构化失败，原文可照常发送')
    } finally {
      loading.value = false
    }
  }

  function toggleConfirm(id: string) {
    const i = confirmedAdded.value.indexOf(id)
    if (i >= 0) confirmedAdded.value.splice(i, 1)
    else confirmedAdded.value.push(id)
  }

  /** 所有"新增·需确认"写步骤是否都被勾选（无此类步骤时恒真）。 */
  const allAddedConfirmed = computed(() => {
    const s = structure.value
    if (!s) return false
    const need = s.steps.filter(st => st.requires_confirm).map(st => st.id)
    return need.every(id => confirmedAdded.value.includes(id))
  })

  function accept() {
    const s = structure.value
    if (!s) return
    if (!allAddedConfirmed.value) {
      toast.info('请先确认所有「新增·需确认」步骤')
      return
    }
    // 审批回落：含新增写步骤 + auto → normal（core 单点判定，与后端 run 级回落对称）。
    // 判定必须基于改写前的档位：setApprovalMode 会把 auto 变成 normal，之后再问
    // 「会不会回落」就永远为假，提示丢失。
    const cur = sshAi.approvalMode
    const next = resolveApprovalOnAccept(cur, s)
    if (next !== cur) {
      sshAi.setApprovalMode(next)
      if (willDowngradeApproval(cur, s)) {
        toast.info('含新增写步骤，本次已切回手动审批')
      }
    }
    sshAi.acceptStructuredDraft(s.structured_text, hasAddedWriteSteps(s))
    drawerOpen.value = false
  }

  /** 一键回退：已采用的回到原文；未采用的直接关窗（原文从未被动过）。 */
  function revert() {
    sshAi.revertToOriginal()
    drawerOpen.value = false
    structure.value = null
    confirmedAdded.value = []
  }

  /** 关闭但不采用：清基准（sharedDraft 从未被改写，无需还原）。 */
  function close() {
    drawerOpen.value = false
    structure.value = null
    confirmedAdded.value = []
    // Pinia setup store 返回的 ref 在实例上已自动解包：这里读的是布尔值本身，
    // 写 .value 恒为 undefined → 会把「已采用后关闭」的基准也误清掉。
    if (!sshAi.enhancedActive) sshAi.clearStructuringState()
  }

  return {
    drawerOpen, loading, error, structure, confirmedAdded,
    canTrigger, allAddedConfirmed,
    open, accept, revert, close, toggleConfirm,
  }
}
