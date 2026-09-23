/**
 * 可拖拽调整宽度的侧边面板 Composable
 * 从 SideChatPanel 中提取的通用拖拽逻辑。
 */
import { ref, onUnmounted } from 'vue'

interface ResizablePanelOptions {
  /** 最小宽度 (px)，默认 280 */
  minWidth?: number
  /** 最大宽度 (px)，默认 800 */
  maxWidth?: number
  /** 初始宽度 (px)，默认 460 */
  initialWidth?: number
}

export function useResizablePanel(options: ResizablePanelOptions = {}) {
  const {
    minWidth = 280,
    maxWidth = 800,
    initialWidth = 460,
  } = options

  const panelWidth = ref(initialWidth)

  let activeMouseMove: ((ev: MouseEvent) => void) | null = null
  let activeMouseUp: (() => void) | null = null
  let isResizing = false // 防重入标志

  function startResize(e: MouseEvent) {
    if (isResizing) return // 防止快速双击拖柄导致监听器泄漏
    e.preventDefault()
    isResizing = true
    const startX = e.clientX
    const startWidth = panelWidth.value

    function onMouseMove(ev: MouseEvent) {
      const delta = startX - ev.clientX
      const newWidth = Math.min(maxWidth, Math.max(minWidth, startWidth + delta))
      panelWidth.value = newWidth
    }

    function onMouseUp() {
      document.removeEventListener('mousemove', onMouseMove)
      document.removeEventListener('mouseup', onMouseUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      activeMouseMove = null
      activeMouseUp = null
      isResizing = false
    }

    activeMouseMove = onMouseMove
    activeMouseUp = onMouseUp
    document.addEventListener('mousemove', onMouseMove)
    document.addEventListener('mouseup', onMouseUp)
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
  }

  // 组件卸载时清理可能残留的拖拽监听器和 body 样式
  onUnmounted(() => {
    if (activeMouseMove) document.removeEventListener('mousemove', activeMouseMove)
    if (activeMouseUp) document.removeEventListener('mouseup', activeMouseUp)
    // 拖拽中卸载时恢复 body 样式，避免光标和选中状态泄漏
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
    isResizing = false
  })

  return { panelWidth, startResize }
}
