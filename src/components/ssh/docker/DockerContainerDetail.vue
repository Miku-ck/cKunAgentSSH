<template>
  <Teleport to="body">
    <div class="dt-overlay" @click.self="emit('close')">
      <div ref="dialogRef" class="dt-dialog" role="dialog" aria-modal="true" :aria-label="'容器详情：' + container.name" tabindex="-1">
        <div class="dp-head">
          <span class="st-dot" :class="rowState"></span>
          <span class="dp-name mono">{{ container.name }}</span>
          <span class="badge" :class="rowState">{{ headStatusText }}</span>
          <span v-if="health" class="badge" :class="health">{{ healthLabel }}</span>
          <span v-if="inspect?.compose" class="badge">{{ inspect.compose.project }}<template v-if="inspect.compose.service"> / {{ inspect.compose.service }}</template></span>
          <button class="dp-close" title="关闭" aria-label="关闭" @click="emit('close')">×</button>
        </div>

        <div class="dt-tabs" role="tablist" aria-label="容器详情">
          <button v-for="t in TABS" :key="t.key" class="dt-tab" role="tab" :aria-selected="tab === t.key" :class="{ on: tab === t.key }" @click="tab = t.key">{{ t.label }}</button>
        </div>

        <!-- 概览：健康告警 + 实时资源 + 基本信息 + 端口/网络/挂载 -->
        <div v-if="tab === 'overview'" class="dp-body dp-body-auto">
          <div v-if="inspectLoading && !inspect" class="load-band">docker container inspect…</div>
          <div v-else-if="inspectError" class="logs-err">
            <div>{{ inspectError.message }}</div>
            <div v-if="inspectError.hint" class="err-hint">{{ inspectError.hint }}</div>
            <button class="btn btn-sm" @click="loadInspect">重试</button>
          </div>
          <template v-else-if="inspect">
            <div v-if="unhealthy" class="health-alert">
              <div class="health-head">
                <span>unhealthy</span>
                <span v-if="typeof inspect.state.failingStreak === 'number'" class="mono">连续失败 {{ inspect.state.failingStreak }} 次</span>
              </div>
              <div v-if="healthMeta" class="health-meta mono">{{ healthMeta }}</div>
              <pre v-if="inspect.state.lastHealth?.output" class="health-out">{{ inspect.state.lastHealth.output }}</pre>
              <div v-if="inspect.state.lastHealth?.end" class="health-time mono">末次检测 {{ inspect.state.lastHealth.end }}</div>
            </div>

            <section>
              <h4>资源 <span class="logs-sub mono">{{ isRunning ? 'docker stats · 每 5s' : '容器未运行' }}</span></h4>
              <div class="stats-grid">
                <div class="stat"><span class="stat-k">CPU</span><span class="stat-v mono">{{ stats?.cpuPerc || '—' }}</span></div>
                <div class="stat">
                  <span class="stat-k">内存</span>
                  <span class="stat-v mono">{{ stats?.memUsage || '—' }}</span>
                  <span v-if="stats?.memPerc" class="stat-sub mono">{{ stats.memPerc }}</span>
                </div>
                <div class="stat"><span class="stat-k">网络 I/O</span><span class="stat-v mono">{{ stats?.netIO || '—' }}</span></div>
                <div class="stat"><span class="stat-k">磁盘 I/O</span><span class="stat-v mono">{{ stats?.blockIO || '—' }}</span></div>
                <div class="stat"><span class="stat-k">进程</span><span class="stat-v mono">{{ stats?.pids || '—' }}</span></div>
              </div>
              <div v-if="statsError" class="stat-err">{{ statsError.message }}</div>
            </section>

            <section>
              <h4>基本信息</h4>
              <div class="kv">
                <span class="k">容器 ID</span><span class="v">{{ inspect.id }}</span>
                <span class="k">镜像</span><span class="v">{{ inspect.image || container.repo + ':' + container.tag }}</span>
                <span class="k">镜像 ID</span><span class="v">{{ inspect.imageId || '—' }}</span>
                <span v-if="inspect.imageDigest" class="k">摘要</span><span v-if="inspect.imageDigest" class="v">{{ inspect.imageDigest }}</span>
                <span class="k">状态</span><span class="v">{{ headStatusText }}</span>
                <template v-if="!isRunning">
                  <span class="k">退出码</span><span class="v">{{ inspect.state.exitCode }}</span>
                </template>
                <span v-if="inspect.state.oomKilled" class="k">OOM</span><span v-if="inspect.state.oomKilled" class="v">是</span>
                <span class="k">创建</span><span class="v">{{ inspect.created || '—' }}</span>
                <span v-if="inspect.state.startedAt" class="k">启动</span><span v-if="inspect.state.startedAt" class="v">{{ inspect.state.startedAt }}</span>
                <span v-if="inspect.state.finishedAt" class="k">结束</span><span v-if="inspect.state.finishedAt" class="v">{{ inspect.state.finishedAt }}</span>
                <span class="k">平台</span><span class="v">{{ inspect.platform || '—' }}</span>
                <span class="k">重启策略</span><span class="v">{{ inspect.restartPolicy || 'no' }}<template v-if="inspect.restartCount"> · 已重启 {{ inspect.restartCount }} 次</template></span>
                <span class="k">命令</span><span class="v">{{ commandText }}</span>
              </div>
            </section>

            <section>
              <h4>端口 <span class="logs-sub mono">{{ inspect.ports.length }}</span></h4>
              <div v-if="!inspect.ports.length" class="list-empty sub">未映射端口</div>
              <table v-else class="ptable sub">
                <colgroup><col style="width:50%"><col style="width:50%"></colgroup>
                <thead><tr><th>容器端口</th><th>主机绑定</th></tr></thead>
                <tbody>
                  <tr v-for="(p, i) in inspect.ports" :key="i">
                    <td class="mono">{{ p.container }}</td>
                    <td class="mono" :class="{ muted: !p.host }">{{ p.host || '未绑定' }}</td>
                  </tr>
                </tbody>
              </table>
            </section>

            <section>
              <h4>网络 <span class="logs-sub mono">{{ inspect.networks.length }}</span></h4>
              <div v-if="!inspect.networks.length" class="list-empty sub">未接入网络</div>
              <table v-else class="ptable sub">
                <colgroup><col style="width:26%"><col style="width:22%"><col style="width:22%"><col style="width:30%"></colgroup>
                <thead><tr><th>网络</th><th>IPv4</th><th>网关</th><th>MAC</th></tr></thead>
                <tbody>
                  <tr v-for="n in inspect.networks" :key="n.name">
                    <td class="mono">{{ n.name }}</td>
                    <td class="mono">{{ n.ip || '—' }}</td>
                    <td class="mono">{{ n.gateway || '—' }}</td>
                    <td class="mono muted">{{ n.mac || '—' }}</td>
                  </tr>
                </tbody>
              </table>
            </section>

            <section>
              <h4>挂载 <span class="logs-sub mono">{{ inspect.mounts.length }}</span></h4>
              <div v-if="!inspect.mounts.length" class="list-empty sub">无挂载</div>
              <table v-else class="ptable sub">
                <colgroup><col style="width:12%"><col style="width:42%"><col style="width:34%"><col style="width:12%"></colgroup>
                <thead><tr><th>类型</th><th>源</th><th>容器内路径</th><th>读写</th></tr></thead>
                <tbody>
                  <tr v-for="(m, i) in inspect.mounts" :key="i">
                    <td class="mono">{{ m.type || '—' }}</td>
                    <td class="mono" :title="m.source || m.name">{{ m.source || m.name || '—' }}</td>
                    <td class="mono" :title="m.destination">{{ m.destination || '—' }}</td>
                    <td class="mono">{{ m.rw ? 'rw' : 'ro' }}</td>
                  </tr>
                </tbody>
              </table>
            </section>
          </template>
          <div v-else class="list-empty">无详情</div>
        </div>

        <!-- 配置：启动参数 + 资源限制 + 健康检查 + 环境变量 + 标签 -->
        <div v-else-if="tab === 'config'" class="dp-body dp-body-auto">
          <div v-if="inspectLoading && !inspect" class="load-band">docker container inspect…</div>
          <div v-else-if="inspectError" class="logs-err">
            <div>{{ inspectError.message }}</div>
            <div v-if="inspectError.hint" class="err-hint">{{ inspectError.hint }}</div>
            <button class="btn btn-sm" @click="loadInspect">重试</button>
          </div>
          <template v-else-if="inspect">
            <section>
              <h4>启动配置</h4>
              <div class="kv kv-wide">
                <span class="k">Entrypoint</span><span class="v">{{ formatArgv(inspect.entrypoint) }}</span>
                <span class="k">Cmd</span><span class="v">{{ formatArgv(inspect.cmd) }}</span>
                <span class="k">工作目录</span><span class="v">{{ inspect.workingDir || '—' }}</span>
                <span class="k">用户</span><span class="v">{{ inspect.user || '—' }}</span>
              </div>
            </section>

            <section>
              <h4>资源限制</h4>
              <div class="kv kv-wide">
                <span class="k">内存上限</span><span class="v">{{ inspect.resources.memory || '不限' }}</span>
                <span class="k">CPU 限额</span><span class="v">{{ inspect.resources.cpus ? inspect.resources.cpus + ' 核' : '不限' }}</span>
                <span class="k">PIDs 上限</span><span class="v">{{ inspect.resources.pidsLimit ?? '不限' }}</span>
              </div>
            </section>

            <section v-if="inspect.healthcheck">
              <h4>健康检查 <span class="logs-sub mono">{{ healthLabel || '未上报' }}</span></h4>
              <div class="kv kv-wide">
                <span class="k">检测命令</span><span class="v">{{ inspect.healthcheck.test }}</span>
                <span class="k">间隔</span><span class="v">{{ inspect.healthcheck.interval || '—' }}</span>
                <span class="k">超时</span><span class="v">{{ inspect.healthcheck.timeout || '—' }}</span>
                <span class="k">启动宽限</span><span class="v">{{ inspect.healthcheck.startPeriod || '—' }}</span>
                <span class="k">重试次数</span><span class="v">{{ inspect.healthcheck.retries }}</span>
              </div>
            </section>

            <section v-if="inspect.compose">
              <h4>Compose</h4>
              <div class="kv kv-wide">
                <span class="k">项目</span><span class="v">{{ inspect.compose.project }}</span>
                <span class="k">服务</span><span class="v">{{ inspect.compose.service || '—' }}</span>
                <span class="k">配置文件</span><span class="v">{{ inspect.compose.configFiles || '—' }}</span>
              </div>
            </section>

            <section>
              <h4>环境变量 <span class="logs-sub mono">{{ inspect.env.length }}</span></h4>
              <div v-if="!inspect.env.length" class="list-empty sub">无环境变量</div>
              <table v-else class="ptable sub wrap-table">
                <colgroup><col style="width:36%"><col style="width:64%"></colgroup>
                <thead><tr><th>变量</th><th>值</th></tr></thead>
                <tbody>
                  <tr v-for="e in inspect.env" :key="e.key">
                    <td class="mono" :title="e.key">{{ e.key }}</td>
                    <td class="mono value-wrap" :title="e.value">{{ e.value || '—' }}</td>
                  </tr>
                </tbody>
              </table>
            </section>

            <section v-if="Object.keys(inspect.labels).length">
              <h4>标签 <span class="logs-sub mono">{{ Object.keys(inspect.labels).length }}</span></h4>
              <table class="ptable sub wrap-table">
                <colgroup><col style="width:44%"><col style="width:56%"></colgroup>
                <thead><tr><th>键</th><th>值</th></tr></thead>
                <tbody>
                  <tr v-for="(val, key) in inspect.labels" :key="key">
                    <td class="mono" :title="String(key)">{{ key }}</td>
                    <td class="mono value-wrap" :title="val">{{ val }}</td>
                  </tr>
                </tbody>
              </table>
            </section>
          </template>
          <div v-else class="list-empty">无详情</div>
        </div>

        <!-- 日志：首次切到本段才拉，离开即停跟随 -->
        <div v-else class="dp-body">
          <section class="logs-section">
            <h4>
              <span>日志 <span class="logs-sub mono">docker logs --tail 200</span></span>
              <button class="follow-btn" title="刷新" @click="loadLogs()">
                刷新
              </button>
              <button class="follow-btn" :class="{ on: follow }" :aria-pressed="follow" title="实时跟随（每 2.5s 轮询）" @click="setFollow(!follow)">
                跟随
              </button>
            </h4>
            <div v-if="logsError" class="logs-err">
              <div>{{ logsError.message }}</div>
              <div v-if="logsError.hint" class="err-hint">{{ logsError.hint }}</div>
              <button class="btn btn-sm" @click="loadLogs()">重试</button>
            </div>
            <div v-else ref="logsEl" class="logs mono">
              <div v-if="logsLoading && !logs.length" class="logs-hint">加载日志…</div>
              <div v-else-if="!logs.length" class="logs-hint">（无日志输出）</div>
              <div v-for="l in logsView" v-else :key="l.id" class="lrow" :class="l.cls">{{ l.text || ' ' }}</div>
            </div>
          </section>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  containerInspectCmd, containerStatsCmd, logsCmd,
  parseContainerInspect, parseContainerStats, parseLogs,
  dockerErrorFromResult, describeThrownDockerError,
  type ContainerRow, type ContainerInspect, type ContainerStats, type DockerError,
  type HealthState,
} from '@/stores/docker-core'
import { useModalFocusTrap } from '../../../composables/useModalFocusTrap'

const props = defineProps<{
  container: ContainerRow
  sessionId: string
}>()
const emit = defineEmits<{ close: [] }>()

const TABS = [
  { key: 'overview', label: '概览' },
  { key: 'config', label: '配置' },
  { key: 'logs', label: '日志' },
] as const
type DetailTab = (typeof TABS)[number]['key']
const tab = ref<DetailTab>('overview')

const READ_TIMEOUT_SEC = 15
const STATS_INTERVAL_MS = 5000
const FOLLOW_INTERVAL_MS = 2500
const HEALTH_LABELS: Record<HealthState, string> = {
  healthy: 'healthy',
  unhealthy: 'unhealthy',
  starting: 'starting',
}

const inspect = ref<ContainerInspect | null>(null)
const inspectLoading = ref(false)
const inspectError = ref<DockerError | null>(null)

const stats = ref<ContainerStats | null>(null)
const statsError = ref<DockerError | null>(null)

const logs = ref<string[]>([])
const logsLoading = ref(false)
const logsError = ref<DockerError | null>(null)
const logsEl = ref<HTMLElement | null>(null)
const follow = ref(false)

let inspectSeq = 0
let statsSeq = 0
let logsSeq = 0
let statsInFlight = false
let disposed = false
let statsTimer: ReturnType<typeof setInterval> | null = null
let followTimer: ReturnType<typeof setInterval> | null = null

const health = computed(() => inspect.value?.state.health ?? props.container.health)
const healthLabel = computed(() => (health.value ? HEALTH_LABELS[health.value] : ''))
const unhealthy = computed(() => health.value === 'unhealthy')
const isRunning = computed(() =>
  inspect.value
    ? inspect.value.state.running && !inspect.value.state.paused
    : props.container.state === 'running',
)
const rowState = computed(() => {
  if (!inspect.value) return props.container.state
  const s = inspect.value.state
  if (s.running && !s.paused) return 'running'
  if (s.paused) return 'stopped'
  return s.exitCode === 0 ? 'stopped' : 'exited'
})
const headStatusText = computed(() => {
  const s = inspect.value?.state
  if (!s) return props.container.stateText
  if (s.paused) return 'paused'
  if (s.restarting) return 'restarting'
  return s.status || props.container.stateText
})
const commandText = computed(() => {
  if (!inspect.value) return containerCommand()
  return [inspect.value.entrypoint, inspect.value.cmd]
    .filter(a => a.length)
    .map(a => formatArgv(a))
    .join(' ') || '—'
})
const healthMeta = computed(() => {
  const hc = inspect.value?.healthcheck
  if (!hc) return ''
  return `${hc.test} · 间隔 ${hc.interval || '—'} · 超时 ${hc.timeout || '—'} · 重试 ${hc.retries}`
})
const logsView = computed(() =>
  logs.value.map((line, i) => ({ id: i, text: line, cls: logClass(line) })),
)

function containerCommand(): string {
  return props.container.cmd || '—'
}
function formatArgv(args: string[]): string {
  if (!args.length) return '—'
  return args.map(a => (a === '' || /\s/.test(a) ? JSON.stringify(a) : a)).join(' ')
}
function logClass(line: string): string {
  if (/error|panic|fatal|exception|failed/i.test(line)) return 'err'
  if (/warn/i.test(line)) return 'warn'
  return ''
}

async function readExec(cmd: string): Promise<string> {
  const api = window.electronAPI
  if (!api || !props.sessionId) {
    throw { code: 'NO_SESSION', message: '未选择目标主机', retryable: false } as DockerError
  }
  let res
  try {
    res = await api.sshExec(props.sessionId, cmd, READ_TIMEOUT_SEC)
  } catch (err) {
    throw describeThrownDockerError(err)
  }
  const dErr = dockerErrorFromResult(res.stderr, res.exitCode, res.stdout)
  if (dErr) throw dErr
  return res.stdout
}

async function loadInspect(): Promise<void> {
  const seq = ++inspectSeq
  inspectLoading.value = true
  inspectError.value = null
  try {
    const next = parseContainerInspect(await readExec(containerInspectCmd(props.container.name)))
    if (seq !== inspectSeq || disposed) return
    inspect.value = next
    if (!next) {
      inspectError.value = { code: 'DOCKER_INSPECT_EMPTY', message: '未取到容器详情', retryable: true }
    }
    syncStatsTimer()
  } catch (e) {
    if (seq !== inspectSeq || disposed) return
    inspectError.value = e as DockerError
  } finally {
    if (seq === inspectSeq && !disposed) inspectLoading.value = false
  }
}

async function loadStats(): Promise<void> {
  if (statsInFlight) return
  statsInFlight = true
  const seq = ++statsSeq
  try {
    const next = parseContainerStats(await readExec(containerStatsCmd(props.container.name)))
    if (seq !== statsSeq || disposed) return
    stats.value = next
    statsError.value = next ? null : { code: 'DOCKER_STATS_EMPTY', message: '未取到资源用量', retryable: true }
  } catch (e) {
    if (seq !== statsSeq || disposed) return
    statsError.value = e as DockerError
  } finally {
    statsInFlight = false
  }
}

async function loadLogs(quiet = false): Promise<void> {
  const seq = ++logsSeq
  logsError.value = null
  if (!quiet) logsLoading.value = true
  try {
    const raw = await readExec(logsCmd(props.container.name))
    if (seq !== logsSeq || disposed) return
    logs.value = parseLogs(raw)
    if (follow.value) await scrollLogsToBottom()
  } catch (e) {
    if (seq !== logsSeq || disposed) return
    logsError.value = e as DockerError
  } finally {
    if (!quiet && seq === logsSeq && !disposed) logsLoading.value = false
  }
}

async function scrollLogsToBottom(): Promise<void> {
  await nextTick()
  const el = logsEl.value
  if (el) el.scrollTop = el.scrollHeight
}

function stopStatsTimer(): void {
  if (statsTimer) { clearInterval(statsTimer); statsTimer = null }
}
function stopFollowTimer(): void {
  if (followTimer) { clearInterval(followTimer); followTimer = null }
}
function syncStatsTimer(): void {
  const shouldPoll = !disposed && isRunning.value && tab.value === 'overview'
  if (!shouldPoll) { stopStatsTimer(); return }
  if (!statsTimer) statsTimer = setInterval(() => { void loadStats() }, STATS_INTERVAL_MS)
}
function setFollow(on: boolean): void {
  stopFollowTimer()
  follow.value = on && tab.value === 'logs'
  if (follow.value) {
    followTimer = setInterval(() => { void loadLogs(true) }, FOLLOW_INTERVAL_MS)
  }
}

const dialogRef = ref<HTMLElement | null>(null)
// 容器详情是全仓唯一连 Esc 都没有的弹窗：只能点 ✕ 或遮罩。Tab 环形与关闭还原一并接上。
const { activate, deactivate } = useModalFocusTrap(dialogRef)
function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') emit('close')
}

watch(tab, (next) => {
  if (next === 'logs') {
    void loadLogs()
    setFollow(false)
    stopStatsTimer()
    return
  }
  setFollow(false)
  if (next === 'overview' && isRunning.value) void loadStats()
  syncStatsTimer()
})

onMounted(() => {
  activate()
  window.addEventListener('keydown', onKeydown)
  void loadInspect()
  if (isRunning.value) void loadStats()
  syncStatsTimer()
})

onUnmounted(() => {
  disposed = true
  window.removeEventListener('keydown', onKeydown)
  deactivate()
  stopStatsTimer()
  stopFollowTimer()
})
</script>

<style scoped>
.dt-tabs {
  display: flex; gap: var(--spacing-2); padding: var(--spacing-6) 18px 0;
  border-bottom: 1px solid var(--divider-primary); flex-shrink: 0;
}
.dt-tab {
  border: none; background: transparent; border-bottom: 2px solid transparent;
  color: var(--text-secondary); font-size: 0.9231rem; padding: var(--spacing-6) var(--spacing-10) var(--spacing-8); cursor: pointer;
  transition: color var(--transition-fast), border-color var(--transition-fast);
}
.dt-tab:hover { color: var(--text-primary); }
.dt-tab.on { color: var(--accent-text); border-bottom-color: var(--accent-primary); }

.st-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--text-tertiary); }
.st-dot.running { background: var(--status-success-text); }
.st-dot.exited { background: var(--status-error-text); }
.st-dot.stopped { background: var(--text-tertiary); }

.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(118px, 1fr)); gap: var(--spacing-8); }
.stat {
  min-width: 0; display: flex; flex-direction: column; gap: 3px; padding: var(--spacing-8) var(--spacing-10);
  background: var(--bg-elevated); border: 1px solid var(--border-subtle); border-radius: var(--radius-md);
}
.stat-k { font-size: 0.7692rem; color: var(--text-tertiary); letter-spacing: .5px; text-transform: uppercase; }
.stat-v { font-size: 1rem; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.stat-sub { font-size: 0.7692rem; color: var(--text-secondary); }
.stat-err { margin-top: var(--spacing-6); font-size: 0.8462rem; color: var(--status-error-text); }

.health-alert {
  display: flex; flex-direction: column; gap: var(--spacing-6); padding: var(--spacing-10) var(--spacing-12);
  background: color-mix(in srgb, var(--status-error-text) 8%, var(--bg-elevated));
  border: 1px solid color-mix(in srgb, var(--status-error-text) 28%, transparent);
  border-radius: var(--radius-md);
}
.health-head {
  display: flex; align-items: center; gap: var(--spacing-8);
  font-size: 0.9231rem; font-weight: 600; color: var(--status-error-text);
}
.health-meta { font-size: 0.8462rem; color: var(--text-secondary); word-break: break-all; }
.health-out {
  margin: 0; max-height: 96px; overflow-y: auto; padding: var(--spacing-6) var(--spacing-8);
  font-family: var(--font-mono); font-size: 0.8462rem; color: var(--text-secondary);
  background: var(--bg-input); border-radius: var(--radius-sm);
  white-space: pre-wrap; word-break: break-all;
}
.health-time { font-size: 0.7692rem; color: var(--text-tertiary); }

.err-hint { margin-top: var(--spacing-4); color: var(--text-secondary); }
.logs-err .btn { margin-top: var(--spacing-8); }
.wrap-table td.value-wrap { white-space: pre-wrap; word-break: break-all; }
</style>
