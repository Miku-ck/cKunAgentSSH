<template>
  <div class="remote-terminal" ref="containerRef">
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
  releaseCompletionSession,
  type CompletionCandidate,
} from '@/composables/useCompletion'
import { useSSHStore } from '@/stores/ssh'
import { buildTerminalOptions, watchTerminalConfig } from '@/composables/useTerminalConfig'
import { stripAnsiBackground } from '@/utils/terminal-colors'

const props = defineProps<{
  sshId: string
  visible: boolean
}>()

const emit = defineEmits<{
  close: []
}>()

const containerRef = ref<HTMLElement | null>(null)
let terminal: Terminal | null = null
let fitAddon: FitAddon | null = null
let unsubData: (() => void) | null = null
let unsubClose: (() => void) | null = null
let unsubTermConfig: (() => void) | null = null
let resizeObserver: ResizeObserver | null = null
let contextMenuHandler: ((e: Event) => void) | null = null
let flushTimer: ReturnType<typeof setTimeout> | null = null

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
  try {
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

    const candidates = await queryCompletion('remote', props.sshId, '', parsed)
    // Optimization 修复：await 后校验终端仍存活，避免向已断开的 SSH 会话写入数据
    if (!terminal) return
    if (candidates.length === 0) {
      hideCompletion()
      return
    }

    // 单候选：直接补全
    if (candidates.length === 1) {
      completionState.value.prefix = parsed.prefix
      applyCompletion(candidates[0])
      return
    }

    // 多候选：显示浮层
    completionState.value.prefix = parsed.prefix
    completionState.value.candidates = candidates
    positionPopup()
    completionState.value.visible = true
  } catch (err) {
    console.error('[completion] triggerCompletion error:', err)
    hideCompletion()
  }
}

function positionPopup(): void {
  if (!terminal || !terminal.element || !containerRef.value) return
  const buffer = terminal.buffer.active
  const cursorX = buffer.cursorX
  const cursorY = buffer.cursorY
  const termElement = terminal.element
  const rect = termElement.getBoundingClientRect()
  const containerRect = containerRef.value.getBoundingClientRect()
  // 估算单元格尺寸
  const cellWidth = terminal.cols > 0 ? rect.width / terminal.cols : 8
  const cellHeight = terminal.rows > 0 ? rect.height / terminal.rows : 16
  let x = containerRect.left + cursorX * cellWidth
  let y = containerRect.top + (cursorY + 1) * cellHeight
  // 边界处理：右溢出
  const popupMaxWidth = 420
  if (x + popupMaxWidth > window.innerWidth) {
    x = window.innerWidth - popupMaxWidth - 8
  }
  // 边界处理：下溢出（向上显示）
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
    window.electronAPI.sshWrite(props.sshId, text)
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
  sshStore.recordCommand(props.sshId, command)
}

onMounted(() => {
  if (!containerRef.value || !window.electronAPI) return

  const api = window.electronAPI

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
  }

  // 监听 SSH 数据
  // 1. 过滤 ANSI 背景色以去除字体后的长方形色块
  // 2. 过滤 OSC 7 序列（由 PROMPT_COMMAND 注入产生，用于跟踪 cwd，不应显示）
  // 3. 过滤启动时注入命令的回显（用唯一标识 __ckun_ 和精确格式避免误伤用户输入）
  //
  // 行缓冲 + 延时刷新：
  // - 注入命令回显较长（~250 字符），可能被拆成多个 TCP 数据块到达。
  //   若按块过滤，含 __ckun_ 的前半段被过滤，但尾部残留会漏出。
  //   因此先按 \n 拆行，仅处理完整行，不完整尾部暂存等待拼接。
  // - 但 PS1 提示符不以 \n 结尾，会一直留在缓冲区。
  //   因此用 30ms 延时刷新：数据停顿后自动刷出剩余内容（提示符/光标移动等）。
  //   30ms 足够短不影响交互感知，又足够长以拼接被拆块的 echo 行。
  let dataBuffer = ''

  function processBufferedData(raw: string): string {
    let cleaned = stripAnsiBackground(raw)
    // 过滤 OSC 7 序列，支持 BEL (\x07) 和 ST (ESC \) 两种结束符
    cleaned = cleaned.replace(/\x1b\]7;[^\x07\x1b]*(?:\x07|\x1b\\)/g, '')
    // 过滤启动注入命令的回显（整行含 __ckun_ 标识则丢弃）
    cleaned = cleaned.replace(/[^\r\n]*__ckun_(?:osc7|host)[^\r\n]*[\r\n]*/g, '')
    cleaned = cleaned.replace(/[^\r\n]*stty (?:-)?echo 2>\/dev\/null[^\r\n]*[\r\n]*/g, '')
    return cleaned
  }

  function scheduleFlush(): void {
    if (flushTimer) clearTimeout(flushTimer)
    flushTimer = setTimeout(() => {
      flushTimer = null
      if (dataBuffer.length > 0) {
        const cleaned = processBufferedData(dataBuffer)
        if (cleaned) terminal?.write(cleaned)
        dataBuffer = ''
      }
    }, 30)
  }

  function flushBuffer(processComplete: boolean): void {
    if (flushTimer) {
      clearTimeout(flushTimer)
      flushTimer = null
    }

    if (processComplete) {
      // 连接关闭时刷新全部剩余缓冲（不再等待完整行）
      if (dataBuffer.length > 0) {
        const cleaned = processBufferedData(dataBuffer)
        if (cleaned) terminal?.write(cleaned)
        dataBuffer = ''
      }
      return
    }

    const nlIdx = dataBuffer.lastIndexOf('\n')
    if (nlIdx === -1) {
      // 尚无完整行，启动延时刷新（处理提示符等不以 \n 结尾的数据）
      scheduleFlush()
      return
    }

    const completePart = dataBuffer.slice(0, nlIdx + 1)
    dataBuffer = dataBuffer.slice(nlIdx + 1)

    const cleaned = processBufferedData(completePart)
    if (cleaned) terminal?.write(cleaned)

    // 若仍有不完整尾部（如提示符），启动延时刷新
    if (dataBuffer.length > 0) {
      scheduleFlush()
    }
  }

  unsubData = api.onSshData(({ id, data }) => {
    if (id === props.sshId) {
      dataBuffer += data
      flushBuffer(false)
    }
  })

  // 监听 SSH 关闭
  unsubClose = api.onSshClose(({ id }) => {
    if (id === props.sshId) {
      // C27 修复：会话关闭时清理补全请求序号（防 _completionRequestIds 泄漏）
      releaseCompletionSession(props.sshId)
      flushBuffer(true) // 刷新行缓冲，避免尾部数据丢失
      terminal?.write('\r\n\x1b[33m[SSH connection closed]\x1b[0m\r\n')
      emit('close')
    }
  })

  // 终端输入 → SSH，并在提交命令时收集历史
  terminal.onData((data) => {
    api.sshWrite(props.sshId, data)
    if (data.includes('\r') || data.includes('\n')) {
      collectCommand()
    }
  })

  // B21 修复：右键粘贴与选中自动复制的剪贴板竞态 —
  // 右键时 xterm 会先选中单词触发 onSelectionChange 自动 writeText，
  // 与 contextmenu 里的 clipboard.readText 并发，可能把待粘贴内容覆盖掉。
  // 右键先快照剪贴板并设时间戳标志，窗口内的自动复制直接跳过
  let suppressAutoCopyUntil = 0

  // 选中文字自动复制到剪贴板
  terminal.onSelectionChange(() => {
    const sel = terminal?.getSelection()
    if (sel) {
      if (Date.now() < suppressAutoCopyUntil) return
      navigator.clipboard.writeText(sel).catch(() => {})
    }
  })

  // 右键粘贴：粘贴时去除末尾换行符，避免自动执行
  contextMenuHandler = (e: Event) => {
    e.preventDefault()
    suppressAutoCopyUntil = Date.now() + 200
    navigator.clipboard.readText().then(text => {
      if (text) {
        // 去除末尾的换行符/回车符，防止粘贴时自动执行命令
        const sanitized = text.replace(/[\r\n]+$/, '')
        if (sanitized) {
          api.sshWrite(props.sshId, sanitized)
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
    api.sshResize(props.sshId, terminal.cols, terminal.rows)
  })
  resizeObserver.observe(containerRef.value)
})

onUnmounted(() => {
  if (flushTimer) {
    clearTimeout(flushTimer)
    flushTimer = null
  }
  if (contextMenuHandler && terminal?.element) {
    terminal.element.removeEventListener('contextmenu', contextMenuHandler)
  }
  // 清理 document 级 keydown 监听
  const docHandler = (terminal as any)?.__docKeyHandler
  if (docHandler) {
    document.removeEventListener('keydown', docHandler, { capture: true } as any)
  }
  resizeObserver?.disconnect()
  unsubData?.()
  unsubClose?.()
  unsubTermConfig?.()
  // 仅当 session 仍在 store 中时才主动断开 SSH
  // 避免 closeSession 已调用 sshDisconnect 后,组件卸载又重复调用造成冗余 IPC
  if (props.sshId && window.electronAPI && sshStore.sessions.some(s => s.id === props.sshId)) {
    window.electronAPI.sshDisconnect(props.sshId)
  }
  terminal?.dispose()
  // 清理可见性定时器
  if (visTimer) clearTimeout(visTimer)
})

// 可见性变化时重新适配
let visTimer: ReturnType<typeof setTimeout> | null = null
watch(
  () => props.visible,
  (visible) => {
    // 清理上一轮的定时器，避免组件卸载后仍执行回调
    if (visTimer) clearTimeout(visTimer)
    // 终端被切换隐藏时,立即关闭补全浮层,避免浮层残留
    if (!visible) hideCompletion()
    if (visible && fitAddon) {
      visTimer = setTimeout(() => {
        visTimer = null
        // 回调开头守卫：组件可能已卸载
        if (!containerRef.value) return
        fitAddon!.fit()
        if (window.electronAPI && terminal) {
          window.electronAPI.sshResize(props.sshId, terminal.cols, terminal.rows)
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
.remote-terminal {
  width: 100%;
  height: 100%;
}

.remote-terminal :deep(.xterm) {
  height: 100%;
}

.remote-terminal :deep(.xterm-viewport) {
  overflow-y: auto;
}
</style>
