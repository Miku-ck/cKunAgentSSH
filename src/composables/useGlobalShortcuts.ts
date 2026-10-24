// useGlobalShortcuts.ts — 全局键盘快捷键（M1-P0）
//
// 设计约束：只劫持带 Ctrl/Cmd 修饰的组合，且必须避开 shell 控制字符冲突 ——
//   ^W 是 readline 删词、^B 是光标后退、^K 是删到行尾，终端里按下去必须原样
//   进 shell。所以关闭/AI 首页/命令面板统一用 Shift 档（Ctrl+Shift+W/B/P），
//   这与 Windows Terminal 的既定习惯（Ctrl+Shift+W 关标签、Ctrl+Shift+P 面板）一致。
// 事件走 window 冒泡相监听：xterm 的隐藏 textarea 不 stopPropagation 这些组合键。
import { onMounted, onUnmounted, reactive } from 'vue'
import { useSSHStore } from '@/stores/ssh'
import type { NavPage } from './useSshNavigation'

/**
 * 独立页 → Ctrl+Shift+数字 的映射（单一来源，TitleBar 的 title 提示也引用它）。
 *
 * 这里刻意**不存**页名文案：标题条的 aria-label、命令面板的「打开：×」条目各有
 * 自己的措辞（同一页在三处是三种写法），把它们并成一份会直接改掉用户读到的字。
 * 这份表管的是「哪些页有快捷键、分别是第几号」—— 页的清单由守卫
 * （src/components/common/command-palette-pages.test.ts）与命令面板对账。
 */
export const PAGE_SHORTCUTS: ReadonlyArray<{
  page: Exclude<NavPage, 'home'>
  num: number
}> = [
  { page: 'remote-mgr', num: 1 },
  { page: 'sftp', num: 2 },
  { page: 'dashboard', num: 3 },
  { page: 'schedules', num: 4 },
  { page: 'tunnels', num: 5 },
  { page: 'docker', num: 6 },
  { page: 'git', num: 7 },
  { page: 'audit', num: 8 },
]

/** 「监控仪表盘 (Ctrl+Shift+3)」形式的 title 提示，供 TitleBar 引用 */
export function pageShortcutHint(page: Exclude<NavPage, 'home'>): string {
  const hit = PAGE_SHORTCUTS.find(p => p.page === page)
  return hit ? ` (Ctrl+Shift+${hit.num})` : ''
}

/** MRU 浮层的共享状态（快捷键侧写、MruSwitcher.vue 读） */
export const mruSwitcher = reactive({ open: false, index: 0 })

/** 打开浮层时的初始高亮：恒为「上一个会话」（栈顶下一位） */
export function mruInitialIndex(len: number): number {
  return len > 1 ? 1 : 0
}

/** 按住 Ctrl 连点 Tab：在 MRU 栈内循环移动 */
export function mruStep(len: number, index: number, dir: 1 | -1): number {
  return (index + dir + len) % len
}

export interface ShortcutDeps {
  /** 切到指定终端会话（含焦点处理，来自 useSshNavigation.activateTerminal） */
  activateTerminal: (id: string) => void
  /** 关闭当前活动会话（Ctrl+Shift+W） */
  closeActiveSession: () => void
  /** 回到 AI 首页任务台（Ctrl+Shift+B） */
  goAiHome: () => void
  toggleNavPage: (page: Exclude<NavPage, 'home'>) => void
  togglePalette: () => void
  isPaletteOpen: () => boolean
  openSettings: () => void
}

export function useGlobalShortcuts(deps: ShortcutDeps): { mruSwitcher: typeof mruSwitcher } {
  const store = useSSHStore()

  /** 当前参与切换的 MRU 会话序列（剔除已关闭残留） */
  function mruSessionIds(): string[] {
    const alive = new Set(store.sessions.map(s => s.id))
    return store.mruIds.filter(id => alive.has(id))
  }

  function mruCommit(): void {
    const ids = mruSessionIds()
    const target = ids[mruSwitcher.index]
    mruSwitcher.open = false
    mruSwitcher.index = 0
    if (target) deps.activateTerminal(target)
  }

  function mruCancel(): void {
    mruSwitcher.open = false
    mruSwitcher.index = 0
  }

  function onKeyup(e: KeyboardEvent): void {
    // 松开 Ctrl = 落定：切到高亮会话
    if ((e.key === 'Control' || e.key === 'Meta') && mruSwitcher.open) mruCommit()
  }

  function onBlur(): void {
    // 窗口失焦时 Ctrl 的 keyup 可能丢失：直接落定，别留一个关不掉的浮层
    if (mruSwitcher.open) mruCommit()
  }

  function handler(e: KeyboardEvent): void {
    if (e.key === 'Escape' && mruSwitcher.open) {
      e.preventDefault()
      mruCancel()
      return
    }
    if (!(e.ctrlKey || e.metaKey)) return

    // 命令面板打开期间只放行自己的开关键，其余快捷键交给面板内部处理
    if (deps.isPaletteOpen()) {
      if (e.ctrlKey && e.shiftKey && (e.key === 'P' || e.key === 'p')) {
        e.preventDefault()
        deps.togglePalette()
      }
      return
    }

    // ── 设置（Ctrl+, 通用约定）──
    if (e.key === ',') {
      e.preventDefault()
      deps.openSettings()
      return
    }

    // ── 终端会话 ──
    if (e.key === 'Tab') {
      e.preventDefault()
      const dir = e.shiftKey ? -1 : 1
      if (!mruSwitcher.open) {
        // 单会话保持旧行为：直接切过去（首页没有标签条，这是唯一可见入口）
        if (store.sessions.length <= 1) {
          const only = store.sessions[0]
          if (only) deps.activateTerminal(only.id)
          return
        }
        mruSwitcher.open = true
        mruSwitcher.index = mruInitialIndex(mruSessionIds().length)
        return
      }
      mruSwitcher.index = mruStep(mruSessionIds().length, mruSwitcher.index, dir)
      return
    }
    // 数字键一律认 e.code：Shift+数字的 e.key 是标点（Shift+1='!'），按 e.key 匹配必漏
    const digit = /^Digit([1-9])$/.exec(e.code)
    if (digit) {
      const n = Number(digit[1])
      if (!e.shiftKey) {
        const target = store.sessions[n - 1]
        if (target) {
          e.preventDefault()
          deps.activateTerminal(target.id)
        }
        return
      }
      const hit = PAGE_SHORTCUTS.find(p => p.num === n)
      if (hit) {
        e.preventDefault()
        deps.toggleNavPage(hit.page)
      }
      return
    }
    if (e.shiftKey && (e.key === 'W' || e.key === 'w')) {
      e.preventDefault()
      deps.closeActiveSession()
      return
    }
    if (e.shiftKey && (e.key === 'B' || e.key === 'b')) {
      e.preventDefault()
      deps.goAiHome()
      return
    }
    if (e.shiftKey && (e.key === 'P' || e.key === 'p')) {
      e.preventDefault()
      deps.togglePalette()
      return
    }
  }

  onMounted(() => {
    window.addEventListener('keydown', handler)
    window.addEventListener('keyup', onKeyup)
    window.addEventListener('blur', onBlur)
  })
  onUnmounted(() => {
    window.removeEventListener('keydown', handler)
    window.removeEventListener('keyup', onKeyup)
    window.removeEventListener('blur', onBlur)
    mruCancel()
  })

  return { mruSwitcher }
}
