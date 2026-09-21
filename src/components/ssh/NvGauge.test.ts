/**
 * NvGauge 渲染回归测试（SSR）。
 *
 * 三件事必须守住：
 *
 * 1. **填充必须有取色 class**。这里曾有一个读源码看不出、只有渲染才暴露的缺陷 ——
 *    props 写成 `cls: { type: Function, default: () => bandCls }`，而 Vue 对
 *    `type: Function` 的 prop 不会把 default 当工厂调用，于是 `cls` 是包装函数本身、
 *    `cls(v)` 返回函数、`normalizeClass` 对函数返回空串 → 填充 `class=""`、
 *    无背景色 → **条带看起来是空轨**。纯函数单测覆盖不到它，必须真的渲染并断言 HTML。
 *
 * 2. **两层都必须真的编码量值**：上层进度条用**宽度**、下层趋势薄条用**高度**。
 *    原实现所有格子恒为 `height: 100%`，0% 与 65% 渲染成完全相同的实心条。
 *
 * 3. **矛盾态提示必须有标记**，不能只在 hover 时存在 —— 否则「哪一行有问题」
 *    要逐条摸一遍才知道。浮层的**显形**是父组件 CSS 的职责（`.nv-plot:hover`），
 *    SSR 验不了，故那部分在构建产物核验脚本里断言，见 verify-nvgauge-bundle.cjs。
 *
 * SSR 在 node 环境下即可完成，无需 happy-dom / jsdom。
 */
import { describe, expect, it } from 'vitest'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { NV_WINDOW, NvGauge } from './NvGauge'

async function render(props: Record<string, unknown>): Promise<string> {
  return renderToString(createSSRApp({ render: () => h(NvGauge, props) }))
}

/** 取出某个 class 的元素片段（NvGauge 内部无嵌套 span，首个 </span> 即其闭合） */
function section(html: string, cls: string): string {
  const m = new RegExp(`<span class="${cls}"[^>]*>`).exec(html)
  if (!m) return ''
  const start = m.index
  const end = html.indexOf('</span>', start)
  return html.slice(start, end)
}

/** 某个 class 的**起始标签本身**（用于断言它带了哪些属性，而不是它包了什么） */
function tagOf(html: string, cls: string): string {
  return (new RegExp(`<span class="${cls}"[^>]*>`).exec(html) ?? [''])[0]
}

/** 警告标记的 <svg> 属性串；无标记时为空串 */
function markAttr(html: string): string {
  return (/<svg([^>]*)>/.exec(html) ?? ['', ''])[1]
}

/** 悬浮提示的文本；无提示时为空串 */
function tipText(html: string): string {
  return (/<span class="nv-tip"[^>]*>([^<]*)<\/span>/.exec(html) ?? ['', ''])[1]
}

interface Cell { cls: string; style: string }

/**
 * 解析片段里所有 `<i>`，**不假设它带不带属性**。
 *
 * 踩过的坑：早先写成"只匹配裸 `<i style=...>`"来取趋势格 ——
 * 一旦趋势格被误加上 class，那条正则就**一个都匹配不到**，
 * 于是「趋势格不应带取色 class」的断言在**空集合上空转通过**。
 * 断言必须先断言数量，再遍历；解析也不能预先假定属性存在。
 */
function cells(fragment: string): Cell[] {
  return [...fragment.matchAll(/<i([^>]*)><\/i>/g)].map(m => ({
    cls: (/class="([^"]*)"/.exec(m[1]) ?? ['', ''])[1],
    style: (/style="([^"]*)"/.exec(m[1]) ?? ['', ''])[1],
  }))
}

const BAND = ['nv-cool', 'nv-warm', 'nv-hot']
const pctOf = (style: string, prop: string): number =>
  parseFloat((new RegExp(`${prop}:\\s*([\\d.]+)%`).exec(style) ?? ['', 'NaN'])[1])

/** 进度条填充（.nv-bar 里带 width 的那个 i） */
const fill = (html: string): Cell =>
  cells(section(html, 'nv-bar')).find(c => c.style.includes('width')) ?? { cls: '', style: '' }
/** 进度条内的参考线 */
const grids = (html: string): Cell[] =>
  cells(section(html, 'nv-bar')).filter(c => c.cls === 'nv-grid')
/** 趋势薄条：无数据格（nv-blank）与有值格（无 class） */
const trend = (html: string) => {
  const all = cells(section(html, 'nv-trend'))
  return { all, blanks: all.filter(c => c.cls === 'nv-blank'), bars: all.filter(c => c.cls !== 'nv-blank') }
}

describe('NvGauge · 回归锁定：填充必须有取色 class', () => {
  it('不传 kind 时填充 class 非空（缺陷复现点：曾产出 class=""）', async () => {
    expect(fill(await render({ value: 87.4 })).cls).not.toBe('')
  })

  it('任何取值下都不产出空 class（含 0 / 100 / 边界）', async () => {
    for (const v of [0, 100, 65.9, 66, 89.9, 90]) {
      const f = fill(await render({ value: v }))
      expect(BAND, `收到非法 class: "${f.cls}"`).toContain(f.cls)
    }
  })

  it('趋势薄条的格子不参与变色（约定：统一暗色）', async () => {
    const html = await render({ value: 80, values: [10, 80, 95] })
    const { bars } = trend(html)
    // 先断言数量：否则下面的遍历会在空集合上空转通过（这类假断言踩过一次）
    expect(bars).toHaveLength(3)
    for (const c of bars) {
      expect(c.cls, `趋势格不应带取色 class，收到 "${c.cls}"`).toBe('')
    }
  })
})

describe('NvGauge · 进度条取色档位', () => {
  it('默认（pct）按 66 / 90 分档', async () => {
    expect(fill(await render({ value: 65.9 })).cls).toBe('nv-cool')
    expect(fill(await render({ value: 66 })).cls).toBe('nv-warm')
    expect(fill(await render({ value: 89.9 })).cls).toBe('nv-warm')
    expect(fill(await render({ value: 90 })).cls).toBe('nv-hot')
  })

  it('kind="load" 按 4 / 8 分档（负载不是百分比）', async () => {
    expect(fill(await render({ value: 3.9, kind: 'load' })).cls).toBe('nv-cool')
    expect(fill(await render({ value: 4, kind: 'load' })).cls).toBe('nv-warm')
    expect(fill(await render({ value: 8, kind: 'load' })).cls).toBe('nv-hot')
  })

  it('同样的数值在两个档位下得到不同颜色（证明 kind 真的生效）', async () => {
    const asPct = fill(await render({ value: 5 })).cls
    const asLoad = fill(await render({ value: 5, kind: 'load' })).cls
    expect(asPct).toBe('nv-cool') // 5% 占用 = 正常
    expect(asLoad).toBe('nv-warm') // 5 的负载 = 警戒
    expect(asPct).not.toBe(asLoad)
  })
})

describe('NvGauge · 进度条宽度编码量值', () => {
  it('宽度 = 当前值占 max 的百分比', async () => {
    expect(pctOf(fill(await render({ value: 87.4 })).style, 'width')).toBeCloseTo(87.4, 1)
    expect(pctOf(fill(await render({ value: 2.1 })).style, 'width')).toBeCloseTo(2.1, 1)
  })

  it('max 参与归一（负载这类非百分比量）', async () => {
    const f = fill(await render({ value: 9, max: 12 }))
    expect(pctOf(f.style, 'width')).toBeCloseTo(75, 1)
  })

  it('0 产出 0% 宽度而非被省略（空条 = 0%，不是「没有条」）', async () => {
    const html = await render({ value: 0 })
    expect(fill(html).style).toContain('width:0.0%')
  })

  it('超范围值被夹到 0–100', async () => {
    expect(pctOf(fill(await render({ value: -5 })).style, 'width')).toBe(0)
    expect(pctOf(fill(await render({ value: 150 })).style, 'width')).toBe(100)
  })

  it('当前值由 value 决定，不由 values 末值决定（SWP 这类无历史的指标也要有条长）', async () => {
    const html = await render({ values: [], value: 53 })
    expect(pctOf(fill(html).style, 'width')).toBeCloseTo(53, 1)
  })
})

describe('NvGauge · 结构与参考线', () => {
  it('两层结构同时存在：.nv-bar 与 .nv-trend', async () => {
    const html = await render({ value: 50, values: [1, 2] })
    expect(section(html, 'nv-bar')).not.toBe('')
    expect(section(html, 'nv-trend')).not.toBe('')
  })

  it('参考线 3 条，位置为 25 / 50 / 75（横向条上必须落在 left）', async () => {
    const g = grids(await render({ value: 50 }))
    expect(g).toHaveLength(3)
    expect(g.map(c => pctOf(c.style, 'left'))).toEqual([25, 50, 75])
  })

  it('两个装饰层对读屏隐藏（图形不重复播报），标记则必须露出来', async () => {
    const html = await render({ value: 50 })
    expect(tagOf(html, 'nv-bar')).toContain('aria-hidden="true"')
    expect(tagOf(html, 'nv-trend')).toContain('aria-hidden="true"')
  })
})

describe('NvGauge · 趋势薄条窗口与占位', () => {
  it('无数据时补齐 NV_WINDOW 个暗格、无有值格', async () => {
    const html = await render({ values: [] })
    expect(trend(html).blanks).toHaveLength(NV_WINDOW)
    expect(trend(html).bars).toHaveLength(0)
  })

  it('数据不足时暗格在前、有值格在后（时间轴左旧右新）', async () => {
    const html = await render({ values: [10, 20, 30] })
    const { blanks, bars } = trend(html)
    expect(blanks).toHaveLength(NV_WINDOW - 3)
    expect(bars).toHaveLength(3)
  })

  it('超过 NV_WINDOW 点时只保留末尾一段', async () => {
    const values = Array.from({ length: NV_WINDOW + 25 }, (_, i) => i % 100)
    const html = await render({ values })
    const { blanks, bars } = trend(html)
    expect(blanks).toHaveLength(0)
    expect(bars).toHaveLength(NV_WINDOW)
    expect(pctOf(bars[bars.length - 1].style, 'height'))
      .toBeCloseTo(values[values.length - 1], 1)
  })

  it('趋势格高度编码量值（原缺陷：恒为 100%）', async () => {
    const html = await render({ values: [0, 65] })
    const { bars } = trend(html)
    expect(bars).toHaveLength(2)
    const h = bars.map(c => pctOf(c.style, 'height'))
    expect(h[0]).toBe(0)
    expect(h[1]).toBeCloseTo(65, 1)
    expect(h[0]).not.toBe(h[1]) // ← 0% 与 65% 必须可区分
  })

  it('趋势格同样按 max 归一（与进度条同一把尺）', async () => {
    const html = await render({ values: [3, 9], max: 12 })
    expect(trend(html).bars.map(c => pctOf(c.style, 'height'))).toEqual([25, 75])
  })
})

describe('NvGauge · 旧结构不得残留', () => {
  it('产物不含 nv-blocks / nv-tall / nv-meter（竖向条带已废弃）', async () => {
    const html = await render({ value: 50, values: [1, 2, 3] })
    expect(html).not.toContain('nv-blocks')
    expect(html).not.toContain('nv-tall')
    expect(html).not.toContain('nv-meter')
  })
})

describe('NvGauge · 矛盾态提示（条右端标记 + hover 浮层）', () => {
  const NOTE = 'SWAP 已满 · 存在换页压力，内存已无法回收'

  it('不传 note 时不产出标记与浮层（常规指标不该多出节点）', async () => {
    const html = await render({ value: 50 })
    expect(markAttr(html)).toBe('')
    expect(html).not.toContain('nv-mark')
    expect(html).not.toContain('nv-tip')
  })

  it('note 非空 → 条右端出标记、浮层带原文', async () => {
    const html = await render({ value: 98, note: NOTE })
    expect(markAttr(html)).toContain('class="nv-mark nv-warm"')
    expect(tipText(html)).toBe(NOTE)
  })

  it('noteTone 决定标记配色（swap-full 用 hot，其余用 warm）', async () => {
    const warm = markAttr(await render({ note: NOTE }))
    const hot = markAttr(await render({ note: NOTE, noteTone: 'nv-hot' }))
    expect(warm).toContain('nv-warm')
    expect(hot).toContain('nv-hot')
    expect(hot).not.toContain('nv-warm')
  })

  it('标记带 role + aria-label —— 提示不只存在于 hover（读屏也能拿到）', async () => {
    const a = markAttr(await render({ note: NOTE }))
    expect(a).toContain('role="img"')
    expect(a).toContain(`aria-label="${NOTE}"`)
  })

  it('条为空（0%）时标记依然挂得住 —— gpu-idle 正是「算力 0% 而显存高」', async () => {
    const html = await render({ value: 0, note: NOTE })
    expect(markAttr(html)).toContain('nv-mark')
    expect(pctOf(fill(html).style, 'width')).toBe(0)
  })

  it('标记位于装饰层之内，但不被它们的 aria-hidden 吞掉', async () => {
    const html = await render({ value: 60, note: NOTE })
    expect(section(html, 'nv-bar')).toContain('nv-mark')
    expect(markAttr(html)).not.toContain('aria-hidden')
    expect(tagOf(html, 'nv-bar')).toContain('aria-hidden="true"')
  })

  it('浮层对读屏隐藏（内容已由标记的 aria-label 承载，避免重复播报）', async () => {
    expect(tagOf(await render({ note: NOTE }), 'nv-tip')).toContain('aria-hidden="true"')
  })

  it('提示文字只以「标记 aria-label + 浮层」两种形式出现，没有第三份常驻文字', async () => {
    const html = await render({ value: 60, note: NOTE })
    expect((html.match(/SWAP 已满/g) ?? []).length).toBe(2)
  })
})
