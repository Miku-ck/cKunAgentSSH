<template>
  <div class="remote-terminal-root">
    <!-- 重连修复：重连状态条 — 重连中：spinner + 尝试序号；自动重连放弃后：「重连」按钮。
         仅在有状态时渲染，平时零高度开销；终端容器 flex:1 自动让位/还原 -->
    <div class="reconnect-bar" v-if="reconnectState || reconnectGaveUp">
      <template v-if="reconnectState">
        <span class="reconnect-spinner"></span>
        <span class="reconnect-text">重连中 ({{ reconnectState.attempt }}/{{ reconnectState.maxAttempts }})...</span>
      </template>
      <template v-else>
        <span class="reconnect-text reconnect-text-failed">已断开</span>
        <button class="reconnect-btn" :disabled="reconnectBusy" @click="manualReconnect">重连</button>
      </template>
    </div>
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
import { useToastStore } from '@/stores/toast'
import { buildTerminalOptions, watchTerminalConfig } from '@/composables/useTerminalConfig'
import { stripAnsiBackground } from '@/utils/terminal-colors'
import { toFriendlyMessage } from '@/api/client'
import { useTerminalContext } from '@/composables/useTerminalContext'

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
// 行缓冲提升到 setup 作用域：重连系统行追加（appendReconnectLine，setup 作用域）
// 需要先刷出残留的不完整行再写入，与 onMounted 内的 flush 逻辑共享同一缓冲
let dataBuffer = ''

const toast = useToastStore()

// 「问 AI」终端上下文（选中登记 + 悬浮按钮 + cwd 轮询）
const {
  askAiBtn,
  onSelectionChange: ctxOnSelectionChange,
  askAi,
  start: startTerminalContext,
} = useTerminalContext({
  getTerminal: () => terminal,
  getSessionId: () => props.sshId,
  fetchCwd: (id) => window.electronAPI?.sshGetCwd(id) ?? Promise.resolve(''),
  isVisible: () => props.visible,
  // 远程 cwd 随 cd 变化（OSC 7 实时跟踪），5s 轮询同步
  cwdPollMs: 5000,
})

// ─── 重连状态（重连修复）──────────────────
/** 重连进行中：状态条显示 spinner + 「重连中 (n/3)」（主进程 ssh:reconnect 事件驱动） */
const reconnectState = ref<{ attempt: number; maxAttempts: number } | null>(null)
/** 自动重连已放弃（3 次失败）且未手动重连：状态条显示「重连」按钮 */
const reconnectGaveUp = ref(false)
/** 手动重连 IPC 在途（防重复点击） */
const reconnectBusy = ref(false)
let unsubReconnect: (() => void) | null = null

/** 向终端追加一条重连系统提示行（先刷出不完整行缓冲，避免与 shell 输出交错） */
function appendReconnectLine(text: string, color: string): void {
  if (!terminal) return
  if (flushTimer) {
    clearTimeout(flushTimer)
    flushTimer = null
  }
  if (dataBuffer.length > 0) {
    const cleaned = processBufferedData(dataBuffer)
    if (cleaned) terminal.write(cleaned)
    dataBuffer = ''
  }
  terminal.write(`\r\n${color}${text}\x1b[0m`)
}

/** 手动重连：重置计数，重新走完整 3 次退避循环（连接由主进程完成，凭据主进程自取） */
async function manualReconnect(): Promise<void> {
  const api = window.electronAPI
  if (!api || !terminal || reconnectBusy.value) return
  reconnectBusy.value = true
  // 乐观隐藏按钮；后续状态由主进程 ssh:reconnect 事件驱动
  reconnectGaveUp.value = false
  try {
    await api.sshReconnect(props.sshId, terminal.cols, terminal.rows)
  } catch (err) {
    // 无重连上下文（如连接配置已删除）：恢复按钮并提示
    reconnectGaveUp.value = true
    appendReconnectLine('[重连失败：连接配置不存在或已删除]', '\x1b[31m')
    toast.error(toFriendlyMessage(err, '重连失败'), 5000)
  } finally {
    reconnectBusy.value = false
  }
}

/** 过滤/清洗 shell 输出（ANSI 背景色、OSC 7、启动注入命令回显） */
function processBufferedData(raw: string): string {
  let cleaned = stripAnsiBackground(raw)
  // 过滤 OSC 7 序列，支持 BEL (\x07) 和 ST (ESC \) 两种结束符
  cleaned = cleaned.replace(/\x1b\]7;[^\x07\x1b]*(?:\x07|\x1b\\)/g, '')
  // 过滤启动注入命令的回显（整行含 __ckun_ 标识则丢弃）
  cleaned = cleaned.replace(/[^\r\n]*__ckun_(?:osc7|host)[^\r\n]*[\r\n]*/g, '')
  cleaned = cleaned.replace(/[^\r\n]*stty (?:-)?echo 2>\/dev\/null[^\r\n]*[\r\n]*/g, '')
  return cleaned
}

// ─── 补全弹窗（与另一侧终端共用 useCompletionPopup，行为差异只由注入参数决定）───
const { completionState, popupRef, hideCompletion, triggerCompletion, applyCompletion, collectCommand } =
  useCompletionPopup({
    getTerminal: () => terminal,
    containerRef,
    target: () => ({ kind: 'remote', id: props.sshId }),
    write: (text) => { window.electronAPI?.sshWrite(props.sshId, text)?.catch(() => {}) },
  })

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

  // 「问 AI」上下文采集启动（cwd 轮询 + 首次同步）
  startTerminalContext()

  // 监听终端配置变化，动态应用主题/字号/字体（无需重建终端）
  // 换字号后容器尺寸不变、ResizeObserver 不会触发，新行列数要在回调里主动下发给 PTY
  unsubTermConfig = watchTerminalConfig(terminal, fitAddon, () => props.visible, () => {
    if (!terminal) return
    api.sshResize(props.sshId, terminal.cols, terminal.rows).catch(() => {})
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
  // （dataBuffer 与 processBufferedData 已提升到 setup 作用域，供重连系统行追加复用）

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
      // 可见终端执行的哨兵捕获已集中到 useAgentDeliver 的全局订阅，组件只管显示
    }
  })

  // 监听 SSH 关闭
  unsubClose = api.onSshClose(({ id, willReconnect }) => {
    if (id === props.sshId) {
      // C27 修复：会话关闭时清理补全请求序号（防 _completionRequestIds 泄漏）
      releaseCompletionSession(props.sshId)
      flushBuffer(true) // 刷新行缓冲，避免尾部数据丢失
      if (willReconnect) {
        // 重连修复：意外断开，主进程将自动重连 — 保留 tab（不 emit close/
        // removeSession）与 xterm 实例（scrollback 保留），具体提示行由紧随的
        // ssh:reconnect 状态事件写入。用户主动断开不发本事件，不受影响
        return
      }
      terminal?.write('\r\n\x1b[33m[SSH connection closed]\x1b[0m\r\n')
      emit('close')
    }
  })

  // 重连修复：监听重连状态变化（主进程推送）— 驱动终端系统行 + 状态条
  unsubReconnect = api.onSshReconnect((data) => {
    if (data.id !== props.sshId) return
    const { phase, attempt, maxAttempts } = data
    if (phase === 'reconnecting') {
      // 首轮排程（断开后自动 / 点击「重连」后）
      reconnectGaveUp.value = false
      reconnectState.value = { attempt, maxAttempts }
      appendReconnectLine(`[连接已断开] 正在自动重连 (${attempt}/${maxAttempts})...`, '\x1b[33m')
    } else if (phase === 'retry') {
      // 上一次尝试失败、下一次退避已排程：追加倒计时提示，状态条更新序号
      reconnectState.value = { attempt, maxAttempts }
      const secs = Math.max(1, Math.round((data.delayMs ?? 1000) / 1000))
      appendReconnectLine(`[重连第 ${attempt - 1}/${maxAttempts} 次失败，${secs} 秒后重试]`, '\x1b[33m')
    } else if (phase === 'success') {
      // 重连成功：新 shell 以同一 id 流式接管，scrollback 无缝衔接
      reconnectState.value = null
      reconnectGaveUp.value = false
      appendReconnectLine('[已重新连接]', '\x1b[32m')
    } else if (phase === 'gave-up') {
      reconnectState.value = null
      if (data.reason === 'cancelled') {
        // 重连被取消（如断开后连接配置被删除）：静默清除状态，隐藏状态条
        reconnectGaveUp.value = false
        return
      }
      if (data.reason === 'config-missing') {
        // 连接配置已被删除：重连失去意义，关闭 tab（等同用户主动断开）
        reconnectGaveUp.value = false
        appendReconnectLine('[连接配置已删除，无法重连]', '\x1b[31m')
        emit('close')
        return
      }
      // 3 次均失败：状态条显示「重连」按钮
      reconnectGaveUp.value = true
      appendReconnectLine('[自动重连失败，可点击「重连」手动重试]', '\x1b[31m')
    }
  })

  // 终端输入 → SSH，并在提交命令时收集历史
  terminal.onData((data) => {
    api.sshWrite(props.sshId, data).catch(() => {})
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
          api.sshWrite(props.sshId, sanitized).catch(() => {})
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
    api.sshResize(props.sshId, terminal.cols, terminal.rows).catch(() => {})
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
  unsubReconnect?.()
  unsubTermConfig?.()
  // 组件卸载【不】断开 SSH 连接：Vue 在 vite HMR 会卸载/重挂本组件
  // （RemoteTerminal / SshView / DashboardView 任一模块被热更新时），
  // 若在此调用 sshDisconnect 会把一条仍健康的实时连接整体 tear down
  // （client.end() → DISCONNECT），导致主进程处频繁报 SESSION_NOT_FOUND。
  // 真实关闭终端走的是显式 closeSession → sshDisconnect（会先从 store 移除
  // 会话），此处只负责组件级清理，不负责连接生命周期。
  terminal?.dispose()
  // 审查修复（P2）：卸载后 terminal 置 null — 原实现只 dispose 不清引用，
  // 卸载窗口内 ssh:write/渲染回调的守卫失效，会在已 dispose 实例上操作
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
          window.electronAPI.sshResize(props.sshId, terminal.cols, terminal.rows).catch(() => {})
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
/* 重连修复：根容器 flex 纵向布局 — 状态条出现时终端容器让位，
   ResizeObserver 自动重新 fit，无需手动协调 */
.remote-terminal-root {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.remote-terminal {
  flex: 1;
  width: 100%;
  min-height: 0;
  position: relative;
}

/* ─── 重连状态条 ─── */
.reconnect-bar {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  height: 30px;
  padding: 0 12px;
  background: var(--bg-base);
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-secondary);
  font-size: 12px;
  z-index: 5;
}

.reconnect-spinner {
  width: 12px;
  height: 12px;
  border: 2px solid var(--border-default);
  border-top-color: var(--accent-primary);
  border-radius: 50%;
  animation: reconnect-spin 0.8s linear infinite;
  flex-shrink: 0;
}

@keyframes reconnect-spin {
  to { transform: rotate(360deg); }
}

.reconnect-text-failed {
  color: var(--accent-red);
}

.reconnect-btn {
  margin-left: auto;
  padding: 2px 14px;
  border: 1px solid var(--accent-primary);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--accent-primary);
  font-size: 12px;
  cursor: pointer;
  transition: all var(--transition-fast);
}

.reconnect-btn:hover:not(:disabled) {
  background: var(--accent-primary);
  color: var(--accent-on);
}

.reconnect-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.remote-terminal :deep(.xterm) {
  height: 100%;
}

/* 「问 AI」悬浮按钮：选区上方，点击把选中内容送进 AI 面板 */
.ask-ai-float-btn {
  /* 同 LocalTerminal：hover 底是 --accent-indigo（主题固定的靛档，三套主题都是深值），
     不是用户强调色，所以前景不能读 --accent-on —— 那个是 applyAccent 按 --accent-primary
     亮度算的，强调色选白/薄荷时它翻成深字，压在靛底上反而糊。 */
  --ask-on: #ffffff;
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
  color: var(--ask-on);
}

.remote-terminal :deep(.xterm-viewport) {
  overflow-y: auto;
}
</style>
