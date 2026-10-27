import { nextTick, type Ref } from 'vue'

/**
 * 弹窗焦点管理 —— Tab 环形 + 关闭后还原焦点
 *
 * 弹窗自绘 overlay 的那批（SFTP 预览、编辑器、隧道、连接…）此前各写各的：
 * 有的只管 Esc，有的连 Esc 都没有，没有一个把 Tab 圈在弹窗内。结果是键盘用户
 * Tab 几下就跑到遮罩后面被挡住的内容上，关掉弹窗后焦点还留在 body 上。
 *
 * 这里只做两件与版式无关的事，所以能套在任何自绘弹窗上：
 *  - activate()：记住打开前的焦点 → 聚焦容器（或 initialFocus 指定的元素）；
 *    同时挂一个 window 级 Tab 监听，把焦点圈在弹窗内；
 *  - deactivate()：摘监听 → 焦点还给打开前的元素（它可能已随列表刷新卸载，
 *    所以要查 isConnected）。
 *
 * Esc 不在这里：各弹窗的 Esc 优先级不同（比如编辑器要先问「未保存的更改」），
 * 交给调用方按自己的链路处理。
 *
 * 弹窗叠层（编辑器上面再压一个「未保存的更改」确认框）时用 suspend()/resume()：
 * 下层停拦 Tab、也不动焦点，但 prevFocus 留着 —— 整叠关完才回到触发器。
 * 两层都挂 window 监听的话，一次 Tab 被两份环形各拦一次，焦点会互相踢。
 */

const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

export interface ModalFocusOptions {
  /** 打开时的初始焦点；不给则聚焦弹窗容器本身（容器要有 tabindex="-1"） */
  initialFocus?: string
}

export function useModalFocusTrap(dialogRef: Ref<HTMLElement | null>, options: ModalFocusOptions = {}) {
  let prevFocus: HTMLElement | null = null
  let active = false
  let suspended = false

  function focusables(): HTMLElement[] {
    return dialogRef.value ? Array.from(dialogRef.value.querySelectorAll<HTMLElement>(FOCUSABLE)) : []
  }

  /**
   * window 级监听而不是绑在容器上：焦点若跑到弹窗外的某个按钮（点遮罩、切窗口、
   * 程序自己挪焦点），绑在容器上的监听收不到事件，焦点就再也回不来了。
   */
  function onKeydown(e: KeyboardEvent) {
    if (!active || e.key !== 'Tab') return
    const dialog = dialogRef.value
    if (!dialog) return
    const items = focusables()
    if (!items.length) return
    const first = items[0]
    const last = items[items.length - 1]
    const current = document.activeElement as HTMLElement | null

    if (!current || !dialog.contains(current)) {
      e.preventDefault()
      ;(e.shiftKey ? last : first).focus()
      return
    }
    if (e.shiftKey && (current === first || current === dialog)) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && current === last) {
      e.preventDefault()
      first.focus()
    }
  }

  function activate() {
    if (active) return
    active = true
    prevFocus = document.activeElement as HTMLElement | null
    window.addEventListener('keydown', onKeydown, true)
    void nextTick(() => {
      const preferred = options.initialFocus ? dialogRef.value?.querySelector<HTMLElement>(options.initialFocus) : null
      ;(preferred ?? dialogRef.value)?.focus()
    })
  }

  function deactivate() {
    if (!active) return
    active = false
    suspended = false
    window.removeEventListener('keydown', onKeydown, true)
    if (prevFocus && prevFocus.isConnected) prevFocus.focus()
    prevFocus = null
  }

  /** 上层弹窗接管：停拦 Tab，但既不还原焦点也不丢掉 prevFocus */
  function suspend() {
    if (!active || suspended) return
    suspended = true
    window.removeEventListener('keydown', onKeydown, true)
  }

  /** 上层弹窗关掉：恢复拦截，焦点已在原处，不重新聚焦 */
  function resume() {
    if (!active || !suspended) return
    suspended = false
    window.addEventListener('keydown', onKeydown, true)
  }

  return { activate, deactivate, suspend, resume, isActive: () => active }
}
