/**
 * 设置弹窗的焦点管理守卫（M-2）
 *
 * 审查实测：打开设置后 activeElement 仍是 BODY，Tab 会先走 4 个背景控件才进弹窗。
 * 没有 jsdom，这里钉住实现形状；Tab 循环本身由 focus-trap.test.ts 的纯函数覆盖，
 * 实际焦点路径由 CDP 验证。
 */
import { describe, expect, it } from 'vitest'
import raw from './SettingsDialog.vue?raw'

const src = raw.replace(/\r\n/g, '\n')

describe('M-2 设置弹窗焦点', () => {
  it('弹窗有可访问名称与可聚焦容器', () => {
    expect(src).toContain('aria-labelledby="settings-dialog-title"')
    expect(src).toContain('id="settings-dialog-title"')
    expect(src).toMatch(/class="settings-dialog"[\s\S]{0,200}?tabindex="-1"/)
    expect(src).toMatch(/ref="dialogRef"/)
  })

  it('打开时接管焦点，关闭时还原', () => {
    expect(src).toContain('restoreFocus')
    expect(src).toMatch(/dialogRef\.value\?\.focus\(\)/)
    expect(src).toMatch(/restoreFocus[^\n]*\.focus\(\)/)
  })

  it('背景应用在弹窗打开期间 inert + aria-hidden，关闭时恢复', () => {
    expect(src).toMatch(/setAttribute\('inert'/)
    expect(src).toMatch(/removeAttribute\('inert'/)
    expect(src).toMatch(/aria-hidden/)
  })

  it('Tab 被限制在最上层模态内（嵌套弹窗优先）', () => {
    expect(src).toContain('nextFocusIndex')
    expect(src).toContain('topmostModal')
    expect(src).toMatch(/e\.key !== 'Tab'/)
    expect(src).toMatch(/aria-modal="true"/)
  })
})
