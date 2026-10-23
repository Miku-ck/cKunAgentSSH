/**
 * 启动关键路径打点（常驻，spec 2026-10-23 §5 定案：审计期打点转为启动
 * 例行日志，供版本迭代回归对比与现场定位）。基线 = Electron 主进程
 * 启动时刻（performance.timeOrigin），跨模块共享同一坐标系。
 */
export function perfMark(name: string): void {
  console.log(`[perf] ${name} @${Math.round(performance.now())}ms`)
}
