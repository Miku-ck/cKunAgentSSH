/**
 * 守卫：搜索高亮三枚装饰色必须从 accentSearchColors() 派生，
 * 不许退回硬编码 hex。SearchAddon 的 decorations 画在 overview ruler 上，
 * 吃不到 CSS 令牌 —— 组件局部调色板豁免形状保留，但取值必须跟强调色走。
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { describe, expect, it } from 'vitest'

const here = dirname(fileURLToPath(import.meta.url))
const source = readFileSync(join(here, 'TermSearchBar.vue'), 'utf-8')

describe('TermSearchBar 搜索高亮跟强调色', () => {
  it('装饰色从 accentSearchColors() 派生', () => {
    expect(source).toContain('accentSearchColors()')
  })

  it('不再有硬编码的概览标尺 hex', () => {
    expect(source).not.toContain('#8b949e')
    expect(source).not.toContain('#58a6ff')
  })
})
