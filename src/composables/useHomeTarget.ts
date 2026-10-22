/**
 * 首页标题里的目标主机（spec 2026-09-29-home-command-panel · 用户故事 2/3）
 *
 * 「换目标」在这套代码里只有一个含义：改 ssh store 的 activeSessionId ——
 * 后端 shell_exec 就是按它决定命令落到哪台机器（ssh-ai.buildContext 读
 * activeSession，_sendAgentMessageInner 把 sshSessionId 透传下去）。所以这里
 * 刻意不调 useSshNavigation.activateTerminal：那个函数会把首页切走并聚焦终端，
 * 而用户正在标题上挑主机、输入框里可能已经打了一半字。
 *
 * 显示值取会话的 shell 字段而不是回查连接记录：远程会话创建时 shell 就被写成
 * `username@host`，它是这条连接**当时**的真实身份；连接记录事后被改名或改了
 * 主机，都不该让一个已经跑着的会话在标题上换成另一个地址。连接记录只用来做
 * 副标题（人认得名字不认 IP）。
 */
import { computed } from 'vue'
import { useSSHStore } from '@/stores/ssh'

export interface HomeTargetOption {
  sessionId: string
  /** 标题与菜单主文案：远程为 username@host，本地为 shell 名 */
  label: string
  /** 副标题：连接名 / 「本地终端」 */
  hint: string
  active: boolean
}

/** 无会话时的占位字样 —— 指令仍要能下达，后端会按无终端上下文跑 */
const NO_TARGET = '服务器'

export function useHomeTarget() {
  const ssh = useSSHStore()

  function labelOf(sessionId: string): string {
    const s = ssh.sessions.find(x => x.id === sessionId)
    return s ? (s.shell || s.name) : NO_TARGET
  }

  const targetLabel = computed(() =>
    ssh.activeSessionId ? labelOf(ssh.activeSessionId) : NO_TARGET,
  )

  /** 底栏审批档右边那枚目标标记：远程给 shell 中 '@' 之后的地址段（落到哪台），
   *  本地给 shell 名（落到哪个 shell，如 WSL）—— 本地留空时用户就看不到本轮落在哪。
   *  远程拆不出地址（旧数据没有 shell、会话名里也没有 '@'）仍返回空：
   *  不能把会话名印成像是 IP 的东西。 */
  const targetChip = computed(() => {
    const s = ssh.sessions.find(x => x.id === ssh.activeSessionId)
    if (!s) return ''
    if (s.type === 'local') return s.shell || s.name
    const addr = s.shell || s.name
    const at = addr.lastIndexOf('@')
    return at >= 0 ? addr.slice(at + 1) : ''
  })

  /** 悬停补全身份：地址是"落到哪台"，连接名才是用户认得的那台 */
  const targetHint = computed(() => {
    const s = ssh.sessions.find(x => x.id === ssh.activeSessionId)
    const conn = s?.connectionId ? ssh.connections.find(c => c.id === s.connectionId) : undefined
    return conn?.name ? `${conn.name} · ${targetLabel.value}` : targetLabel.value
  })

  const options = computed<HomeTargetOption[]>(() =>
    ssh.sessions.map(s => ({
      sessionId: s.id,
      label: s.shell || s.name,
      hint: s.type === 'local'
        ? '本地终端'
        : (ssh.connections.find(c => c.id === s.connectionId)?.name || s.name),
      active: s.id === ssh.activeSessionId,
    })),
  )

  /** 只认已打开的会话：菜单项与真实状态之间不留"配了但没连上"的第三种目标 */
  function pick(sessionId: string): void {
    if (!ssh.sessions.some(s => s.id === sessionId)) return
    ssh.switchSession(sessionId)
  }

  const remoteCount = computed(() => ssh.sessions.filter(s => s.type === 'remote').length)
  /** 一台谈不上批量 —— 那就是一条命令，和批量下发弹窗的语义不符 */
  const canBulkDispatch = computed(() => remoteCount.value > 1)

  return { targetLabel, targetChip, targetHint, options, pick, remoteCount, canBulkDispatch }
}
