/**
 * 多节点批量下发 — 纯核心（无 vue 依赖，可单测）：结果逐行对比 + 执行前分支决策
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

// 危险命令判定的规则源唯一在后端 agent/harness/safety.py::classify_command。
// 渲染层曾自带一份 DANGEROUS_PATTERNS 词表（第四份规则表，与后端长期漂移），已删除；
// 这里只做「拿到后端判定结论后怎么走」的分支决策，不掺任何命令词表。

/** 后端 POST /agent/safety/classify 的单条结论（取用到的字段；SafetyClassifyResult 的结构子集） */
export interface BulkSafetyVerdict {
  level: 'hardline' | 'dangerous' | 'safe'
  reason: string
  explain: string
}

/**
 * 批量下发执行前的分支决策。
 *
 * - verdict 为 null（判定接口不可达）→ block：fail-closed。这里是「一个人的动作 × N 台」
 *   的放大点，未判定的命令不允许下发。
 * - hardline → block：不提供「确认执行」，与 AI 通道上 FilesystemGuard 的无条件阻断同口径。
 * - dangerous，或目标是生产环境 → confirm：由调用方弹二次确认框。
 * - 其余 → run。
 *
 * @param prodHosts 目标节点中 environment==='prod' 的主机名
 */
export function decideBulkDispatch(
  cmd: string,
  verdict: BulkSafetyVerdict | null,
  prodHosts: string[],
): { action: 'run' } | { action: 'confirm'; warnings: string[] } | { action: 'block'; title: string; lines: string[] } {
  if (!verdict) {
    return {
      action: 'block',
      title: '无法完成安全判定',
      lines: [
        '后端风险判定接口不可用，已拒绝下发。',
        '未判定的命令不允许批量打到节点上，请确认后端服务在运行后重试。',
      ],
    }
  }
  if (verdict.level === 'hardline') {
    return {
      action: 'block',
      title: '命令被安全底线阻止',
      lines: [cmd, '', `判定：hardline（${verdict.reason}）`, verdict.explain, '', '底线命令即使单台也不允许经批量通道下发。'],
    }
  }
  const warnings: string[] = []
  if (prodHosts.length) warnings.push(`目标包含 ${prodHosts.length} 台生产环境主机：${prodHosts.join('、')}`)
  if (verdict.level === 'dangerous') warnings.push(`命令存在风险：${verdict.explain || verdict.reason}`)
  if (warnings.length === 0) return { action: 'run' }
  return { action: 'confirm', warnings }
}
