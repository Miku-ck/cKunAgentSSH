/**
 * Toast Store — 轻量全局通知
 * 用法：toast.success('安装成功') / toast.error('失败') / toast.info('提示')
 */
import { ref } from 'vue'
import { defineStore } from 'pinia'

export type ToastType = 'success' | 'error' | 'info' | 'warning'

export interface ToastItem {
  id: number
  type: ToastType
  message: string
  /** 自动关闭时长（ms），0 表示不自动关闭 */
  duration: number
}

let seq = 0

export const useToastStore = defineStore('toast', () => {
  const items = ref<ToastItem[]>([])

  function push(type: ToastType, message: string, duration = 3000): number {
    const id = ++seq
    items.value.push({ id, type, message, duration })
    if (duration > 0) {
      setTimeout(() => dismiss(id), duration)
    }
    return id
  }

  function dismiss(id: number) {
    items.value = items.value.filter(t => t.id !== id)
  }

  function success(message: string, duration = 3000): number {
    return push('success', message, duration)
  }

  function error(message: string, duration = 5000): number {
    return push('error', message, duration)
  }

  function info(message: string, duration = 3000): number {
    return push('info', message, duration)
  }

  function warning(message: string, duration = 4000): number {
    return push('warning', message, duration)
  }

  return { items, push, dismiss, success, error, info, warning }
})
