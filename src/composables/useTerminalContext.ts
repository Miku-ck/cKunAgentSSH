/**
 * 终端现场上下文 —「问 AI」入口（A 阶段）
 *
 * 统一处理两个终端组件（Remote/Local）的公共逻辑：
 * 1. 选中变化时登记 selection 到 ssh store（「问 AI」上下文采集）
 * 2. 选中非空时在选区旁显示「问 AI」悬浮按钮
 * 3. 定时/一次性同步终端 cwd 到 ssh store（远程 OSC 7 实时跟踪；本地 pty 初始目录）
 */
import { onScopeDispose, ref } from 'vue'
import type { Terminal } from '@xterm/xterm'
import { useSSHStore } from '@/stores/ssh'
import { useSshAiStore } from '@/stores/ssh-ai'

export interface AskAiButtonState {
  visible: boolean
  x: number
  y: number
}

export interface UseTerminalContextOptions {
  /** 当前 xterm 实例（未初始化/已卸载返回 null） */
  getTerminal: () => Terminal | null
  /** 当前终端会话 ID（store sessions 维度） */
  getSessionId: () => string
  /** 取该会话的 cwd（远程 sshGetCwd / 本地 ptyGetCwd） */
  fetchCwd: (id: string) => Promise<string>
  /** 仅终端可见时轮询（隐藏终端不发 IPC） */
  isVisible: () => boolean
  /** cwd 轮询间隔 ms；0 = 挂载时取一次即可（本地 pty cwd 为初始值，不会变） */
  cwdPollMs: number
}

const SELECTION_MAX_CHARS = 8000

export function useTerminalContext(opts: UseTerminalContextOptions) {
  const sshStore = useSSHStore()
  const sshAiStore = useSshAiStore()

  const askAiBtn = ref<AskAiButtonState>({ visible: false, x: 0, y: 0 })

  let cwdTimer: ReturnType<typeof setInterval> | null = null

  /** 选中变化回调：登记 selection 并定位/隐藏悬浮按钮 */
  function onSelectionChange(terminal: Terminal | null): void {
    const id = opts.getSessionId()
    if (!id) return
    let sel = terminal?.getSelection() || ''
    // 超长选中截断，避免 prompt 膨胀
    if (sel.length > SELECTION_MAX_CHARS) sel = sel.slice(0, SELECTION_MAX_CHARS) + '…'
    sshStore.setTerminalSelection(id, sel)
    if (!sel || !terminal) {
      askAiBtn.value.visible = false
      return
    }
    positionAskAiBtn(terminal, sel)
  }

  /** 悬浮按钮定位：取选区左上角（视口内坐标 → 容器坐标） */
  function positionAskAiBtn(terminal: Terminal, _sel: string): void {
    // xterm getSelectionPosition() 返回 IBufferRange = { start:{x,y}, end:{x,y} }，
    // 没有顶层 x/y —— 误用 pos.x/pos.y 会恒为 undefined → 算出 NaN → 按钮不可用。
    const range = terminal.getSelectionPosition?.()
    const start = range?.start
    if (!start || !terminal.element) return
    const rect = terminal.element.getBoundingClientRect()
    const containerRect = terminal.element.parentElement?.getBoundingClientRect()
      ?? rect
    const cellWidth = terminal.cols > 0 ? rect.width / terminal.cols : 8
    const cellHeight = terminal.rows > 0 ? rect.height / terminal.rows : 16
    const btnW = 76
    const btnH = 24
    // start.y 是 buffer 绝对行号，必须减去视口滚动偏移 viewportY 才是屏幕内位置 —
    // 选区向上滚出可见区时，直接按绝对行号定位会把按钮放到容器外（y 为负或超界）。
    const viewportY = terminal.buffer.active.viewportY
    const relY = (start.y - viewportY) * cellHeight
    // 按钮相对容器定位（容器为 terminal.element 的父级，position: relative）
    let x = start.x * cellWidth + (rect.left - containerRect.left)
    let y = relY + (rect.top - containerRect.top) - btnH - 4
    if (y < 0) y = relY + (rect.top - containerRect.top) + cellHeight + 4
    // y 仍可能超出容器底部（选区在视口下缘时按钮下方空间不足）→ clamp 进容器
    if (y + btnH > containerRect.height) y = containerRect.height - btnH - 4
    if (x + btnW > containerRect.width) x = containerRect.width - btnW - 4
    x = Math.max(4, x)
    y = Math.max(4, y)
    askAiBtn.value = { visible: true, x, y }
  }

  /** 点击悬浮按钮：把选中内容送进 AI 面板（问题草稿预填，上下文随发送自动注入） */
  function askAi(): void {
    askAiBtn.value.visible = false
    sshAiStore.askAiAboutSelection()
  }

  async function syncCwd(): Promise<void> {
    const id = opts.getSessionId()
    if (!id) return
    try {
      const cwd = await opts.fetchCwd(id)
      if (cwd) sshStore.setTerminalCwd(id, cwd)
    } catch {
      // 会话可能已断开，静默
    }
  }

  function start(): void {
    void syncCwd()
    const pollMs = opts.cwdPollMs > 0 ? opts.cwdPollMs : 0
    if (pollMs > 0) {
      cwdTimer = setInterval(() => {
        if (!opts.isVisible()) return
        void syncCwd()
      }, Math.max(2000, pollMs))
    }
  }

  function stop(): void {
    if (cwdTimer) {
      clearInterval(cwdTimer)
      cwdTimer = null
    }
  }

  onScopeDispose(() => {
    stop()
    const id = opts.getSessionId()
    if (id) sshStore.setTerminalSelection(id, '')
  })

  return {
    askAiBtn,
    onSelectionChange,
    askAi,
    start,
  }
}
