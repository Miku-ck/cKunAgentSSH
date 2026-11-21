/**
 * 启动关键路径打点（常驻，spec 2026-10-23 §5 定案：审计期打点转为启动
 * 例行日志，供版本迭代回归对比与现场定位）。基线 = Electron 主进程
 * 启动时刻（performance.timeOrigin），跨模块共享同一坐标系。
 *
 * 对齐后端 main.py 的 _log_startup_summary：默认只输出**一行**汇总，
 * 逐条明细仅在 CKUN_PERF=1 时落地。后端早就是这样做的（明细是 WARNING、
 * 常驻只留汇总行），此前 Electron 侧每次启动刷十几行 [perf]，两边不对称。
 *
 * perfMark 的签名与所有调用点保持不变 —— main-startup-loading.test.ts:45
 * 硬断言 perfMark('dev server probe done') 必须留在 main.ts。
 */
const VERBOSE = typeof process !== 'undefined' && process.env.CKUN_PERF === '1'

interface PerfMark {
  name: string
  at: number
}

const marks: PerfMark[] = []

export function perfMark(name: string): void {
  const at = Math.round(performance.now())
  marks.push({ name, at })
  if (VERBOSE) console.log(`[perf] ${name} @${at}ms`)
}

/** 启动编排结束后调用一次，输出一行常驻汇总（成功/失败路径都覆盖）。 */
export function logPerfSummary(): void {
  if (marks.length === 0) return
  const total = marks[marks.length - 1].at
  console.log(
    `[perf] startup total=@${total}ms marks=${marks.length}` +
    (VERBOSE ? '' : ' (CKUN_PERF=1 看明细)'),
  )
}
