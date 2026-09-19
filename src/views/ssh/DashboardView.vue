<template>
  <div class="dashboard-view">
    <div class="dash-header">
      <div class="dash-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="18" height="18"><path d="M3 3v18h18"/><path d="M7 14l4-4 3 3 5-6"/></svg>
        <span>监控仪表盘</span>
        <span v-if="monitor.current" class="dash-host">{{ monitor.current.host }}</span>
      </div>
      <div class="dash-header-right">
        <!-- 仅在有旧数据残留时显示（无数据时中间空态已完整说明原因，避免重复） -->
        <span v-if="monitor.lastError && !monitor.polling && monitor.current" class="dash-stopped" :title="monitor.lastError.message">
          监控已中断：{{ monitor.lastError.message }}
          <span v-if="monitor.lastError.retryable" class="dash-stopped-hint">（连接恢复后自动重连）</span>
        </span>
      </div>
    </div>

    <!-- 无数据空态：错误优先于"采集中"，避免任何失败都被转圈遮罩（"一直卡在正在采集"根因） -->
    <div v-if="!monitor.current" class="dash-empty">
      <template v-if="monitor.lastError">
        <div class="dash-empty-icon dash-empty-icon-err">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="32" height="32"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
        </div>
        <p>{{ monitor.polling ? '采集失败（自动重试中）' : '监控已中断' }}</p>
        <p class="dash-empty-sub">{{ monitor.lastError.message }}</p>
        <p v-if="monitor.lastError.retryable" class="dash-empty-sub dash-empty-sub-hint">连接恢复后将自动继续采集</p>
      </template>
      <template v-else-if="monitor.polling">
        <div class="dash-empty-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="32" height="32"><path d="M21 12a9 9 0 1 1-6.2-8.56"/></svg>
        </div>
        <p>正在采集服务器指标…</p>
      </template>
      <template v-else>
        <div class="dash-empty-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="32" height="32"><path d="M3 3v18h18"/><path d="M7 14l4-4 3 3 5-6"/></svg>
        </div>
        <p>当前终端没有远程连接</p>
        <p class="dash-empty-sub">连接一台远程服务器后，指标将每 5 秒自动刷新</p>
      </template>
    </div>

    <div v-else class="dash-body">
      <!-- 指标卡 -->
      <div class="dash-cards">
        <div class="card" :class="toneCls(monitor.current.cpu.used)">
          <div class="card-head">
            <span class="card-label">CPU</span>
            <span class="card-val">{{ monitor.current.cpu.used.toFixed(1) }}%</span>
          </div>
          <Sparkline :values="monitor.cpuHistory" :width="180" :height="36" />
          <div class="card-sub">iowait {{ monitor.current.cpu.iowait.toFixed(1) }}%</div>
          <button class="card-ask" @click="monitor.askAiAboutMetrics(`CPU 使用率 ${monitor.current!.cpu.used.toFixed(1)}%，请结合 TOP 进程分析瓶颈。`)">分析</button>
        </div>

        <div class="card" :class="toneCls(monitor.current.mem.percent)">
          <div class="card-head">
            <span class="card-label">内存</span>
            <span class="card-val">{{ monitor.current.mem.percent.toFixed(1) }}%</span>
          </div>
          <Sparkline :values="monitor.memHistory" :width="180" :height="36" />
          <div class="card-sub">{{ (monitor.current.mem.used / 1024).toFixed(1) }}G / {{ (monitor.current.mem.total / 1024).toFixed(1) }}G</div>
          <button class="card-ask" @click="monitor.askAiAboutMetrics('请分析内存使用情况并给出建议。')">分析</button>
        </div>

        <div class="card" :class="loadToneCls()">
          <div class="card-head">
            <span class="card-label">负载</span>
            <span class="card-val">{{ monitor.current.load[0].toFixed(2) }}</span>
          </div>
          <Sparkline :values="monitor.loadHistory" :width="180" :height="36" :max="loadMax" />
          <div class="card-sub">1 / 5 / 15 min：{{ monitor.current.load.map(v => v.toFixed(1)).join(' · ') }}</div>
          <button class="card-ask" @click="monitor.askAiAboutMetrics('请分析当前系统负载是否合理。')">分析</button>
        </div>
      </div>

      <!-- 磁盘 -->
      <div class="dash-section">
        <div class="section-title">磁盘</div>
        <div v-if="monitor.current.disks.length === 0" class="section-empty">未检测到磁盘挂载信息</div>
        <div v-else class="disk-list">
          <div v-for="d in monitor.current.disks" :key="d.mount" class="disk-row">
            <span class="disk-mount" :title="d.mount">{{ d.mount }}</span>
            <div class="disk-bar">
              <div class="disk-bar-fill" :class="toneCls(d.percent)" :style="{ width: Math.min(100, d.percent) + '%' }"></div>
            </div>
            <span class="disk-pct" :class="toneCls(d.percent)">{{ d.percent }}%</span>
            <span class="disk-size">{{ (d.used / 1024).toFixed(0) }}G / {{ (d.total / 1024).toFixed(0) }}G</span>
            <button class="card-ask" @click="monitor.askAiAboutMetrics(`磁盘 ${d.mount} 使用率 ${d.percent}%，请分析占用并给出清理建议。`)">分析</button>
          </div>
        </div>
      </div>

      <!-- GPU（nvidia-smi 采集；无驱动/无卡时显示提示） -->
      <div class="dash-section">
        <div class="section-title">GPU<span class="section-sub">· nvidia-smi</span></div>
        <div v-if="monitor.current.gpus.length === 0" class="section-empty">未检测到 NVIDIA GPU（未安装 nvidia-smi 驱动或无 GPU）</div>
        <template v-else>
          <div class="gpu-trend">
            <span class="gpu-trend-label">主卡利用率趋势</span>
            <Sparkline :values="monitor.gpuHistory" :width="240" :height="40" />
          </div>
          <table class="proc-table gpu-table">
            <thead>
              <tr><th>#</th><th>名称</th><th>利用率</th><th>显存</th><th>温度</th><th></th></tr>
            </thead>
            <tbody>
              <tr v-for="g in monitor.current.gpus" :key="g.index">
                <td class="mono">{{ g.index }}</td>
                <td class="proc-name" :title="g.name">{{ g.name }}</td>
                <td class="gpu-util">
                  <div class="disk-bar"><div class="disk-bar-fill" :class="toneCls(g.utilization)" :style="{ width: Math.min(100, g.utilization) + '%' }"></div></div>
                  <span class="disk-pct" :class="toneCls(g.utilization)">{{ g.utilization.toFixed(0) }}%</span>
                </td>
                <td class="mono">{{ (g.memUsed / 1024).toFixed(1) }}G / {{ (g.memTotal / 1024).toFixed(1) }}G <span class="dim">({{ g.memPercent.toFixed(0) }}%)</span></td>
                <td :class="{ hot: g.temperature > 80 }">{{ g.temperature }}℃</td>
                <td><button class="card-ask" @click="monitor.askAiAboutMetrics(`GPU ${g.name} 利用率 ${g.utilization.toFixed(0)}%、显存 ${g.memPercent.toFixed(0)}%、温度 ${g.temperature}℃，请分析。`)">分析</button></td>
              </tr>
            </tbody>
          </table>
        </template>
      </div>

      <!-- 进程 TOP -->
      <div class="dash-section">
        <div class="section-title">进程 TOP（按 CPU）</div>
        <div v-if="monitor.current.procs.length === 0" class="section-empty">未检测到进程信息</div>
        <table v-else class="proc-table">
          <thead>
            <tr><th>PID</th><th>名称</th><th>CPU %</th><th>MEM %</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="p in monitor.current.procs" :key="p.pid">
              <td class="mono">{{ p.pid }}</td>
              <td class="mono proc-name">{{ p.name }}</td>
              <td :class="{ hot: p.cpu > 80 }">{{ p.cpu.toFixed(1) }}</td>
              <td>{{ p.mem.toFixed(1) }}</td>
              <td><button class="card-ask" @click="monitor.askAiAboutMetrics(`进程 ${p.name} (PID ${p.pid}) CPU ${p.cpu.toFixed(1)}%，请分析。`)">分析</button></td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 监听端口 -->
      <div class="dash-section">
        <div class="section-title">监听端口</div>
        <div v-if="monitor.current.ports.length === 0" class="section-empty">未检测到监听端口（ss 不可用时为空）</div>
        <div v-else class="port-list">
          <span v-for="(pt, i) in monitor.current.ports" :key="i" class="port-chip" :class="{ udp: pt.proto === 'udp' }">
            <span class="port-proto">{{ pt.proto.toUpperCase() }}</span>
            {{ pt.addr }}:{{ pt.port }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, defineComponent, h } from 'vue'
import { useServerMonitorStore } from '@/stores/server-monitor'

const monitor = useServerMonitorStore()

function toneCls(v?: number): string {
  if (v == null) return ''
  if (v >= 90) return 'tone-danger'
  if (v >= 75) return 'tone-warn'
  return ''
}

/** 负载卡片配色：按 1min 负载值（4 告警 / 8 危险，单机经验阈值），不复用百分比语义的 toneCls */
function loadToneCls(): string {
  const l = monitor.current?.load[0]
  if (l == null) return ''
  if (l >= 8) return 'tone-danger'
  if (l >= 4) return 'tone-warn'
  return ''
}

/** 负载 sparkline 上限：取峰值 1.5 倍，避免曲线贴底 */
const loadMax = computed(() => {
  const peak = Math.max(...monitor.loadHistory, 0.1)
  return Math.max(1, peak * 1.5)
})

// Sparkline 与状态条同构（手搓 SVG，避免引图表库）
const Sparkline = defineComponent({
  name: 'Sparkline',
  props: {
    values: { type: Array as () => number[], default: () => [] },
    width: { type: Number, default: 180 },
    height: { type: Number, default: 36 },
    max: { type: Number, default: 100 },
  },
  setup(p) {
    const path = computed(() => {
      const vals = p.values
      if (vals.length < 2) return ''
      const w = p.width
      const h = p.height
      const step = w / Math.max(1, vals.length - 1)
      const pts = vals.map((v, i) => {
        const x = i * step
        const y = h - Math.max(0, Math.min(p.max, v)) / p.max * (h - 4) - 2
        return `${x.toFixed(1)},${y.toFixed(1)}`
      })
      return `M${pts.join(' L')}`
    })
    return () =>
      h('svg', {
        class: 'sparkline-lg',
        width: p.width,
        height: p.height,
        viewBox: `0 0 ${p.width} ${p.height}`,
        'aria-hidden': 'true',
      }, [
        h('line', { x1: 0, y1: p.height - 2, x2: p.width, y2: p.height - 2, class: 'spark-baseline' }),
        path.value
          ? h('path', { d: path.value, class: 'sparkline-lg-path', fill: 'none', 'stroke-width': 1.5 })
          : null,
      ])
  },
})
</script>

<style scoped>
.dashboard-view {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--bg-base);
  font-family: var(--font-sans);
  color: var(--text-primary);
}

.dash-header {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border-subtle, #2d2d30);
}

.dash-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
}

.dash-host {
  font-size: 12px;
  font-weight: 400;
  color: var(--text-secondary);
  font-family: var(--font-mono, monospace);
}

.dash-header-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.dash-stopped {
  font-size: 11px;
  color: var(--accent-red, #f48771);
  /* 长文案在头部单行截断，完整内容经 :title 悬浮查看 */
  max-width: 480px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dash-stopped-hint {
  color: var(--text-tertiary, #6e6e73);
}



.dash-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--text-secondary);
  font-size: 13px;
}

.dash-empty-icon {
  color: var(--text-tertiary, #6e6e73);
  margin-bottom: 8px;
}

.dash-empty-icon-err {
  color: var(--accent-red, #f48771);
}

.dash-empty-sub {
  font-size: 12px;
  color: var(--text-tertiary, #6e6e73);
  max-width: 420px;
  text-align: center;
  line-height: 1.5;
}

.dash-empty-sub-hint {
  color: var(--text-secondary, #9a9a9a);
}

.dash-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.dash-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
}

.card {
  position: relative;
  padding: 12px 14px;
  border: 1px solid var(--border-subtle, #2d2d30);
  border-radius: 8px;
  background: var(--bg-elevated, #1a1a1e);
}

.card-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 6px;
}

.card-label {
  font-size: 11px;
  color: var(--text-tertiary, #6e6e73);
  letter-spacing: 0.4px;
}

.card-val {
  font-size: 20px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.card.tone-warn .card-val { color: var(--accent-orange, #e5c07b); }
.card.tone-danger .card-val { color: var(--accent-red, #f48771); }

.card-sub {
  margin-top: 4px;
  font-size: 11px;
  color: var(--text-tertiary, #6e6e73);
}

.card-ask {
  position: absolute;
  top: 10px;
  right: 10px;
  padding: 1px 8px;
  border: 1px solid var(--border-default, #3a3a3e);
  border-radius: 4px;
  background: transparent;
  color: var(--text-tertiary, #6e6e73);
  font-size: 10px;
  cursor: pointer;
  opacity: 0;
  transition: all var(--transition-fast, 0.15s);
}

.card:hover .card-ask,
.disk-row .card-ask,
.proc-table .card-ask {
  opacity: 1;
}

.card-ask:hover {
  border-color: var(--accent-indigo, #7aa2d0);
  color: var(--accent-indigo, #7aa2d0);
}

.sparkline-lg {
  display: block;
}

:deep(.sparkline-lg-path) { stroke: var(--accent-blue, #7aa2d0); }
:deep(.spark-baseline) { stroke: var(--border-subtle, #2d2d30); stroke-width: 1; }

.dash-section {
  border: 1px solid var(--border-subtle, #2d2d30);
  border-radius: 8px;
  background: var(--bg-elevated, #1a1a1e);
  padding: 12px 14px;
}

.section-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 10px;
}

.section-empty {
  font-size: 12px;
  color: var(--text-tertiary, #6e6e73);
}

/* ── 磁盘 ── */
.disk-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.disk-row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
}

.disk-mount {
  width: 120px;
  font-family: var(--font-mono, monospace);
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.disk-bar {
  flex: 1;
  height: 6px;
  border-radius: 3px;
  background: var(--bg-hover, rgba(255, 255, 255, 0.06));
  overflow: hidden;
}

.disk-bar-fill {
  height: 100%;
  border-radius: 3px;
  background: var(--accent-blue, #7aa2d0);
  transition: width 0.4s ease;
}

.disk-bar-fill.tone-warn { background: var(--accent-orange, #e5c07b); }
.disk-bar-fill.tone-danger { background: var(--accent-red, #f48771); }

.disk-pct {
  width: 48px;
  text-align: right;
  font-variant-numeric: tabular-nums;
  color: var(--text-primary);
}

/* ── GPU ── */
.section-sub {
  font-size: 10px;
  font-weight: 400;
  color: var(--text-tertiary, #6e6e73);
  margin-left: 6px;
}

.gpu-trend {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.gpu-trend-label {
  font-size: 11px;
  color: var(--text-tertiary, #6e6e73);
  white-space: nowrap;
}

.gpu-util {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 140px;
}

.gpu-util .disk-bar {
  flex: 1;
}

.dim {
  color: var(--text-tertiary, #6e6e73);
}

.disk-pct.tone-warn { color: var(--accent-orange, #e5c07b); }
.disk-pct.tone-danger { color: var(--accent-red, #f48771); }

.disk-size {
  width: 120px;
  text-align: right;
  font-size: 11px;
  color: var(--text-tertiary, #6e6e73);
  font-variant-numeric: tabular-nums;
}

/* ── 进程表 ── */
.proc-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.proc-table th {
  text-align: left;
  padding: 4px 8px;
  color: var(--text-tertiary, #6e6e73);
  font-weight: 500;
  font-size: 11px;
  border-bottom: 1px solid var(--border-subtle, #2d2d30);
}

.proc-table td {
  padding: 5px 8px;
  border-bottom: 1px solid var(--border-subtle, #2d2d30);
  font-variant-numeric: tabular-nums;
}

.proc-table tr:last-child td {
  border-bottom: none;
}

.mono {
  font-family: var(--font-mono, monospace);
}

.proc-name {
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hot {
  color: var(--accent-red, #f48771);
  font-weight: 600;
}

.proc-table .card-ask {
  position: static;
  opacity: 1;
}

/* ── 端口 ── */
.port-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.port-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  border: 1px solid var(--border-default, #3a3a3e);
  border-radius: 4px;
  font-size: 11px;
  font-family: var(--font-mono, monospace);
  color: var(--text-primary);
}

.port-chip.udp {
  border-style: dashed;
}

.port-proto {
  font-size: 9px;
  color: var(--text-tertiary, #6e6e73);
  letter-spacing: 0.5px;
}
</style>
