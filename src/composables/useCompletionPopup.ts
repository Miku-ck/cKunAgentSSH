/**
 * 终端命令补全弹窗的**共享**状态与行为（LocalTerminal / RemoteTerminal 用同一套）。
 *
 * 抽出的前提：两个终端原本是逐行近重复的两份实现，且已经漂移过一次 ——
 * 「回读终端重算前缀」这个防重复字符的修复只落在 Local 一侧。改这里时
 * 只需改一处，但请务必保持两个终端的差异只体现在注入的参数上。
 */
import { ref, type Ref } from 'vue'
import type { Terminal } from '@xterm/xterm'
import { useSSHStore } from '@/stores/ssh'
import {
  readCurrentLine,
  stripPrompt,
  parseInput,
  columnToIndex,
  queryCompletion,
  buildCompletionText,
  type CompletionCandidate,
} from '@/composables/useCompletion'

interface CompletionPopupDeps {
  /** 取当前终端实例；终端已销毁时返回 null（await 之后也会被重新校验） */
  getTerminal: () => Terminal | null
  /** 弹窗定位所相对的容器元素 */
  containerRef: Ref<HTMLElement | null>
  /** 补全查询与命令记录的目标通道（local=pty / remote=ssh） */
  target: () => { kind: 'local' | 'remote'; id: string; shell?: string }
  /** 把补全文本写回该通道 */
  write: (text: string) => void
}

export function useCompletionPopup(deps: CompletionPopupDeps) {
  const sshStore = useSSHStore()

  const completionState = ref<{
    visible: boolean
    candidates: CompletionCandidate[]
    x: number
    y: number
    prefix: string
  }>({
    visible: false,
    candidates: [],
    x: 0,
    y: 0,
    prefix: '',
  })

  const popupRef = ref<{ moveUp: () => void; moveDown: () => void; selectCurrent: () => CompletionCandidate | null } | null>(null)

  function hideCompletion(): void {
    completionState.value.visible = false
    completionState.value.candidates = []
    completionState.value.prefix = ''
  }

  /** 光标列（buffer 坐标）→ 输入串下标 */
  function cursorIndexInInput(terminal: Terminal): { input: string; cursorIdx: number } | null {
    const lineInfo = readCurrentLine(terminal)
    if (!lineInfo) return null
    const { input, startCol } = stripPrompt(lineInfo.line)
    const cursorCol = Math.max(0, lineInfo.cursorPos - startCol)
    return { input, cursorIdx: columnToIndex(input, cursorCol) }
  }

  async function triggerCompletion(): Promise<void> {
    const terminal = deps.getTerminal()
    if (!terminal) return
    try {
      const at = cursorIndexInInput(terminal)
      if (!at) return
      const parsed = parseInput(at.input, at.cursorIdx)
      if (!parsed) {
        hideCompletion()
        return
      }

      const t = deps.target()
      const candidates = await queryCompletion(t.kind, t.id, '', parsed, t.shell)
      // await 期间终端可能已关闭/断开，再校验一次避免向死通道写入
      if (!deps.getTerminal()) return
      if (candidates.length === 0) {
        hideCompletion()
        return
      }

      completionState.value.prefix = parsed.prefix
      // 唯一候选直接补全，不闪弹窗
      if (candidates.length === 1) {
        applyCompletion(candidates[0])
        return
      }

      completionState.value.candidates = candidates
      positionPopup()
      completionState.value.visible = true
    } catch (err) {
      console.error('[completion] triggerCompletion error:', err)
      hideCompletion()
    }
  }

  function positionPopup(): void {
    const terminal = deps.getTerminal()
    const container = deps.containerRef.value
    if (!terminal || !terminal.element || !container) return
    const buffer = terminal.buffer.active
    const rect = terminal.element.getBoundingClientRect()
    const containerRect = container.getBoundingClientRect()
    const cellWidth = terminal.cols > 0 ? rect.width / terminal.cols : 8
    const cellHeight = terminal.rows > 0 ? rect.height / terminal.rows : 16
    let x = containerRect.left + buffer.cursorX * cellWidth
    let y = containerRect.top + (buffer.cursorY + 1) * cellHeight
    const popupMaxWidth = 420
    if (x + popupMaxWidth > window.innerWidth) {
      x = window.innerWidth - popupMaxWidth - 8
    }
    const popupMaxHeight = 8 * 22 + 8
    if (y + popupMaxHeight > window.innerHeight) {
      y = containerRect.top + buffer.cursorY * cellHeight - popupMaxHeight
    }
    completionState.value.x = x
    completionState.value.y = y
  }

  function applyCompletion(candidate: CompletionCandidate): void {
    const terminal = deps.getTerminal()
    if (!terminal || !window.electronAPI) return
    const isUnique = completionState.value.candidates.length <= 1

    // 直接从终端读当前前缀，避免状态不同步导致重复字符
    let prefix = completionState.value.prefix
    const at = cursorIndexInInput(terminal)
    if (at) {
      const lastSpace = at.input.lastIndexOf(' ', at.cursorIdx - 1)
      prefix = at.input.slice(lastSpace + 1, at.cursorIdx)
    }

    const text = buildCompletionText(candidate.text, prefix, isUnique)
    if (text) deps.write(text)
    hideCompletion()
    terminal.focus()
  }

  /** 从当前行捕获用户提交的完整命令行 */
  function collectCommand(): void {
    const terminal = deps.getTerminal()
    if (!terminal) return
    const lineInfo = readCurrentLine(terminal)
    if (!lineInfo) return
    const { input } = stripPrompt(lineInfo.line)
    const command = input.trim()
    if (!command) return
    sshStore.recordCommand(deps.target().id, command)
  }

  return {
    completionState,
    popupRef,
    hideCompletion,
    triggerCompletion,
    applyCompletion,
    collectCommand,
  }
}
