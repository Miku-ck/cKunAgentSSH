/**
 * 终端 ANSI 色板守卫：8 主题齐全、键完整、对比度达标、dark/light 钉住基线。
 * 色板是「组件局部调色板」豁免（xterm 吃不到 CSS 令牌），本测试是它的核对方。
 * TERMINAL_BG 与 variables.css 各主题 --bg-terminal 对账，那边改了这里必须跟着改。
 */
import { describe, expect, it } from 'vitest'
import { contrastRatio } from '@/utils/appearance-tokens'
import { TERMINAL_THEMES, resolveThemeName, type ResolvedTheme } from './useTerminalConfig'

const TERMINAL_BG: Record<ResolvedTheme, string> = {
  dark: '#000000',
  light: '#17181c',
  'blue-screen': '#080d16',
  forest: '#0a120d',
  mint: '#071113',
  bee: '#0f0b05',
  coral: '#100908',
  atelier: '#17181c',
}

const ANSI_KEYS = [
  'black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white',
  'brightBlack', 'brightRed', 'brightGreen', 'brightYellow',
  'brightBlue', 'brightMagenta', 'brightCyan', 'brightWhite',
] as const

describe('终端 ANSI 色板', () => {
  it('8 个主题齐全', () => {
    expect(Object.keys(TERMINAL_THEMES).sort()).toEqual(Object.keys(TERMINAL_BG).sort())
  })

  it.each(Object.keys(TERMINAL_BG) as ResolvedTheme[])('%s：16 个 ANSI 键 + 前景/光标/选区齐全', (theme) => {
    const t = TERMINAL_THEMES[theme]
    for (const k of ANSI_KEYS) expect(t[k], k).toMatch(/^#[0-9a-f]{6}$/i)
    expect(t.foreground).toMatch(/^#/)
    expect(t.cursor).toMatch(/^#/)
    expect(t.selectionBackground).toMatch(/^#/)
  })

  it.each(Object.keys(TERMINAL_BG) as ResolvedTheme[])('%s：前景对终端底 ≥4.5，ANSI 色 ≥2.5（black 除外）', (theme) => {
    const t = TERMINAL_THEMES[theme]
    const bg = TERMINAL_BG[theme]
    expect(contrastRatio(t.foreground!, bg)).toBeGreaterThanOrEqual(4.5)
    for (const k of ANSI_KEYS) {
      // ANSI black 刻意贴近底色（暗纹/反白的底），可读职责由 brightBlack 承担
      if (k === 'black') continue
      expect(contrastRatio(t[k]!, bg), `${theme}.${k}`).toBeGreaterThanOrEqual(2.5)
    }
  })

  it('dark/light 钉住现状基线（零回归）', () => {
    for (const theme of ['dark', 'light'] as const) {
      expect(TERMINAL_THEMES[theme]).toMatchObject({
        foreground: '#e6edf3', cursor: '#58a6ff', selectionBackground: '#264f78',
        black: '#111111', red: '#f85149', green: '#3fb950', yellow: '#d29922',
        blue: '#58a6ff', magenta: '#bc8cff', cyan: '#39d2c0', white: '#e6edf3',
        brightBlack: '#6e7681', brightRed: '#ff7b72', brightGreen: '#7ee787',
        brightYellow: '#e3b341', brightBlue: '#79c0ff', brightMagenta: '#d2a8ff',
        brightCyan: '#56d4dd', brightWhite: '#f0f6fc',
      })
    }
  })

  it('resolveThemeName：system 在 node 环境回落 dark，其余原样透传', () => {
    expect(resolveThemeName('dark')).toBe('dark')
    expect(resolveThemeName('atelier')).toBe('atelier')
    expect(['dark', 'light']).toContain(resolveThemeName('system'))
  })
})
