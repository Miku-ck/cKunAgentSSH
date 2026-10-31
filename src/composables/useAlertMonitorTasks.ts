/**
 * 告警监控任务 — 持久化与 CRUD（模块级 reactive 单例，供 useAlertTrigger 与 OpsPanel 共享）。
 *
 * 这是 v2「显式任务」模型的存储层：告警监控不再默认开启，必须由用户从模板显式新建
 * 任务。无任务 = 零监控（见 alert-trigger-core.ts 头注与 docs/p0-ops-agent-enhancements.md §1）。
 *
 * 持久化：独立 localStorage 键 `ckun.alertMonitor.tasks.v1`，深 watch 防抖写回；
 * 读取即经 sanitizeAlertMonitorTasks 收敛，损坏/非法一律落成空数组（= 零监控，fail-closed）。
 * 与 settings store 的两个后端同步分区无关，纯本地、无淘汰。
 */
import { ref, watch } from 'vue'
import {
  ALERT_MONITOR_STORAGE_KEY,
  alertMonitorTemplate,
  sanitizeAlertMonitorTasks,
  type AlertMonitorTask,
} from '@/utils/alert-trigger-core'

function loadFromStorage(): AlertMonitorTask[] {
  try {
    const raw = localStorage.getItem(ALERT_MONITOR_STORAGE_KEY)
    if (!raw) return []
    return sanitizeAlertMonitorTasks(JSON.parse(raw))
  } catch {
    // 坏 JSON / 结构非法：落成空数组，不写回以免覆盖潜在可读数据（下次读取仍会再收敛）
    return []
  }
}

// 模块级单例：同一会话内所有消费者（useAlertTrigger、OpsPanel）看到同一份响应式状态
const tasks = ref<AlertMonitorTask[]>(loadFromStorage())

let saveTimer: ReturnType<typeof setTimeout> | undefined
function persist(): void {
  try {
    localStorage.setItem(ALERT_MONITOR_STORAGE_KEY, JSON.stringify(tasks.value))
  } catch (e) {
    console.warn('[alertMonitorTasks] 持久化失败（localStorage 可能已满或被禁用）:', e)
  }
}

watch(tasks, () => {
  clearTimeout(saveTimer)
  saveTimer = setTimeout(persist, 300)
}, { deep: true })

/** 返回共享任务状态与操作方法。同一模块单例，调用方多次调用得到同一份状态。 */
export function useAlertMonitorTasks() {
  /** 从模板新建一个任务（host 必填，去空格） */
  function addFromTemplate(host: string, name?: string): AlertMonitorTask | null {
    const trimmed = host.trim()
    if (!trimmed) return null
    const task = alertMonitorTemplate(trimmed, name)
    tasks.value = [...tasks.value, task]
    return task
  }

  /** 整体替换某任务（UI 编辑 name / cooldownMinutes / 规则开关后回写） */
  function update(id: string, patch: Partial<Omit<AlertMonitorTask, 'id'>>): void {
    tasks.value = tasks.value.map(t => (t.id === id ? { ...t, ...patch } : t))
  }

  /** 切换启用状态 */
  function toggle(id: string, enabled: boolean): void {
    tasks.value = tasks.value.map(t => (t.id === id ? { ...t, enabled } : t))
  }

  /** 删除任务（撤销监控）。删空后自然回到零监控 */
  function remove(id: string): void {
    tasks.value = tasks.value.filter(t => t.id !== id)
  }

  /** 测试 / 重置用：直接覆盖任务数组 */
  function _setAll(next: AlertMonitorTask[]): void {
    tasks.value = next
  }

  return { tasks, addFromTemplate, update, toggle, remove, _setAll }
}
