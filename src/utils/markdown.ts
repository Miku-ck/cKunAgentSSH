/**
 * Markdown 渲染工具 — 统一 marked + highlight.js + DOMPurify 净化
 * 所有 v-html 渲染 AI/用户内容的地方都应使用此模块，防止 XSS。
 */
import { marked } from 'marked'
import hljs from 'highlight.js/lib/core'
// 启动性能：只 core + 精选语言，绝不从 highlight.js 根入口全量引入（~190 语言实测
// 把首屏 useMarkdownDelegation chunk 撑到 1.65MB）。精选集覆盖运维终端高频场景
// （脚本/配置/IaC/查询/主流语言）；未注册语言仍走 highlightAuto，只是扫描集更小更快。
import bash from 'highlight.js/lib/languages/bash'
import shell from 'highlight.js/lib/languages/shell'
import python from 'highlight.js/lib/languages/python'
import json from 'highlight.js/lib/languages/json'
import yaml from 'highlight.js/lib/languages/yaml'
import ini from 'highlight.js/lib/languages/ini'
import dockerfile from 'highlight.js/lib/languages/dockerfile'
import nginx from 'highlight.js/lib/languages/nginx'
import go from 'highlight.js/lib/languages/go'
import sql from 'highlight.js/lib/languages/sql'
import javascript from 'highlight.js/lib/languages/javascript'
import typescript from 'highlight.js/lib/languages/typescript'
import diff from 'highlight.js/lib/languages/diff'
import plaintext from 'highlight.js/lib/languages/plaintext'
import DOMPurify from 'dompurify'

for (const [id, def] of [
  ['bash', bash], ['shell', shell], ['python', python], ['json', json],
  ['yaml', yaml], ['ini', ini], ['dockerfile', dockerfile], ['nginx', nginx],
  ['go', go], ['sql', sql], ['javascript', javascript], ['typescript', typescript],
  ['diff', diff], ['plaintext', plaintext],
] as const) {
  hljs.registerLanguage(id, def)
}

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

  // SVG 几何属性值合法性校验：模型输出的内联 SVG 偶见伪代码表达式（如 rx="2.5*u"），
  // DOMPurify 白名单只过滤标签/属性名，不校验值 —— 非法值落到 DOM 后 Chromium 报
  // "Expected length" 告警且该属性失效。这里对纯数字语义的属性做值校验，非法即删。
  if (node.namespaceURI?.startsWith('http://www.w3.org/2000/svg')) {
    for (const attr of SVG_LENGTH_ATTRS) {
      const val = node.getAttribute(attr)
      if (val !== null && !isValidSvgLength(val)) {
        node.removeAttribute(attr)
      }
    }
  }
})

/** 仅接受纯数字语义的 SVG 属性：值必须是无符号数字或数字百分比 */
const SVG_LENGTH_ATTRS = [
  'x', 'y', 'cx', 'cy', 'r', 'rx', 'ry',
  'width', 'height', 'x1', 'y1', 'x2', 'y2',
] as const

function isValidSvgLength(val: string): boolean {
  // SVG length 语法：<sign><number>[exponent][unit]，负数/指数合法（如 x="-10"）
  return /^[+-]?(\d+(\.\d+)?|\.\d+)([eE][+-]?\d+)?(%|em|ex|px|pt|pc|in|cm|mm)?$/.test(val)
}

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
/**
 * S9-01 修复：此前只 ADD_ATTR、从没禁过任何标签/属性，而 DOMPurify 的**默认白名单
 * 含 `<style>` 与 `style=`** —— 被渲染的 markdown 里有远端主机书写的内容（终端回显、
 * 读来的文件正文、工具输出），于是它们拿到的是全文档级 CSS：`position:fixed;inset:0`
 * 覆盖整个窗口、把敏感内容渲染到不可见处、用 `url()` 外带页面结构。
 *
 * 只禁样式通道，其余保持默认（代码块高亮靠 class，由 highlight.js 生成，无内联样式；
 * 复制按钮是 `<button type="button">`，不在禁止之列）。原 ADD_ATTR 里的 `class` 是
 * DOMPurify 默认已含项（S9-22 登记的空操作），删掉；`target` 留着（外链新标签打开）。
 */
const SANITIZE_OPTIONS = {
  ADD_ATTR: ['target'],
  FORBID_TAGS: ['style'],
  FORBID_ATTR: ['style'],
}

export function renderMarkdown(text: string): string {
  if (!text) return ''
  try {
    const html = marked.parse(text, { async: false }) as string
    // 语言标签不能只匹配 \w：marked 原样透传 info string，shell-session / c++ /
    // vb.net 这类带 - + . 的 hljs 合法 id 会整条不匹配 —— 代码块既不高亮也没了复制按钮
    const highlighted = html.replace(
      /<pre><code class="language-([^"]*)">([\s\S]*?)<\/code><\/pre>/g,
      (_match, lang, code) => {
        // lang 来自围栏 info string，放宽匹配后可能带任意字符：写进下面两处
        // HTML 之前先削成语言 id 的合法字符集，别让 <...> 落到标签文本里
        const safeLang = String(lang).replace(/[^-\w.+#]/g, '')
        const decoded = decodeHtml(code)
        let highlightedCode: string
        try {
          highlightedCode = safeLang && hljs.getLanguage(safeLang)
            ? hljs.highlight(decoded, { language: safeLang }).value
            : hljs.highlightAuto(decoded).value
        } catch {
          highlightedCode = code
        }
        const langLabel = safeLang || 'text'
        return `<div class="code-block-wrapper">` +
          `<div class="code-block-header">` +
          `<span class="code-lang">${langLabel}</span>` +
          `<button class="code-copy-btn" type="button">复制</button>` +
          `</div>` +
          `<pre><code class="hljs language-${safeLang}">${highlightedCode}</code></pre>` +
          `</div>`
      },
    )
    return DOMPurify.sanitize(highlighted, SANITIZE_OPTIONS)
  } catch (e) {
    console.warn('[renderMarkdown] failed, falling back to sanitized plain text:', e)
    return DOMPurify.sanitize(text, SANITIZE_OPTIONS)
  }
}
