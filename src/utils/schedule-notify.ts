/**
 * 定时任务桌面通知的选取判据（S7-08）—— 纯函数，脱离组件与 Electron 可测。
 *
 * 后台调度器触发的执行没有前端动作可挂靠，列表轮询是唯一能看到它进展的地方。
 * 原判据写在 SchedulesView 里且只看「last_status 字符串变没变」，于是每天成功的
 * 任务只在第一次成功时通知一次、此后 completed→completed 全静默；每天失败的同理
 * —— 而「定时任务失败」恰恰是这条通知存在的理由。改成按**执行身份**去重：
 * `last_run_id` 换了才是新的一次结果。该字段是后端专为对账存的
 * （api/client.ts 里此前只有类型声明、零读取点）。
 */
import type { ScheduleRecord } from '@/api/client'

/** 只对「有结果」的状态发通知：pending/running 是过程态，停用中的任务不算转态 */
export const NOTICE_BODIES: Record<string, string> = {
  completed: '执行完毕',
  failed: '执行失败，可在历史里查看',
  waiting_permission: '有需要审批的命令',
}

export interface PendingNotice {
  title: string
  body: string
}

export function pickNotices(
  prev: ScheduleRecord[],
  next: ScheduleRecord[],
): PendingNotice[] {
  // 首次加载（旧列表为空）只建基线不通知，否则每次打开页面都会把存量 completed
  // 历史重播一遍
  if (prev.length === 0) return []
  const out: PendingNotice[] = []
  for (const s of next) {
    const before = prev.find((p) => p.id === s.id)
    if (!before || !s.enabled) continue
    const body = NOTICE_BODIES[s.last_status]
    if (!body) continue
    if (s.last_run_id || before.last_run_id) {
      // 同一次执行只播一次：run 号没变就说明还是上次那条结果（轮询每 30s 会反复看到它）
      if (s.last_run_id === before.last_run_id) continue
    } else if (s.last_status === before.last_status) {
      // 两侧 run 号都为空（历史数据/从未跑过）时退回旧判据，免得把缺字段变成
      // 「所有任务都不通知」或「每次轮询都弹」
      continue
    }
    out.push({ title: `定时任务 · ${s.name}`, body })
  }
  return out
}
