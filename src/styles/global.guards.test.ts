/**
 * global.css 守卫 —— 焦点可见性契约
 * 钉：:focus-visible 只加描边，不改元素圆角。早先那句 `border-radius: 2px`
 * 会把按钮/输入框自己的圆角覆盖成 2px（焦点一到，方形按钮变圆角），
 * 手记主题下尤其突兀。
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const src = readFileSync(
  fileURLToPath(new URL('./global.css', import.meta.url)),
  'utf8',
).replace(/\r\n/g, '\n')

const focusBlock = src.slice(src.indexOf(':focus-visible {'), src.indexOf('}', src.indexOf(':focus-visible {')))

describe('global.css 焦点可见', () => {
  it(':focus-visible 有 2px 主色描边', () => {
    expect(focusBlock).toContain('outline: 2px solid var(--accent-primary)')
  })
  it('不覆写元素自身的圆角', () => {
    expect(focusBlock).not.toContain('border-radius')
  })
})
