<template>
  <div class="dashboard-view">
    <!-- 连接状态条：仿 SFTP 风格。有远程会话时显示主机名 + 已连接；
         无会话时显示"未连接远程" + "连接远程"按钮。 -->
    <div class="dash-conn-bar">
      <div class="conn-left">
        <div class="conn-dot" :class="hasRemoteSession ? 'online' : 'offline'"></div>
        <span class="conn-name">{{ connStatusText }}</span>
        <span v-if="hasRemoteSession && monitor.current" class="conn-host">{{ monitor.current.host }}</span>
      </div>
      <div class="conn-right">
        <span v-if="monitor.lastError && !monitor.polling && monitor.current" class="conn-stopped" :title="monitor.lastError.message">
          监控已中断：{{ monitor.lastError.message }}
          <span v-if="monitor.lastError.retryable" class="conn-stopped-hint">（连接恢复后自动重连）</span>
        </span>
        <span v-if="monitor.polling && !monitor.current" class="conn-collecting">正在采集服务器指标…</span>
        <button v-if="hasRemoteSession" class="conn-btn" @click="handleDisconnect">断开</button>
        <button class="conn-btn conn-btn-primary" @click="showConnPicker = true">连接远程</button>
      </div>
    </div>

     <!-- 已连接但还没拿到指标 → 简单等待态（轮询超时或异常会由 store 写入 lastError 上浮到条上） -->
     <div v-if="!monitor.current && (monitor.polling || hasRemoteSession)" class="dash-empty">
       <template v-if="monitor.lastError">
         <p class="dash-empty-err">{{ monitor.polling ? '采集失败（自动重试中）' : '监控已中断' }}</p>
         <p class="dash-empty-sub">{{ monitor.lastError.message }}</p>
         <p v-if="monitor.lastError.retryable" class="dash-empty-sub dash-empty-sub-hint">连接恢复后将自动继续采集</p>
       </template>
       <template v-else>
         <p>正在采集服务器指标…</p>
       </template>
     </div>

     <!-- 无远程会话 → 连接引导态：有保存的连接才可连 -->
     <div v-else-if="!monitor.current" class="dash-empty">
       <p>未连接远程服务器</p>
       <p class="dash-empty-sub">连接一台远程服务器后，指标将每 5 秒自动刷新</p>
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
        </div>

        <div class="card" :class="toneCls(monitor.current.mem.percent)">
          <div class="card-head">
            <span class="card-label">内存</span>
            <span class="card-val">{{ monitor.current.mem.percent.toFixed(1) }}%</span>
          </div>
          <Sparkline :values="monitor.memHistory" :width="180" :height="36" />
          <div class="card-sub">{{ (monitor.current.mem.used / 1024).toFixed(1) }}G / {{ (monitor.current.mem.total / 1024).toFixed(1) }}G</div>
        </div>

        <div class="card" :class="loadToneCls()">
          <div class="card-head">
            <span class="card-label">负载</span>
            <span class="card-val">{{ monitor.current.load[0].toFixed(2) }}</span>
          </div>
          <Sparkline :values="monitor.loadHistory" :width="180" :height="36" :max="loadMax" />
          <div class="card-sub">1 / 5 / 15 min：{{ monitor.current.load.map(v => v.toFixed(1)).join(' · ') }}</div>
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
          </div>
        </div>
      </div>

      <!-- GPU（nvidia-smi 采集；无驱动/无卡时显示提示）
           无外层 dash-section 边框：GpuNvtopPanel 自带终端风格边框，
           双层框会产生"框中框"的割裂感。无 GPU 时降级为普通 section -->
      <div v-if="monitor.current.gpus.length === 0" class="dash-section">
        <div class="section-title">
          <span>GPU<span class="section-sub">· nvidia-smi</span></span>
        </div>
        <div class="section-empty">
          <template v-if="monitor.current.gpuError">GPU 采集异常：{{ monitor.current.gpuError }}</template>
          <template v-else>未检测到 NVIDIA GPU（无 GPU，或远端无驱动）</template>
        </div>
      </div>
      <div v-else class="gpu-embed">
        <div class="gpu-embed-head">
          <span>GPU<span class="section-sub">· nvidia-smi</span></span>
        </div>
        <GpuNvtopPanel
          :metrics="monitor.current"
          :gpu-hist="monitor.gpuHist"
          :cpu-hist="monitor.cpuHistory"
          :mem-hist="monitor.memHistory"
          :load-hist="monitor.loadHistory"
        />
      </div>

      <!-- 进程 TOP -->
      <div class="dash-section">
        <div class="section-title">进程 TOP（按 CPU）<span class="section-sub">前 10 · 每 5 秒刷新</span></div>
        <div v-if="monitor.current.procs.length === 0" class="section-empty">未检测到进程信息</div>
        <table v-else class="proc-table">
          <thead>
            <tr><th>PID</th><th>名称</th><th>用户</th><th>CPU %</th><th>内存</th><th>运行</th><th>状态</th><th class="proc-act">操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="p in monitor.current.procs" :key="p.pid">
              <td class="mono">{{ p.pid }}</td>
              <td class="mono proc-name" :title="p.name">{{ p.name }}</td>
              <td class="mono">{{ p.user }}</td>
              <td :class="{ hot: p.cpu > 80 }">{{ p.cpu.toFixed(1) }}</td>
              <td>{{ formatRss(p.rss) }}</td>
              <td class="proc-etime">{{ p.etime }}</td>
              <td><span class="proc-stat" :title="statFull(p.stat)">{{ p.stat }}</span></td>
              <td class="proc-act"><button class="proc-kill" title="终止进程" @click="confirmKill(p)">终止</button></td>
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

    <!-- 连接选择模态 -->
    <Teleport to="body">
      <div v-if="showConnPicker" class="dc-overlay" @click.self="showConnPicker = false">
        <div class="dc-dialog" role="dialog" aria-modal="true">
          <div class="dc-header">
            <span>选择 SSH 连接</span>
            <button class="dc-close" @click="showConnPicker = false">×</button>
          </div>
          <div class="dc-list">
            <div v-if="sshStore.connections.length === 0" class="dc-empty">
              暂无 SSH 连接配置，请先在「远程管理」中新建连接
            </div>
            <div
              v-for="conn in sshStore.connections"
              :key="conn.id"
              class="dc-item"
              :class="{ disabled: connecting }"
              @click="handleConnect(conn)"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="2" y="3" width="20" height="14" rx="2"/><line x="8" y="21" x2="16" y2="21"/><line x="12" y="17" x2="12" y2="21"/></svg>
              <div class="dc-info">
                <span class="dc-name">{{ conn.name }}</span>
                <span class="dc-host">{{ conn.username }}@{{ conn.host }}:{{ conn.port }}</span>
              </div>
              <svg v-if="connecting && pendingConnId === conn.id" class="dc-spinner" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"/><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"/></svg>
            </div>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 终止进程确认模态 -->
    <Teleport to="body">
      <div v-if="killDialog.visible" class="dc-overlay" @click.self="killDialog.visible = false">
        <div class="dc-dialog dc-dialog-sm" role="dialog" aria-modal="true">
          <div class="dc-header">
            <span>确认终止进程</span>
            <button class="dc-close" @click="killDialog.visible = false">×</button>
          </div>
          <div class="dc-kill-body">
            <p class="dc-kill-line"><span class="dc-killLabel">PID</span><span class="mono dc-killVal">{{ killDialog.proc?.pid }}</span></p>
            <p class="dc-kill-line"><span class="dc-killLabel">名称</span><span class="dc-killVal">{{ killDialog.proc?.name }}</span></p>
            <p class="dc-kill-line"><span class="dc-killLabel">用户</span><span class="mono dc-killVal">{{ killDialog.proc?.user }}</span></p>
            <p class="dc-kill-hint">SIGTERM 通知进程优雅退出；若进程未响应可使用 SIGKILL 强制终止。此操作不可撤销。</p>
          </div>
          <div class="dc-kill-actions">
            <button class="dc-btn" @click="killDialog.visible = false">取消</button>
            <button class="dc-btn dc-btn-danger" :disabled="killing" @click="handleKill(15)">
              <svg v-if="killing && killDialog.signal === 15" class="dc-btn-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"/><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"/></svg>
              终止（SIGTERM）
            </button>
            <button class="dc-btn dc-btn-warn" :disabled="killing" @click="handleKill(9)">
              <svg v-if="killing && killDialog.signal === 9" class="dc-btn-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"/><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"/></svg>
              强制（SIGKILL）
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, defineComponent, h, ref } from 'vue'
import { useServerMonitorStore } from '@/stores/server-monitor'
import { useSSHStore } from '@/stores/ssh'
import { useToastStore } from '@/stores/toast'
import GpuNvtopPanel from '@/components/ssh/GpuNvtopPanel.vue'
import type { SSHConnection } from '@/stores/ssh'
import type { ProcessTopEntry } from '@/stores/server-monitor-core'

const monitor = useServerMonitorStore()
const sshStore = useSSHStore()

const showConnPicker = ref(false)
const connecting = ref(false)
const pendingConnId = ref('')

/** 当前是否有活跃的远程会话（判断依据：store.sessions 中是否存在 type='remote'） */
const hasRemoteSession = computed(() =>
  sshStore.sessions.some(s => s.type === 'remote'),
)

/** 连接状态条文字：有远程会话时显示第一台远程会话名称，无会话时显示"未连接远程" */
const connStatusText = computed(() => {
  const remote = sshStore.sessions.find(s => s.type === 'remote')
  return remote ? remote.name : '未连接远程'
})

async function handleConnect(conn: SSHConnection): Promise<void> {
  if (connecting.value) return
  connecting.value = true
  pendingConnId.value = conn.id
  try {
    await sshStore.connectRemote(conn.id, 80, 24)
    sshStore.switchSession(
      sshStore.sessions.find(s => s.type === 'remote' && s.connectionId === conn.id)?.id || '',
    )
    showConnPicker.value = false
    useToastStore().success(`已连接 ${conn.name}`)
  } catch (err: unknown) {
    useToastStore().error(
      err instanceof Error ? err.message : '连接失败',
    )
  } finally {
    connecting.value = false
    pendingConnId.value = ''
  }
}

function handleDisconnect(): void {
  const remote = sshStore.sessions.find(s => s.type === 'remote')
  if (remote) {
    sshStore.closeSession(remote.id)
  }
}

// ── 终止进程 ──
const killDialog = ref({ visible: false, proc: null as ProcessTopEntry | null, signal: 15 })
const killing = ref(false)

function confirmKill(proc: ProcessTopEntry): void {
  killDialog.value = { visible: true, proc, signal: 15 }
}

async function handleKill(signal: number): Promise<void> {
  const proc = killDialog.value.proc
  const sshId = monitor.current?.sshId
  if (!proc || !sshId) return
  killDialog.value.signal = signal
  killing.value = true
  try {
    const r = await window.electronAPI!.sshKillProcess(sshId, proc.pid, signal)
    if (r.code === 0) {
      useToastStore().success(`已发送 SIG${signal} 到 PID ${proc.pid}`)
      killDialog.value.visible = false
    } else {
      const msg = (r.stderr || r.stdout || `exit code ${r.code}`).split('\n')[0].slice(0, 120)
      useToastStore().error(msg || '终止失败')
    }
  } catch (err: unknown) {
    useToastStore().error(err instanceof Error ? err.message : '终止进程失败')
  } finally {
    killing.value = false
  }
}

/** RSS（KB）→ MB/GB 友好格式 */
function formatRss(kb: number): string {
  if (!kb || kb <= 0) return '—'
  if (kb >= 1048576) return (kb / 1048576).toFixed(1) + 'G'
  if (kb >= 1024) return (kb / 1024).toFixed(0) + 'M'
  return kb + 'K'
}

/** 进程状态码 → 完整提示 */
function statFull(stat: string): string {
  const MAP: Record<string, string> = {
    R: 'R — 运行中',
    S: 'S — 可中断睡眠',
    D: 'D — 不可中断睡眠（通常 IO）',
    Z: 'Z — 僵尸进程',
    T: 'T — 已停止',
    t: 't — 被调试器停止',
    X: 'X — 已消亡',
    I: 'I — 内核空闲线程',
  }
  const base = stat.charAt(0)
  return MAP[base] || stat
}

function toneCls(v?: number): string {
  if (v == null) return ''
  if (v > 80) return 'tone-danger'
  if (v >= 60) return 'tone-warn'
  return 'tone-cool'
}

/** 负载卡片配色：按 1min 负载值（4 告警 / 8 危险，单机经验阈值）。正常档也归入 tone-cool（绿），与百分比口径一致 */
function loadToneCls(): string {
  const l = monitor.current?.load[0]
  if (l == null) return ''
  if (l >= 8) return 'tone-danger'
  if (l >= 4) return 'tone-warn'
  return 'tone-cool'
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

/* ── 连接状态条（仿 SFTP） ── */
.dash-conn-bar {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: var(--page-head-h);
  padding: 0 16px;
  border-bottom: 1px solid var(--border-subtle);
  gap: 12px;
}

.conn-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.conn-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.conn-dot.online { background: var(--metric-cool); }
.conn-dot.offline { background: var(--text-tertiary); }

.conn-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conn-host {
  font-size: 12px;
  font-weight: 400;
  color: var(--text-secondary);
  font-family: var(--font-mono);
  flex-shrink: 0;
}

.conn-right {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

.conn-collecting {
  font-size: 12px;
  color: var(--text-tertiary);
}

.conn-stopped {
  font-size: 11px;
  color: var(--accent-red);
  max-width: 480px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conn-stopped-hint {
  color: var(--text-tertiary);
}

.conn-btn {
  padding: 5px 14px;
  border: 1px solid var(--border-default);
  border-radius: 4px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
}

.conn-btn:hover {
  border-color: var(--accent-indigo);
  color: var(--accent-indigo);
}

.conn-btn-primary {
  background: var(--accent-indigo);
  border-color: var(--accent-indigo);
  color: var(--accent-on, #fff);
}

.conn-btn-primary:hover {
  filter: brightness(1.1);
  color: var(--accent-on, #fff);
}

/* ── 空态 ── */
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

.dash-empty-err {
  color: var(--accent-red);
}

.dash-empty-sub {
  font-size: 12px;
  color: var(--text-tertiary);
  max-width: 420px;
  text-align: center;
  line-height: 1.5;
}

.dash-empty-sub-hint {
  color: var(--text-secondary);
}

.dash-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 18px;
  background: var(--bg-base);
}

.dash-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
}

.card {
  padding: 12px 14px;
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  background: var(--bg-elevated);
}

.card-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 6px;
}

.card-label {
  font-size: 11px;
  color: var(--text-tertiary);
  letter-spacing: 0.4px;
}

.card-val {
  font-size: 20px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.card.tone-cool .card-val { color: var(--metric-cool); }
.card.tone-warn .card-val { color: var(--metric-warm); }
.card.tone-danger .card-val { color: var(--metric-hot); }

.card-sub {
  margin-top: 4px;
  font-size: 11px;
  color: var(--text-tertiary);
}

.sparkline-lg {
  display: block;
}

:deep(.sparkline-lg-path) { stroke: var(--accent-primary); }
:deep(.spark-baseline) { stroke: var(--border-subtle); stroke-width: 1; }

.dash-section {
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  background: var(--bg-elevated);
  padding: 12px 14px;
}

.section-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 10px;
}

.section-empty {
  font-size: 12px;
  color: var(--text-tertiary);
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
  font-family: var(--font-mono);
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.disk-bar {
  flex: 1;
  height: 6px;
  border-radius: 3px;
  background: var(--bg-hover);
  overflow: hidden;
}

.disk-bar-fill {
  height: 100%;
  border-radius: 3px;
  background: var(--metric-cool);
  transition: width 0.4s ease;
}

.disk-bar-fill.tone-cool { background: var(--metric-cool); }
.disk-bar-fill.tone-warn { background: var(--metric-warm); }
.disk-bar-fill.tone-danger { background: var(--metric-hot); }

.disk-pct {
  width: 48px;
  text-align: right;
  font-variant-numeric: tabular-nums;
  color: var(--text-primary);
}

.section-sub {
  font-size: 10px;
  font-weight: 400;
  color: var(--text-tertiary);
  margin-left: 6px;
}

.disk-pct.tone-cool { color: var(--metric-cool); }
.disk-pct.tone-warn { color: var(--metric-warm); }
.disk-pct.tone-danger { color: var(--metric-hot); }

.disk-size {
  width: 120px;
  text-align: right;
  font-size: 11px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}

/* ── GPU ── */
.gpu-embed {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.gpu-embed-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 2px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
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
  color: var(--text-tertiary);
  font-weight: 500;
  font-size: 11px;
  border-bottom: 1px solid var(--border-subtle);
}

.proc-table td {
  padding: 5px 8px;
  border-bottom: 1px solid var(--border-subtle);
  font-variant-numeric: tabular-nums;
}

.proc-table tr:last-child td {
  border-bottom: none;
}

.mono {
  font-family: var(--font-mono);
}

.proc-name {
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hot {
  color: var(--accent-red);
  font-weight: 600;
}

.proc-etime {
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.proc-stat {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--bg-hover);
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--text-secondary);
  cursor: help;
}

.proc-act {
  width: 56px;
  text-align: right;
}

.proc-kill {
  padding: 2px 8px;
  border: none;
  border-radius: 3px;
  background: transparent;
  color: var(--accent-red);
  font-size: 11px;
  cursor: pointer;
  opacity: 0;
  transition: opacity var(--transition-fast), background var(--transition-fast);
}

.proc-table tr:hover .proc-kill {
  opacity: 1;
}

.proc-kill:hover {
  background: var(--bg-hover);
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
  border: 1px solid var(--border-default);
  border-radius: 4px;
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--text-primary);
}

.port-chip.udp {
  border-style: dashed;
}

.port-proto {
  font-size: 9px;
  color: var(--text-tertiary);
  letter-spacing: 0.5px;
}

/* ── 窄窗口 ── */
@media (max-width: 760px) {
  .dash-body { padding: 12px; gap: 14px; }
  .conn-stopped { max-width: 220px; }
  .dash-section { padding: 10px 12px; }
  .disk-row { gap: 6px; }
  .disk-mount { width: 76px; font-size: 11px; }
  .disk-size { width: 84px; font-size: 10px; }
  .proc-name { max-width: 110px; }
}

/* ===============================================================
   连接选择弹窗。unstoped：scoped 属性进不到 Teleport 到 body 的元素，
   所以弹窗样式写在末尾非 scoped 的 <style> 块里。
   =============================================================== */
</style>

<style>
.dc-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.55);
  backdrop-filter: blur(2px);
}

.dc-dialog {
  width: min(420px, 100vw - 48px);
  max-height: min(460px, 100vh - 96px);
  display: flex;
  flex-direction: column;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: 10px;
  box-shadow: var(--shadow-lg);
}

.dc-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 18px;
  border-bottom: 1px solid var(--border-subtle);
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  flex-shrink: 0;
}

.dc-close {
  width: 24px;
  height: 24px;
  border: none;
  background: transparent;
  color: var(--text-tertiary);
  font-size: 18px;
  cursor: pointer;
  border-radius: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: color var(--transition-fast);
}

.dc-close:hover {
  color: var(--text-primary);
  background: var(--bg-hover);
}

.dc-list {
  overflow-y: auto;
  padding: 8px;
}

.dc-empty {
  padding: 28px 18px;
  text-align: center;
  color: var(--text-tertiary);
  font-size: 13px;
}

.dc-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 6px;
  cursor: pointer;
  transition: background var(--transition-fast);
}

.dc-item:hover {
  background: var(--bg-hover);
}

.dc-item.disabled {
  opacity: 0.5;
  pointer-events: none;
}

.dc-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.dc-name {
  font-size: 13px;
  color: var(--text-primary);
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dc-host {
  font-size: 11px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dc-spinner {
  width: 16px;
  height: 16px;
  animation: dc-spin 0.8s linear infinite;
  flex-shrink: 0;
}

@keyframes dc-spin {
  0% { transform: rotate(0deg); }
  100% { transform: rotate(360deg); }
}

/* ── 终止进程确认弹窗 ── */
.dc-dialog-sm {
  width: min(360px, 100vw - 48px);
}

.dc-kill-body {
  padding: 16px 18px;
}

.dc-kill-line {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 0 0 8px;
  font-size: 13px;
}

.dc-kill-label {
  width: 40px;
  flex-shrink: 0;
  color: var(--text-tertiary);
  text-align: right;
  font-size: 12px;
}

.dc-kill-val {
  color: var(--text-primary);
  word-break: break-all;
}

.dc-kill-hint {
  margin: 14px 0 0;
  padding-top: 12px;
  border-top: 1px solid var(--border-subtle);
  font-size: 11px;
  line-height: 1.6;
  color: var(--text-tertiary);
}

.dc-kill-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 18px;
  border-top: 1px solid var(--border-subtle);
}

.dc-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border: 1px solid var(--border-default);
  border-radius: 5px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
  transition: all var(--transition-fast);
}

.dc-btn:hover:not(:disabled) {
  border-color: var(--accent-indigo);
  color: var(--accent-indigo);
}

.dc-btn-danger {
  background: var(--accent-red);
  border-color: var(--accent-red);
  color: #fff;
}

.dc-btn-danger:hover:not(:disabled) {
  filter: brightness(1.1);
  color: #fff;
}

.dc-btn-warn {
  background: var(--metric-warm);
  border-color: var(--metric-warm);
  color: var(--bg-base);
}

.dc-btn-warn:hover:not(:disabled) {
  filter: brightness(1.1);
  color: var(--bg-base);
}

.dc-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.dc-btn-spin {
  width: 12px;
  height: 12px;
  animation: dc-spin 0.8s linear infinite;
}
</style>
