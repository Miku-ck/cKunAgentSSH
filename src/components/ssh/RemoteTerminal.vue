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
    <div class="remote-terminal">
      <!-- 挂载点不能带 padding：FitAddon 量的是 .xterm 的父级，且只扣 .xterm
           自己的 padding。padding 留在外层，挂载点尺寸才是真实可绘制区。 -->
      <div class="remote-terminal-mount" ref="containerRef">
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
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="12" height="12" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
          问 AI
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import CompletionPopup from './CompletionPopup.vue'
import TermSearchBar from './TermSearchBar.vue'
import { releaseCompletionSession } from '@/composables/useCompletion'
import { useCompletionPopup } from '@/composables/useCompletionPopup'
import { useXtermInstance } from '@/composables/useXtermInstance'
import { useToastStore } from '@/stores/toast'
import { useSSHStore } from '@/stores/ssh'
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
let unsubData: (() => void) | null = null
let unsubClose: (() => void) | null = null
let unsubReconnect: (() => void) | null = null
// 行缓冲提升到 setup 作用域：重连系统行追加（appendReconnectLine，setup 作用域）
// 需要先刷出残留的不完整行再写入，与下面 onMounted 内的 flush 逻辑共享同一缓冲
let dataBuffer = ''
let flushTimer: ReturnType<typeof setTimeout> | null = null

const toast = useToastStore()

// xterm 实例脚手架：创建与装配、终端内搜索、键位拦截、右键粘贴、尺寸与可见性适配、
// 卸载清理 —— 与本地终端共用 useXtermInstance，差异只体现在下面注入的参数上。
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
  onResized: (c, r) => { window.electronAPI?.sshResize(props.sshId, c, r)?.catch(() => {}) },
  write: (data) => { window.electronAPI?.sshWrite(props.sshId, data)?.catch(() => {}) },
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
  // 远程侧初始尺寸由主进程同步，不需要在这里补发一次 sshResize
  resizeOnInitialFit: false,
  onBeforeDispose: () => {
    if (flushTimer) {
      clearTimeout(flushTimer)
      flushTimer = null
    }
    unsubData?.()
    unsubClose?.()
    unsubReconnect?.()
  },
})

// 「问 AI」终端上下文（选中登记 + 悬浮按钮 + cwd 轮询）
const {
  askAiBtn,
  onSelectionChange: ctxOnSelectionChange,
  askAi,
  start: startTerminalContext,
} = useTerminalContext({
  getTerminal,
  getSessionId: () => props.sshId,
  fetchCwd: (id) => window.electronAPI?.sshGetCwd(id) ?? Promise.resolve(''),
  isVisible: () => props.visible,
  // 远程 cwd 随 cd 变化（OSC 7 实时跟踪），5s 轮询同步
  cwdPollMs: 5000,
})

// ─── 补全弹窗（与另一侧终端共用 useCompletionPopup，行为差异只由注入参数决定）───
const { completionState, popupRef, hideCompletion, triggerCompletion, applyCompletion, collectCommand } =
  useCompletionPopup({
    getTerminal,
    containerRef,
    target: () => ({ kind: 'remote', id: props.sshId }),
    write: (text) => { window.electronAPI?.sshWrite(props.sshId, text)?.catch(() => {}) },
  })

// ─── 重连状态（重连修复）──────────────────
const sshStore = useSSHStore()
// 重连状态条改读 store（标签状态点的单一事实源）；系统行写入仍由本地事件处理器负责
const sessionSelf = computed(() => sshStore.sessions.find(s => s.id === props.sshId))
const reconnectState = computed(() => sessionSelf.value?.reconnect ?? null)
const reconnectGaveUp = computed(() => sessionSelf.value?.reconnectGaveUp ?? false)
/** 手动重连 IPC 在途（防重复点击） */
const reconnectBusy = ref(false)

/** 向终端追加一条重连系统提示行（先刷出不完整行缓冲，避免与 shell 输出交错） */
function appendReconnectLine(text: string, color: string): void {
  const terminal = getTerminal()
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
  const terminal = getTerminal()
  if (!api || !terminal || reconnectBusy.value) return
  reconnectBusy.value = true
  // 乐观隐藏按钮；后续状态由主进程 ssh:reconnect 事件驱动（store 单一事实源）
  if (sessionSelf.value) sessionSelf.value.reconnectGaveUp = false
  try {
    await api.sshReconnect(props.sshId, terminal.cols, terminal.rows)
  } catch (err) {
    // 无重连上下文（如连接配置已删除）：恢复按钮并提示
    if (sessionSelf.value) sessionSelf.value.reconnectGaveUp = true
    appendReconnectLine('[重连失败：连接配置不存在或已删除]', '\x1b[31m')
    toast.error(toFriendlyMessage(err, '重连失败'), 5000)
  } finally {
    reconnectBusy.value = false
  }
}

/**
 * 启动注入命令（electron/ssh-manager.ts initCmd）回显段的匹配：
 * 起点 = 注入命令的开头 `stty -echo 2>/dev/null; __ckun_host=`，
 * 终点 = 它的收尾 `stty echo 2>/dev/null`（中间非贪婪 + 长度上限）。
 *
 * Q-F9：原实现「行内含 __ckun_osc7/__ckun_host 哨兵即整行删除」——慢主机下
 * 命令回显与 MOTD/首条输出粘连在同一行时，粘连的真实输出会随整行一起被吞。
 * 改为仅剥哨兵段：段前的提示符片段与段后粘连的输出原样保留。
 * 长度上限（1024，实际回显 ~300 字符）防误伤：含相似文本且段长超限的
 * 真实输出不会被匹配删除。
 */
const INJECT_ECHO_RE =
  /stty -echo 2>\/dev\/null; __ckun_host=[\s\S]{0,1024}?stty echo 2>\/dev\/null(?:\r?\n)?/g

/** 过滤/清洗 shell 输出（ANSI 背景色、OSC 7、启动注入命令回显） */
function processBufferedData(raw: string): string {
  let cleaned = stripAnsiBackground(raw)
  // 过滤 OSC 7 序列，支持 BEL (\x07) 和 ST (ESC \) 两种结束符
  cleaned = cleaned.replace(/\x1b\]7;[^\x07\x1b]*(?:\x07|\x1b\\)/g, '')
  // 剥启动注入命令的回显段（仅哨兵起止范围，见 INJECT_ECHO_RE 注释）
  cleaned = cleaned.replace(INJECT_ECHO_RE, '')
  return cleaned
}

// 数据面：xterm 实例由 useXtermInstance 的 onMounted 创建（注册在前，先于此执行）
onMounted(() => {
  const api = window.electronAPI
  if (!api || !containerRef.value) return

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
        if (cleaned) getTerminal()?.write(cleaned)
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
        if (cleaned) getTerminal()?.write(cleaned)
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
    if (cleaned) getTerminal()?.write(cleaned)

    // 若仍有不完整尾部（如提示符），启动延时刷新
    if (dataBuffer.length > 0) {
      scheduleFlush()
    }
  }

  unsubData = api.onSshData(({ id, data }) => {
    if (id === props.sshId) {
      dataBuffer += data
      flushBuffer(false)
      sshStore.markActivity(id)
      // 可见终端执行的哨兵捕获已集中到 useAgentDeliver 的全局订阅，组件只管显示
    }
  })

  // 监听 SSH 关闭
  unsubClose = api.onSshClose(({ id, willReconnect }) => {
    if (id === props.sshId) {
      flushBuffer(true) // 刷新行缓冲，避免尾部数据丢失
      if (willReconnect) {
        // 重连修复：意外断开，主进程将自动重连 — 保留 tab（不 emit close/
        // removeSession）与 xterm 实例（scrollback 保留），具体提示行由紧随的
        // ssh:reconnect 状态事件写入。用户主动断开不发本事件，不受影响
        //
        // S4-08 修复：补全会话状态同样保留。releaseCompletionSession 的契约是
        // 「会话关闭时清理」，此前无条件跑在这个提前返回之前，而重连沿用同一个
        // 会话 id：序号被删后从 0 重启，断连前那条仍在途的补全响应回来时
        // requestId 反而匹配上「最新」一次，把陈旧候选列表渲染给新一次 Tab；
        // _aiPredictInFlight 被清则重新打开「双发 LLM 请求」的口子。
        return
      }
      // C27 修复：会话真正关闭时才清理补全请求序号（防 _completionRequestIds 泄漏）
      releaseCompletionSession(props.sshId)
      getTerminal()?.write('\r\n\x1b[33m[SSH connection closed]\x1b[0m\r\n')
      emit('close')
    }
  })

  // 重连修复：监听重连状态变化（主进程推送）— 状态本体在 store
  // （_setupReconnectTracking，状态条与标签状态点共用），这里只负责终端内系统行
  unsubReconnect = api.onSshReconnect((data) => {
    if (data.id !== props.sshId) return
    const { phase, attempt, maxAttempts } = data
    if (phase === 'reconnecting') {
      // 首轮排程（断开后自动 / 点击「重连」后）
      appendReconnectLine(`[连接已断开] 正在自动重连 (${attempt}/${maxAttempts})...`, '\x1b[33m')
    } else if (phase === 'retry') {
      // 上一次尝试失败、下一次退避已排程：追加倒计时提示
      const secs = Math.max(1, Math.round((data.delayMs ?? 1000) / 1000))
      appendReconnectLine(`[重连第 ${attempt - 1}/${maxAttempts} 次失败，${secs} 秒后重试]`, '\x1b[33m')
    } else if (phase === 'success') {
      // 重连成功：新 shell 以同一 id 流式接管，scrollback 无缝衔接
      appendReconnectLine('[已重新连接]', '\x1b[32m')
    } else if (phase === 'gave-up') {
      if (data.reason === 'cancelled') {
        // 重连被取消（如断开后连接配置被删除）：静默，不写系统行
        return
      }
      if (data.reason === 'config-missing') {
        // 连接配置已被删除：重连失去意义，关闭 tab（等同用户主动断开）
        appendReconnectLine('[连接配置已删除，无法重连]', '\x1b[31m')
        emit('close')
        return
      }
      // 耗尽（exhausted）或确定性失败（not-retryable，如口令错/主机密钥变更）：
      // 状态条显示「重连」按钮。S0-18：带上主进程已分类的失败原因，
      // 否则屏幕上无论什么原因都是同一句话，用户不知道要改配置还是等网络恢复。
      const detail = data.errorMessage ? `：${data.errorMessage}` : ''
      appendReconnectLine(
        data.reason === 'not-retryable'
          ? `[自动重连已停止${detail}，修正连接配置后可点击「重连」手动重试]`
          : `[自动重连失败${detail}，可点击「重连」手动重试]`,
        '\x1b[31m',
      )
    }
  })
})

defineExpose({
  focus,
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
  /* 只负责呼吸边距；xterm 挂在内层 mount 上，FitAddon 才不会把这段 padding
     算进可绘制高度（否则多算一行、最后一行被终端区底边裁掉半截）。 */
  padding: 8px 10px;
}

.remote-terminal-mount {
  position: relative;
  width: 100%;
  height: 100%;
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
  color: var(--accent-text);
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

/* 「问 AI」悬浮按钮：选区上方，点击把选中内容送进首页任务台 */
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
  border-radius: var(--radius-sm);
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
  /* xterm 6 的屏幕底叠了两层：scrollable element 用 theme.background
     （auto 模式已是 rgba 透明），下面这层 viewport 仍被 xterm.css 铺了 #000。
     一起置空，padding 区和屏幕才同色（否则浅色主题下露出一圈「边框」，
     背景图也透不进来）。 */
  background-color: transparent;
}
</style>
