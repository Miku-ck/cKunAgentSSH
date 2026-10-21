import { watch, onScopeDispose, getCurrentScope } from 'vue'
import type { Terminal, ITerminalOptions, ITheme } from '@xterm/xterm'
import type { FitAddon } from '@xterm/addon-fit'
import { useSettingsStore } from '@/stores/settings'
import { ACCENT_MAP, isHexColor, type Theme } from '@/utils/appearance-tokens'

/** 终端字号允许范围（与设置面板滑块一致），防御 localStorage 越界值 */
const MIN_FONT_SIZE = 12
const MAX_FONT_SIZE = 18
function clampFontSize(size: number): number {
  return Math.max(MIN_FONT_SIZE, Math.min(MAX_FONT_SIZE, size))
}

/** 终端字体栈映射：key 为设置存储值，value 为 CSS font-family 字符串 */
const TERMINAL_FONT_FAMILIES: Record<string, string> = {
  jetbrains: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'Consolas', monospace",
  fira: "'Fira Code', 'JetBrains Mono', 'Cascadia Code', 'Consolas', monospace",
  cascadia: "'Cascadia Code', 'Cascadia Mono', 'JetBrains Mono', 'Consolas', monospace",
  cascadiaMono: "'Cascadia Mono', 'Cascadia Code', 'Consolas', monospace",
  consolas: "'Consolas', 'JetBrains Mono', monospace",
  menlo: "'Menlo', 'Monaco', 'Consolas', monospace",
  monospace: 'monospace',
}

const DEFAULT_FONT_KEY = 'jetbrains'

function getTerminalFontFamily(key: string): string {
  return TERMINAL_FONT_FAMILIES[key] ?? TERMINAL_FONT_FAMILIES[DEFAULT_FONT_KEY]
}

/**
 * 终端正文前景色 —— ANSI 主题固定暗色，这条不随主题走。
 * 单列导出是因为设置面板要拿它算对比度：用户自选的终端纯色底如果和它太近，
 * 终端文字就没了（见 AppearancePanel 的终端背景提示）。
 */
export const TERMINAL_FOREGROUND = '#e6edf3'

/** 基线终端暗色主题：背景透明以跟随全局背景，只控制文字/光标/ANSI 颜色 */
const TERMINAL_BASE: ITheme = {
  background: 'transparent',
  foreground: TERMINAL_FOREGROUND,
  cursor: '#58a6ff',
  cursorAccent: '#111111',
  selectionBackground: '#264f78',
  black: '#111111',
  red: '#f85149',
  green: '#3fb950',
  yellow: '#d29922',
  blue: '#58a6ff',
  magenta: '#bc8cff',
  cyan: '#39d2c0',
  white: '#e6edf3',
  brightBlack: '#6e7681',
  brightRed: '#ff7b72',
  brightGreen: '#7ee787',
  brightYellow: '#e3b341',
  brightBlue: '#79c0ff',
  brightMagenta: '#d2a8ff',
  brightCyan: '#56d4dd',
  brightWhite: '#f0f6fc',
}

export type ResolvedTheme = Exclude<Theme, 'system'>

/** system 主题的运行时解析：浏览器读媒体查询，node（测试环境无 matchMedia）回落 dark */
export function resolveThemeName(theme: Theme): ResolvedTheme {
  if (theme !== 'system') return theme
  if (typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches) return 'dark'
  return 'dark'
}

/**
 * 每主题一套 ANSI 16 色：终端底恒深（--bg-terminal 契约不动），只调色相温度。
 * dark/light 共用基线（中性界面不配色偏）；未列的键继承基线。
 * 守卫：terminal-palettes.test.ts 钉住键完整性、对比度与 dark/light 基线。
 */
export const TERMINAL_THEMES: Record<ResolvedTheme, ITheme> = {
  dark: TERMINAL_BASE,
  light: TERMINAL_BASE,
  'blue-screen': {
    ...TERMINAL_BASE,
    blue: '#6cb6ff', brightBlue: '#8ecbff',
    cyan: '#4fd8e0', brightCyan: '#76e4ea',
    magenta: '#c8a1ff', green: '#56d364',
  },
  forest: {
    ...TERMINAL_BASE,
    red: '#ff6a55', green: '#7ed491', brightGreen: '#97e3a8',
    yellow: '#e0c060', blue: '#82aaff', cyan: '#4fd6be',
  },
  mint: {
    ...TERMINAL_BASE,
    red: '#fb7185', green: '#6ee7b7', brightGreen: '#8ff0c8',
    yellow: '#f0c674', blue: '#7dd3fc', magenta: '#c4b5fd',
    cyan: '#5eead4', brightCyan: '#86f0e0',
  },
  bee: {
    ...TERMINAL_BASE,
    red: '#ff7a66', green: '#9ece6a', brightGreen: '#b5e085',
    yellow: '#ffc777', brightYellow: '#ffd892',
    blue: '#82aaff', cyan: '#7dcfff',
  },
  coral: {
    ...TERMINAL_BASE,
    red: '#ff8a7a', brightRed: '#ffa496', magenta: '#f0a6c0',
    yellow: '#f0b866', green: '#a6d189', blue: '#8caaee', cyan: '#81c8be',
  },
  atelier: {
    ...TERMINAL_BASE,
    foreground: '#e8e2d5', white: '#e8e2d5', brightWhite: '#f5efe2',
    red: '#d67a76', brightRed: '#e3928e',
    green: '#9db384', brightGreen: '#b3c79b',
    yellow: '#d8b26a', brightYellow: '#e6c684',
    blue: '#7e9cc4', brightBlue: '#98b3d6',
    magenta: '#b594b6', brightMagenta: '#caadcc',
    cyan: '#7fb5ac', brightCyan: '#99c9c0',
    brightBlack: '#6e6a60',
  },
}

/**
 * hex 色 + 透明度百分比 → rgba 字符串。
 * 非法输入回落 'transparent'（= auto 模式的现状行为），不抛错——
 * 设置值可能被历史脏 localStorage 污染，渲染层只该降级不该炸。
 */
export function hexToRgba(hex: string, opacityPct: number): string {
  if (!isHexColor(hex)) return 'transparent'
  const int = parseInt(hex.slice(1), 16)
  const r = (int >> 16) & 255
  const g = (int >> 8) & 255
  const b = int & 255
  const a = Math.max(0, Math.min(100, opacityPct)) / 100
  return `rgba(${r}, ${g}, ${b}, ${a})`
}

/**
 * 按设置解析当前终端主题：主题色板定 16 个 ANSI 色，accent 覆盖 cursor/selection
 * （用户显式选的强调色优先；ACCENT_MAP 查不到时回落色板自带值）。
 * solid 背景模式再把用户色转 rgba 写进 background。
 */
export function resolveTerminalTheme(): ITheme {
  const store = useSettingsStore()
  const a = store.settings.appearance
  const palette = TERMINAL_THEMES[resolveThemeName(a.theme)]
  const accentHex = ACCENT_MAP[a.accent]?.hex
  const themed: ITheme = accentHex
    ? { ...palette, cursor: accentHex, selectionBackground: hexToRgba(accentHex, 35) }
    : palette
  const t = a.terminal
  if (t.backgroundMode !== 'solid') return themed
  return { ...themed, background: hexToRgba(t.backgroundColor, t.backgroundOpacity) }
}

/**
 * 构建 xterm Terminal 创建时的 options。
 * 字号/字体/背景从设置读取；ANSI 颜色固定暗色，背景可跟随全局或自定义纯色。
 */
export function buildTerminalOptions(extra?: ITerminalOptions): ITerminalOptions {
  const store = useSettingsStore()
  const t = store.settings.appearance.terminal
  return {
    theme: resolveTerminalTheme(),
    fontSize: clampFontSize(t.fontSize),
    fontFamily: getTerminalFontFamily(t.fontFamily),
    // 必须开启透明度，theme.background 才能使用透明/rgba 色，让全局背景图透出
    allowTransparency: true,
    ...extra,
  }
}

/**
 * 监听终端字号/字体/背景变化，动态应用到已创建的 terminal 实例（无需重建）。
 * 字号/字体变化时调用 fitAddon.fit() 重新计算行列数，并通过 onResized 通知宿主
 * 把新的 cols/rows 下发给 PTY —— 容器尺寸没变，ResizeObserver 不会触发，
 * 少了这一步则 vim/htop 等按尺寸渲染的程序会在换字号后串列。
 * 背景变化只换 theme 引用（xterm 需整体重赋才触发重绘），不影响单元格尺寸，
 * 不进 fit/resize 流程。
 *
 * @param isActive 终端是否当前可见（不可见时跳过 fit，避免 0x0 计算错误；
 *                 重新可见时宿主的 visible 监听会补一次 fit + resize）
 * @param onResized fit 之后回调，宿主在此下发 sshResize / ptyResize
 * @returns 取消监听函数
 */
export function watchTerminalConfig(
  terminal: Terminal,
  fitAddon: FitAddon,
  isActive: () => boolean,
  onResized?: () => void
): () => void {
  const store = useSettingsStore()
  const unsubFont = watch(
    [
      () => store.settings.appearance.terminal.fontSize,
      () => store.settings.appearance.terminal.fontFamily,
    ],
    ([size, family]) => {
      terminal.options.fontSize = clampFontSize(size)
      terminal.options.fontFamily = getTerminalFontFamily(family)
      // 字号/字体影响单元格尺寸，需重新 fit；仅在终端可见时执行避免 0x0
      if (!isActive()) return
      const { cols, rows } = terminal
      fitAddon.fit()
      // 网格未变时不打扰 PTY（同尺寸 SIGWINCH 会让部分 TUI 无谓重绘）
      if (terminal.cols !== cols || terminal.rows !== rows) onResized?.()
    }
  )
  const unsubBg = watch(
    [
      () => store.settings.appearance.terminal.backgroundMode,
      () => store.settings.appearance.terminal.backgroundColor,
      () => store.settings.appearance.terminal.backgroundOpacity,
      () => store.settings.appearance.theme,
      () => store.settings.appearance.accent,
    ],
    () => {
      // 整体替换 theme 对象引用，xterm 才会重新解析颜色并触发重绘
      terminal.options.theme = resolveTerminalTheme()
    }
  )
  const stop = () => { unsubFont(); unsubBg() }
  // 若处于组件 setup 的作用域中（终端组件场景），作用域销毁时自动释放 watcher，
  // 避免终端反复开关泄漏；否则退回返回显式 disposer 由调用方处理。
  if (getCurrentScope()) onScopeDispose(stop)
  return stop
}

/**
 * 终端内搜索的三枚装饰色：从当前 accent 派生（SearchAddon 的装饰直接画在
 * canvas/overview ruler 上，吃不到 CSS 令牌，只能 JS 侧给值）。
 * match 用 40% 透明弱化普通命中，active/border 用实色标当前命中。
 */
export function accentSearchColors(): { match: string; active: string; border: string } {
  const accentHex = ACCENT_MAP[useSettingsStore().settings.appearance.accent]?.hex
  if (!accentHex) return { match: 'rgba(88, 166, 255, 0.4)', active: '#58a6ff', border: '#58a6ff' }
  return { match: hexToRgba(accentHex, 40), active: accentHex, border: accentHex }
}

/** 设置面板可用的终端字体选项 */
export const TERMINAL_FONT_OPTIONS: { value: string; label: string }[] = [  { value: 'jetbrains', label: 'JetBrains Mono' },
  { value: 'fira', label: 'Fira Code' },
  { value: 'cascadia', label: 'Cascadia Code' },
  { value: 'cascadiaMono', label: 'Cascadia Mono' },
  { value: 'consolas', label: 'Consolas' },
  { value: 'menlo', label: 'Menlo' },
  { value: 'monospace', label: '系统等宽' },
]
