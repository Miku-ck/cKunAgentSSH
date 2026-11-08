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
    <table v-if="gpuProcs.length" class="nv-proc-table">
      <thead>
        <tr><th>GPU</th><th>PID</th><th>USER</th><th>GPU-MEM</th><th>%CPU</th><th>%MEM</th><th>TIME</th><th>COMMAND</th></tr>
      </thead>
      <tbody>
        <tr v-for="p in gpuProcs" :key="p.gpuIndex + ':' + p.pid">
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
import type { GpuProcessEntry, GpuSample, ServerMetrics } from '@/stores/server-monitor-core'
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
  /** GPU 占用进程：不在 metrics 里 —— 它是 60s 慢轮询，metrics 每 5s 替换 */
  gpuProcs: GpuProcessEntry[]
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

<style scoped src="./gpu-nvtop-panel.css"></style>

<!-- 浅色系主题例外覆盖：nvtop 面板底随 --bg-base（浅底主题下自动变浅），
     深色写死的终端专属色（亮绿卡名、白网格线、笔画描边）在浅底上对比不足/隐形，
     在此翻转。其余背景/前景/边框全由 scoped .nvtop 的项目令牌驱动，此处不动。
     选择器覆盖全部浅底主题：light / 手记 atelier ——
     旧版只认 light 一个，浅底的手记会漏翻（抄一份清单的漂移族）。 -->
<style>
[data-theme="light"] .nvtop,
[data-theme="atelier"] .nvtop {
  --nv-name: #2d6a1a;
  --nv-grid: rgba(0, 0, 0, 0.1);
  --nv-mark-stroke: #ffffff;
}
</style>
