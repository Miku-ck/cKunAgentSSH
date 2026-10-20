/**
 * 内联执行过程的纯映射：终态文案、工具行状态、命令摘要。
 *
 * 组件只渲染这些结果，不在模板里重排分支 —— 文案与色调写在一个可测函数里，
 * 历史回放与实时运行读的是同一张表。
 */
import type { AgentTraceSnapshot } from '@/api/client'

export type ProcessTone = 'run' | 'ok' | 'bad'

export interface ProcessStatus {
  text: string
  tone: ProcessTone
}

/** 本次 run 的归因四格：只有与最近一次 run 匹配的消息才由宿主传入 */
export interface ProcessMeta {
  modelName?: string
  permissionLabel?: string
  targetLabel?: string
  runId?: string
  durationMs?: number
}

export function processStatusLabel(trace: AgentTraceSnapshot, isLive: boolean): ProcessStatus {
  if (isLive) return { text: '执行中', tone: 'run' }
  // 中止没有独立终态串，snapshot 仍记 failed；aborted 是唯一的区分依据，
  // 必须排在 failed 之前，否则历史重载后会把「已中止」念成「失败」。
  if (trace.aborted) return { text: '已中止', tone: 'bad' }
  if (trace.status === 'failed') return { text: '失败', tone: 'bad' }
  if (trace.status === 'human_help') return { text: '需要人工', tone: 'bad' }
  if (trace.status === 'max_reached') return { text: '达到迭代上限', tone: 'bad' }
  return { text: '已完成', tone: 'ok' }
}

export interface ToolStateEntry {
  result?: string
  success?: boolean
}

/** result 为 undefined = 结果还没回来（在途）；空串是"跑完了、没输出"，两者不同。 */
export function toolState(entry: ToolStateEntry): 'running' | 'failed' | 'done' {
  if (entry.result === undefined) return 'running'
  return entry.success === false ? 'failed' : 'done'
}

/** 工具行那行命令：命令执行类直接给 command，其余给参数摘要
 *  （整包 JSON 会让一行记录变成一屏噪声） */
export function headline(e: { tool: string; args?: Record<string, unknown> }): string {
  const cmd = typeof e.args?.command === 'string' ? e.args.command : ''
  if (cmd) return cmd
  const keys = Object.keys(e.args || {})
  if (!keys.length) return e.tool
  const preview = keys.slice(0, 3).map(k => {
    const v = String(e.args?.[k])
    // 截断必须留痕：不给省略号，用户会把半截参数当全文
    return `${k}=${v.length > 40 ? `${v.slice(0, 40)}…` : v}`
  }).join(' ')
  return `${e.tool}(${preview})`
}
