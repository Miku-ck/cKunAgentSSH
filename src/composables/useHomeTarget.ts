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
import { DEFAULT_LOCAL_SHELL } from '@/stores/exec-target'

export interface HomeTargetOption {
  sessionId: string
  /** 标题与菜单主文案：远程为 username@host，本地为目录名（不是 shell 名；读不出目录时为空） */
  label: string
  /** 副标题：连接名 / 本地目录全路径 */
  hint: string
  active: boolean
  /** 本地会话落脚的目录：调用方按它去重、过滤、显示。空 = 这个会话没有绑定目录
   *  （「不在项目中工作」那种），不该作为一行目录出现在列表里。 */
  path?: string
}

/** 无会话时的占位字样 —— 指令仍要能下达，后端会按无终端上下文跑 */
const NO_TARGET = '服务器'

/** 路径末级名：本地会话靠目录区分，不靠 shell 名区分。 */
function dirNameOf(p?: string): string {
  if (!p) return ''
  const parts = p.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] || p
}

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

  const options = computed<HomeTargetOption[]>(() =>
    ssh.sessions.map(s => {
      // 本地会话的读数用目录，不用 shell 名：默认 shell 一律 PowerShell，
      // 三行都写 PowerShell 等于没标签（2026-11-10 用户口径）。目录才是本地的身份。
      // 目录取「实时 cwd 优先、建会话时的初始 cwd 兜底」；两个都空说明这个会话
      // 没绑定目录 —— label 留空，由调用方决定不列这行，而不是编一个「本地终端」。
      if (s.type === 'local') {
        // 只有**绑定了目录**的会话才读 cwd：没绑定（「不在项目中工作」）的会话
        // 终端也会同步上来主目录，读它等于把主目录当成用户选的文件夹。
        const cwd = s.cwd ? ssh.sessionCwd(s.id) : ''
        return {
          sessionId: s.id,
          label: dirNameOf(cwd),
          hint: cwd,
          path: cwd,
          active: s.id === ssh.activeSessionId,
        }
      }
      return {
        sessionId: s.id,
        label: s.shell || s.name,
        hint: ssh.connections.find(c => c.id === s.connectionId)?.name || s.name,
        active: s.id === ssh.activeSessionId,
      }
    }),
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
