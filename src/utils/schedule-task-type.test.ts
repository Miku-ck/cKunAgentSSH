// spec 2026-10-27-schedule-task-type
import { describe, it, expect } from 'vitest'
import {
  groupSchedules,
  templatesForType,
  taskTypeLabel,
  type TaskType,
  type ScheduleLike,
  type TemplateLike,
} from './schedule-task-type'

const mk = (id: string, task_type?: string): ScheduleLike =>
  ({ id, name: id, task_type } as ScheduleLike)

describe('groupSchedules', () => {
  it('存量/缺省 task_type 归运维', () => {
    const { ops, daily } = groupSchedules([mk('a'), mk('b', 'ops')])
    expect(ops.map(s => s.id)).toEqual(['a', 'b'])
    expect(daily).toHaveLength(0)
  })

  it('daily 单独成组，保持原顺序', () => {
    const { ops, daily } = groupSchedules([mk('a', 'daily'), mk('b', 'ops'), mk('c', 'daily')])
    expect(ops.map(s => s.id)).toEqual(['b'])
    expect(daily.map(s => s.id)).toEqual(['a', 'c'])
  })
})

describe('templatesForType', () => {
  const tpls: TemplateLike[] = [
    { id: 'x', name: '巡检', task_type: 'ops' },
    { id: 'y', name: '周报', task_type: 'daily' },
    { id: 'z', name: '无标注' },
  ]
  it('按类型过滤，缺省 task_type 视为 ops', () => {
    const ops = templatesForType(tpls, 'ops')
    expect(ops.map(t => t.id)).toEqual(['x', 'z'])
    expect(templatesForType(tpls, 'daily').map(t => t.id)).toEqual(['y'])
  })
})

describe('taskTypeLabel', () => {
  it('ops→运维 daily→日常', () => {
    expect(taskTypeLabel('ops' as TaskType)).toBe('运维')
    expect(taskTypeLabel('daily' as TaskType)).toBe('日常')
  })
})
