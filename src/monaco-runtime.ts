/**
 * Monaco 重负载运行时 — 编辑器核心 + 4 worker + ~30 种语言语法 + 主题定义。
 * 只允许经 monaco-setup.loadMonaco() 动态 import（首次打开文件编辑器时），
 * 禁止被启动路径静态引用：它曾是主包 ~10MB 的主要来源。
 *
 * 主题名解析与懒同步在 monaco-setup（轻量 shim）里做；本模块加载即完成
 * worker 配置与 defineTheme 注册（模块副作用），并置 monaco-loaded 标志。
 */
// ──────────────────────────────────────────────────────────────────────────────
// 按需加载：核心 API + 运维常用语言（原全量入口 editor.main 打包 85 种语言，
// 体积膨胀约 500KB+；现仅保留 language.ts 实际映射的语言）
// ──────────────────────────────────────────────────────────────────────────────
import * as monaco from 'monaco-editor/esm/vs/editor/editor.api.js'
import editorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import jsonWorker from 'monaco-editor/esm/vs/language/json/json.worker?worker'
import cssWorker from 'monaco-editor/esm/vs/language/css/css.worker?worker'
import htmlWorker from 'monaco-editor/esm/vs/language/html/html.worker?worker'
import tsWorker from 'monaco-editor/esm/vs/language/typescript/ts.worker?worker'

// 富语言（语言服务 + worker）
import 'monaco-editor/esm/vs/language/json/monaco.contribution.js'
import 'monaco-editor/esm/vs/language/typescript/monaco.contribution.js'
import 'monaco-editor/esm/vs/language/css/monaco.contribution.js'
import 'monaco-editor/esm/vs/language/html/monaco.contribution.js'

// 语法高亮（Monarch）— 与 utils/language.ts 的扩展名映射一一对应
// plaintext / makefile 为 core 内置语言，无需导入
import 'monaco-editor/esm/vs/basic-languages/bat/bat.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/cpp/cpp.contribution.js' // 注册 c + cpp
import 'monaco-editor/esm/vs/basic-languages/csharp/csharp.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/css/css.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/dockerfile/dockerfile.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/go/go.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/graphql/graphql.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/html/html.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/ini/ini.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/java/java.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/javascript/javascript.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/kotlin/kotlin.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/less/less.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/markdown/markdown.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/php/php.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/powershell/powershell.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/python/python.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/ruby/ruby.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/rust/rust.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/scala/scala.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/scss/scss.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/shell/shell.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/sql/sql.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/swift/swift.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/typescript/typescript.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/xml/xml.contribution.js'
import 'monaco-editor/esm/vs/basic-languages/yaml/yaml.contribution.js'

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

// 森林主题：editor.background 与 --bg-base (#0b1a12) 一致
monaco.editor.defineTheme('ckun-forest', {
  base: 'vs-dark',
  inherit: true,
  rules: [
    { token: 'comment', foreground: '6d8a76' },
    { token: 'keyword', foreground: '7ee2a8' },
    { token: 'string', foreground: 'a5d6ff' },
    { token: 'number', foreground: '79c0ff' },
    { token: 'type', foreground: 'ffa657' },
  ],
  colors: {
    'editor.background': '#0b1a12',
    'editor.foreground': '#d3e8d9',
    'editorLineNumber.foreground': '#6d8a76',
    'editorLineNumber.activeForeground': '#d3e8d9',
    'editor.selectionBackground': '#2a4a36',
    'editor.lineHighlightBackground': '#1d3526',
    'editorCursor.foreground': '#d3e8d9',
    'editorWhitespace.foreground': '#2a4a36',
    'editorIndentGuide.background': '#1d3526',
    'editorIndentGuide.activeBackground': '#3a5a44',
    'editorGutter.background': '#0b1a12',
    'editorWidget.background': '#0b1a12',
    'editorWidget.border': '#2a4a36',
    'editorSuggestWidget.background': '#0b1a12',
    'editorSuggestWidget.border': '#2a4a36',
    'editorSuggestWidget.selectedBackground': '#2a4a36',
    'minimap.background': '#0b1a12',
  },
})

// 薄荷主题：editor.background 与 --bg-base (#0a1b1a) 一致
monaco.editor.defineTheme('ckun-mint', {
  base: 'vs-dark',
  inherit: true,
  rules: [
    { token: 'comment', foreground: '6b938e' },
    { token: 'keyword', foreground: '7eead9' },
    { token: 'string', foreground: 'a5d6ff' },
    { token: 'number', foreground: '79c0ff' },
    { token: 'type', foreground: 'ffa657' },
  ],
  colors: {
    'editor.background': '#0a1b1a',
    'editor.foreground': '#d0ece8',
    'editorLineNumber.foreground': '#6b938e',
    'editorLineNumber.activeForeground': '#d0ece8',
    'editor.selectionBackground': '#27514e',
    'editor.lineHighlightBackground': '#1c3a38',
    'editorCursor.foreground': '#d0ece8',
    'editorWhitespace.foreground': '#27514e',
    'editorIndentGuide.background': '#1c3a38',
    'editorIndentGuide.activeBackground': '#3f6b66',
    'editorGutter.background': '#0a1b1a',
    'editorWidget.background': '#0a1b1a',
    'editorWidget.border': '#27514e',
    'editorSuggestWidget.background': '#0a1b1a',
    'editorSuggestWidget.border': '#27514e',
    'editorSuggestWidget.selectedBackground': '#27514e',
    'minimap.background': '#0a1b1a',
  },
})

// 蜜蜂主题：editor.background 与 --bg-base (#1a1206) 一致
monaco.editor.defineTheme('ckun-bee', {
  base: 'vs-dark',
  inherit: true,
  rules: [
    { token: 'comment', foreground: '8f7d57' },
    { token: 'keyword', foreground: 'f5c542' },
    { token: 'string', foreground: 'a5d6ff' },
    { token: 'number', foreground: '79c0ff' },
    { token: 'type', foreground: 'ffa657' },
  ],
  colors: {
    'editor.background': '#1a1206',
    'editor.foreground': '#ece0c4',
    'editorLineNumber.foreground': '#8f7d57',
    'editorLineNumber.activeForeground': '#ece0c4',
    'editor.selectionBackground': '#57411f',
    'editor.lineHighlightBackground': '#3f2f18',
    'editorCursor.foreground': '#ece0c4',
    'editorWhitespace.foreground': '#57411f',
    'editorIndentGuide.background': '#3f2f18',
    'editorIndentGuide.activeBackground': '#6b5227',
    'editorGutter.background': '#1a1206',
    'editorWidget.background': '#1a1206',
    'editorWidget.border': '#57411f',
    'editorSuggestWidget.background': '#1a1206',
    'editorSuggestWidget.border': '#57411f',
    'editorSuggestWidget.selectedBackground': '#57411f',
    'minimap.background': '#1a1206',
  },
})

// 珊瑚主题：editor.background 与 --bg-base (#180d0b) 一致
monaco.editor.defineTheme('ckun-coral', {
  base: 'vs-dark',
  inherit: true,
  rules: [
    { token: 'comment', foreground: '92746e' },
    { token: 'keyword', foreground: 'ff9a8f' },
    { token: 'string', foreground: 'a5d6ff' },
    { token: 'number', foreground: '79c0ff' },
    { token: 'type', foreground: 'ffa657' },
  ],
  colors: {
    'editor.background': '#180d0b',
    'editor.foreground': '#f0dcd7',
    'editorLineNumber.foreground': '#92746e',
    'editorLineNumber.activeForeground': '#f0dcd7',
    'editor.selectionBackground': '#543028',
    'editor.lineHighlightBackground': '#3d211c',
    'editorCursor.foreground': '#f0dcd7',
    'editorWhitespace.foreground': '#543028',
    'editorIndentGuide.background': '#3d211c',
    'editorIndentGuide.activeBackground': '#6b3a30',
    'editorGutter.background': '#180d0b',
    'editorWidget.background': '#180d0b',
    'editorWidget.border': '#543028',
    'editorSuggestWidget.background': '#180d0b',
    'editorSuggestWidget.border': '#543028',
    'editorSuggestWidget.selectedBackground': '#543028',
    'minimap.background': '#180d0b',
  },
})

// 默认主题
monaco.editor.setTheme('ckun-dark')

// 主题名解析与切换在 monaco-setup（轻量 shim）中，保持单一来源；
// monaco-setup 的 loadMonaco() 按约定取 default 拿实例（本模块加载完成即
// 已带 worker 配置与 defineTheme 注册等副作用）
export default monaco
