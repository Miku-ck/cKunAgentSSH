/**
 * Markdown 渲染工具 — 统一 marked + highlight.js + DOMPurify 净化
 * 所有 v-html 渲染 AI/用户内容的地方都应使用此模块，防止 XSS。
 */
import { marked } from 'marked'
import hljs from 'highlight.js'
import DOMPurify from 'dompurify'

marked.setOptions({
  breaks: true,
  gfm: true,
})

// C40 修复：DOMPurify 放行 target 属性但未强制 rel —
// target="_blank" 的链接缺 rel 会造成 reverse tabnabbing。
// 用 afterSanitizeAttributes hook（sanitize 过程中、输出 HTML 前执行，
// 比 AFTER_SANITIZE 字符串后处理更稳妥）给所有 target="_blank" 的 a 补 rel
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A' && node.getAttribute('target') === '_blank') {
    const rel = node.getAttribute('rel')
    node.setAttribute(
      'rel',
      rel ? `${rel} noopener noreferrer`.trim() : 'noopener noreferrer',
    )
  }
})

/** 反转义 marked 输出的 HTML 实体，便于 hljs 重新高亮 */
function decodeHtml(s: string): string {
  return s
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
}

/**
 * 将 markdown 渲染为安全的 HTML 字符串。
 * - 代码块用 highlight.js 高亮
 * - 每个代码块包裹在 .code-block-wrapper 中，附带语言标签和复制按钮
 * - 整体经 DOMPurify 净化，移除 onerror/onclick 等危险属性与 <script> 标签
 */
export function renderMarkdown(text: string): string {
  if (!text) return ''
  try {
    const html = marked.parse(text, { async: false }) as string
    const highlighted = html.replace(
      /<pre><code class="language-(\w*)">([\s\S]*?)<\/code><\/pre>/g,
      (_match, lang, code) => {
        const decoded = decodeHtml(code)
        let highlightedCode: string
        try {
          highlightedCode = lang && hljs.getLanguage(lang)
            ? hljs.highlight(decoded, { language: lang }).value
            : hljs.highlightAuto(decoded).value
        } catch {
          highlightedCode = code
        }
        const langLabel = lang || 'text'
        return `<div class="code-block-wrapper">` +
          `<div class="code-block-header">` +
          `<span class="code-lang">${langLabel}</span>` +
          `<button class="code-copy-btn" type="button">复制</button>` +
          `</div>` +
          `<pre><code class="hljs language-${lang}">${highlightedCode}</code></pre>` +
          `</div>`
      },
    )
    return DOMPurify.sanitize(highlighted, {
      ADD_ATTR: ['class', 'target'],
    })
  } catch (e) {
    console.warn('[renderMarkdown] failed, falling back to sanitized plain text:', e)
    return DOMPurify.sanitize(text)
  }
}
