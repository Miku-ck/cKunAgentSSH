<template>
  <div class="local-terminal">
    <!-- 挂载点不能带 padding：FitAddon 量的是 .xterm 的父级，且只扣 .xterm
         自己的 padding。padding 留在外层，挂载点尺寸才是真实可绘制区。 -->
    <div class="local-terminal-mount" ref="containerRef">
      <CompletionPopup
        ref="popupRef"
        :visible="completionState.visible"
        :candidates="completionState.candidates"
        :x="completionState.x"
        :y="completionState.y"
        @select="applyCompletion"
      />
      <!-- 终端内搜索条（Ctrl+Shift+F）：addon 父组件持有，条只管 UI -->
      <TermSearchBar
        v-if="searchOpen && searchAddonRef"
        :addon="searchAddonRef"
        @close="closeSearch"
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
        问 AI
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import CompletionPopup from './CompletionPopup.vue'
import TermSearchBar from './TermSearchBar.vue'
import { releaseCompletionSession } from '@/composables/useCompletion'
import { useCompletionPopup } from '@/composables/useCompletionPopup'
import { useXtermInstance } from '@/composables/useXtermInstance'
import { useSSHStore } from '@/stores/ssh'
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
let unsubData: (() => void) | null = null
let unsubExit: (() => void) | null = null

const sshStore = useSSHStore()

// xterm 实例脚手架：创建与装配、终端内搜索、键位拦截、右键粘贴、尺寸与可见性适配、
// 卸载清理 —— 与远程终端共用 useXtermInstance，差异只体现在下面注入的参数上。
// （必须先于两个 use* 调用：它们要拿这里的 getTerminal）
const {
  getTerminal,
  searchAddonRef,
  searchOpen,
  closeSearch,
  focus,
} = useXtermInstance({
  containerRef,
  isVisible: () => props.visible,
  onResized: (c, r) => { window.electronAPI?.ptyResize(props.ptyId, c, r)?.catch(() => {}) },
  write: (data) => { window.electronAPI?.ptyWrite(props.ptyId, data)?.catch(() => {}) },
  // 惰性取值：这些符号来自下面的 useCompletionPopup，而它反过来要拿 getTerminal
  completion: () => ({
    visible: () => completionState.value.visible,
    moveUp: () => popupRef.value?.moveUp(),
    moveDown: () => popupRef.value?.moveDown(),
    selectCurrent: () => popupRef.value?.selectCurrent() ?? null,
    apply: applyCompletion,
    hide: hideCompletion,
    trigger: () => { void triggerCompletion() },
    collect: collectCommand,
  }),
  onSelectionChange: (t) => ctxOnSelectionChange(t),
  onOpened: () => startTerminalContext(),
  // 本地 pty 的初始尺寸要主动通告一次；远程侧由主进程同步，不需要
  resizeOnInitialFit: true,
  onBeforeDispose: () => {
    unsubData?.()
    unsubExit?.()
    // 仅当 session 仍在 store 中时才主动销毁 PTY：
    // 避免 closeSession 已调用 ptyDestroy 后，组件卸载又重复调用造成冗余 IPC
    if (props.ptyId && window.electronAPI && sshStore.sessions.some(s => s.id === props.ptyId)) {
      // 卸载期 PTY 可能已被后端回收，ptyDestroy 会 reject；不 catch 会抛未处理拒绝
      window.electronAPI.ptyDestroy(props.ptyId).catch(() => {})
    }
  },
})

// 「问 AI」终端上下文（选中登记 + 悬浮按钮；本地 pty cwd 取一次）
const {
  askAiBtn,
  onSelectionChange: ctxOnSelectionChange,
  askAi,
  start: startTerminalContext,
} = useTerminalContext({
  getTerminal,
  getSessionId: () => props.ptyId,
  fetchCwd: (id) => window.electronAPI?.ptyGetCwd(id) ?? Promise.resolve(''),
  isVisible: () => props.visible,
  // 本地 pty cwd 为初始目录，不随交互更新，挂载取一次即可
  cwdPollMs: 0,
})

// ─── 补全弹窗（与另一侧终端共用 useCompletionPopup，行为差异只由注入参数决定）───
const { completionState, popupRef, hideCompletion, triggerCompletion, applyCompletion, collectCommand } =
  useCompletionPopup({
    getTerminal,
    containerRef,
    target: () => ({ kind: 'local', id: props.ptyId, shell: props.shell }),
    write: (text) => { window.electronAPI?.ptyWrite(props.ptyId, text)?.catch(() => {}) },
  })

// 数据面：xterm 实例由 useXtermInstance 的 onMounted 创建（注册在前，先于此执行）
onMounted(() => {
  const api = window.electronAPI
  if (!api || !containerRef.value) return
  const id = props.ptyId

  // 监听 PTY 数据，过滤 ANSI 背景色以去除字体后的长方形色块
  unsubData = api.onPtyData(({ id: ptyId, data }) => {
    if (ptyId === id) {
      getTerminal()?.write(stripAnsiBackground(data))
      sshStore.markActivity(id)
      // 可见终端执行的哨兵捕获已集中到 useAgentDeliver 的全局订阅，组件只管显示
    }
  })

  // 监听 PTY 退出
  unsubExit = api.onPtyExit(({ id: ptyId, exitCode }) => {
    if (ptyId === id) {
      // C27 修复：会话关闭时清理补全请求序号（防 _completionRequestIds 泄漏）
      releaseCompletionSession(props.ptyId)
      getTerminal()?.write(`\r\n\x1b[33m[Process exited with code ${exitCode}]\x1b[0m\r\n`)
      emit('exit', exitCode)
    }
  })
})

defineExpose({
  focus,
})
</script>

<style scoped>
.local-terminal {
  width: 100%;
  height: 100%;
  position: relative;
  /* 只负责呼吸边距；xterm 挂在内层 mount 上，FitAddon 才不会把这段 padding
     算进可绘制高度（否则多算一行、最后一行被终端区底边裁掉半截）。 */
  padding: var(--spacing-8) var(--spacing-10);
}

.local-terminal-mount {
  position: relative;
  width: 100%;
  height: 100%;
}

/* 「问 AI」悬浮按钮：选区上方，点击把选中内容送进首页任务台 */
.ask-ai-float-btn {
  /* indigo 档在深色/浅色/手记三套主题里都是深值（#818cf8/#4f46e5/#3b4f7a），
     hover 铺满它时前景恒为白 —— 与用户选的强调色无关，所以不读 --accent-on */
  --ask-on: #ffffff;
  position: absolute;
  z-index: 20;
  display: inline-flex;
  align-items: center;
  padding: 3px var(--spacing-8);
  border: 1px solid var(--accent-indigo);
  border-radius: var(--radius-sm);
  background: var(--bg-elevated-solid);
  color: var(--accent-indigo-soft);
  font-size: 0.8462rem;
  cursor: pointer;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45);
  transition: all var(--transition-fast);
}

.ask-ai-float-btn:hover {
  background: var(--accent-indigo);
  color: var(--ask-on);
}

.local-terminal :deep(.xterm) {
  height: 100%;
}

.local-terminal :deep(.xterm-viewport) {
  overflow-y: auto;
  /* 同 RemoteTerminal：xterm.css 给 viewport 铺的 #000 也要置空，
     padding 区与屏幕区才会同色。 */
  background-color: transparent;
}
</style>
