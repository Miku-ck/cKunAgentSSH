<template>
  <Teleport to="body">
    <div class="dt-overlay" @click.self="emit('close')">
      <div ref="dialogRef" class="dt-dialog dt-dialog-auto k8s-dt" role="dialog" aria-modal="true" :aria-label="`${target.kind} 详情：${target.name}`" tabindex="-1">
        <div class="dp-head">
          <span class="st-dot" :class="headState"></span>
          <span class="dp-name mono">{{ target.name }}</span>
          <span class="badge">{{ target.kind }}</span>
          <span v-if="target.namespace" class="badge mono">{{ target.namespace }}</span>
          <span v-if="detail?.owner" class="badge mono">{{ detail.owner }}</span>
          <button class="dp-close" title="关闭" aria-label="关闭" @click="emit('close')">×</button>
        </div>

        <div class="dt-tabs" role="tablist" :aria-label="`${target.kind} 详情分区`">
          <button
            v-for="t in DETAIL_TABS" :key="t.key"
            class="dt-tab" role="tab" :aria-selected="tab === t.key"
            :class="{ on: tab === t.key }"
            :disabled="t.key === 'logs' && !isPod"
            :title="t.key === 'logs' && !isPod ? '只有 Pod 有日志' : undefined"
            @click="selectTab(t.key)"
          >{{ t.label }}</button>
        </div>

        <!-- 概览 -->
        <div v-if="tab === 'overview'" class="dp-body dp-body-auto">
          <div v-if="headLoading && !detail" class="load-band">kubectl get {{ target.kind.toLowerCase() }} …</div>
          <div v-else-if="headError" class="logs-err">
            <div>{{ headError.message }}</div>
            <div v-if="headError.hint" class="err-hint">{{ headError.hint }}</div>
            <button class="btn btn-sm" @click="loadDetail">重试</button>
          </div>
          <template v-else-if="detail">
            <section>
              <h4>标识</h4>
              <div class="kv">
                <span class="k">类型</span><span class="v">{{ detail.kind || '—' }}</span>
                <span class="k">名称</span><span class="v mono">{{ detail.name }}</span>
                <span class="k">命名空间</span><span class="v mono">{{ detail.namespace || '<集群级>' }}</span>
                <span class="k">属主</span><span class="v mono">{{ detail.owner || '<none>（裸资源，删掉不会重建）' }}</span>
              </div>
            </section>

            <section v-if="Object.keys(detail.labels).length">
              <h4>标签 <span class="logs-sub mono">{{ Object.keys(detail.labels).length }}</span></h4>
              <div class="kv">
                <template v-for="(v, k) in detail.labels" :key="k">
                  <span class="k mono">{{ k }}</span><span class="v mono">{{ v }}</span>
                </template>
              </div>
            </section>

            <section v-if="detail.conditions.length">
              <h4>条件 <span class="logs-sub mono">{{ detail.conditions.length }}</span></h4>
              <table class="ptable sub">
                <colgroup><col style="width:22%"><col style="width:9%"><col style="width:20%"><col style="width:49%"></colgroup>
                <thead><tr><th>类型</th><th>状态</th><th>原因</th><th>信息</th></tr></thead>
                <tbody>
                  <tr v-for="c in detail.conditions" :key="c.type">
                    <td class="mono">{{ c.type }}</td>
                    <td><span class="badge" :class="c.status === 'True' ? 'healthy' : 'unhealthy'">{{ c.status }}</span></td>
                    <td class="mono muted">{{ c.reason || '—' }}</td>
                    <td class="wrap" :title="c.message">{{ c.message || '—' }}</td>
                  </tr>
                </tbody>
              </table>
            </section>

            <section v-if="detail.containers.length">
              <h4>容器 <span class="logs-sub mono">{{ detail.containers.length }}</span></h4>
              <table class="ptable sub">
                <colgroup><col style="width:15%"><col style="width:26%"><col style="width:10%"><col style="width:9%"><col style="width:40%"></colgroup>
                <thead><tr><th>名称</th><th>镜像</th><th>就绪</th><th>重启</th><th>状态 / 资源</th></tr></thead>
                <tbody>
                  <tr v-for="c in detail.containers" :key="`${c.init ? 'i' : 'c'}-${c.name}`">
                    <td class="mono">{{ c.name }}<span v-if="c.init" class="logs-sub"> init</span></td>
                    <td class="mono ell" :title="c.image">{{ c.image }}</td>
                    <td class="mono" :class="c.ready === 'ready' ? 'ok' : c.ready === 'done' ? 'muted' : 'bad'">{{ c.ready }}</td>
                    <td class="mono" :class="c.restarts > 0 ? 'bad' : 'muted'">{{ c.restarts }}</td>
                    <td class="wrap" :title="`${c.state}\nlimits: ${c.limits || '—'}\nrequests: ${c.requests || '—'}`">
                      {{ c.state || '—' }}<span v-if="c.limits || c.requests" class="logs-sub mono"> · {{ c.limits || '—' }} / {{ c.requests || '—' }}</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </section>

            <section>
              <h4>
                原始 JSON
                <button class="btn btn-sm" @click="showRaw = !showRaw">{{ showRaw ? '收起' : '展开' }}</button>
              </h4>
              <pre v-if="showRaw" class="raw mono" :title="`${detail.raw.length} 字符`">{{ detail.raw }}</pre>
            </section>
          </template>
        </div>

        <!-- YAML：原文直出（kubectl get -o yaml），与概览的原始 JSON 互补 -->
        <div v-else-if="tab === 'yaml'" class="dp-body">
          <div class="log-bar">
            <span class="logs-sub mono">kubectl get … -o yaml</span>
            <button class="btn btn-sm" @click="loadYaml">重取</button>
          </div>
          <div v-if="yamlError" class="logs-err">
            <div>{{ yamlError }}</div>
            <button class="btn btn-sm" @click="loadYaml">重试</button>
          </div>
          <pre v-else-if="yamlText" class="logs mono">{{ yamlText }}</pre>
          <div v-else class="list-empty">{{ yamlLoading ? '取数中…' : '暂无内容' }}</div>
        </div>

        <!-- 事件：这个对象自己的关联事件，服务端已按时间排好 -->
        <div v-else-if="tab === 'events'" class="dp-body dp-body-auto">
          <div v-if="eventsLoading && !events.length" class="load-band">kubectl get events --field-selector …</div>
          <div v-else-if="eventsError" class="logs-err">
            <div>{{ eventsError }}</div>
            <button class="btn btn-sm" @click="loadEvents">重试</button>
          </div>
          <table v-else-if="events.length" class="ptable">
            <colgroup><col style="width:8%"><col style="width:6%"><col style="width:17%"><col style="width:43%"><col style="width:12%"><col style="width:14%"></colgroup>
            <thead><tr><th>类型</th><th>次数</th><th>原因</th><th>信息</th><th>最近</th><th>来源</th></tr></thead>
            <tbody>
              <tr v-for="(e, i) in events" :key="`${e.reason}/${e.lastSeen}/${i}`">
                <td><span class="badge" :class="e.type === 'Warning' ? 'unhealthy' : ''">{{ e.type }}</span></td>
                <td class="mono" :class="e.count > 1 ? 'bad' : 'muted'">{{ e.count }}</td>
                <td class="mono">{{ e.reason || '—' }}</td>
                <td class="wrap" :title="e.message">{{ e.message || '—' }}</td>
                <td class="mono muted">{{ e.lastSeen || '—' }}</td>
                <td class="mono muted ell" :title="e.source">{{ e.source || '—' }}</td>
              </tr>
            </tbody>
          </table>
          <div v-else class="list-empty">该对象没有事件记录</div>
        </div>

        <!-- 日志：仅 Pod。跟随后定时重拉，不用 kubectl logs -f -->
        <div v-else class="dp-body">
          <div class="log-bar">
            <select v-if="containerNames.length > 1" v-model="container" class="host-sel" aria-label="选择容器">
              <option v-for="c in containerNames" :key="c.name" :value="c.name">{{ c.name }}{{ c.init ? ' (init)' : '' }}</option>
            </select>
            <label class="ck"><input v-model="previous" type="checkbox" /><span>上一次死亡（--previous）</span></label>
            <label class="ck"><input v-model="follow" type="checkbox" /><span>跟随</span></label>
            <button class="btn btn-sm" @click="loadLogs">重取</button>
            <span v-if="follow" class="logs-sub">每 {{ FOLLOW_INTERVAL_MS / 1000 }}s 拉最近 {{ TAIL }} 行</span>
          </div>
          <div v-if="logsError" class="logs-err">
            <div>{{ logsError }}</div>
            <button class="btn btn-sm" @click="loadLogs">重试</button>
          </div>
          <pre v-else-if="logs.length" class="logs mono">{{ logs.join('\n') }}</pre>
          <div v-else class="list-empty">{{ logsLoading ? '取数中…' : (follow ? '等待新日志…' : '该容器暂无日志') }}</div>
        </div>

        <div class="k8s-dt-foot mono">
          <span>{{ target.kind }}/{{ target.name }}</span>
          <span class="sb-right">{{ syncedAt || '取数中' }}</span>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
// K8sResourceDetail.vue — 详情弹窗：四条独立取数腿（对象本体 / YAML / 关联事件 / 日志）。
// 每条各持自己的序号，页面隐藏或卸载后到达的回包一律丢弃 —— 弹窗挂在 body 上，
// 用户完全可以在日志飞回来之前关掉它、甚至切到另一台主机。
//
// 日志用轮询 tail 而不是 kubectl logs -f：follow 是一个永不结束的 exec，
// 只能等 600s 超时才收尾，会把这条 SSH 会话的通道占住（docker logs 同理）。
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useModalFocusTrap } from '../../../composables/useModalFocusTrap'
import {
  getOneCmd, getOneYamlCmd, logsCmd, objectEventsCmd,
  parseEvents, parseResourceDetail, podContainerNames, k8sErrText,
  type EventRow, type K8sError, type ResourceDetail,
} from '@/stores/k8s-core'
import { useK8sHost } from './context'

const props = defineProps<{
  target: { kind: string; name: string; namespace: string }
  sessionId: string
}>()
const emit = defineEmits<{ close: [] }>()

const host = useK8sHost()
const dialogRef = ref<HTMLElement | null>(null)
const { activate, deactivate } = useModalFocusTrap(dialogRef)
function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') emit('close')
}
const DETAIL_TABS = [
  { key: 'overview', label: '概览' },
  { key: 'yaml', label: 'YAML' },
  { key: 'events', label: '事件' },
  { key: 'logs', label: '日志' },
] as const
type DetailTab = (typeof DETAIL_TABS)[number]['key']
const TAIL = 200
const FOLLOW_INTERVAL_MS = 2500

const tab = ref<DetailTab>('overview')
const detail = ref<ResourceDetail | null>(null)
const headError = ref<K8sError | null>(null)
const headLoading = ref(false)
const yamlText = ref('')
const yamlError = ref('')
const yamlLoading = ref(false)
const events = ref<EventRow[]>([])
const eventsError = ref('')
const eventsLoading = ref(false)
const containerNames = ref<{ name: string; init: boolean }[]>([])
const container = ref('')
const previous = ref(false)
const follow = ref(false)
const logs = ref<string[]>([])
const logsError = ref('')
const logsLoading = ref(false)
const showRaw = ref(false)
const syncedAt = ref('')

const isPod = computed(() => props.target.kind === 'Pod')
const headState = computed(() => {
  if (headError.value) return 'bad'
  const ready = detail.value?.containers.filter(c => !c.init) || []
  if (!ready.length) return 'muted'
  return ready.every(c => c.ready === 'ready' || c.ready === 'done') ? 'ok' : 'bad'
})

let detailSeq = 0
let yamlSeq = 0
let eventsSeq = 0
let logsSeq = 0
let disposed = false
onMounted(() => {
  activate()
  window.addEventListener('keydown', onKeydown)
})
onUnmounted(() => {
  disposed = true
  stopFollow()
  window.removeEventListener('keydown', onKeydown)
  deactivate()
})

const SEQ = { detail: () => detailSeq, yaml: () => yamlSeq, events: () => eventsSeq, logs: () => logsSeq }

/** 回包能不能写回：本组件未被销毁、会话仍是打开时那条、且同一腿没有更新的请求 */
function fresh(kind: keyof typeof SEQ, n: number): boolean {
  if (disposed || host.targetId.value !== props.sessionId) return false
  return n === SEQ[kind]()
}

async function loadDetail(): Promise<void> {
  const n = ++detailSeq
  headLoading.value = true
  try {
    const raw = await host.k8sExec(getOneCmd(props.target.kind, props.target.name, props.target.namespace), props.sessionId)
    if (!fresh('detail', n)) return
    detail.value = parseResourceDetail(raw)
    headError.value = null
    syncedAt.value = new Date().toLocaleTimeString()
    // Pod 的容器名取自本体响应：省掉一次额外的 get
    if (isPod.value) {
      containerNames.value = podContainerNames(raw)
      if (!containerNames.value.some(c => c.name === container.value)) {
        container.value = containerNames.value[0]?.name || ''
      }
    }
  } catch (e) {
    if (!fresh('detail', n)) return
    headError.value = e as K8sError
    detail.value = null
  } finally {
    if (fresh('detail', n)) headLoading.value = false
  }
}

async function loadYaml(): Promise<void> {
  const n = ++yamlSeq
  yamlLoading.value = true
  try {
    const raw = await host.k8sExec(getOneYamlCmd(props.target.kind, props.target.name, props.target.namespace), props.sessionId)
    if (!fresh('yaml', n)) return
    yamlText.value = raw.replace(/\r\n/g, '\n').trimEnd()
    yamlError.value = ''
  } catch (e) {
    if (!fresh('yaml', n)) return
    yamlText.value = ''
    yamlError.value = k8sErrText(e)
  } finally {
    if (fresh('yaml', n)) yamlLoading.value = false
  }
}

async function loadEvents(): Promise<void> {
  const n = ++eventsSeq
  eventsLoading.value = true
  try {
    const raw = await host.k8sExec(
      objectEventsCmd(props.target.name, props.target.kind, props.target.namespace), props.sessionId,
    )
    if (!fresh('events', n)) return
    events.value = parseEvents(raw)
    eventsError.value = ''
  } catch (e) {
    if (!fresh('events', n)) return
    events.value = []
    eventsError.value = k8sErrText(e)
  } finally {
    if (fresh('events', n)) eventsLoading.value = false
  }
}

async function loadLogs(): Promise<void> {
  const n = ++logsSeq
  logsLoading.value = true
  try {
    const raw = await host.k8sExec(
      logsCmd(props.target.name, props.target.namespace, {
        container: container.value || undefined, tail: TAIL, previous: previous.value,
      }),
      props.sessionId,
    )
    if (!fresh('logs', n)) return
    const arr = raw.split('\n')
    if (arr.length && arr[arr.length - 1] === '') arr.pop()
    logs.value = arr
    logsError.value = ''
  } catch (e) {
    if (!fresh('logs', n)) return
    logs.value = []
    logsError.value = k8sErrText(e)
  } finally {
    if (fresh('logs', n)) logsLoading.value = false
  }
}

let followTimer: ReturnType<typeof setInterval> | null = null
function stopFollow(): void {
  if (followTimer) { clearInterval(followTimer); followTimer = null }
}
function syncFollow(): void {
  stopFollow()
  if (!follow.value || tab.value !== 'logs' || disposed) return
  followTimer = setInterval(() => { void loadLogs() }, FOLLOW_INTERVAL_MS)
}

function selectTab(next: DetailTab): void {
  tab.value = next
  if (next === 'yaml' && !yamlText.value && !yamlError.value) void loadYaml()
  if (next === 'events' && !events.value.length && !eventsError.value) void loadEvents()
  if (next === 'logs') {
    if (!logs.value.length && !logsError.value) void loadLogs()
    syncFollow()
  } else {
    stopFollow()
  }
}

// 切容器 / 切 previous 立刻重取；跟随开关只动定时器
watch([container, previous], () => { if (tab.value === 'logs') void loadLogs() })
watch(follow, syncFollow)

void loadDetail()
</script>
