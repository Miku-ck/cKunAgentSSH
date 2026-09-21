/**
 * nvtop 面板取色 / 量值映射的回归测试。
 *
 * 核心锁定对象：**量值必须被几何编码**（进度条用长度、趋势薄条用高度）。
 * 原实现里每格都是 `height: 100%`、只按三档换色，于是
 *   `blkCls(0)` === `blkCls(65)` === 'nv-cool'，且高度都恒为满格
 * —— 0% 与 65% 渲染成完全相同的实心绿条，图形不携带任何幅度信息。
 * 下面的 `fillPct` 用例就是防止这个缺陷被改回去。
 */
import { describe, expect, it } from 'vitest'

import {
  BAR_GRID,
  THRESHOLDS,
  averageSeries,
  bandCls,
  bandFnOf,
  detectDivergences,
  fillPct,
  lastOf,
  loadBandCls,
  loadChartMax,
  peakOf,
  swapPctOf,
  type BandKind,
} from '@/utils/nvtop-scale'

describe('fillPct —— 条带高度归一（本缺陷的修复点）', () => {
  it('0% 与 65% 必须得到不同的柱高（原实现两者都是满格）', () => {
    expect(fillPct(0)).toBe(0)
    expect(fillPct(65)).toBe(65)
    expect(fillPct(0)).not.toBe(fillPct(65))
  })

  it('按量值线性归一', () => {
    expect(fillPct(50)).toBe(50)
    expect(fillPct(100)).toBe(100)
    expect(fillPct(12.4)).toBeCloseTo(12.4, 6)
  })

  it('支持自定义上限（负载不是百分比，用峰值×1.5 归一）', () => {
    expect(fillPct(6.44, 9.66)).toBeCloseTo(66.67, 1)
    expect(fillPct(9.66, 9.66)).toBe(100)
  })

  it('超限截断，不产生越界柱高', () => {
    expect(fillPct(140)).toBe(100)
    expect(fillPct(-3)).toBe(0)
  })

  it('非有限值与非法上限退化为 0，不做除零', () => {
    expect(fillPct(NaN)).toBe(0)
    expect(fillPct(Infinity)).toBe(0)
    expect(fillPct(50, 0)).toBe(0)
    expect(fillPct(50, -1)).toBe(0)
  })
})

describe('bandCls —— 百分比阈值三档', () => {
  it('60 / 80 为分界：≥60 警戒、>80 危险、边界归入低一档', () => {
    expect(bandCls(0)).toBe('nv-cool')
    expect(bandCls(59.9)).toBe('nv-cool')
    expect(bandCls(60)).toBe('nv-warm')
    expect(bandCls(80)).toBe('nv-warm')
    expect(bandCls(80.1)).toBe('nv-hot')
    expect(bandCls(100)).toBe('nv-hot')
  })

  it('取色与量值解耦：0 与 59 同色但柱高不同（颜色只作阈值辅助）', () => {
    expect(bandCls(0)).toBe(bandCls(59))
    expect(fillPct(0)).not.toBe(fillPct(59))
  })
})

describe('loadBandCls —— 负载用经验阈值而非百分比', () => {
  it('4 / 8 为分界', () => {
    expect(loadBandCls(0)).toBe('nv-cool')
    expect(loadBandCls(3.99)).toBe('nv-cool')
    expect(loadBandCls(4)).toBe('nv-warm')
    expect(loadBandCls(7.99)).toBe('nv-warm')
    expect(loadBandCls(8)).toBe('nv-hot')
  })

  it('与百分比阈值不同：负载 6.44（≥4）已是警戒色，而按百分比口径仍低于 60', () => {
    expect(loadBandCls(6.44)).toBe('nv-warm')
    expect(bandCls(6.44)).toBe('nv-cool')
  })
})

describe('bandFnOf —— 档位字符串到档位函数的映射', () => {
  it('pct → 百分比阈值，load → 负载阈值', () => {
    expect(bandFnOf('pct')(66)).toBe('nv-warm')
    expect(bandFnOf('load')(5)).toBe('nv-warm')
    expect(bandFnOf('pct')(5)).toBe('nv-cool')
    expect(bandFnOf('load')(66)).toBe('nv-hot')
  })

  it('返回的一定是**函数**，且调用后得到合法 class 字符串', () => {
    // 这里存在过的缺陷：组件把档位以「函数型 prop + 工厂式 default」传递，
    // 而 Vue 对 type: Function 的 prop 不调用 default 工厂，
    // 导致拿到的是包装函数本身 → cls(v) 返回函数 → class 解析为空串 → 柱子透明。
    // 断言「是函数」不够，必须断言「调用后是合法 class」。
    for (const kind of ['pct', 'load'] as BandKind[]) {
      const fn = bandFnOf(kind)
      expect(typeof fn).toBe('function')
      for (const v of [0, 5, 66, 100]) {
        expect(['nv-cool', 'nv-warm', 'nv-hot']).toContain(fn(v))
      }
    }
  })

  it('两个档位覆盖所有取值且互不返回空串', () => {
    for (const v of [0, 3.99, 4, 7.99, 8, 65.9, 66, 89.9, 90, 100]) {
      expect(bandFnOf('pct')(v)).not.toBe('')
      expect(bandFnOf('load')(v)).not.toBe('')
    }
  })
})

describe('BAR_GRID —— 进度条内的参考线位置', () => {
  it('三条线，落在条宽 0–100 之间且递增', () => {
    expect(BAR_GRID).toHaveLength(3)
    expect([...BAR_GRID]).toEqual([...BAR_GRID].slice().sort((a, b) => a - b))
    for (const p of BAR_GRID) {
      expect(p).toBeGreaterThan(0)
      expect(p).toBeLessThan(100)
    }
  })

  it('是 25 / 50 / 75 四等分刻度', () => {
    // 换成横向条后刻度必须落在 X 轴；原竖向版是 bottom: p% 的 Y 轴刻度
    expect([...BAR_GRID]).toEqual([25, 50, 75])
  })
})

describe('peakOf / loadChartMax / lastOf', () => {
  it('空序列取 0，不抛异常', () => {
    expect(peakOf([])).toBe(0)
    expect(lastOf([])).toBe(0)
  })

  it('峰值与末值', () => {
    expect(peakOf([3, 12.4, 5])).toBe(12.4)
    expect(lastOf([3, 12.4, 5])).toBe(5)
  })

  it('图上限定为 1 下限，全 0 序列也不至于除零', () => {
    expect(loadChartMax([])).toBe(1)
    expect(loadChartMax([0, 0])).toBe(1)
    expect(loadChartMax([4])).toBe(6)
  })
})

describe('swapPctOf', () => {
  it('未启用 swap 时返回 0 而非除零', () => {
    expect(swapPctOf({ used: 0, total: 0 })).toBe(0)
  })

  it('按比例计算并截断在 100', () => {
    expect(swapPctOf({ used: 2, total: 4 })).toBe(50)
    expect(swapPctOf({ used: 5, total: 4 })).toBe(100)
  })
})

describe('averageSeries —— 多卡均值序列', () => {
  it('无有效序列时返回空', () => {
    expect(averageSeries([])).toEqual([])
    expect(averageSeries([[], []])).toEqual([])
  })

  it('长度不齐时按尾部对齐，截断到最短', () => {
    expect(averageSeries([[1, 2, 3], [10, 20]])).toEqual([6, 11.5])
  })

  it('单序列原样返回', () => {
    expect(averageSeries([[4, 8]])).toEqual([4, 8])
  })
})

describe('detectDivergences —— 矛盾态提示', () => {
  const healthy = {
    load1: 1.2,
    cpuUsed: 35,
    swapPct: 10,
    swapEnabled: true,
    gpuMemAvg: 30,
    gpuUtilAvg: 70,
  }

  it('健康快照不产生任何提示', () => {
    expect(detectDivergences(healthy)).toEqual([])
  })

  it('实测快照：负载 6.44 / CPU 4.7% / SWP 100% / 显存 65.3% / 算力 0% → 三条全中', () => {
    const notes = detectDivergences({
      load1: 6.44,
      cpuUsed: 4.7,
      swapPct: 100,
      swapEnabled: true,
      gpuMemAvg: 65.3,
      gpuUtilAvg: 0,
    })
    expect(notes.map(n => n.key)).toEqual(['load-cpu', 'swap-full', 'gpu-idle'])
    expect(notes.find(n => n.key === 'swap-full')?.tone).toBe('nv-hot')
    expect(notes.find(n => n.key === 'load-cpu')?.tone).toBe('nv-warm')
  })

  it('负载与 CPU 背离的边界：CPU 恰好等于阈值不算背离', () => {
    const at = { ...healthy, load1: THRESHOLDS.loadHigh, cpuUsed: THRESHOLDS.cpuLow }
    expect(detectDivergences(at).some(n => n.key === 'load-cpu')).toBe(false)

    const below = { ...healthy, load1: THRESHOLDS.loadHigh, cpuUsed: THRESHOLDS.cpuLow - 0.1 }
    expect(detectDivergences(below).some(n => n.key === 'load-cpu')).toBe(true)
  })

  it('负载低时即使 CPU 低也不报警（不是背离）', () => {
    const low = { ...healthy, load1: 0.4, cpuUsed: 2 }
    expect(detectDivergences(low)).toEqual([])
  })

  it('swap 未启用时不报满（total 为 0）', () => {
    const off = { ...healthy, swapPct: 0, swapEnabled: false }
    expect(detectDivergences(off).some(n => n.key === 'swap-full')).toBe(false)
  })

  it('swap 到阈值才算满', () => {
    expect(detectDivergences({ ...healthy, swapPct: THRESHOLDS.swapFull - 0.1, swapEnabled: true })
      .some(n => n.key === 'swap-full')).toBe(false)
    expect(detectDivergences({ ...healthy, swapPct: THRESHOLDS.swapFull, swapEnabled: true })
      .some(n => n.key === 'swap-full')).toBe(true)
  })

  it('显存占用但算力为零才算占卡不干活；算力恢复即消失', () => {
    const busy = { ...healthy, gpuMemAvg: 90, gpuUtilAvg: THRESHOLDS.gpuUtilLow }
    expect(detectDivergences(busy).some(n => n.key === 'gpu-idle')).toBe(false)

    const idle = { ...healthy, gpuMemAvg: THRESHOLDS.gpuMemHigh, gpuUtilAvg: THRESHOLDS.gpuUtilLow - 0.1 }
    expect(detectDivergences(idle).some(n => n.key === 'gpu-idle')).toBe(true)
  })

  it('显存占用不足一半时不误报（算力 0 也可能是真空闲）', () => {
    const fresh = { ...healthy, gpuMemAvg: THRESHOLDS.gpuMemHigh - 0.1, gpuUtilAvg: 0 }
    expect(detectDivergences(fresh)).toEqual([])
  })

  it('提示文案带具体数值，便于直接定位问题', () => {
    const notes = detectDivergences({
      load1: 6.44, cpuUsed: 4.7, swapPct: 100, swapEnabled: true, gpuMemAvg: 65.3, gpuUtilAvg: 0,
    })
    expect(notes.find(n => n.key === 'load-cpu')?.text).toContain('6.44')
    expect(notes.find(n => n.key === 'load-cpu')?.text).toContain('4.7')
    expect(notes.find(n => n.key === 'gpu-idle')?.text).toContain('65.3')
  })
})
