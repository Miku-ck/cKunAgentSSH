/**
 * 可拖拽调整宽度的侧边面板 Composable
 * 从 AiChatPanel / SshAiPanel 中提取的通用拖拽逻辑。
 */
import { ref, onUnmounted } from 'vue'

export interface ResizablePanelOptions {
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

  function startResize(e: MouseEvent) {
    e.preventDefault()
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
    }

    activeMouseMove = onMouseMove
    activeMouseUp = onMouseUp
    document.addEventListener('mousemove', onMouseMove)
    document.addEventListener('mouseup', onMouseUp)
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
  }

  // 组件卸载时清理可能残留的拖拽监听器
  onUnmounted(() => {
    if (activeMouseMove) document.removeEventListener('mousemove', activeMouseMove)
    if (activeMouseUp) document.removeEventListener('mouseup', activeMouseUp)
  })

  return { panelWidth, startResize }
}
