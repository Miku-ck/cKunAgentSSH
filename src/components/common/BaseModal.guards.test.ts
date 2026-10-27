/**
 * BaseModal 形状守卫 —— DESIGN.md 模态标准
 * 钉：Teleport 到 body、role/aria-modal/aria-labelledby、Esc 关闭、Tab 焦点陷阱、
 * 遮罩走 --scrim + blur、弹窗走 --radius-lg/--shadow-lg、头部固定 + body 独立滚动。
 * 任何一项丢掉，弹窗就退回「整框 80vh 滚动 + 冷黑遮罩 + 手记主题破相」的老样子。
 */
import { describe, expect, it } from 'vitest'
import raw from './BaseModal.vue?raw'

const src = raw.replace(/\r\n/g, '\n')
const style = src.slice(src.indexOf('<style scoped>'))

describe('BaseModal 结构', () => {
  it('Teleport 到 body（逃出父级 overflow/z-index 上下文）', () => {
    expect(src).toContain('<Teleport to="body">')
  })
  it('role=dialog + aria-modal + aria-labelledby 关联标题', () => {
    expect(src).toContain('role="dialog"')
    expect(src).toContain('aria-modal="true"')
    expect(src).toContain(':aria-labelledby="titleId"')
    expect(src).toContain(':id="titleId"')
  })
  it('标题 id 来自模块级计数器（每实例唯一）', () => {
    expect(src).toContain('let _bmUid = 0')
    expect(src).toContain('`bm-title-${++_bmUid}`')
  })
  it('Esc 关闭监听在 window 并在卸载时移除', () => {
    expect(src).toContain("e.key !== 'Escape'")
    expect(src).toContain("window.addEventListener('keydown', onWindowKeydown)")
    expect(src).toContain("window.removeEventListener('keydown', onWindowKeydown)")
  })
  it('Tab 焦点陷阱交给 useModalFocusTrap（一份实现，自绘弹窗也用它）', () => {
    expect(src).toContain('useModalFocusTrap')
    expect(src).toContain('activate()')
    expect(src).toContain('deactivate()')
    // 环形逻辑与选择器只该有一份；组件里再出现就是两套行为
    expect(src).not.toContain('const FOCUSABLE')
  })
  it('挂载即激活焦点管理，卸载时还原并摘监听', () => {
    expect(src).toMatch(/onMounted\(\(\) => \{\s*activate\(\)/)
    expect(src).toMatch(/onUnmounted\(\(\) => \{[\s\S]*deactivate\(\)/)
  })
  it('遮罩点击自身关闭，三段式 head/body/actions', () => {
    expect(src).toContain('@click.self="emit(\'close\')"')
    expect(src).toContain('class="bm-head"')
    expect(src).toContain('class="bm-body"')
    expect(src).toContain('class="bm-actions"')
  })
})

describe('BaseModal 视觉令牌', () => {
  it('遮罩读 --scrim（随主题染色）+ 2px 模糊 + z-index 2000', () => {
    expect(style).toContain('background: var(--scrim)')
    expect(style).toContain('backdrop-filter: blur(2px)')
    expect(style).toMatch(/z-index:\s*2000/)
  })
  it('弹窗走 --radius-lg / --shadow-lg / --bg-elevated，无字面圆角', () => {
    expect(style).toContain('border-radius: var(--radius-lg)')
    expect(style).toContain('box-shadow: var(--shadow-lg)')
    expect(style).toContain('background: var(--bg-elevated)')
    expect(style).not.toMatch(/border-radius:\s*\d+px/)
  })
  it('头部固定、body 独立滚动（长内容不把底部操作滚走）', () => {
    expect(style).toMatch(/\.bm-body\s*\{[^}]*overflow:\s*auto/)
    expect(style).toMatch(/\.bm-body\s*\{[^}]*min-height:\s*0/)
    expect(style).toMatch(/\.bm-head\s*\{[^}]*flex:\s*0 0 auto/)
    expect(style).toMatch(/\.bm-actions\s*\{[^}]*flex:\s*0 0 auto/)
  })
  it('标题单行省略，头部不因长标题撑破', () => {
    expect(style).toMatch(/\.bm-title\s*\{[^}]*text-overflow:\s*ellipsis/)
    expect(style).toMatch(/\.bm-title\s*\{[^}]*min-width:\s*0/)
  })
  it('尊重 prefers-reduced-motion', () => {
    expect(style).toContain('prefers-reduced-motion: reduce')
  })
})
