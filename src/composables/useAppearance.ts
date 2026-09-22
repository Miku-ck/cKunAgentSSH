import { watch } from 'vue'
import { useSettingsStore } from '@/stores/settings'
// H13 修复：Monaco 主题跟随设置切换
// 原实现 Monaco 主题在启动时硬编码，不随设置变化
import { applyMonacoTheme } from '@/monaco-setup'

// 外观枚举的唯一来源：settings store 的字段类型与设置面板的选项都从这里派生。
// 此前 Theme/Accent 在 store 与面板里各抄一份，面板色板还另抄了一份 hex ——
// 已经抄错：蓝色 swatch 显示 #58a6ff，实际写进 --accent-primary 的是 #0078d4。
export type Theme = 'dark' | 'light' | 'system' | 'blue-screen' | 'forest' | 'mint' | 'bee' | 'coral'
export type Accent = 'blue' | 'purple' | 'green' | 'orange' | 'white' | 'forest' | 'mint' | 'bee' | 'coral'
export type GradientMode = 'glow' | 'linear' | 'radial'
export type GradientDirection = 'tb' | 'bt' | 'lr' | 'rl' | 'tlbr' | 'trbl'

/** 强调色 → CSS 变量值（面板色板与 applyAccent 共用同一张表） */
export const ACCENT_MAP: Record<Accent, { hex: string; rgb: string }> = {
  blue:   { hex: '#0078d4', rgb: '0, 120, 212' },
  purple: { hex: '#c586c0', rgb: '197, 134, 192' },
  green:  { hex: '#4ec9b0', rgb: '78, 201, 176' },
  orange: { hex: '#ce9178', rgb: '206, 145, 120' },
  white:  { hex: '#ffffff', rgb: '255, 255, 255' },
  forest: { hex: '#2ea44f', rgb: '46, 164, 79' },
  mint:   { hex: '#5eead4', rgb: '94, 234, 212' },
  bee:    { hex: '#f5c542', rgb: '245, 197, 66' },
  coral:  { hex: '#ff7b72', rgb: '255, 123, 114' },
}

// 修复：模块级标记，确保 darkMq 监听器只添加一次（多实例共享场景）
let _systemThemeListenerAdded = false

// 渐变方向 → linear-gradient 角度
const GRADIENT_ANGLE: Record<GradientDirection, string> = {
  tb: '180deg',
  bt: '0deg',
  lr: '90deg',
  rl: '270deg',
  tlbr: '135deg',
  trbl: '225deg',
}

// 渐变方向 → 放射起点位置
const RADIAL_POS: Record<GradientDirection, string> = {
  tb: 'center',
  bt: 'center',
  lr: 'center',
  rl: 'center',
  tlbr: 'top left',
  trbl: 'top right',
}

export function useAppearance() {
  const store = useSettingsStore()
  const root = document.documentElement

  // ── 系统主题媒体查询 ──
  const darkMq = window.matchMedia('(prefers-color-scheme: dark)')

  function applySystemTheme() {
    if (store.settings.appearance.theme !== 'system') return
    root.setAttribute('data-theme', darkMq.matches ? 'dark' : 'light')
    // H13 修复：系统主题变化时同步 Monaco 主题
    applyMonacoTheme('system')
  }

  const systemHandler = () => applySystemTheme()
  // 监听器是模块级单例（多实例共享场景）：只注册一次且随应用存活，不随实例卸载移除
  // —— matchMedia 每次返回新对象，移除后再次调用本函数不会重新挂上，系统主题切换会永久失效
  if (!_systemThemeListenerAdded) {
    darkMq.addEventListener('change', systemHandler)
    _systemThemeListenerAdded = true
  }

  // ── 主题 ──
  function applyTheme(theme: Theme) {
    if (theme === 'system') {
      applySystemTheme()
    } else if (theme === 'dark') {
      root.removeAttribute('data-theme')
      // H13 修复：同步切换 Monaco 主题
      applyMonacoTheme('dark')
    } else {
      root.setAttribute('data-theme', theme)
      // H13 修复：同步切换 Monaco 主题（light / blue-screen）
      applyMonacoTheme(theme)
    }
  }

  // ── 强调色 ──
  // 单一事实源：--accent-primary。历史上另有同语义的 --accent-blue（被 60+ 处样式引用、
  // 值硬编码为蓝），切换强调色时不会跟随 —— 已全部合并到本令牌。
  function applyAccent(accent: Accent) {
    const v = ACCENT_MAP[accent]
    if (!v) return
    root.style.setProperty('--accent-primary', v.hex)
    root.style.setProperty('--accent-primary-rgb', v.rgb)
  }

  // ── 字号：界面基础字号（根元素）与消息正文字号是两套，分别应用 ──
  function applyUiFontSize(size: number) {
    root.style.fontSize = `${size}px`
  }

  // ── 动效 ──
  function applyAnimation(enabled: boolean) {
    root.setAttribute('data-animation', enabled ? 'true' : 'false')
  }

  // ── 自定义背景图 ──
  function applyBackground(enabled: boolean, opacity: number, overlay: number) {
    if (!enabled) {
      root.style.removeProperty('--app-bg-opacity')
      root.style.removeProperty('--app-bg-overlay')
      return
    }
    root.style.setProperty('--app-bg-opacity', `${opacity / 100}`)
    root.style.setProperty('--app-bg-overlay', `${overlay / 100}`)
  }

  // ── 主题渐变色 ──
  function applyThemeGradient() {
    const g = store.settings.appearance
    // localStorage 脏值（"abc" → v-model.number → NaN）会让 Math.max/min 都返回 NaN，
    // s <= 0 判 false 后走进渐变分支，把 NaN 拼进 CSS 变量 —— 渐变悄悄消失但属性上
    // 挂着非法值。显式折 0 走"移除"分支，与强度归零等价。
    const raw = g.themeGradientStrength
    const s = Number.isFinite(raw) ? Math.max(0, Math.min(100, raw)) : 0
    if (!g.themeGradientEnabled || s <= 0) {
      root.removeAttribute('data-gradient')
      root.style.removeProperty('--theme-gradient')
      root.style.removeProperty('--gradient-alpha')
      return
    }
    // 空色 = 跟随默认（强调色 / 主题底色），CSS 变量引用随主题自动联动
    const start = g.themeGradientStartColor || 'var(--accent-primary)'
    const end = g.themeGradientEndColor || 'var(--bg-base)'
    const tint = s * 0.18
    let gradient: string
    if (g.themeGradientMode === 'linear') {
      const angle = GRADIENT_ANGLE[g.themeGradientDirection] ?? '180deg'
      gradient = `linear-gradient(${angle}, ${start} 0%, ${end} 100%)`
    } else if (g.themeGradientMode === 'radial') {
      const pos = RADIAL_POS[g.themeGradientDirection] ?? 'center'
      gradient = `radial-gradient(circle at ${pos}, ${start} 0%, transparent 55%, ${end} 100%)`
    } else {
      // glow：顶部微光 → 透明过渡 → 主题底色（强度控制强调色混入比例）
      gradient = `linear-gradient(180deg, ${start} ${tint}%, transparent 28%, ${end} 100%)`
    }
    root.style.setProperty('--theme-gradient', gradient)
    // 面板半透明程度：强度越大越透（30→94%，100→80%），透出 body 渐变
    const alpha = 100 - s * 0.2
    root.style.setProperty('--gradient-alpha', `${alpha}%`)
    root.setAttribute('data-gradient', 'on')
  }

  // ── 消息字号 ──
  function applyMessageFontSize(size: number) {
    root.style.setProperty('--chat-font-size', `${size}px`)
  }

  // ─ 初始化（立即应用一次） ──
  const a = store.settings.appearance
  applyTheme(a.theme)
  applyAccent(a.accent)
  applyUiFontSize(a.uiFontSize)
  applyAnimation(a.animation)
  applyBackground(a.backgroundEnabled, a.backgroundOpacity, a.backgroundOverlay)
  applyThemeGradient()
  applyMessageFontSize(store.settings.chat.messageFontSize)

  // ── 响应式监听 ──
  watch(() => store.settings.appearance.theme, applyTheme)
  watch(() => store.settings.appearance.accent, applyAccent)
  watch(() => store.settings.appearance.uiFontSize, applyUiFontSize)
  watch(() => store.settings.appearance.animation, applyAnimation)
  watch(
    () => ({
      enabled: store.settings.appearance.backgroundEnabled,
      opacity: store.settings.appearance.backgroundOpacity,
      overlay: store.settings.appearance.backgroundOverlay,
    }),
    ({ enabled, opacity, overlay }) => {
      applyBackground(enabled, opacity, overlay)
    }
  )
  watch(
    () => ({
      enabled: store.settings.appearance.themeGradientEnabled,
      strength: store.settings.appearance.themeGradientStrength,
      mode: store.settings.appearance.themeGradientMode,
      direction: store.settings.appearance.themeGradientDirection,
      start: store.settings.appearance.themeGradientStartColor,
      end: store.settings.appearance.themeGradientEndColor,
    }),
    () => {
      applyThemeGradient()
    }
  )
  watch(() => store.settings.chat.messageFontSize, applyMessageFontSize)
}
