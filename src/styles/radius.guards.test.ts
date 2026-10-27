/// <reference types="node" />
/**
 * 圆角令牌守卫 —— DESIGN.md「形状：--radius-xs/sm/md/lg/xl = 2/4/6/8/10px（手记 1/2/3/4/5）+ --radius-pill」
 *
 * 字面 px 圆角在默认主题下常常看不出毛病，切到「手记」主题（整根刻度收紧）时
 * 就不跟随主题了 —— 与旁边的令牌圆角并排会显毛躁。这里直接拦死字面 px：
 * 要新档先加令牌，再改组件。
 *
 * 例外只有两件事：`border-radius: 0`（不平滑，不是档位）与 `50%`（圆形表达
 * 形状，头像/指示灯），它们不是尺度问题。
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { SRC, rel, walk } from './fs-walk'

const LITERAL = /border-radius:\s*\d+px/g

function tokens(block: string, name: string): string | undefined {
  return block.match(new RegExp(`--radius-${name}:\\s*([^;]+);`))?.[1].trim()
}

describe('圆角走令牌', () => {
  it('没有残留的字面 px 圆角', () => {
    const found: string[] = []
    for (const file of walk(SRC)) {
      const text = readFileSync(file, 'utf8')
      for (const match of text.matchAll(LITERAL)) {
        found.push(`${rel(file)}  ${match[0].trim()}`)
      }
    }
    expect(found, `这些位置仍写死圆角：\n${found.join('\n')}`).toEqual([])
  })

  it('两套主题各自给齐六档，手记整根刻度都更紧', () => {
    const css = readFileSync(join(SRC, 'styles/variables.css'), 'utf8')
    const [rootBlock, atelierBlock] = [
      css.slice(0, css.indexOf('[data-theme="light"]')),
      css.slice(css.indexOf('[data-theme="atelier"]')),
    ]

    expect(tokens(rootBlock, 'xs')).toBe('2px')
    expect(tokens(rootBlock, 'sm')).toBe('4px')
    expect(tokens(rootBlock, 'md')).toBe('6px')
    expect(tokens(rootBlock, 'lg')).toBe('8px')
    expect(tokens(rootBlock, 'xl')).toBe('10px')
    expect(tokens(rootBlock, 'pill')).toBe('999px')

    // 手记：xs→xl 恰好是默认刻度的一半；胶囊与刻度无关，两套同值
    expect(tokens(atelierBlock, 'xs')).toBe('1px')
    expect(tokens(atelierBlock, 'sm')).toBe('2px')
    expect(tokens(atelierBlock, 'md')).toBe('3px')
    expect(tokens(atelierBlock, 'lg')).toBe('4px')
    expect(tokens(atelierBlock, 'xl')).toBe('5px')
    expect(tokens(atelierBlock, 'pill')).toBe('999px')
  })

  it('DESIGN.md 记着这根刻度', () => {
    const design = readFileSync(join(SRC, '..', 'DESIGN.md'), 'utf8')
    expect(design).toContain('--radius-xs/sm/md/lg/xl')
    expect(design).toContain('--radius-pill')
  })
})
