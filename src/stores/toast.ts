/**
 * Toast Store — 轻量全局通知
 * 用法：toast.success('安装成功') / toast.error('失败') / toast.info('提示')
 *
 * v2 改进：支持可选 details（技术详情，用户点击"展开详情"时展示）
 *         结构化传递 SSHError 的原始技术信息，便于开发者诊断
 *         支持可选 actions（动作按钮，如"重试"）
 */
import { ref } from 'vue'
import { defineStore } from 'pinia'

export type ToastType = 'success' | 'error' | 'info' | 'warning'

/** 可选动作按钮（如"重试"） */
export interface ToastAction {
  label: string
  onClick: () => void
}

export interface ToastItem {
  id: number
  type: ToastType
  message: string
  /** 自动关闭时长（ms），0 表示不自动关闭 */
  duration: number
  /** 可选技术详情（来自 SSHError 的 original.message 等）；默认折叠 */
  details?: string
  /** 用户是否手动展开了详情 */
  expanded?: boolean
  /** 可选动作按钮 */
  actions?: ToastAction[]
}

let seq = 0

export const useToastStore = defineStore('toast', () => {
  const items = ref<ToastItem[]>([])
  // 跟踪所有活跃的定时器，用于清理
  const timers = new Map<number, ReturnType<typeof setTimeout>>()

  function push(type: ToastType, message: string, duration = 3000, opts: { details?: string; actions?: ToastAction[] } = {}): number {
    const id = ++seq
    items.value.push({ id, type, message, duration, details: opts.details, actions: opts.actions })
    if (duration > 0) {
      const timer = setTimeout(() => {
        timers.delete(id)
        dismiss(id)
      }, duration)
      timers.set(id, timer)
    }
    return id
  }

  function dismiss(id: number) {
    // 清理对应的定时器
    const timer = timers.get(id)
    if (timer) {
      clearTimeout(timer)
      timers.delete(id)
    }
    items.value = items.value.filter(t => t.id !== id)
  }

  /** 切换详情折叠状态；展开时暂停自动关闭给用户阅读，折叠后重新计时 */
  function toggleDetails(id: number) {
    const item = items.value.find(t => t.id === id)
    if (!item) return
    item.expanded = !item.expanded
    if (item.expanded) {
      // 展开详情时暂停自动关闭（用户需要时间阅读）
      const timer = timers.get(id)
      if (timer) {
        clearTimeout(timer)
        timers.delete(id)
      }
    } else if (item.duration > 0 && !timers.has(id)) {
      // 折叠后重新挂上自动关闭定时器，否则该 toast 永久驻留
      const timer = setTimeout(() => {
        timers.delete(id)
        dismiss(id)
      }, item.duration)
      timers.set(id, timer)
    }
  }

  function success(message: string, duration = 3000): number {
    return push('success', message, duration)
  }

  function error(message: string, duration = 5000, opts?: { details?: string; actions?: ToastAction[] }): number {
    return push('error', message, duration, opts || {})
  }

  function info(message: string, duration = 3000): number {
    return push('info', message, duration)
  }

  function warning(message: string, duration = 4000): number {
    return push('warning', message, duration)
  }

  return { items, dismiss, toggleDetails, success, error, info, warning }
})
