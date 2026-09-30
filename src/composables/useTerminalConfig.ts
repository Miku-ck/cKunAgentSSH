import { watch } from 'vue'
import type { Terminal, ITerminalOptions, ITheme } from '@xterm/xterm'
import type { FitAddon } from '@xterm/addon-fit'
import { useSettingsStore } from '@/stores/settings'
import { isHexColor } from '@/utils/appearance-tokens'

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

/** 统一的终端暗色主题：背景透明以跟随全局背景，只控制文字/光标/ANSI 颜色 */
const TERMINAL_DARK_THEME: ITheme = {
  background: 'transparent',
  foreground: '#e6edf3',
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
 * 按设置解析当前终端主题：auto 返回原暗色主题（背景透明，跟随主题底色/全局背景图），
 * solid 把用户色转 rgba 写进 background。build 与 watch 共用，避免两处口径漂。
 */
export function resolveTerminalTheme(): ITheme {
  const store = useSettingsStore()
  const t = store.settings.appearance.terminal
  if (t.backgroundMode !== 'solid') return TERMINAL_DARK_THEME
  return { ...TERMINAL_DARK_THEME, background: hexToRgba(t.backgroundColor, t.backgroundOpacity) }
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
    ],
    () => {
      // 整体替换 theme 对象引用，xterm 才会重新解析颜色并触发重绘
      terminal.options.theme = resolveTerminalTheme()
    }
  )
  return () => { unsubFont(); unsubBg() }
}

/** 设置面板可用的终端字体选项 */
export const TERMINAL_FONT_OPTIONS: { value: string; label: string }[] = [
  { value: 'jetbrains', label: 'JetBrains Mono' },
  { value: 'fira', label: 'Fira Code' },
  { value: 'cascadia', label: 'Cascadia Code' },
  { value: 'cascadiaMono', label: 'Cascadia Mono' },
  { value: 'consolas', label: 'Consolas' },
  { value: 'menlo', label: 'Menlo' },
  { value: 'monospace', label: '系统等宽' },
]
