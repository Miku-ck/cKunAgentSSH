/// <reference types="node" />
/**
 * 圆角令牌守卫 —— DESIGN.md「形状：--radius-sm/md/lg = 4/6/8px（手记 2/3/4px）」
 *
 * 字面 4/6/8px 与默认主题的令牌同值，所以在深色主题下看不出问题；切到「手记」
 * 主题（三档整体收成 2/3/4）时，写死的那批还是 4/6/8 —— 与旁边的令牌圆角并排
 * 就显得毛躁。这里只收 4/6/8 这三档：值与令牌一一对应，换掉是等价改写。
 *
 * 其余字面值（2/3/5/7/9/10/12px、999px 胶囊、50% 圆形）没有对应令牌，属设计
 * 裁决范围，本守卫不碰；要沉淀时先加令牌再改，别在组件里就地发明数值。
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { SRC, rel, walk } from './fs-walk'

const LITERAL = /border-radius:\s*(4px|6px|8px)\s*;/g

describe('圆角走令牌', () => {
  it('没有残留的 4/6/8px 字面圆角', () => {
    const found: string[] = []
    for (const file of walk(SRC)) {
      const text = readFileSync(file, 'utf8')
      for (const match of text.matchAll(LITERAL)) {
        found.push(`${rel(file)}  ${match[0].trim()}`)
      }
    }
    expect(found, `这些位置仍写死圆角：\n${found.join('\n')}`).toEqual([])
  })

  it('令牌值确实与那三档一一对应，换掉是等价改写', () => {
    const css = readFileSync(join(SRC, 'styles/variables.css'), 'utf8')
    expect(css).toMatch(/--radius-sm:\s*4px/)
    expect(css).toMatch(/--radius-md:\s*6px/)
    expect(css).toMatch(/--radius-lg:\s*8px/)
    // 手记主题整体收紧，正是字面值会「不跟随主题」的那套
    expect(css).toMatch(/--radius-sm:\s*2px/)
  })

  it('DESIGN.md 记着这条规则', () => {
    const design = readFileSync(join(SRC, '..', 'DESIGN.md'), 'utf8')
    expect(design).toContain('--radius-sm/md/lg')
  })
})
