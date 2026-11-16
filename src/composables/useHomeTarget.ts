/**
 * 首页底栏的目标读数（spec 2026-09-29-home-command-panel · 用户故事 2/3）
 *
 * 这里只做**展示派生**，不再承担「换目标」：AI 的远程落点已收敛到 exec-target
 * 的 aiSessionId（与用户终端隔离），由 landing chip 显示与切换。本 composable
 * 留给底栏两件事：① 本地非默认 shell 的差量标记（targetChip / targetHint），
 * ② 批量下发的门槛（remoteCount / canBulkDispatch）。
 *
 * 显示值取会话的 shell 字段而不是回查连接记录：远程会话创建时 shell 就被写成
 * `username@host`，它是这条连接**当时**的真实身份；连接记录事后被改名或改了
 * 主机，都不该让一个已经跑着的会话在标题上换成另一个地址。连接记录只用来做
 * 副标题（人认得名字不认 IP）。
 */
import { computed } from 'vue'
import { useSSHStore } from '@/stores/ssh'
import { DEFAULT_LOCAL_SHELL } from '@/stores/exec-target'

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

  /** 底栏审批档右边那枚目标标记：**只给「chip 上没有的差量」**。
   *  本地落点：只有非默认 shell（WSL / Git Bash / CMD）才亮 —— 那时命令语法真的换了
   *  一套，是放行前值得看见的差量；默认 PowerShell 是产品默认设定，不是本轮信息
   *  （2026-11-10 用户口径：选本地就不要显示 PowerShell）。
   *  远程落点：不亮。第一枚 chip 已经写着这台机器（连接名或地址），再在右边印一遍
   *  同一个地址纯属重复（2026-11-11 用户截图：`192.168.150.40` 在底栏出现两次）。
   *  地址本身没丢 —— 它在第一枚 chip 的 title 里（targetHint: 连接名 · username@host）。 */
  const targetChip = computed(() => {
    const s = ssh.sessions.find(x => x.id === ssh.activeSessionId)
    if (!s || s.type !== 'local') return ''
    const shell = s.shell || s.name || ''
    return shell === DEFAULT_LOCAL_SHELL ? '' : shell
  })

  /** 悬停补全身份：地址是"落到哪台"，连接名才是用户认得的那台 */
  const targetHint = computed(() => {
    const s = ssh.sessions.find(x => x.id === ssh.activeSessionId)
    const conn = s?.connectionId ? ssh.connections.find(c => c.id === s.connectionId) : undefined
    return conn?.name ? `${conn.name} · ${targetLabel.value}` : targetLabel.value
  })

  const remoteCount = computed(() => ssh.sessions.filter(s => s.type === 'remote').length)
  /** 一台谈不上批量 —— 那就是一条命令，和批量下发弹窗的语义不符 */
  const canBulkDispatch = computed(() => remoteCount.value > 1)

  return { targetLabel, targetChip, targetHint, remoteCount, canBulkDispatch }
}
