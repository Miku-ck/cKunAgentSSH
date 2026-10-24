/**
 * nvitop 面板的取色与量值映射（纯函数，便于单测）。
 *
 * 阈值说明：本面板与 DashboardView 顶部指标卡/磁盘条**共用同一档**：
 * 正常 <60 绿 / 警戒 ≥60 黄 / 危险 >80 红。统一阈值见 bandCls 与 toneCls。
 */

/**
 * 百分比三档阈值。**单一来源**：本文件的 bandCls 与 DashboardView 的
 * 顶部指标卡/磁盘条/进程表 toneCls 都读这四个数（曾各写一遍字面量，
 * 改一处漏一处就让两块面板同值不同色）。
 */
export const PCT_HOT = 80
export const PCT_WARM = 60

/** 负载单机经验阈值。同上：loadBandCls 与 DashboardView 负载卡同源。 */
export const LOAD_HOT = 8
export const LOAD_WARM = 4

/**
 * 百分比三档：>80 危险 / ≥60 警戒 / 其余正常。
 * 统一阈值（与 DashboardView 顶部指标卡/磁盘条一致）：正常 <60 绿 / ≥60 黄 / >80 红。 */
export function bandCls(v: number): string {
  if (v > PCT_HOT) return 'nv-hot'
  if (v >= PCT_WARM) return 'nv-warm'
  return 'nv-cool'
}

/** 负载不是百分比，用单机经验阈值：≥8 危险 / ≥4 警戒 */
export function loadBandCls(v: number): string {
  if (v >= LOAD_HOT) return 'nv-hot'
  if (v >= LOAD_WARM) return 'nv-warm'
  return 'nv-cool'
}

/** 取色档位。用**字符串**而非函数 prop 传递，理由见 bandFnOf 注释。 */
export type BandKind = 'pct' | 'load'

/**
 * 取色档位 → 档位函数。
 *
 * 为什么是字符串而非直接传函数：组件里曾写成
 * `cls: { type: Function, default: () => bandCls }`，而 Vue 对 `type: Function`
 * 的 prop **不会**把 default 当工厂调用（`@vue/runtime-core` 的 resolvePropValue
 * 只在 `opt.type !== Function` 时执行 `defaultValue.call(null, props)`），
 * 于是 `p.cls` 是那个包装函数本身 → `p.cls(v)` 返回函数 →
 * `normalizeClass` 对函数返回空串（isObject 只认 `typeof === 'object'`）
 * → 柱子 `class=""` → 无背景色 → **条带看起来是空轨**。
 *
 * 改用字符串档位后，默认值走普通赋值路径，该类陷阱从根上消除；且映射逻辑
 * 落在纯函数里，可直接单测。
 */
export function bandFnOf(kind: BandKind): (v: number) => string {
  return kind === 'load' ? loadBandCls : bandCls
}

/**
 * 把采样值归一为柱体高度百分比。
 *
 * **这是「历史条带不携带幅度」缺陷的核心修复**：原实现所有格子恒为
 * `height: 100%`，只按三档换色，因此 0% 与 65% 渲染成完全相同的实心条。
 * 归一后柱体高度才是量值的载体，颜色降级为阈值辅助。
 */
export function fillPct(v: number, max = 100): number {
  if (!Number.isFinite(v) || max <= 0) return 0
  return Math.max(0, Math.min(100, (v / max) * 100))
}

/**
 * 横向进度条内的参考线位置（占条宽百分比）。
 *
 * 形态从竖向历史条带改为横向进度条后，刻度必须**跟着转轴**：原来是
 * `bottom: p%` 的 Y 轴横线，现在必须是 `left: p%` 的 X 轴竖线。
 * 若不转，刻度与长度编码的方向互相垂直 —— 一条 87% 的条配上竖向刻度，
 * 读数只能靠猜。
 */
export const BAR_GRID = [25, 50, 75] as const

export function peakOf(series: number[]): number {
  return series.length ? Math.max(...series) : 0
}

/** 负载图上限：峰值 ×1.5 避免曲线贴顶；下限 1 以免全 0 时除零 */
export function loadChartMax(series: number[]): number {
  return Math.max(1, peakOf(series) * 1.5)
}

export function lastOf(series: number[]): number {
  return series.length ? series[series.length - 1] : 0
}

/** swap 使用率（无 swap 时返回 0） */
export function swapPctOf(swap: { used: number; total: number }): number {
  return swap.total > 0 ? Math.min(100, (swap.used / swap.total) * 100) : 0
}

/**
 * 平均序列：多个序列逐点求均值（长度取最短 —— 采样来自同一轮 push，
 * 长度差异只出现在首轮补齐，截断到最短即可对齐）。
 */
export function averageSeries(lists: number[][]): number[] {
  const valid = lists.filter(l => l.length > 0)
  if (!valid.length) return []
  const n = Math.min(...valid.map(l => l.length))
  return Array.from({ length: n }, (_, i) =>
    valid.reduce((s, l) => s + l[l.length - n + i], 0) / valid.length)
}

export const THRESHOLDS = {
  /** 负载高于此值 */
  loadHigh: 4,
  /** 且 CPU 低于此值 → 负载与 CPU 背离（D-state / IO 等待） */
  cpuLow: 20,
  /** swap 使用率高于此值 → 判定已满 */
  swapFull: 95,
  /** 显存占用高于此值 */
  gpuMemHigh: 50,
  /** 且算力低于此值 → 占卡不干活 */
  gpuUtilLow: 5,
} as const

interface Divergence {
  key: 'load-cpu' | 'swap-full' | 'gpu-idle'
  tone: 'nv-warm' | 'nv-hot'
  text: string
}

interface DivergenceInput {
  load1: number
  cpuUsed: number
  swapPct: number
  swapEnabled: boolean
  gpuMemAvg: number
  gpuUtilAvg: number
}

/**
 * 检测「数据自己在喊、但 UI 不说」的矛盾态。
 * 纯函数，只负责判定与文案；组件负责把结果渲染成 图标 + 文字（不靠颜色单独表意）。
 */
export function detectDivergences(input: DivergenceInput): Divergence[] {
  const out: Divergence[] = []
  const t = THRESHOLDS

  if (input.load1 >= t.loadHigh && input.cpuUsed < t.cpuLow) {
    out.push({
      key: 'load-cpu',
      tone: 'nv-warm',
      text: `负载 ${input.load1.toFixed(2)} 而 CPU 仅 ${input.cpuUsed.toFixed(1)}% · 疑似 IO 等待`,
    })
  }

  if (input.swapEnabled && input.swapPct >= t.swapFull) {
    out.push({
      key: 'swap-full',
      tone: 'nv-hot',
      text: 'SWAP 已满 · 存在换页压力，内存已无法回收',
    })
  }

  if (input.gpuMemAvg >= t.gpuMemHigh && input.gpuUtilAvg < t.gpuUtilLow) {
    out.push({
      key: 'gpu-idle',
      tone: 'nv-warm',
      text: `显存占用 ${input.gpuMemAvg.toFixed(1)}% 而算力仅 ${input.gpuUtilAvg.toFixed(1)}% · 占卡不干活`,
    })
  }

  return out
}
