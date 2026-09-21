/**
 * nvitop 面板的**双层量值图**（渲染函数组件）。
 *
 * 结构（自上而下）：
 *   .nv-bar    12px 横向进度条 —— 长度编码**当前值**，填充按 66/90（或 4/8）变色
 *                有 note 时其右端另挂一个警告三角 .nv-mark
 *   .nv-trend   4px 趋势薄条   —— 60 格历史，高度编码，**统一暗色**不参与变色
 *   .nv-tip    悬浮提示        —— note 正文，默认不可见，由父级 :hover 显形（无 JS 状态）
 *
 * 单独成文件的**唯一目的**：能被 SSR 直接渲染，从而对渲染结果写真实回归测试。
 * 内联在 `<script setup>` 里的组件无法从测试中导入，只能靠读源码断言 —— 而这个
 * 组件恰恰出过「读源码看不出问题、渲染出来才有问题」的缺陷（见 kind prop 的说明）。
 */
import { defineComponent, h } from 'vue'
import { BAR_GRID, bandFnOf, fillPct, type BandKind } from '@/utils/nvtop-scale'

/** 趋势薄条的窗口点数（与 store 的 HISTORY_MAX 对齐，5s/点 ≈ 5 分钟） */
export const NV_WINDOW = 60

export const NvGauge = defineComponent({
  name: 'NvGauge',
  props: {
    /** 历史序列，仅用于下方的趋势薄条 */
    values: { type: Array as () => number[], default: () => [] },
    /**
     * **当前值** —— 进度条长度与填充色都由它决定。
     *
     * 刻意**不**从 `values` 末值推导：无历史的指标（如 SWP）也有当前值，
     * 而负载行的当前值取的是 1 分钟分量、未必等于历史序列末点。
     * 显式传入可以让"条长"与"右侧数字"永远指向同一个数。
     */
    value: { type: Number, default: 0 },
    /** 归一到多少为满格（负载等非百分比量需显式给） */
    max: { type: Number, default: 100 },
    /**
     * 取色档位：'pct' = 百分比（66/90），'load' = 负载（4/8）。
     *
     * **必须是字符串而非函数**。历史实现写成
     * `cls: { type: Function, default: () => bandCls }`，踩了 Vue 的语义陷阱：
     * `type: Function` 时 prop 默认值**不会**被当工厂调用
     * （`@vue/runtime-core` resolvePropValue 仅在 `opt.type !== Function` 时调用它），
     * 于是 `cls` 是包装函数本身 → `cls(v)` 返回函数 → `normalizeClass` 对函数返回
     * 空串 → 柱子 `class=""`、无背景色、看起来是空轨。
     * 凡是**不传 `cls` 的调用点**都会中招，显式传了的反而正常 —— 这正是当时难发现的原因。
     */
    kind: { type: String as () => BandKind, default: 'pct' },
    /**
     * 矛盾态 / 资源异常的提示文案（如「SWAP 已满 · 存在换页压力…」）。
     *
     * 非空时在进度条右端挂一个警告三角，并把这段文字放进**悬浮提示**，
     * 而不是常驻一行文字 —— 常驻行会撑高该指标所在的行，破坏「一行一指标」的对齐。
     * 刻意**保留标记**（而不是纯靠 hover）：否则「哪一行有问题」必须逐条摸一遍才知道，
     * 这与本面板「不把矛盾留给用户自己发现」的意图相反。
     */
    note: { type: String, default: '' },
    /** 警告标记的配色档位，取值同取色 class（'nv-warm' | 'nv-hot'） */
    noteTone: { type: String, default: 'nv-warm' },
  },
  setup(p) {
    return () => {
      const band = bandFnOf(p.kind)

      /* 警告标记：进度条右端的小三角。
         必须带深色描边（见父组件 CSS）—— 它的填充色与进度条填充同色系，
         条长接近满格时会糊在一起。 */
      const mark = p.note
        ? h('svg', {
            class: ['nv-mark', p.noteTone],
            viewBox: '0 0 9 9',
            role: 'img',
            'aria-label': p.note,
          }, [h('path', { d: 'M4.5 0.6 L8.4 8.2 L0.6 8.2 Z' })])
        : null

      /* ── 上层：12px 横向进度条，长度 = 当前值 ── */
      const fill = h('i', {
        class: band(p.value),
        style: { width: `${fillPct(p.value, p.max).toFixed(1)}%` },
      })
      // 参考线随形态转轴：竖向条带时代是 bottom:p% 的 Y 轴刻度，现在是 X 轴竖线
      const grids = BAR_GRID.map(g => h('i', {
        class: 'nv-grid',
        style: { left: `${g}%` },
      }))
      // mark 排在最后：与填充、参考线同为定位元素，DOM 靠后者压在上面
      const bar = h('span', { class: 'nv-bar', 'aria-hidden': 'true' }, [fill, ...grids, mark])

      /* ── 下层：4px 趋势薄条，高度编码且统一暗色（不按阈值变色） ── */
      const vals = p.values.slice(-NV_WINDOW)
      // 无数据的格子用满高暗格，与「值为 0」（0 高度）区分
      const blanks = Array.from({ length: NV_WINDOW - vals.length }, () =>
        h('i', { class: 'nv-blank' }))
      const cells = vals.map(v => h('i', {
        style: { height: `${fillPct(v, p.max).toFixed(1)}%` },
      }))
      const trend = h('span', { class: 'nv-trend', 'aria-hidden': 'true' }, [...blanks, ...cells])

      /* ── 悬浮提示：纯 CSS 显形（父级 .nv-plot:hover 规则），此处不持有任何 JS 状态。
         定位上下文是 .nv-plot（position:relative），故落在进度条下方、右对齐。
         对读屏隐藏 —— 内容已由 .nv-mark 的 aria-label 承载，否则会重复播报。 ── */
      const tip = p.note ? h('span', { class: 'nv-tip', 'aria-hidden': 'true' }, p.note) : null

      return h('span', { class: 'nv-gauge' }, [bar, trend, tip])
    }
  },
})
