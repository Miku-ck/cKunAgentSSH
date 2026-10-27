/**
 * 定时任务双类型（运维 / 日常）的分类判据 —— 纯函数，脱离组件可测。
 *
 * spec 2026-10-27-schedule-task-type
 * 差异仅体现在列表分区、模板集合与卡片色条；执行与审批链路不感知类型。
 * 存量任务与缺省值一律归 'ops'（与后端 DEFAULT 'ops' 同口径）。
 */
import type { ScheduleRecord } from '@/api/client'

export type TaskType = 'ops' | 'daily'

/** 计算所需的 ScheduleRecord 子集（便于测试传入最小对象） */
export type ScheduleLike = Pick<ScheduleRecord, 'id' | 'name'> & {
  task_type?: TaskType | string
}

export interface TemplateLike {
  id: string
  name: string
  task_type?: TaskType
}

/** 归一：任何非 'daily' 的值（含 undefined/存量）都按运维处理 */
export function normalizeTaskType(t?: TaskType | string): TaskType {
  return t === 'daily' ? 'daily' : 'ops'
}

/** 列表分区：运维在上、日常在下，各自保持后端返回顺序 */
export function groupSchedules<T extends ScheduleLike>(items: T[]): {
  ops: T[]
  daily: T[]
} {
  const ops: T[] = []
  const daily: T[] = []
  for (const s of items) {
    if (normalizeTaskType(s.task_type) === 'daily') daily.push(s)
    else ops.push(s)
  }
  return { ops, daily }
}

/** 模板集按类型过滤；模板未标注 task_type 视为运维（现有模板无需逐条补标） */
export function templatesForType<T extends TemplateLike>(templates: T[], type: TaskType): T[] {
  return templates.filter(t => normalizeTaskType(t.task_type) === type)
}

/** 卡片/工具行展示用的中文短标签 */
export function taskTypeLabel(type?: TaskType | string): string {
  return normalizeTaskType(type) === 'daily' ? '日常' : '运维'
}
