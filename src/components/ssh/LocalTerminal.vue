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
    <!-- 「问 AI」悬浮按钮：选中终端内容后在选区上方出现 -->
    <button
      v-if="askAiBtn.visible && visible"
      class="ask-ai-float-btn"
      :style="{ left: askAiBtn.x + 'px', top: askAiBtn.y + 'px' }"
      title="把选中的终端内容发给 AI 分析"
      @mousedown.prevent
      @click="askAi"
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="12" height="12" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
      问 AI
    </button>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import '@xterm/xterm/css/xterm.css'
import CompletionPopup from './CompletionPopup.vue'
import { releaseCompletionSession } from '@/composables/useCompletion'
import { useCompletionPopup } from '@/composables/useCompletionPopup'
import { useSSHStore } from '@/stores/ssh'
import { buildTerminalOptions, watchTerminalConfig } from '@/composables/useTerminalConfig'
import { stripAnsiBackground } from '@/utils/terminal-colors'
import { useTerminalContext } from '@/composables/useTerminalContext'

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

// 「问 AI」终端上下文（选中登记 + 悬浮按钮；本地 pty cwd 取一次）
const {
  askAiBtn,
  onSelectionChange: ctxOnSelectionChange,
  askAi,
  start: startTerminalContext,
} = useTerminalContext({
  getTerminal: () => terminal,
  getSessionId: () => props.ptyId,
  fetchCwd: (id) => window.electronAPI?.ptyGetCwd(id) ?? Promise.resolve(''),
  isVisible: () => props.visible,
  // 本地 pty cwd 为初始目录，不随交互更新，挂载取一次即可
  cwdPollMs: 0,
})

// ─── 补全弹窗（与另一侧终端共用 useCompletionPopup，行为差异只由注入参数决定）───
const { completionState, popupRef, hideCompletion, triggerCompletion, applyCompletion, collectCommand } =
  useCompletionPopup({
    getTerminal: () => terminal,
    containerRef,
    target: () => ({ kind: 'local', id: props.ptyId, shell: props.shell }),
    write: (text) => { window.electronAPI?.ptyWrite(props.ptyId, text)?.catch(() => {}) },
  })

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

  // 「问 AI」上下文采集启动（本地 cwd 取一次）
  startTerminalContext()

  // 监听终端配置变化，动态应用主题/字号/字体（无需重建终端）
  // 换字号后容器尺寸不变、ResizeObserver 不会触发，新行列数要在回调里主动下发给 PTY
  unsubTermConfig = watchTerminalConfig(terminal, fitAddon, () => props.visible, () => {
    if (!terminal) return
    api.ptyResize(id, terminal.cols, terminal.rows).catch(() => {})
  })

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
    api.ptyResize(id, terminal.cols, terminal.rows).catch(() => {})
  }

  // 监听 PTY 数据，过滤 ANSI 背景色以去除字体后的长方形色块
  unsubData = api.onPtyData(({ id: ptyId, data }) => {
    if (ptyId === id) {
      terminal?.write(stripAnsiBackground(data))
      // 可见终端执行的哨兵捕获已集中到 useAgentDeliver 的全局订阅，组件只管显示
    }
  })

  // 监听 PTY 退出
  unsubExit = api.onPtyExit(({ id: ptyId, exitCode }) => {
    if (ptyId === id) {
      // C27 修复：会话关闭时清理补全请求序号（防 _completionRequestIds 泄漏）
      releaseCompletionSession(props.ptyId)
      terminal?.write(`\r\n\x1b[33m[Process exited with code ${exitCode}]\x1b[0m\r\n`)
      emit('exit', exitCode)
    }
  })

  // 终端输入 → PTY，并在提交命令时收集历史
  terminal.onData((data) => {
    api.ptyWrite(id, data).catch(() => {})
    if (data.includes('\r') || data.includes('\n')) {
      collectCommand()
    }
  })

  // B21 修复：右键粘贴与选中自动复制的剪贴板竞态 —
  // 右键时 xterm 会先选中单词触发 onSelectionChange 自动 writeText，
  // 与 contextmenu 里的 clipboard.readText 并发，可能把待粘贴内容覆盖掉。
  // 右键先快照剪贴板并设时间戳标志，窗口内的自动复制直接跳过
  let suppressAutoCopyUntil = 0

  // 选中文字自动复制到剪贴板 + 「问 AI」上下文登记
  // ctxOnSelectionChange 必须在 suppressAutoCopy 早退之前调用：
  // 右键粘贴窗口内的选中也要登记，否则「问 AI」拿不到选区内容
  terminal.onSelectionChange(() => {
    ctxOnSelectionChange(terminal)
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
          api.ptyWrite(id, sanitized).catch(() => {})
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
    api.ptyResize(id, terminal.cols, terminal.rows).catch(() => {})
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
  // 审查修复（P2）：卸载后 terminal 置 null — 原实现只 dispose 不清引用，
  // 卸载窗口内补全/渲染回调的 terminal 守卫失效，会在已 dispose 实例上操作
  terminal = null
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
  position: relative;
}

/* 「问 AI」悬浮按钮：选区上方，点击把选中内容送进 AI 面板 */
.ask-ai-float-btn {
  position: absolute;
  z-index: 20;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  border: 1px solid var(--accent-indigo);
  border-radius: 4px;
  background: var(--bg-elevated-solid);
  color: var(--accent-indigo-soft);
  font-size: 11px;
  cursor: pointer;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45);
  transition: all var(--transition-fast);
}

.ask-ai-float-btn:hover {
  background: var(--accent-indigo);
  color: #fff;
}

.local-terminal :deep(.xterm) {
  height: 100%;
}

.local-terminal :deep(.xterm-viewport) {
  overflow-y: auto;
}
</style>
