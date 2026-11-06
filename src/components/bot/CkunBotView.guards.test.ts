/**
 * cKunBot 面板守卫 —— 目前只钉一件事：tab 按钮的字号来源。
 *
 * `.bot-tabs button` 不写 font-size 时会落到 Chromium 给表单控件的 UA 默认值
 * 13.3333px（=10pt），而全站正文是 13px（global.css 的 html 上声明）。差 2.5% 看着
 * 不明显，但它让同一屏里出现两套字号，且这套漂移是「谁忘了写」而不是「谁想大一号」。
 */
import viewRaw from './CkunBotView.vue?raw'
import { describe, expect, it } from 'vitest'

const src = viewRaw.replace(/\r\n/g, '\n')

describe('bot 面板 tab 按钮', () => {
  it('显式声明字号，不落进 UA 的 13.3333px', () => {
    const i = src.indexOf('.bot-tabs button {')
    expect(i, '样式里要还能找到 .bot-tabs button 这条规则').toBeGreaterThan(-1)
    const block = src.slice(i, src.indexOf('}', i) + 1)
    expect(/(?:^|[;\s])font-size:/.test(block), `${block.slice(0, 120)} —— 缺 font-size`).toBe(true)
  })
})
