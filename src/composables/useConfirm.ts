/**
 * 全局确认对话框 composable
 *
 * 用法:
 *   const { confirm } = useConfirm()
 *   if (await confirm({ message: '确定删除?', danger: true })) { ... }
 *
 * 需要在 App.vue 中挂载一次 <ConfirmDialogHolder />
 */
import { ref } from 'vue'

export interface ConfirmOptions {
  /** 对话框标题,默认 "确认" */
  title?: string
  /** 必填,主体消息 */
  message: string
  /** 确认按钮文本,默认 "确定" */
  confirmText?: string
  /** 取消按钮文本,默认 "取消" */
  cancelText?: string
  /** 是否为危险操作(红色按钮 + 警示图标) */
  danger?: boolean
}

// ─── 模块级状态(单例) ─────────────────────────
const dialogInstance = ref<{ open: (opts: ConfirmOptions) => Promise<boolean> } | null>(null)

/** 由 ConfirmDialogHolder 调用以注册实例 */
export function registerConfirmDialog(instance: { open: (opts: ConfirmOptions) => Promise<boolean> }) {
  dialogInstance.value = instance
}

/**
 * 显示确认框,返回 Promise<boolean>:true=确认,false=取消
 *
 * 即使对话框未挂载,也会 resolve(false) 而非抛错,方便调用方
 */
export function useConfirm() {
  async function confirm(options: ConfirmOptions | string): Promise<boolean> {
    const opts: ConfirmOptions = typeof options === 'string' ? { message: options } : options

    if (!dialogInstance.value) {
      // 兜底:如果未挂载 Holder,使用原生 confirm 避免阻塞业务流程
      // (实际上 App.vue 必会挂载,这里只是保险)
      return window.confirm(opts.message)
    }
    return dialogInstance.value.open(opts)
  }

  return { confirm }
}
