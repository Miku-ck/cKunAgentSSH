/**
 * 强调色前景的静态验收
 *
 * 起因：一批按钮把 `color:#fff` 写死在 `background:var(--accent-primary)` 上
 * （首页 .hcc-send、队列的逐条批准，以及 ConfirmDialog / SftpView / ConnectionModal
 * 等 11 处）。强调色是可点的十档（面板色板渲染全部 Accent 键），其中「白色」= #ffffff，
 * 于是那些按钮变成白底白字整枚消失。修法是让前景跟着底色走（--accent-on），
 * 而"跟着走"本身必须钉住：手写一张 accent→on 表就是第三份需要同步的色表。
 * 面上的部分由 variables.test.ts 的全仓扫描钉（那里禁止强调色底配硬白字）。
 */
import { describe, expect, it } from 'vitest'
import tokens from './appearance-tokens.ts?raw'
import {
  ACCENT_MAP, accentTextOn, contrastOnAccent, isHexColor, relativeLuminance, type Accent,
} from './appearance-tokens'

/** 白字/深字与底色之间的 WCAG 对比度 */
function ratio(hex: string, on: string): number {
  const a = relativeLuminance(hex)
  const b = relativeLuminance(on)
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
}

describe('contrastOnAccent：压在强调色上的字', () => {
  it('十档强调色全部 ≥3:1 —— 白底白字那一类不可能再出现', () => {
    const failures = (Object.keys(ACCENT_MAP) as Accent[])
      .map(value => ({ value, r: Number(ratio(ACCENT_MAP[value].hex, contrastOnAccent(ACCENT_MAP[value].hex)).toFixed(2)) }))
      .filter(x => x.r < 3)
    expect(failures).toEqual([])
  })

  it('「白色」这一档翻成深字（它就是那条 bug 的现场）', () => {
    const on = contrastOnAccent(ACCENT_MAP.white.hex)
    expect(on).toBe('#15151a')
    // 写死 #fff 时对比度是 1.0 —— 字完全不存在；换深字后 17+
    expect(ratio(ACCENT_MAP.white.hex, on)).toBeGreaterThan(15)
  })

  it('亮底档一律深字：薄荷 / 蜜蜂 / 珊瑚 / 绿 / 紫 / 橙', () => {
    for (const value of ['mint', 'bee', 'coral', 'green', 'purple', 'orange'] as Accent[]) {
      expect(contrastOnAccent(ACCENT_MAP[value].hex), `${value} 应翻深`).toBe('#15151a')
    }
  })

  it('暗底档保持白字：朱砂 / 蓝 / 森林 —— 不为凑阈值把已经能读的设计换掉', () => {
    // 阈值取 3:1 而不是 4.5:1 的理由就钉在这条：#0078d4 配白字 4.5:1 本来达标，
    // 按 4.5 的线切会把它一起翻成深字，那是换设计不是修 bug。
    for (const value of ['verm', 'blue', 'forest'] as Accent[]) {
      expect(contrastOnAccent(ACCENT_MAP[value].hex), `${value} 应保持白字`).toBe('#ffffff')
    }
    expect(ratio(ACCENT_MAP.blue.hex, '#ffffff')).toBeGreaterThanOrEqual(4.5)
  })
})

describe('accentTextOn：强调色当文字时的可读变体', () => {
  // 三张实际底：结构底 / 浮起层 / 控件凹陷底。文字可能落在任意一张上。
  const DARK_SURFACES = ['#000000', '#1a1a1e', '#101013']
  const LIGHT_SURFACES = ['#f3f4f6', '#ffffff', '#eef0f3']
  const accents = Object.keys(ACCENT_MAP) as Accent[]

  it.each(accents)('%s：深色主题三张底全部 ≥4.5:1', (value) => {
    const color = accentTextOn(ACCENT_MAP[value].hex, DARK_SURFACES)
    expect(isHexColor(color)).toBe(true)
    for (const bg of DARK_SURFACES) {
      expect(ratio(color, bg), `${value} 落在 ${bg} 上`).toBeGreaterThanOrEqual(4.5)
    }
  })

  it.each(accents)('%s：浅色主题三张底全部 ≥4.5:1', (value) => {
    const color = accentTextOn(ACCENT_MAP[value].hex, LIGHT_SURFACES)
    expect(isHexColor(color)).toBe(true)
    for (const bg of LIGHT_SURFACES) {
      expect(ratio(color, bg), `${value} 落在 ${bg} 上`).toBeGreaterThanOrEqual(4.5)
    }
  })

  it('已经达标的强调色原样保留，不无谓改色', () => {
    expect(accentTextOn('#ffffff', ['#000000', '#1a1a1e'])).toBe('#ffffff')
  })

  it('深色底上把暗色强调色提亮；浅色底上把它压深', () => {
    const base = ACCENT_MAP.blue.hex
    const dark = accentTextOn(base, DARK_SURFACES)
    const light = accentTextOn(base, LIGHT_SURFACES)
    expect(relativeLuminance(dark)).toBeGreaterThan(relativeLuminance(base))
    expect(relativeLuminance(light)).toBeLessThan(relativeLuminance(base))
  })

  it('没有可用底色 / 非法输入时原样返回，不抛错', () => {
    expect(accentTextOn('#0078d4', [])).toBe('#0078d4')
    expect(accentTextOn('not-a-color', ['#000000'])).toBe('not-a-color')
    expect(accentTextOn('#0078d4', ['not-a-color'])).toBe('#0078d4')
  })
})

describe('appearance-tokens 必须是零依赖叶子', () => {
  it('源码里不出现 store / composable 的 import', () => {
    // settings.ts 在**模块顶层**就 `Object.keys(ACCENT_MAP)` 取白名单。这个表一旦
    // 搬回 useAppearance.ts（它 import store），就成环：先碰到 useAppearance 的入口
    // 会在这句上炸 `Cannot convert undefined or null to object`。今天 App.vue 恰好
    // 先导 store 所以不响 —— 侥幸不该是契约，所以把它写成断言。
    // 三条都要钉：`import x from '@/stores/…'`、`export { x } from '@/stores/…'`、
    // 副作用 `import '@/stores/…'` 与动态 `import('@/stores/…')` 都能把环带回来，
    // 而只盯第一种写法的断言对后三种一律放行。别名与非相对路径一并禁掉。
    expect(tokens).not.toMatch(/from\s+['"](?!\.)/)
    expect(tokens).not.toMatch(/^\s*import\s+['"]/m)
    expect(tokens).not.toMatch(/import\s*\(\s*['"](?!\.)/)
  })
})
