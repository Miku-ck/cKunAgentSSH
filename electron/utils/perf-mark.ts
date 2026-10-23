/**
 * 启动关键路径审计打点（临时工具，audit 完成后随方案落地决定去留）。
 * 基线 = Electron 主进程启动时刻（performance.timeOrigin），跨模块共享同一坐标系。
 */
export function perfMark(name: string): void {
  console.log(`[perf] ${name} @${Math.round(performance.now())}ms`)
}
