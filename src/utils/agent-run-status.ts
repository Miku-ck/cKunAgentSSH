/**
 * agent-run-status.ts — run 状态串的单一事实源（渲染端）
 *
 * S9-13：`AgentTraceSnapshot.status` 此前只声明 `completed | failed` 两值，而
 * 生产者写的是**原始终态**（权威集合见 backend/agent/harness/contracts.py 的
 * `TERMINAL_RUN_STATES` 与 database.py 的 `_TERMINAL_RUN_STATUSES`：
 * completed / failed / human_help / max_reached）。类型窄于能力面 ⇒ 两处后果：
 *   1. `agentStore.snapshot()` 只能把 human_help/max_reached 折成 failed（自述
 *      「以兼容类型」），固化进库的记录就此丢真；
 *   2. 渲染侧的二元 `=== 'completed' ? '完成' : '失败'` 把「迭代耗尽」和
 *      「需要人工介入」都念成「失败」—— 这三种结局用户下一步该做的事完全不同。
 *
 * 文案表原先在 RunHistoryView 里另有一份，两处同族标签各抄各的 ⇒ 一并收到这里，
 * 顺带当筛选下拉的枚举源（少列一个状态就有一类 run 筛不出来）。
 * 未认识的串一律原样显示、配色走中性：绝不把「新终态」猜成「失败」。
 */
export const RUN_TERMINAL_STATES = ['completed', 'failed', 'human_help', 'max_reached'] as const

export type RunTerminalState = typeof RUN_TERMINAL_STATES[number]

/** 后端 run 表的全部可见状态：终态 + 两个在途值。键序即筛选下拉的展示顺序。 */
export const RUN_STATE_LABELS: Record<string, string> = {
  init: '初始化',
  running: '运行中',
  completed: '完成',
  failed: '失败',
  human_help: '请求人工介入',
  max_reached: '达到迭代上限',
}

export function isRunTerminalState(value: unknown): value is RunTerminalState {
  return typeof value === 'string' && (RUN_TERMINAL_STATES as readonly string[]).includes(value)
}

/** 状态标签；表里没有的值原样返回（不猜翻译，也不降级成「失败」） */
export function runStatusLabel(value: unknown): string {
  const s = typeof value === 'string' ? value : ''
  if (!s) return '状态未知'
  return RUN_STATE_LABELS[s] || s
}

/**
 * 卡片配色：只有真正跑完是 ok，只有明确失败是 err，
 * 其余（含 human_help / max_reached / 在途 / 未知）保持中性 —— 它们不是失败，
 * 但也不是成功，红绿色调会把「下一步该做什么」误导成「这事黄了」。
 */
export function runStatusChip(value: unknown): 'ok' | 'err' | 'muted' {
  if (value === 'completed') return 'ok'
  if (value === 'failed') return 'err'
  return 'muted'
}
