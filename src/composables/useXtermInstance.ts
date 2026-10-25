/**
 * useXtermInstance — xterm 实例的生命周期脚手架（RemoteTerminal / LocalTerminal 共用）
 *
 * 抽出的直接原因（同 useCompletionPopup 的注释）：两个终端的初始化段落原本是逐行
 * 近重复的两份实现，且已经漂移过一次。重复段落包括：实例创建与 addon 装配、
 * 终端内搜索状态、配置监听、document 级键位拦截（85 行逐字相同）、右键粘贴与
 * 剪贴板竞态、ResizeObserver、可见性重适配、卸载清理。
 *
 * 边界：这里只管「脚手架」。数据面（ssh 的行缓冲与重连、pty 的退出）差异太大，
 * 强收编会把两种语义搅在一起 —— 例如把 Local 的即时写入改成 Remote 的 30ms
 * 行缓冲就是行为变更。数据面留在组件，通过 write / onResized / onBeforeDispose
 * 注入。
 */
import { ref, shallowRef, watch, onMounted, onUnmounted, type Ref, type ShallowRef } from 'vue'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import { SearchAddon } from '@xterm/addon-search'
import '@xterm/xterm/css/xterm.css'
import { buildTerminalOptions, watchTerminalConfig } from './useTerminalConfig'
import type { CompletionCandidate } from './useCompletion'

/** 补全弹窗的能力面 —— 由组件惰性提供（见 UseXtermInstanceOptions.completion 注释） */
export interface XtermCompletionDeps {
  visible: () => boolean
  moveUp: () => void
  moveDown: () => void
  selectCurrent: () => CompletionCandidate | null
  apply: (candidate: CompletionCandidate) => void
  hide: () => void
  trigger: () => void
  collect: () => void
}

export interface UseXtermInstanceOptions {
  /** 终端挂载容器 */
  containerRef: Ref<HTMLElement | null>
  /** 当前是否为活动终端：不可见时不响应全局快捷键、不做 fit（0x0 会算错） */
  isVisible: () => boolean
  /**
   * 行列数变化 → 下发给对端。已做 0 尺寸守卫与同尺寸去重，
   * 回调里不用再判一次（同尺寸 SIGWINCH 会让部分 TUI 无谓重绘）。
   */
  onResized: (cols: number, rows: number) => void
  /** 用户输入 / 粘贴内容 → 写回通道（sshWrite / ptyWrite） */
  write: (data: string) => void
  /**
   * 补全依赖。用 getter 而非直接传对象：组件里这些符号来自 useCompletionPopup，
   * 而后者又要拿本 composable 的 getTerminal —— 直接传会撞 TDZ。getter 只在
   * 事件回调里求值，那时组件 setup 早已跑完。
   */
  completion: () => XtermCompletionDeps
  /** 选区变化钩子（「问 AI」上下文登记）。必须早于剪贴板早退，否则右键粘贴窗口内的选区登记不上 */
  onSelectionChange?: (terminal: Terminal) => void
  /** terminal.open() 之后立即执行（如启动 cwd 轮询） */
  onOpened?: (terminal: Terminal) => void
  /**
   * 卸载时、dispose 之前的额外清理（退订 IPC、清定时器、ptyDestroy）。
   * 必须在 dispose 前跑完：否则 dispose 后仍有回调往死实例上写。
   */
  onBeforeDispose?: () => void
  /** 初始 fit 之后补发一次 onResized（本地 pty 需要通告初始尺寸；远程 ssh 由主进程侧同步，不需要） */
  resizeOnInitialFit?: boolean
}

export interface XtermInstance {
  /** 取当前实例；销毁后返回 null（所有异步回调落点都要走它，不要缓存） */
  getTerminal: () => Terminal | null
  /** 搜索 addon：模板要传给 TermSearchBar，用 shallowRef 持有（addon 无需深度响应） */
  searchAddonRef: ShallowRef<SearchAddon | null>
  searchOpen: Ref<boolean>
  closeSearch: () => void
  focus: () => void
}

export function useXtermInstance(opts: UseXtermInstanceOptions): XtermInstance {
  const terminalRef = shallowRef<Terminal | null>(null)
  const fitAddonRef = shallowRef<FitAddon | null>(null)
  const searchAddonRef = shallowRef<SearchAddon | null>(null)
  const searchOpen = ref(false)

  function getTerminal(): Terminal | null {
    return terminalRef.value
  }

  function closeSearch(): void {
    searchOpen.value = false
    terminalRef.value?.focus()
  }

  function focus(): void {
    terminalRef.value?.focus()
  }

  let unsubTermConfig: (() => void) | null = null
  let resizeObserver: ResizeObserver | null = null
  let contextMenuHandler: ((e: Event) => void) | null = null
  let visTimer: ReturnType<typeof setTimeout> | null = null
  // B21 修复：右键粘贴与选中自动复制的剪贴板竞态。右键时 xterm 会先选中单词触发
  // onSelectionChange 自动 writeText，与 contextmenu 里的 readText 并发，
  // 可能把待粘贴内容覆盖掉 —— 右键先设时间戳标志，窗口内的自动复制直接跳过。
  let suppressAutoCopyUntil = 0

  onMounted(() => {
    const container = opts.containerRef.value
    if (!container) return

    // 创建 xterm 实例（主题/字号/字体从设置读取，其余行为参数固定）
    const terminal = new Terminal(buildTerminalOptions({
      cursorBlink: true,
      cursorStyle: 'block',
      scrollback: 5000,
      allowProposedApi: true,
      rightClickSelectsWord: true,
    }))
    const fitAddon = new FitAddon()
    terminal.loadAddon(fitAddon)
    terminal.loadAddon(new WebLinksAddon())

    // 终端内搜索（Ctrl+Shift+F 唤起）：addon 与终端同生命周期，由本 composable dispose
    searchAddonRef.value = new SearchAddon()
    terminal.loadAddon(searchAddonRef.value)

    terminal.open(container)
    terminalRef.value = terminal
    fitAddonRef.value = fitAddon

    opts.onOpened?.(terminal)

    // 监听终端配置变化，动态应用主题/字号/字体（无需重建终端）。
    // 换字号后容器尺寸不变、ResizeObserver 不会触发，新行列数要在这里主动下发。
    unsubTermConfig = watchTerminalConfig(terminal, fitAddon, opts.isVisible, () => {
      const t = terminalRef.value
      if (t) opts.onResized(t.cols, t.rows)
    })

    // 拦截按键：Tab 触发补全；补全可见时 ↑↓/Enter/Esc/Tab 控制浮层。
    // 在 document 级别 capture 阶段拦截，确保比 xterm 自己的 capture 监听更早触发。
    const docKeyHandler = (e: KeyboardEvent) => {
      if (e.type !== 'keydown') return
      // 多终端并存时，仅当前可见（活动）终端响应全局快捷键
      if (!opts.isVisible()) return
      const c = opts.completion()

      // 终端内搜索唤起（Ctrl+Shift+F）：^F 是 shell 前进字符，必须用 Shift 档避开。
      // 拦截要早于补全逻辑（同为 document capture，先到先得）
      if (searchOpen.value && e.key === 'Escape') {
        e.preventDefault()
        e.stopImmediatePropagation()
        e.stopPropagation()
        closeSearch()
        return
      }
      if (e.ctrlKey && e.shiftKey && (e.key === 'F' || e.key === 'f')) {
        // Q-F8：焦点判据 —— 焦点落在输入落点（聊天草稿、设置弹窗输入框等）
        // 时不劫持 Ctrl+Shift+F，让输入框正常收字、浏览器/应用搜索各归其位。
        // xterm 用容器内隐藏的 .xterm-helper-textarea 承接键入：焦点在终端时
        // activeElement 正是它（TEXTAREA）—— 按标签名一刀切会误伤终端输入，
        // 必须排除这个特例；终端容器内的其他输入（如终端内搜索条）同样放行。
        const ae = document.activeElement as HTMLElement | null
        const isXtermHelper = !!ae?.classList?.contains('xterm-helper-textarea')
        const editable = !!ae
          && !isXtermHelper
          && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.isContentEditable)
        if (editable) return
        e.preventDefault()
        e.stopImmediatePropagation()
        e.stopPropagation()
        searchOpen.value = true
        return
      }

      // 浮层可见时：在 document 范围拦截（不限于终端容器内），焦点在外部也能导航
      if (c.visible()) {
        if (e.key === 'ArrowDown') {
          e.preventDefault()
          e.stopImmediatePropagation()
          e.stopPropagation()
          c.moveDown()
          return
        }
        if (e.key === 'ArrowUp') {
          e.preventDefault()
          e.stopImmediatePropagation()
          e.stopPropagation()
          c.moveUp()
          return
        }
        if (e.key === 'Enter') {
          const cand = c.selectCurrent()
          if (cand) {
            e.preventDefault()
            e.stopImmediatePropagation()
            e.stopPropagation()
            c.apply(cand)
            return
          }
        }
        if (e.key === 'Escape') {
          e.preventDefault()
          e.stopImmediatePropagation()
          e.stopPropagation()
          c.hide()
          return
        }
        // Tab / Shift+Tab 导航浮层
        if (e.key === 'Tab') {
          e.preventDefault()
          e.stopImmediatePropagation()
          e.stopPropagation()
          if (e.shiftKey) c.moveUp()
          else c.moveDown()
          return
        }
        // 可打印字符 / Backspace / 空格：关闭浮层
        if (!e.ctrlKey && !e.altKey && !e.metaKey) {
          if (e.key.length === 1 || e.key === 'Backspace' || e.key === ' ') {
            c.hide()
          }
        }
        return
      }

      // 浮层不可见时：仅处理来自 terminal 容器内的事件
      if (!terminal.element?.contains(e.target as Node)) return

      if (e.key === 'Tab' && !e.ctrlKey && !e.altKey && !e.metaKey) {
        e.preventDefault()
        e.stopImmediatePropagation()
        e.stopPropagation()
        void c.trigger()
      }
    }
    document.addEventListener('keydown', docKeyHandler, { capture: true })
    // 挂在实例上，卸载时再读回来退订（dispose 后 element 可能已失效）
    ;(terminal as unknown as { __docKeyHandler?: typeof docKeyHandler }).__docKeyHandler = docKeyHandler

    // 只在容器有有效尺寸时进行初始 fit，避免 display:none 时计算为 0x0
    const rect = container.getBoundingClientRect()
    if (rect.width > 0 && rect.height > 0) {
      fitAddon.fit()
      if (opts.resizeOnInitialFit) opts.onResized(terminal.cols, terminal.rows)
    }

    // 终端输入 → 通道，并在提交命令时收集历史
    terminal.onData((data) => {
      opts.write(data)
      if (data.includes('\r') || data.includes('\n')) {
        opts.completion().collect()
      }
    })

    // 选中文字自动复制到剪贴板 + 「问 AI」上下文登记
    terminal.onSelectionChange(() => {
      opts.onSelectionChange?.(terminal)
      const sel = terminalRef.value?.getSelection()
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
            opts.write(sanitized)
          }
          terminalRef.value?.focus()
        }
      }).catch(() => {})
    }
    terminal.element?.addEventListener('contextmenu', contextMenuHandler)

    // 监听容器大小变化：拖动分栏/窗口时 ResizeObserver 每帧触发，用 rAF 合并为
    // 每帧最多一次 fit + 一次 resize IPC；0 尺寸守卫避免 v-show 过渡瞬时发 (0,0)。
    let resizePending = false
    resizeObserver = new ResizeObserver(() => {
      if (!terminalRef.value || !fitAddon || !opts.isVisible() || resizePending) return
      resizePending = true
      requestAnimationFrame(() => {
        resizePending = false
        const t = terminalRef.value
        if (!t || !opts.isVisible()) return
        const beforeC = t.cols
        const beforeR = t.rows
        fitAddon.fit()
        const c = t.cols
        const r = t.rows
        if (c <= 0 || r <= 0) return
        if (c !== beforeC || r !== beforeR) {
          opts.onResized(c, r)
        }
      })
    })
    resizeObserver.observe(container)
  })

  // 可见性变化时重新适配
  watch(opts.isVisible, (visible) => {
    // 清理上一轮的定时器，避免组件卸载后仍执行回调
    if (visTimer) clearTimeout(visTimer)
    // 终端被切换隐藏时，立即关闭补全浮层，避免浮层残留
    if (!visible) opts.completion().hide()
    if (visible && fitAddonRef.value) {
      visTimer = setTimeout(() => {
        visTimer = null
        // 回调开头守卫：组件可能已卸载
        if (!opts.containerRef.value) return
        fitAddonRef.value!.fit()
        const t = terminalRef.value
        if (t) opts.onResized(t.cols, t.rows)
      }, 50)
    }
  })

  onUnmounted(() => {
    const terminal = terminalRef.value
    // contextmenu 与 docHandler 的移除必须早于 dispose：dispose 后
    // terminal.element 可能已失效，届时守卫会静默失效、监听器泄漏
    if (contextMenuHandler && terminal?.element) {
      terminal.element.removeEventListener('contextmenu', contextMenuHandler)
    }
    const docHandler = (terminal as unknown as { __docKeyHandler?: (e: KeyboardEvent) => void })?.__docKeyHandler
    if (docHandler) {
      document.removeEventListener('keydown', docHandler, { capture: true } as any)
    }
    resizeObserver?.disconnect()
    // 组件侧的数据面退订 / 额外清理：必须早于 dispose，否则 dispose 后
    // 仍有 IPC 回调向死实例 write
    opts.onBeforeDispose?.()
    unsubTermConfig?.()
    terminal?.dispose()
    searchAddonRef.value = null
    fitAddonRef.value = null
    // 审查修复（P2）：卸载后实例置 null —— 只 dispose 不清引用的话，卸载窗口内
    // 补全/渲染回调的守卫失效，会在已 dispose 的实例上操作
    terminalRef.value = null
    if (visTimer) clearTimeout(visTimer)
  })

  return { getTerminal, searchAddonRef, searchOpen, closeSearch, focus }
}
