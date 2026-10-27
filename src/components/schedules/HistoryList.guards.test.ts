/**
 * HistoryList 源码守卫 —— spec 2026-10-27-schedule-history-render
 * 钉展示契约的存在性：A 方案判定式、渲染通道、slot、时区与令牌纪律。
 * 本仓无挂载测试环境（vitest node、无 @vue/test-utils），?raw 形状守卫为先例模式
 * （RunbookPanel.test.ts 头注同口径）。
 */
import { describe, expect, it } from 'vitest'
import raw from './HistoryList.vue?raw'

const src = raw.replace(/\r\n/g, '\n')

describe('HistoryList 展示契约', () => {
  it('可展开判定 = expandable || summary || error', () => {
    expect(src).toContain('expandableOf(row)')
    expect(src).toContain('!!row.expandable || !!row.summary || !!row.error')
  })
  it('默认展开仅失败行（A 方案：成功行有内容也默认折叠）', () => {
    expect(src).toContain("status === 'failed'")
  })
  it('展开区经 renderMarkdown 渲染、时间戳过 formatBackendTs', () => {
    expect(src).toContain('renderMarkdown(')
    expect(src).toContain('formatBackendTs(')
  })
  it('两个 slot 都存在且回传 row；actions 仅失败行渲染', () => {
    expect(src).toContain('<slot name="extra"')
    expect(src).toContain('<slot name="actions"')
    expect(src).toContain("v-if=\"row.status === 'failed'\"")
  })
  it('不可展开行不响应点击（toggle 内有守卫）', () => {
    expect(src).toContain('if (!expandableOf(row)) return')
  })
  it('running 无内容行有占位文案', () => {
    expect(src).toContain('（尚无摘要）')
  })
  it('无硬编码色值：颜色全走主题令牌', () => {
    const styleBlock = src.slice(src.indexOf('<style scoped>'))
    expect(styleBlock).not.toMatch(/#[0-9a-fA-F]{3,8}\b/)
    expect(styleBlock).not.toMatch(/\brgba?\(/)
    expect(styleBlock).toContain('var(--')
  })
  it('两域状态键并集全部有专属色（防 stopped/stale_reaped 退化为默认灰）', () => {
    const styleBlock = src.slice(src.indexOf('<style scoped>'))
    for (const s of ['running', 'completed', 'success', 'failed', 'waiting_permission', 'blocked', 'stopped', 'stale_reaped']) {
      expect(styleBlock, `缺少 .status.${s} 配色`).toContain(`.status.${s}`)
    }
  })
  it('摘要容器用共享类 .markdown-body（不是死类 md-body）', () => {
    expect(src).toContain('class="history-summary markdown-body"')
    expect(src).not.toContain('md-body')
  })
  it('摘要区复用 Markdown 事件委托（复制 + 外链拦截）', () => {
    expect(src).toContain('useMarkdownDelegation')
    expect(src).toContain('@click="onMarkdownClick"')
  })
  it('圆角走 --radius-* 令牌，不用字面 px（手记主题会改小）', () => {
    const styleBlock = src.slice(src.indexOf('<style scoped>'))
    expect(styleBlock).not.toMatch(/border-radius:\s*\d+px/)
  })
})
