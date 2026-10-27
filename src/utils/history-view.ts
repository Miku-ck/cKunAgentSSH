/**
 * 执行历史的纯函数层 —— spec 2026-10-27-schedule-history-render
 * 状态标签 / 耗时 / run 号截断 / 折叠态预览句 / 两域行适配器。
 * mdPreviewLine 落这里而非 markdown.ts：后者顶层执行 DOMPurify.addHook，
 * vitest node 环境 import 即抛错，纯函数没法直测。
 * 对 API 类型的引用必须 import type（编译期擦除，node 测试不触运行时）。
 */
import { parseBackendTs } from '@/utils/format'
import type { ScheduleRun } from '@/api/agent'
import type { RunbookRunRecord } from '@/api/runbook'

/** 共享展示组件 HistoryList 的行契约（spec §1） */
export interface HistoryRow {
  id: string
  started_at?: string | null
  finished_at?: string | null
  status: string
  summary?: string
  error?: string
  run_id?: string
  /** 折叠态预览句：适配层算好传入，组件不猜（spec §2） */
  preview: string
  /** 适配层追加的展开理由（剧本 = 有 steps_state/diagnosis） */
  expandable?: boolean
}

export const STATUS_LABEL_SCHEDULE: Record<string, string> = {
  idle: '未执行',
  running: '执行中',
  completed: '已完成',
  failed: '失败',
  waiting_permission: '待审批',
  pending: '待执行',
}

export const STATUS_LABEL_RUNBOOK: Record<string, string> = {
  running: '执行中',
  success: '成功',
  failed: '失败',
  stopped: '已停止',
  stale_reaped: '中断（重启收尸）',
}

/** 未知值原样透出（与两页现状一致）；emptyText 供卡片位「未执行」兜底 */
export function statusLabel(
  s: string | null | undefined,
  map: Record<string, string>,
  emptyText = '',
): string {
  if (!s) return emptyText
  return map[s] || s
}

/** 单次执行耗时（秒，1 位小数）；缺失/倒挂/非法 → 空串。
 * toFixed(1) 而非原两页的模板字面量：后者会把 76.0 吃成 76，与注释口径不符 */
export function durationText(started?: string | null, finished?: string | null): string {
  if (!started || !finished) return ''
  const a = parseBackendTs(started).getTime()
  const b = parseBackendTs(finished).getTime()
  if (isNaN(a) || isNaN(b) || b <= a) return ''
  return `${((b - a) / 1000).toFixed(1)}s`
}

export function shortId(id?: string | null): string {
  return id ? id.slice(0, 8) : ''
}

/** 剥 markdown 标记取首行有效文本（折叠态预览句）。刻意保守：只剥行内标记 */
export function mdPreviewLine(text: string): string {
  if (!text) return ''
  let inFence = false
  for (const raw of text.split('\n')) {
    const line = raw.trim()
    if (/^(```|~~~)/.test(line)) { inFence = !inFence; continue }
    if (inFence || !line || /^(-{3,}|\*{3,}|_{3,})$/.test(line)) continue
    return stripInlineMd(line)
  }
  return ''
}

function stripInlineMd(s: string): string {
  return s
    .replace(/^#{1,6}\s+/, '')            // 标题
    .replace(/^[-*+]\s+/, '')             // 无序列表符
    .replace(/^\d+[.)]\s+/, '')           // 有序列表符
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1') // 链接/图片留文字
    .replace(/`{1,3}([^`]*)`{1,3}/g, '$1') // 行内代码/围栏残留
    .replace(/\*{1,3}([^*]+)\*{1,3}/g, '$1') // 加粗/斜体
    .replace(/_{2,3}([^_]+)_{2,3}/g, '$1')   // 下划线式加粗
    .replace(/~{2}([^~]+)~{2}/g, '$1')       // 删除线
    .trim()
}

export function scheduleRowPreview(r: ScheduleRun): string {
  return mdPreviewLine(r.summary || '') || r.error || ''
}

/** 步骤命令在前：error 优先会让成功行印出无关错误文本（spec §2 决议，失败行默认展开不靠预览辨错） */
export function runbookRowPreview(r: RunbookRunRecord): string {
  return r.steps_state?.[0]?.command || r.error || ''
}

export function toScheduleRow(r: ScheduleRun): HistoryRow {
  return {
    id: r.id, started_at: r.started_at, finished_at: r.finished_at,
    status: r.status, summary: r.summary, error: r.error, run_id: r.run_id,
    preview: scheduleRowPreview(r),
  }
}

export function toRunbookRow(r: RunbookRunRecord): HistoryRow {
  return {
    id: r.id, started_at: r.started_at, finished_at: r.finished_at,
    status: r.status, error: r.error, run_id: r.run_id,
    preview: runbookRowPreview(r),
    expandable: (r.steps_state?.length ?? 0) > 0 || !!r.diagnosis,
  }
}
