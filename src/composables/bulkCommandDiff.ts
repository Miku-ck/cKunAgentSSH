/**
 * 多节点批量下发 — 结果对比纯核心（F 阶段，无 vue 依赖，可单测）
 */

export interface BulkResult {
  label: string
  lines: string[]
  exitCode: number | null
  durationMs: number
  error?: string
}

export interface DiffLine {
  line: string
  /** 出现该行的节点数量 */
  count: number
  total: number
  /** 部分节点有、部分没有 → 差异行 */
  inconsistent: boolean
}

/**
 * 多份结果按行对比：每行统计出现在几个节点中。
 * 节点数 > 1 时，出现在 0 < count < total 的行标记为差异行。
 */
export function diffResults(results: BulkResult[]): DiffLine[] {
  const ok = results.filter(r => !r.error)
  const total = ok.length
  if (total === 0) return []
  const counts = new Map<string, number>()
  const order: string[] = []
  for (const r of ok) {
    const seenInThis = new Set<string>()
    for (const ln of r.lines) {
      if (seenInThis.has(ln)) continue
      seenInThis.add(ln)
      if (!counts.has(ln)) order.push(ln)
      counts.set(ln, (counts.get(ln) || 0) + 1)
    }
  }
  return order.map(line => {
    const count = counts.get(line) || 0
    return { line, count, total, inconsistent: total > 1 && count > 0 && count < total }
  })
}

/** 判断两份结果是否完全一致（用于状态条"N 台一致 / M 台差异"统计） */
export function resultsConsistent(results: BulkResult[]): boolean {
  const ok = results.filter(r => !r.error && r.exitCode !== null)
  if (ok.length <= 1) return true
  const first = ok[0].lines.join('\n')
  return ok.every(r => r.lines.join('\n') === first && r.exitCode === ok[0].exitCode)
}

// ── 危险命令启发式（生产环境防护）────────────────────────────────────────
// 批量下发会把同一命令并行打到 N 台机器，误发的放大效应远大于单会话。
// 纯函数启发式：命中即视为"变更/破坏性命令"，执行前必须二次确认；
// 未命中视为只读/低风险（df/uptime/ps/cat 等），prod 目标仅需普通确认。
// 词边界用 \b 防误伤（"rm" 不命中 "confirm"）；宁可多报确认，不漏放破坏。

const DANGEROUS_PATTERNS: Array<[RegExp, string]> = [
  [/\brm\b/, 'rm'],
  [/\brmdir\b/, 'rmdir'],
  [/\breboot\b/, 'reboot'],
  [/\bshutdown\b/, 'shutdown'],
  [/\bpoweroff\b/, 'poweroff'],
  [/\bhalt\b/, 'halt'],
  [/\bkill\b/, 'kill'],
  [/\bkillall\b/, 'killall'],
  [/\bpkill\b/, 'pkill'],
  [/\bdd\b/, 'dd'],
  [/\bmkfs/, 'mkfs'],
  [/\bfdisk\b|\bparted\b/, '磁盘分区操作'],
  [/\bdrop\s+(table|database|schema|index)\b/i, 'drop'],
  [/\btruncate\b/, 'truncate'],
  [/\bchmod\b/, 'chmod'],
  [/\bchown\b/, 'chown'],
  [/\bmv\b/, 'mv'],
  [/\bcp\b/, 'cp'],
  [/\btee\b/, 'tee(写文件)'],
  [/\b(apt-get|apt|yum|dnf)\s+install\b|\bapk\s+add\b/, '包安装'],
  [/\b(apt-get|apt|yum|dnf)\s+(remove|purge)\b/, '包移除'],
  [/\bapk\s+del\b/, '包移除'],
  [/\bsystemctl\s+(stop|disable|restart|mask)\b/, 'systemctl 停/禁/重启'],
  [/\bservice\s+\S+\s+(stop|restart)\b/, 'service 停/重启'],
  [/\buserdel\b|\busermod\b|\buseradd\b/, '用户管理'],
  [/\bcrontab\b/, 'crontab'],
  [/\biptables\b|\bfirewall-cmd\b|\bufw\b/, '防火墙变更'],
  [/(?<!>)>(?!>)(?!&)/, '重定向覆盖(>)'],
]

/**
 * 检测命令中的危险操作，返回命中的危险项描述列表（空数组 = 视为只读/低风险）。
 * 覆盖 `;` `&&` `|` 串联与括号子命令（词边界正则天然匹配串内任意位置）。
 */
export function detectDangerousCommand(cmd: string): string[] {
  const c = (cmd || '').toLowerCase()
  if (!c.trim()) return []
  const hits: string[] = []
  for (const [re, label] of DANGEROUS_PATTERNS) {
    if (re.test(c) && !hits.includes(label)) hits.push(label)
  }
  return hits
}
