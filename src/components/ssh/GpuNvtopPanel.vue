<template>
  <div class="nvtop">
    <!-- 顶部条：CUDA 驱动版本 + 主机 -->
    <div class="nv-bar nv-head">
      <span>Driver Version: {{ metrics.gpuDriver || '—' }}　CUDA Driver Version: {{ metrics.gpuCuda || '—' }}</span>
      <span class="nv-grow"></span>
      <span class="nv-dim">{{ metrics.host || '' }}</span>
    </div>

    <!-- 列头 -->
    <div class="nv-row nv-colhead">
      <div class="nv-cells">
        <span>GPU</span><span>Name</span><span>Fan</span><span>Temp</span><span>Perf</span>
        <span class="nv-c-pwr">Pwr:Usage/Cap</span><span>Bus-Id</span><span>Memory-Usage</span>
        <span>GPU-Util</span><span>ECC Fl</span><span>Compute M.</span>
      </div>
      <div class="nv-hists nv-hists-head">
        <span class="nv-dim">MEM</span><span class="nv-dim">UTL</span>
      </div>
    </div>

    <!-- 每卡一行 -->
    <div v-for="g in metrics.gpus" :key="g.index" class="nv-row">
      <div class="nv-cells">
        <span class="nv-idx" :class="blkCls(g.utilization)">{{ g.index }}</span>
        <span class="nv-name" :title="g.name">{{ g.name }}</span>
        <span>{{ g.fanSpeed == null ? 'N/A' : g.fanSpeed.toFixed(0) + '%' }}</span>
        <span :class="g.temperature > 80 ? 'nv-hot' : ''">{{ g.temperature.toFixed(0) }}C</span>
        <span>{{ g.perfState || '—' }}</span>
        <span class="nv-c-pwr" :class="pwrCls(g)">{{ fmt(g.powerDraw, 'W') }} / {{ g.powerLimit != null ? g.powerLimit.toFixed(0) + 'W' : '?' }}</span>
        <span class="nv-dim">{{ shortBus(g.busId) }}</span>
        <span :class="blkCls(g.memPercent)">{{ (g.memUsed / 1024).toFixed(2) }}GiB / {{ (g.memTotal / 1024).toFixed(2) }}GiB</span>
        <span :class="blkCls(g.utilization)">{{ g.utilization.toFixed(0) }}%</span>
        <span>{{ g.eccUncorr == null ? 'N/A' : g.eccUncorr }}</span>
        <span>{{ g.computeMode || '—' }}</span>
      </div>
      <div class="nv-hists">
        <div class="nv-hist">
          <span class="nv-hist-label">MEM:</span>
          <NvGauge :values="hist(g.index)?.mem || []" :value="g.memPercent" />
          <span class="nv-hist-val" :class="blkCls(g.memPercent)">{{ g.memPercent.toFixed(1) }}%</span>
        </div>
        <div class="nv-hist">
          <span class="nv-hist-label">UTL:</span>
          <NvGauge :values="hist(g.index)?.util || []" :value="g.utilization" />
          <span class="nv-hist-val" :class="blkCls(g.utilization)">{{ g.utilization.toFixed(0) }}%</span>
        </div>
      </div>
    </div>

    <!-- 系统区：一行一指标（名称 / 当前值 / 历史图 / 峰值），底部共享单一时间轴 -->
    <div class="nv-bar nv-sysbar">System</div>
    <div class="nv-sys">
      <div v-for="row in sysRows" :key="row.name" class="nv-sys-item">
        <span class="nv-sys-name">{{ row.name }}</span>

        <span class="nv-sys-val" :class="row.toneCls">
          {{ row.value }}
          <span v-if="row.sub" class="nv-sys-sub">{{ row.sub }}</span>
        </span>

        <!-- 背离 / 资源异常：不靠颜色单独表意 —— 条右端挂警告标记，
             文字进 hover 浮层（常驻一行会把该行撑高、破坏「一行一指标」的对齐） -->
        <div class="nv-plot">
          <NvGauge
            :values="row.values"
            :value="row.raw"
            :max="row.max"
            :kind="row.kind"
            :note="row.note"
            :note-tone="row.noteTone"
          />
        </div>

        <span class="nv-sys-peak" :class="row.peakTone">{{ row.peak }}</span>
      </div>

      <div class="nv-axis">
        <span>← 5 分钟前</span>
        <span>现在 →</span>
      </div>
    </div>

    <!-- 进程表 -->
    <div class="nv-bar nv-procbar">
      <span>Processes:</span>
      <span class="nv-grow"></span>
      <span class="nv-host-chip">{{ metrics.host || 'no-host' }}</span>
    </div>
    <table v-if="metrics.gpuProcs.length" class="nv-proc-table">
      <thead>
        <tr><th>GPU</th><th>PID</th><th>USER</th><th>GPU-MEM</th><th>%CPU</th><th>%MEM</th><th>TIME</th><th>COMMAND</th></tr>
      </thead>
      <tbody>
        <tr v-for="p in metrics.gpuProcs" :key="p.gpuIndex + ':' + p.pid">
          <td>{{ p.gpuIndex }}</td>
          <td>{{ p.pid }}</td>
          <td>{{ p.user }}</td>
          <td>{{ p.memMB }}MiB</td>
          <td>{{ p.cpu == null ? '—' : p.cpu.toFixed(1) }}</td>
          <td>{{ p.mem == null ? '—' : p.mem.toFixed(1) }}</td>
          <td>{{ p.etime || '—' }}</td>
          <td class="nv-cmd" :title="p.name">{{ p.name }}</td>
        </tr>
      </tbody>
    </table>
    <div v-else class="nv-noproc">无占用 GPU 的进程</div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { GpuSample, ServerMetrics } from '@/stores/server-monitor-core'
import { NvGauge } from './NvGauge'
import {
  averageSeries,
  bandCls,
  detectDivergences,
  lastOf,
  loadBandCls,
  loadChartMax,
  peakOf,
  swapPctOf,
  type BandKind,
} from '@/utils/nvtop-scale'

const props = defineProps<{
  metrics: ServerMetrics
  gpuHist: Record<string, { util: number[]; mem: number[] }>
  cpuHist: number[]
  memHist: number[]
  loadHist: number[]
}>()

/* 取色与量值映射见 @/utils/nvtop-scale（纯函数，有单测覆盖）。
   模板中 blkCls 出现十余次，保留同名别名以缩小改动面。
   参考线（25/50/75）已随进度条移入 NvGauge 内部。 */
const blkCls = bandCls

function fmt(v: number | null, unit: string): string {
  return v == null ? '—' : `${v.toFixed(0)}${unit}`
}

function hist(idx: number) {
  return props.gpuHist[String(idx)]
}

/** Bus-Id 只留有效段（00000000:41:00.0 → 41:00.0），全宽放得下更少的卡也不挤 */
function shortBus(bus: string): string {
  const parts = bus.split(':')
  return parts.length >= 3 ? parts.slice(-2).join(':') : bus || '—'
}

function pwrCls(g: GpuSample): string {
  if (g.powerDraw == null || g.powerLimit == null || g.powerLimit <= 0) return ''
  return blkCls((g.powerDraw / g.powerLimit) * 100)
}

const swap = computed(() => swapPctOf(props.metrics.swap))

/** 每卡历史按采样对齐（同一轮 push），逐点求均值 → nvtop 的 AVG GPU 图 */
function avgHist(key: 'util' | 'mem'): number[] {
  return averageSeries(props.metrics.gpus.map(g => hist(g.index)?.[key] ?? []))
}
const avgMemHist = computed(() => avgHist('mem'))
const avgUtilHist = computed(() => avgHist('util'))
const avgMemNow = computed(() => lastOf(avgMemHist.value))
const avgUtilNow = computed(() => lastOf(avgUtilHist.value))

/* 矛盾态：判定与文案是纯函数（@/utils/nvtop-scale 已单测），这里只收集输入 */
const divergences = computed(() => detectDivergences({
  load1: props.metrics.load[0],
  cpuUsed: props.metrics.cpu.used,
  swapPct: swap.value,
  swapEnabled: props.metrics.swap.total > 0,
  gpuMemAvg: avgMemNow.value,
  gpuUtilAvg: avgUtilNow.value,
}))

function noteOf(key: 'load-cpu' | 'swap-full' | 'gpu-idle') {
  return divergences.value.find(d => d.key === key)
}

interface SysRow {
  name: string
  value: string
  sub?: string
  /** 历史序列；Swp 这类无历史的指标留空数组（趋势薄条只显示暗格占位） */
  values: number[]
  /** 历史归一到多少为满格（进度条与趋势薄条共用同一把尺） */
  max: number
  /**
   * **当前值** —— 进度条的填充长度取它。
   *
   * 注意：这里**不再**是"仅 values 为空时用的兜底值"。曾被写成 `raw: 0`（Load 行）
   * 因为当时它只服务 `.nv-meter` 那条走不到的分支 —— 现在它是条长的唯一来源，
   * 留 0 就会渲染成一根空条。
   */
  raw: number
  /** 取色档位（字符串，避免函数型 prop 的默认值陷阱，见 nvtop-scale.bandFnOf） */
  kind: BandKind
  toneCls: string
  peak: string
  peakTone?: string
  note?: string
  noteTone?: string
}

const sysRows = computed<SysRow[]>(() => {
  const m = props.metrics
  const load1 = m.load[0]
  const sw = swap.value
  const swapOn = m.swap.total > 0
  const avgMem = avgMemNow.value
  const avgUtil = avgUtilNow.value
  const loadMax = loadChartMax(props.loadHist)
  const loadPeak = peakOf(props.loadHist)
  const cpuPeak = peakOf(props.cpuHist)
  const memPeak = peakOf(props.memHist)
  const gmemPeak = peakOf(avgMemHist.value)
  const gutlPeak = peakOf(avgUtilHist.value)

  const rows: SysRow[] = [
    {
      name: 'Load Average',
      value: load1.toFixed(2),
      sub: `5m ${m.load[1].toFixed(2)} · 15m ${m.load[2].toFixed(2)}`,
      values: props.loadHist,
      max: loadMax,
      raw: load1,
      kind: 'load',
      toneCls: loadBandCls(load1),
      peak: `峰 ${loadPeak.toFixed(2)}`,
      peakTone: loadBandCls(loadPeak),
      note: noteOf('load-cpu')?.text,
      noteTone: noteOf('load-cpu')?.tone,
    },
    {
      name: 'CPU',
      value: `${m.cpu.used.toFixed(1)}%`,
      sub: `iowait ${m.cpu.iowait.toFixed(1)}%`,
      values: props.cpuHist,
      max: 100,
      raw: m.cpu.used,
      kind: 'pct',
      toneCls: blkCls(m.cpu.used),
      peak: `峰 ${cpuPeak.toFixed(1)}%`,
      peakTone: blkCls(cpuPeak),
    },
    {
      name: 'MEM',
      value: `${m.mem.percent.toFixed(1)}%`,
      sub: `${(m.mem.used / 1024).toFixed(1)} / ${(m.mem.total / 1024).toFixed(1)} GiB`,
      values: props.memHist,
      max: 100,
      raw: m.mem.percent,
      kind: 'pct',
      toneCls: blkCls(m.mem.percent),
      peak: `峰 ${memPeak.toFixed(1)}%`,
      peakTone: blkCls(memPeak),
    },
    {
      name: 'SWP',
      value: swapOn ? `${sw.toFixed(1)}%` : '—',
      sub: swapOn
        ? `${(m.swap.used / 1024).toFixed(1)} / ${(m.swap.total / 1024).toFixed(1)} GiB`
        : '未启用',
      values: [],            // store 未保留 swap 历史 → 趋势薄条只有暗格占位，不伪造趋势
      max: 100,
      raw: sw,
      kind: 'pct',
      toneCls: swapOn ? blkCls(sw) : 'nv-dim',
      peak: swapOn ? `当前 ${sw.toFixed(0)}%` : '',
      peakTone: swapOn ? blkCls(sw) : 'nv-dim',
      note: noteOf('swap-full')?.text,
      noteTone: noteOf('swap-full')?.tone,
    },
    {
      name: 'AVG GPU MEM',
      value: `${avgMem.toFixed(1)}%`,
      values: avgMemHist.value,
      max: 100,
      raw: avgMem,
      kind: 'pct',
      toneCls: blkCls(avgMem),
      peak: `峰 ${gmemPeak.toFixed(1)}%`,
      peakTone: blkCls(gmemPeak),
    },
    {
      name: 'AVG GPU UTL',
      value: `${avgUtil.toFixed(1)}%`,
      values: avgUtilHist.value,
      max: 100,
      raw: avgUtil,
      kind: 'pct',
      toneCls: blkCls(avgUtil),
      peak: `峰 ${gutlPeak.toFixed(1)}%`,
      peakTone: blkCls(gutlPeak),
      note: noteOf('gpu-idle')?.text,
      noteTone: noteOf('gpu-idle')?.tone,
    },
  ]
  return rows
})
</script>

<style scoped>
/* 复刻 nvtop 终端屏：mono 字体、绿/橙/红分色。
   调色板集中此处，随主题切换：所有背景/前景/边框颜色直接引用 variables.css 中的
   项目设计令牌（--bg-base / --text-primary 等），换主题即换皮，无需逐个组件重写。
   终端专属的──名字绿、品牌绿、网格透明度等写死值仍保留在下方。 */
.nvtop {
  --nv-bg: var(--bg-base);
  --nv-bg-bar: var(--bg-elevated);
  --nv-track: var(--bg-input);
  --nv-frame: var(--border-default);
  --nv-border-soft: var(--border-subtle);
  --nv-border-row: var(--border-subtle);
  --nv-ink: var(--text-primary);
  --nv-dim: var(--text-secondary);
  --nv-mute: var(--text-tertiary);
  /* 终端专属色：nvtop 风格刷绿的卡名/芯片。深色下浅绿读性好；浅色主题下自动饱和加深 */
  --nv-name: #9ccc65;
  --nv-brand: var(--accent-green);
  /* 三档取色转发到共享的 --metric-* 令牌，与仪表盘指标卡/磁盘条同源 */
  --nv-cool: var(--metric-cool);
  --nv-warm: var(--metric-warm);
  --nv-hot: var(--metric-hot);
  --nv-blank: var(--bg-input);
  --nv-grid: rgba(255, 255, 255, 0.09);

  font-family: var(--font-mono);
  font-size: 11px;
  /* 原实现用 var(--text-primary)：浅色主题下为 #000000，落在 #050505 底上不可读。
     本面板底色固定为深色，故正文色也必须固定。 */
  color: var(--nv-ink);
  background: var(--nv-bg);
  border: 1px solid var(--nv-frame);
  /* 与仪表盘指标卡 / 磁盘区 / 进程表的 .dash-section 同语言：8px 圆角 + 浅阴影。
     内部仍保持 nvtop 终端复刻的黑底双线风格，但外壳融合到卡片层级体系中 */
  border-radius: 8px;
  box-shadow: var(--shadow-sm);
  padding: 6px 8px;
  display: flex;
  flex-direction: column;
}

.nv-bar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 2px 6px;
  border: 1px solid var(--nv-border-soft);
  background: var(--nv-bg-bar);
}

.nv-head { margin-bottom: 4px; }
.nv-grow { flex: 1; }
.nv-dim { color: var(--nv-dim); }

.nv-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 3px 6px;
  border-bottom: 1px solid var(--nv-border-row);
}

.nv-colhead {
  color: var(--nv-mute);
  border-top: 1px solid var(--nv-border-soft);
  border-bottom: 1px solid var(--nv-border-soft);
  font-size: 10px;
}

.nv-cells {
  display: grid;
  grid-template-columns: 26px 148px 36px 40px 34px 96px 66px 122px 48px 44px 62px;
  gap: 8px;
  align-items: center;
  flex-shrink: 0;
}

.nv-cells > span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.nv-c-pwr { text-align: right; }

.nv-idx { font-weight: 700; }

.nv-name { color: var(--nv-name); }

.nv-hists {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.nv-hists-head {
  flex-direction: row;
  justify-content: space-around;
  align-items: baseline;
}

.nv-hist {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.nv-hist-label { width: 32px; color: var(--nv-mute); }
.nv-hist-val { width: 48px; text-align: right; font-variant-numeric: tabular-nums; }

/* ── 双层量值图（NvGauge，见 ./NvGauge.ts）──
   上层 12px 横向进度条：长度编码**当前值**，填充按 60/80 变色；
   下层 4px 趋势薄条：60 格历史，高度编码，统一暗色（不参与阈值变色）。

   必须是**块级 flex**：系统区父级 .nv-plot 是普通块容器，若用 inline-flex 会触发
   shrink-to-fit，宽度退化成内容宽而不撑满整列（上一轮 .nv-blocks 踩过此坑）。 */
.nv-gauge {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
  min-width: 0;
}

/* 注意 :deep —— `.nv-gauge` 是子组件 NvGauge 的**根元素**（会带本组件的 scoped 属性），
   但 `.nv-bar` / `.nv-trend` 在它**内部**、不带该属性。若这里直接写 `.nv-gauge > .nv-bar`，
   编译后 scoped 属性会被挂到 `.nv-bar` 上，而那是一个永远没有该属性的元素
   → 规则**命中 0 个元素**：产物里能 grep 到 "height: 12px"，页面上却是 0 高。
   实测过这个坑：进度条与趋势条两处漏写，整条量值图直接不显示。
   （这里刻意不写出编译后的选择器字面量，否则它会出现在产物里，
     让"该形态不应存在"的负向断言产生假阳性 —— 本项目踩过这类注释坑。） */
:deep(.nv-gauge > .nv-bar) {
  position: relative;
  height: 12px;          /* 全panel统一厚度，GPU 卡行与 System 区一致 */
  flex: 0 0 12px;
  background: var(--nv-track);
  border-radius: 2px;
  overflow: hidden;
}

/* NvGauge 是独立组件：其**根元素**会带上本组件的 scoped 属性，故 .nv-gauge 可直接选择；
   但其内部元素不带 scoped 属性，必须用 :deep 穿透。 */
:deep(.nv-gauge > .nv-bar > i) {
  display: block;
  height: 100%;
  border-radius: 2px;
  /* 「动态」就是这里：宽度随采样平滑滑动，颜色跨阈值时渐变而非硬跳。
     注：参考线也会命中本规则，故下面单独把它的 transition 关掉。 */
  transition: width 0.5s cubic-bezier(0.4, 0, 0.2, 1), background-color 0.5s linear;
}

:deep(.nv-gauge > .nv-bar > i.nv-cool) { background: var(--nv-cool); }
:deep(.nv-gauge > .nv-bar > i.nv-warm) { background: var(--nv-warm); }
:deep(.nv-gauge > .nv-bar > i.nv-hot) { background: var(--nv-hot); }

/* 参考线：条内**竖向**刻度，让「有多满」可读。
   上一版是竖向条带图，刻度用 bottom: p% 做 Y 轴；形态转横向后刻度必须跟着转轴，
   否则刻度与长度编码方向垂直，读数只能靠猜。 */
:deep(.nv-gauge > .nv-bar > i.nv-grid) {
  position: absolute;
  top: 0;
  bottom: 0;
  height: auto;          /* 由 top/bottom 拉伸，避免与上一条的 height:100% 互相覆盖 */
  width: 1px;
  background: var(--nv-grid);
  border-radius: 0;
  transition: none;
}

/* 同样必须 :deep —— 理由见上面 .nv-gauge > .nv-bar 那段注释（那里也说明了为何不写出
   编译后的选择器字面量：注释会进产物，让负向断言假阳性）。 */
:deep(.nv-gauge > .nv-trend) {
  display: flex;
  align-items: flex-end;
  gap: 1px;
  height: 4px;
  background: var(--nv-track);
  border-radius: 1px;
  overflow: hidden;
}

/* 趋势格统一暗色，不按阈值变色 —— 主体是上方的进度条，趋势只做背景信息 */
:deep(.nv-gauge > .nv-trend > i) {
  flex: 1;
  min-width: 1px;
  background: var(--nv-dim);
  opacity: 0.55;
}

/* 无数据格：满高暗格，与「值为 0」（0 高度）区分 */
:deep(.nv-gauge > .nv-trend > i.nv-blank) {
  height: 100%;
  background: var(--nv-blank);
  opacity: 1;
}

.nv-cool { color: var(--nv-cool); }
.nv-warm { color: var(--nv-warm); }
.nv-hot { color: var(--nv-hot); }

.nv-sysbar { margin: 6px 0 4px; }

/* ── 系统区：一行一指标；时间轴只保留一条 ── */
.nv-sys {
  display: flex;
  flex-direction: column;
  padding: 4px 6px;
  border-bottom: 1px solid var(--nv-border-soft);
}

.nv-sys-item {
  display: grid;
  grid-template-columns: 84px 118px 1fr 58px;
  gap: 8px;
  align-items: center;
  padding: 3px 0;
}

.nv-sys-name {
  color: var(--nv-mute);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.nv-sys-val {
  text-align: right;
  font-variant-numeric: tabular-nums;
  line-height: 1.35;
}

.nv-sys-sub {
  display: block;
  font-size: 10px;
  color: var(--nv-dim);
}

.nv-plot {
  position: relative;
  min-width: 0;
}

.nv-sys-peak {
  text-align: right;
  font-variant-numeric: tabular-nums;
  color: var(--nv-dim);
  white-space: nowrap;
}

/* ── 矛盾态提示：进度条右端的标记 + hover 浮层（两者都由 NvGauge 产出，须 :deep 穿透）── */

/* 标记的填充色与进度条填充同色系（都可能落在同一条绿/黄/红条上），
   故必须带与底色反差描边，否则条长接近满格时标记会糊进填充里、等于没有。 */
:deep(.nv-mark) {
  position: absolute;
  right: 3px;
  top: 50%;
  width: 9px;
  height: 9px;
  transform: translateY(-50%);
  stroke: var(--nv-mark-stroke, var(--nv-bg));
  stroke-width: 0.9;
  fill: var(--nv-warm);
  pointer-events: none;
}

:deep(.nv-mark.nv-warm) { fill: var(--nv-warm); }
:deep(.nv-mark.nv-hot) { fill: var(--nv-hot); }

/* 浮层的定位上下文是 .nv-plot（position: relative），不是 .nv-gauge ——
   后者是 static，故 top:100% 落在**整幅图**（进度条 + 趋势条）的下方。
   默认不可见且不吃鼠标事件，由下面的 .nv-plot:hover 规则显形，不引入任何 JS 状态。 */
:deep(.nv-tip) {
  position: absolute;
  top: calc(100% + 4px);
  right: 0;
  z-index: 6;
  /* 上限取 340px 与「图区列宽」的较小者：窄窗口下自动收窄成多行，不需要 media query */
  max-width: min(340px, 100%);
  padding: 3px 6px;
  color: var(--nv-ink);
  background: var(--nv-bg-bar);
  border: 1px solid var(--nv-border-soft);
  border-radius: 2px;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.65);
  line-height: 1.45;
  opacity: 0;
  visibility: hidden;
  pointer-events: none;
  /* visibility 是离散属性，淡出要显式给它一个延迟，否则 opacity 还没走完就被抽掉 */
  transition: opacity 0.12s ease, visibility 0s linear 0.12s;
}

.nv-plot:hover :deep(.nv-tip) {
  opacity: 1;
  visibility: visible;
  /* 淡入时立即变 visible，否则整段 opacity 动画都看不见 */
  transition: opacity 0.12s ease, visibility 0s;
}

.nv-axis {
  display: flex;
  justify-content: space-between;
  color: var(--nv-dim);
  padding-top: 5px;
  margin-top: 3px;
  border-top: 1px solid var(--nv-border-row);
}

.nv-procbar { margin: 6px 0 2px; }

.nv-host-chip {
  color: var(--nv-bg);
  background: var(--nv-brand);
  padding: 0 6px;
  border-radius: 2px;
  font-weight: 600;
}

.nv-proc-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11px;
}

.nv-proc-table th {
  text-align: left;
  padding: 2px 8px 2px 6px;
  color: var(--nv-mute);
  font-weight: 500;
}

.nv-proc-table td {
  padding: 2px 8px 2px 6px;
  border-top: 1px solid var(--nv-border-row);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.nv-cmd {
  max-width: 480px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.nv-noproc {
  color: var(--nv-dim);
  padding: 4px 6px;
}

/* ── 窄窗口：系统区固定列收窄，图区优先保留 ── */
@media (max-width: 860px) {
  .nv-sys-item { grid-template-columns: 66px 92px 1fr 50px; gap: 6px; }
  .nv-cells { grid-template-columns: 26px 110px 36px 40px 34px 88px 60px 104px 44px 40px 56px; }
}
</style>

<!-- 浅色主题例外覆盖：深色写死的终端专属色在浅色下对比度不足，在此翻转。
     其余背景/前景/边框全都由 scoped .nvtop 中的项目令牌驱动，此处不动。 -->
<style>
[data-theme="light"] .nvtop {
  --nv-name: #2d6a1a;
  --nv-grid: rgba(0, 0, 0, 0.1);
  --nv-mark-stroke: #ffffff;
}
</style>
