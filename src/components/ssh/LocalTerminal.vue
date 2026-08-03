<template>
  <div class="local-terminal" ref="containerRef">
    <CompletionPopup
      ref="popupRef"
      :visible="completionState.visible"
      :candidates="completionState.candidates"
      :x="completionState.x"
      :y="completionState.y"
      @select="applyCompletion"
      @cancel="hideCompletion"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import '@xterm/xterm/css/xterm.css'
import CompletionPopup from './CompletionPopup.vue'
import {
  readCurrentLine,
  stripPrompt,
  parseInput,
  columnToIndex,
  queryCompletion,
  buildCompletionText,
  type CompletionCandidate,
} from '@/composables/useCompletion'
import { useSSHStore } from '@/stores/ssh'
import { buildTerminalOptions, watchTerminalConfig } from '@/composables/useTerminalConfig'
import { stripAnsiBackground } from '@/utils/terminal-colors'

const props = defineProps<{
  ptyId: string
  visible: boolean
  shell?: string
}>()

const emit = defineEmits<{
  exit: [exitCode: number]
}>()

const containerRef = ref<HTMLElement | null>(null)
let terminal: Terminal | null = null
let fitAddon: FitAddon | null = null
let unsubData: (() => void) | null = null
let unsubExit: (() => void) | null = null
let unsubTermConfig: (() => void) | null = null
let resizeObserver: ResizeObserver | null = null
let contextMenuHandler: ((e: Event) => void) | null = null

const sshStore = useSSHStore()

// ─── 补全状态 ────────────────────────────
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

async function triggerCompletion(): Promise<void> {
  if (!terminal) return
  const lineInfo = readCurrentLine(terminal)
  if (!lineInfo) return
  const { input, startCol } = stripPrompt(lineInfo.line)
  const cursorInInputCol = Math.max(0, lineInfo.cursorPos - startCol)
  const cursorInInput = columnToIndex(input, cursorInInputCol)
  const parsed = parseInput(input, cursorInInput)
  if (!parsed) {
    hideCompletion()
    return
  }

  const candidates = await queryCompletion('local', props.ptyId, '', parsed, props.shell)
  // 注意：queryCompletion 内部会再传一次 props.ptyId 给 ptyCompleteCommand 以获取 cwd
  if (candidates.length === 0) {
    hideCompletion()
    return
  }

  if (candidates.length === 1) {
    completionState.value.prefix = parsed.prefix
    applyCompletion(candidates[0])
    return
  }

  completionState.value.prefix = parsed.prefix
  completionState.value.candidates = candidates
  positionPopup()
  completionState.value.visible = true
}

function positionPopup(): void {
  if (!terminal || !terminal.element || !containerRef.value) return
  const buffer = terminal.buffer.active
  const cursorX = buffer.cursorX
  const cursorY = buffer.cursorY
  const termElement = terminal.element
  const rect = termElement.getBoundingClientRect()
  const containerRect = containerRef.value.getBoundingClientRect()
  const cellWidth = terminal.cols > 0 ? rect.width / terminal.cols : 8
  const cellHeight = terminal.rows > 0 ? rect.height / terminal.rows : 16
  let x = containerRect.left + cursorX * cellWidth
  let y = containerRect.top + (cursorY + 1) * cellHeight
  const popupMaxWidth = 420
  if (x + popupMaxWidth > window.innerWidth) {
    x = window.innerWidth - popupMaxWidth - 8
  }
  const popupMaxHeight = 8 * 22 + 8
  if (y + popupMaxHeight > window.innerHeight) {
    y = containerRect.top + cursorY * cellHeight - popupMaxHeight
  }
  completionState.value.x = x
  completionState.value.y = y
}

function applyCompletion(candidate: CompletionCandidate): void {
  if (!terminal || !window.electronAPI) return
  const isUnique = completionState.value.candidates.length <= 1
  const text = buildCompletionText(candidate.text, completionState.value.prefix, isUnique)
  if (text) {
    window.electronAPI.ptyWrite(props.ptyId, text)
  }
  hideCompletion()
  terminal.focus()
}

/** 从当前行捕获用户提交的完整命令行 */
function collectCommand(): void {
  if (!terminal) return
  const lineInfo = readCurrentLine(terminal)
  if (!lineInfo) return
  const { input } = stripPrompt(lineInfo.line)
  const command = input.trim()
  if (!command) return
  sshStore.recordCommand(props.ptyId, command)
}

onMounted(() => {
  if (!containerRef.value || !window.electronAPI) return

  const api = window.electronAPI
  const id = props.ptyId

  // 创建 xterm 实例（主题/字号/字体从设置读取，其余行为参数固定）
  terminal = new Terminal(buildTerminalOptions({
    cursorBlink: true,
    cursorStyle: 'block',
    scrollback: 5000,
    allowProposedApi: true,
    rightClickSelectsWord: true,
  }))

  fitAddon = new FitAddon()
  terminal.loadAddon(fitAddon)
  terminal.loadAddon(new WebLinksAddon())

  terminal.open(containerRef.value)

  // 监听终端配置变化，动态应用主题/字号/字体（无需重建终端）
  unsubTermConfig = watchTerminalConfig(terminal, fitAddon, () => props.visible)

  // 拦截按键：Tab 触发补全；补全可见时 ↑↓/Enter/Esc/Tab 控制浮层
  // 在 document 级别 capture 阶段拦截 keydown，确保比 xterm 自己的 capture 监听更早触发
  const docKeyHandler = (e: KeyboardEvent) => {
    if (e.type !== 'keydown') return
    // 多终端并存时,仅当前可见(活动)终端响应全局快捷键
    // 避免不可见终端也触发补全,或拦截方向键导致活动终端浮层无法导航
    if (!props.visible) return

    // 浮层可见时：在 document 范围拦截键盘事件（不限于 terminal 容器内）
    // 这样即使焦点在外部，按 Tab 也能导航浮层
    if (completionState.value.visible) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        e.stopImmediatePropagation()
        e.stopPropagation()
        popupRef.value?.moveDown()
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        e.stopImmediatePropagation()
        e.stopPropagation()
        popupRef.value?.moveUp()
        return
      }
      if (e.key === 'Enter') {
        const cand = popupRef.value?.selectCurrent()
        if (cand) {
          e.preventDefault()
          e.stopImmediatePropagation()
          e.stopPropagation()
          applyCompletion(cand)
          return
        }
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        e.stopImmediatePropagation()
        e.stopPropagation()
        hideCompletion()
        return
      }
      // Tab / Shift+Tab 导航浮层
      if (e.key === 'Tab') {
        e.preventDefault()
        e.stopImmediatePropagation()
        e.stopPropagation()
        if (e.shiftKey) popupRef.value?.moveUp()
        else popupRef.value?.moveDown()
        return
      }
      // 可打印字符 / Backspace / 空格：关闭浮层
      if (!e.ctrlKey && !e.altKey && !e.metaKey) {
        if (e.key.length === 1 || e.key === 'Backspace' || e.key === ' ') {
          hideCompletion()
        }
      }
      return
    }

    // 浮层不可见时：仅处理来自 terminal 容器内的事件
    if (!terminal?.element?.contains(e.target as Node)) return

    if (e.key === 'Tab' && !e.ctrlKey && !e.altKey && !e.metaKey) {
      e.preventDefault()
      e.stopImmediatePropagation()
      e.stopPropagation()
      void triggerCompletion()
    }
  }
  document.addEventListener('keydown', docKeyHandler, { capture: true })
  ;(terminal as any).__docKeyHandler = docKeyHandler

  // 只在容器有有效尺寸时进行初始 fit，避免 display:none 时计算为 0x0
  const rect = containerRef.value.getBoundingClientRect()
  if (rect.width > 0 && rect.height > 0) {
    fitAddon.fit()
    api.ptyResize(id, terminal.cols, terminal.rows)
  }

  // 监听 PTY 数据，过滤 ANSI 背景色以去除字体后的长方形色块
  unsubData = api.onPtyData(({ id: ptyId, data }) => {
    if (ptyId === id) {
      terminal?.write(stripAnsiBackground(data))
    }
  })

  // 监听 PTY 退出
  unsubExit = api.onPtyExit(({ id: ptyId, exitCode }) => {
    if (ptyId === id) {
      terminal?.write(`\r\n\x1b[33m[Process exited with code ${exitCode}]\x1b[0m\r\n`)
      emit('exit', exitCode)
    }
  })

  // 终端输入 → PTY，并在提交命令时收集历史
  terminal.onData((data) => {
    api.ptyWrite(id, data)
    if (data.includes('\r') || data.includes('\n')) {
      collectCommand()
    }
  })

  // 选中文字自动复制到剪贴板
  terminal.onSelectionChange(() => {
    const sel = terminal?.getSelection()
    if (sel) {
      navigator.clipboard.writeText(sel).catch(() => {})
    }
  })

  // 右键粘贴：粘贴时去除末尾换行符，避免自动执行
  contextMenuHandler = (e: Event) => {
    e.preventDefault()
    navigator.clipboard.readText().then(text => {
      if (text) {
        // 去除末尾的换行符/回车符，防止粘贴时自动执行命令
        const sanitized = text.replace(/[\r\n]+$/, '')
        if (sanitized) {
          api.ptyWrite(id, sanitized)
        }
        terminal?.focus()
      }
    }).catch(() => {})
  }
  terminal.element?.addEventListener('contextmenu', contextMenuHandler)

  // 监听容器大小变化
  resizeObserver = new ResizeObserver(() => {
    if (!terminal || !fitAddon || !props.visible) return
    fitAddon.fit()
    api.ptyResize(id, terminal.cols, terminal.rows)
  })
  resizeObserver.observe(containerRef.value)
})

onUnmounted(() => {
  if (contextMenuHandler && terminal?.element) {
    terminal.element.removeEventListener('contextmenu', contextMenuHandler)
  }
  const docHandler = (terminal as any)?.__docKeyHandler
  if (docHandler) {
    document.removeEventListener('keydown', docHandler, { capture: true } as any)
  }
  resizeObserver?.disconnect()
  unsubData?.()
  unsubExit?.()
  unsubTermConfig?.()
  // 仅当 session 仍在 store 中时才主动销毁 PTY
  // 避免 closeSession 已调用 ptyDestroy 后,组件卸载又重复调用造成冗余 IPC
  if (props.ptyId && window.electronAPI && sshStore.sessions.some(s => s.id === props.ptyId)) {
    window.electronAPI.ptyDestroy(props.ptyId)
  }
  terminal?.dispose()
})

// 可见性变化时重新适配
watch(
  () => props.visible,
  (visible) => {
    // 终端被切换隐藏时,立即关闭补全浮层,避免浮层残留
    if (!visible) hideCompletion()
    if (visible && fitAddon) {
      setTimeout(() => {
        fitAddon!.fit()
        if (window.electronAPI && terminal) {
          window.electronAPI.ptyResize(props.ptyId, terminal.cols, terminal.rows)
        }
      }, 50)
    }
  },
)

defineExpose({
  focus: () => terminal?.focus(),
})
</script>

<style scoped>
.local-terminal {
  width: 100%;
  height: 100%;
  padding: 4px;
}

.local-terminal :deep(.xterm) {
  height: 100%;
}

.local-terminal :deep(.xterm-viewport) {
  overflow-y: auto;
}
</style>
