/**
 * Monaco Editor Workers 配置
 * 在 main.ts 中 import 此文件 (必须在 Monaco 使用前)
 *
 * H13 修复：Monaco 主题跟随设置切换
 * 原实现仅启动时硬编码 setTheme('ckun-dark')，后续主题切换不生效
 * 现导出 applyMonacoTheme()，由 useAppearance.ts 在主题变化时调用
 */
import * as monaco from 'monaco-editor'
import editorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import jsonWorker from 'monaco-editor/esm/vs/language/json/json.worker?worker'
import cssWorker from 'monaco-editor/esm/vs/language/css/css.worker?worker'
import htmlWorker from 'monaco-editor/esm/vs/language/html/html.worker?worker'
import tsWorker from 'monaco-editor/esm/vs/language/typescript/ts.worker?worker'

self.MonacoEnvironment = {
  getWorker(_: string, label: string) {
    if (label === 'json') return new jsonWorker()
    if (label === 'css' || label === 'scss' || label === 'less') return new cssWorker()
    if (label === 'html' || label === 'handlebars' || label === 'razor') return new htmlWorker()
    if (label === 'typescript' || label === 'javascript') return new tsWorker()
    return new editorWorker()
  },
}

// ──────────────────────────────────────────────────────────────────────────────
// Monaco 主题定义 — 与 variables.css 的主题保持一致
// ──────────────────────────────────────────────────────────────────────────────

// 深色主题（默认）：editor.background 与 --bg-base (#000000) 一致
// Monaco 的编辑器背景需与应用背景统一，否则会有明显色差
monaco.editor.defineTheme('ckun-dark', {
  base: 'vs-dark',
  inherit: true,
  rules: [
    { token: 'comment', foreground: '6e7681' },
    { token: 'keyword', foreground: 'ff7b72' },
    { token: 'string', foreground: 'a5d6ff' },
    { token: 'number', foreground: '79c0ff' },
    { token: 'type', foreground: 'ffa657' },
  ],
  colors: {
    'editor.background': '#000000',
    'editor.foreground': '#d4d4d4',
    'editorLineNumber.foreground': '#858585',
    'editorLineNumber.activeForeground': '#c6c6c6',
    'editor.selectionBackground': '#264f78',
    'editor.lineHighlightBackground': '#1a1a1a',
    'editorCursor.foreground': '#aeafad',
    'editorWhitespace.foreground': '#3e3e42',
    'editorIndentGuide.background': '#1a1a1a',
    'editorIndentGuide.activeBackground': '#3e3e3e',
    'editorGutter.background': '#000000',
    'editorWidget.background': '#0a0a0a',
    'editorWidget.border': '#1a1a1a',
    'editorSuggestWidget.background': '#0a0a0a',
    'editorSuggestWidget.border': '#1a1a1a',
    'editorSuggestWidget.selectedBackground': '#094771',
    'minimap.background': '#000000',
  },
})

// 浅色主题：editor.background 与 --bg-base (#ffffff) 一致
monaco.editor.defineTheme('ckun-light', {
  base: 'vs',
  inherit: true,
  rules: [
    { token: 'comment', foreground: '6a737d' },
    { token: 'keyword', foreground: 'd73a49' },
    { token: 'string', foreground: '032f62' },
    { token: 'number', foreground: '005cc5' },
    { token: 'type', foreground: '6f42c1' },
  ],
  colors: {
    'editor.background': '#ffffff',
    'editor.foreground': '#1e1e1e',
    'editorLineNumber.foreground': '#888888',
    'editorLineNumber.activeForeground': '#1e1e1e',
    'editor.selectionBackground': '#add6ff',
    'editor.lineHighlightBackground': '#f0f0f0',
    'editorCursor.foreground': '#000000',
    'editorWhitespace.foreground': '#d0d0d0',
    'editorIndentGuide.background': '#eaeaea',
    'editorIndentGuide.activeBackground': '#b0b0b0',
    'editorGutter.background': '#ffffff',
    'editorWidget.background': '#f5f5f5',
    'editorWidget.border': '#d0d0d0',
    'editorSuggestWidget.background': '#f5f5f5',
    'editorSuggestWidget.border': '#d0d0d0',
    'editorSuggestWidget.selectedBackground': '#e0e0e0',
    'minimap.background': '#ffffff',
  },
})

// 蓝屏主题：editor.background 与 --bg-base (#0a1628) 一致
monaco.editor.defineTheme('ckun-blue-screen', {
  base: 'vs-dark',
  inherit: true,
  rules: [
    { token: 'comment', foreground: '5a7898' },
    { token: 'keyword', foreground: '8aa8c8' },
    { token: 'string', foreground: 'a8c8e8' },
    { token: 'number', foreground: '79c0ff' },
    { token: 'type', foreground: 'ffa657' },
  ],
  colors: {
    'editor.background': '#0a1628',
    'editor.foreground': '#c8ddf0',
    'editorLineNumber.foreground': '#5a7898',
    'editorLineNumber.activeForeground': '#c8ddf0',
    'editor.selectionBackground': '#1e3a5f',
    'editor.lineHighlightBackground': '#152d50',
    'editorCursor.foreground': '#c8ddf0',
    'editorWhitespace.foreground': '#1e3a5f',
    'editorIndentGuide.background': '#152d50',
    'editorIndentGuide.activeBackground': '#3a5a8a',
    'editorGutter.background': '#0a1628',
    'editorWidget.background': '#0a1628',
    'editorWidget.border': '#1e3a5f',
    'editorSuggestWidget.background': '#0a1628',
    'editorSuggestWidget.border': '#1e3a5f',
    'editorSuggestWidget.selectedBackground': '#1e3a5f',
    'minimap.background': '#0a1628',
  },
})

// 默认主题
monaco.editor.setTheme('ckun-dark')

// ──────────────────────────────────────────────────────────────────────────────
// 主题切换函数 — 供 useAppearance.ts 调用
// ──────────────────────────────────────────────────────────────────────────────

type AppTheme = 'dark' | 'light' | 'system' | 'blue-screen'

const THEME_MAP: Record<string, 'ckun-dark' | 'ckun-light' | 'ckun-blue-screen'> = {
  dark: 'ckun-dark',
  light: 'ckun-light',
  'blue-screen': 'ckun-blue-screen',
}

export function applyMonacoTheme(theme: AppTheme): void {
  let monacoTheme: 'ckun-dark' | 'ckun-light' | 'ckun-blue-screen'
  if (theme === 'system') {
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
    monacoTheme = prefersDark ? 'ckun-dark' : 'ckun-light'
  } else {
    monacoTheme = THEME_MAP[theme] || 'ckun-dark'
  }
  monaco.editor.setTheme(monacoTheme)
}

export default monaco
