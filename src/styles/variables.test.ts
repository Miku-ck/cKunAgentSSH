/**
 * 主题令牌的静态验收（spec 2026-09-29-home-command-panel · 接缝 S4）
 *
 * 为什么在 CSS 上写测试：新主题是一组 `data-theme` 变量覆盖，**漏掉哪一条都不会
 * 编译报错**，只会在米纸底上留着一块深色值 —— 而这类缺陷恰好长在"没人截图看过"
 * 的角落里。所以把"覆盖集合"与文件头自己写下的约定变成断言，而不是靠人眼扫。
 */
import { describe, expect, it } from 'vitest'
// ?raw 由 vite/client 声明类型：本文件在 tsconfig.web 下检查，那里没有 node 类型，
// 读 node:fs 会让 typecheck 报错（测试跑在 vitest 的 node 环境里，两边不是一回事）。
import css from './variables.css?raw'

/** 取某个 `[data-theme="x"]` 块的原文（到第一个 `}` 为止 —— 主题块内不嵌套规则） */
function blockOf(theme: string): string {
  const marker = `[data-theme="${theme}"]`
  const start = css.indexOf(marker)
  if (start < 0) throw new Error(`找不到主题块：${marker}`)
  const end = css.indexOf('}', start)
  return css.slice(start, end < 0 ? undefined : end)
}

const tokensOf = (block: string) =>
  [...block.matchAll(/(--[a-z0-9-]+)\s*:/g)].map(m => m[1]).sort()

const valueOf = (block: string, token: string) =>
  block.match(new RegExp(`${token}:\\s*([^;]+);`))?.[1]?.trim()

const atelier = blockOf('atelier')
const atelierTokens = tokensOf(atelier)

describe('atelier（手记）主题的令牌覆盖', () => {
  it('浅色主题覆盖的令牌，手记一个都不能少', () => {
    // 少一条 = 那条在米纸底上仍是深色主题的值。深色底写死的 status-*/danger-*/
    // card-perm-* 搬到纸上不是"不好看"，是文字对比度直接不够读不出来。
    const missing = tokensOf(blockOf('light')).filter(t => !atelierTokens.includes(t))
    expect(missing).toEqual([])
  })

  it('--bg-* 与同名 --bg-*-solid 同值（variables.css 头部写死的约定）', () => {
    // global.css 的 [data-gradient="on"] 用 -solid 派生半透明版；两者不同值时
    // 渐变主题下的面板底与实色主题差一档，且不会有任何报错。
    for (const base of ['--bg-elevated', '--bg-input']) {
      expect(valueOf(atelier, `${base}-solid`), `${base}-solid 要与 ${base} 同值`)
        .toBe(valueOf(atelier, base))
    }
  })

  it('手记的字体栈一律本地优先：整份令牌表里没有任何网络引用', () => {
    // 预览稿实测 fonts.googleapis.com 不可达（ERR_CONNECTION_REFUSED），
    // 当时整套界面静默掉进回退字体。Electron 断网/离线分发是常态不是边缘场景，
    // 所以字体这一层不允许出现 @import / url() / http 引用。
    expect(css).not.toMatch(/@import/)
    expect(css).not.toMatch(/url\(/)
    expect(css).not.toMatch(/https?:/)
  })

  it('字体栈以通用族收尾 —— 一个都没装时落到 serif/mono，而不是悄悄掉回无衬线', () => {
    expect(valueOf(atelier, '--font-sans')?.endsWith('serif')).toBe(true)
    expect(valueOf(atelier, '--font-mono')?.endsWith('monospace')).toBe(true)
  })

  it('手记不写 --accent-primary：那个令牌归 JS 管', () => {
    // applyAccent 在 documentElement 上 setProperty，内联样式压过 [data-theme] 块
    // —— 在主题里写强调色是一条永远不生效的声明（"亮的空开关"的同族）。
    // 朱砂因此做成一条强调色（Accent: verm），不是塞进主题令牌。
    expect(atelierTokens).not.toContain('--accent-primary')
    expect(atelierTokens).not.toContain('--accent-primary-rgb')
  })
})
