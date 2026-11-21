/**
 * 关键故障告知：把「后端起不来 / 初始化异常 / 未处理异常」这类硬故障
 * 立即、明确地告知用户（原生对话框 + main.log 留痕），而不是只写日志后
 * 让窗口静默地"功能不可用"。
 *
 * 设计约束：
 *   1. 绝不成为新崩溃源 —— dialog 失败、ready 前 dialog 不可用都静默吞掉，
 *      故障路径本身已经出问题，不能因"弹窗失败"再炸一次。落日志是更保底的通道。
 *   2. 不传 parentWindow —— 关键故障时刻窗口可能已无响应，挂 parent 会把
 *      对话框变成 window-modal sheet，用户看到"点了没反应"。不传 parent 仍是
 *      应用级模态（会打断，符合产品裁决），但不依赖窗口健康度。
 *   3. 一次性去重 —— backend 故障在 starting→error→restarting→error 循环中会
 *      反复到达；同一"故障周期"只弹一次，ready 时复位。uncaught 用独立 key +
 *      60s cooldown，与 main.ts 的 UNCAUGHT_WINDOW_MS 对齐。
 *   4. 渲染层的 toast 由 src/stores/app.ts 的 reportBackendFault 负责（每周期
 *      一次、duration=0 永驻）；本模块只管主进程对话框，两边都靠 "ready 复位" 同步。
 */
import { dialog } from 'electron'

/** 与 main.ts 的 UNCAUGHT_WINDOW_MS 保持一致：uncaught 弹窗的最小间隔 */
const UNCAUGHT_COOLDOWN_MS = 60_000

let shownThisEpisode = false
let lastUncaughtAt = 0

/** 后端恢复 ready（或新一次启动周期开始）时调用，允许下一轮故障重新弹窗。 */
export function resetCriticalFaultEpisode(): void {
  shownThisEpisode = false
}

export type CriticalFaultKey = 'backend-fatal' | 'uncaught'

/**
 * 立即告知用户一个关键故障。返回是否真的弹了对话框（便于测试与去重判断）。
 *
 * console.error 已被 installMainLogger 劫持 → 自动加时间戳/级别并落 userData/main.log，
 * 所以即便 dialog 因故没弹出，现场仍有一条 [critical] 日志可查。
 */
export function announceCriticalFault(
  key: CriticalFaultKey,
  title: string,
  detail: string,
): boolean {
  const now = Date.now()
  if (key === 'uncaught') {
    if (now - lastUncaughtAt < UNCAUGHT_COOLDOWN_MS) return false
    lastUncaughtAt = now
  } else {
    if (shownThisEpisode) return false
    shownThisEpisode = true
  }

  const safeDetail = String(detail).slice(0, 800)
  console.error(`[critical][${key}] ${title}: ${safeDetail}`)

  // 不传 parentWindow（见约束 2）；整个调用吞异常（见约束 1）。
  void dialog.showMessageBox({
    type: 'error',
    title,
    message: title,
    detail: safeDetail,
    buttons: ['我知道了'],
    defaultId: 0,
    cancelId: 0,
    noLink: true,
  }).catch(() => { /* 对话框失败不能成为新崩溃源 */ })
  return true
}
