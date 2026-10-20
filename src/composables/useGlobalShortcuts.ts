// useGlobalShortcuts.ts — 全局键盘快捷键（M1-P0）
//
// 设计约束：只劫持带 Ctrl/Cmd 修饰的组合，且必须避开 shell 控制字符冲突 ——
//   ^W 是 readline 删词、^B 是光标后退、^K 是删到行尾，终端里按下去必须原样
//   进 shell。所以关闭/AI 首页/命令面板统一用 Shift 档（Ctrl+Shift+W/B/P），
//   这与 Windows Terminal 的既定习惯（Ctrl+Shift+W 关标签、Ctrl+Shift+P 面板）一致。
// 事件走 window 冒泡相监听：xterm 的隐藏 textarea 不 stopPropagation 这些组合键。
import { onMounted, onUnmounted } from 'vue'
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
  { page: 'github', num: 7 },
  { page: 'audit', num: 8 },
]

/** 「监控仪表盘 (Ctrl+Shift+3)」形式的 title 提示，供 TitleBar 引用 */
export function pageShortcutHint(page: Exclude<NavPage, 'home'>): string {
  const hit = PAGE_SHORTCUTS.find(p => p.page === page)
  return hit ? ` (Ctrl+Shift+${hit.num})` : ''
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

export function useGlobalShortcuts(deps: ShortcutDeps): void {
  const store = useSSHStore()

  function cycleSession(dir: 1 | -1): void {
    const sessions = store.sessions
    if (sessions.length < 2) return
    const idx = sessions.findIndex(s => s.id === store.activeSessionId)
    const next = sessions[(idx + dir + sessions.length) % sessions.length]
    deps.activateTerminal(next.id)
  }

  function handler(e: KeyboardEvent): void {
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
      cycleSession(e.shiftKey ? -1 : 1)
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

  onMounted(() => window.addEventListener('keydown', handler))
  onUnmounted(() => window.removeEventListener('keydown', handler))
}
