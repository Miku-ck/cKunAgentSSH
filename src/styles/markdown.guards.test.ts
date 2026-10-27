/// <reference types="node" />
/**
 * markdown.css 共享视觉词汇守卫 —— spec 2026-10-27-schedule-history-render 后续修复
 * 钉：代码块三件套（wrapper/header/copy-btn）、表格、标题都在全局共享文件里，
 * 且颜色全走令牌。聊天与执行历史都消费 `.markdown-body`，这份样式缺一项，
 * 历史摘要就会退化成裸文本（复制按钮无布局、点不动）。
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

// vitest 对 .css 的 ?raw 会返回空模块（CSS 走独立转换），直接读源文件
const src = readFileSync(
  fileURLToPath(new URL('./markdown.css', import.meta.url)),
  'utf8',
).replace(/\r\n/g, '\n')

describe('markdown.css 共享渲染词汇', () => {
  it('代码块 wrapper / header / 语言 / 复制按钮齐全', () => {
    for (const sel of ['.code-block-wrapper', '.code-block-header', '.code-lang', '.code-copy-btn']) {
      expect(src, `缺少 ${sel}`).toContain(sel)
    }
  })

  it('表格/引用/分隔/标题都有容器级规则', () => {
    for (const sel of ['.markdown-body table', '.markdown-body th', '.markdown-body blockquote', '.markdown-body hr', '.markdown-body h1']) {
      expect(src, `缺少 ${sel}`).toContain(sel)
    }
  })

  it('颜色全走令牌，无字面量 hex/rgba', () => {
    expect(src).not.toMatch(/#[0-9a-fA-F]{3,8}\b/)
    expect(src).not.toMatch(/\brgba?\(/)
    expect(src).toContain('var(--')
  })
})

describe('hljs 语法着色', () => {
  // renderMarkdown 用 highlight.js 产出 hljs-* 类；没有规则这些类就全部无色
  it('七类常用 token 都有配色', () => {
    for (const sel of ['.hljs-keyword', '.hljs-string', '.hljs-number', '.hljs-title', '.hljs-variable', '.hljs-built_in', '.hljs-comment']) {
      expect(src, `缺少 ${sel}`).toContain(sel)
    }
  })
  it('着色挂在 .markdown-body 容器下，不污染全局', () => {
    expect(src).toContain('.markdown-body .hljs-keyword')
  })
  it('注释/强调不是裸色块（保留可读的字体样式）', () => {
    expect(src).toMatch(/\.hljs-comment[^{]*\{[^}]*font-style:\s*italic/)
  })
})
