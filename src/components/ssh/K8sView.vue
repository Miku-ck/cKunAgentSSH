<template>
  <div class="k8s-view">
    <!-- 连接状态条：与 Docker 页同一套骨架，只多一枚 context 芯片 -->
    <div class="page-head">
      <div class="conn-left">
        <div class="conn-dot" :class="targetSession ? 'online' : 'offline'"></div>
        <span class="conn-name">K8s</span>
        <span v-if="targetHost" class="conn-host mono" :title="targetHost">{{ targetHost }}</span>
        <span v-if="contextName" class="ctx-chip mono" :title="`当前 context：${contextName}（本页只展示不切换）`">{{ contextName }}</span>
      </div>

      <select v-if="remoteSessions.length > 1" v-model="targetId" class="host-sel" aria-label="切换目标主机">
        <option v-for="s in remoteSessions" :key="s.id" :value="s.id">{{ s.name }}</option>
      </select>

      <span class="head-spacer"></span>
      <!-- 按钮单枚随态切换，与 Docker / 监控仪表盘同一口径 -->
      <div class="conn-right">
        <button v-if="targetSession" class="btn btn-danger" @click="disconnectTarget">断开远程</button>
        <button v-else class="btn btn-primary" @click="showConnPicker = true">连接远程</button>
      </div>
    </div>

    <div v-if="!remoteSessions.length" class="empty">
      <IconK8s :width="36" :height="36" :stroke-width="1.5" />
      <span>尚未连接远程主机</span>
    </div>

    <template v-else>
      <div class="toolbar">
        <div class="seg" role="tablist" aria-label="K8s 资源">
          <button
            v-for="t in TABS" :key="t.key" class="seg-btn" role="tab"
            :aria-selected="tab === t.key" :class="{ on: tab === t.key }" @click="selectTab(t.key)"
          >{{ t.label }}</button>
        </div>
        <div class="search">
          <input v-model="query" placeholder="按名称 / 命名空间过滤…" aria-label="过滤" />
        </div>
        <!-- 命名空间是对已取回行的客户端收窄：列表仍一律 -A，切它不发命令 -->
        <select
          v-if="tab !== 'nodes' && namespaces.length" v-model="nsFilter" class="host-sel"
          aria-label="按命名空间过滤" title="只显示该命名空间的资源；不重新取数"
        >
          <option value="">全部命名空间</option>
          <option v-for="ns in namespaces" :key="ns" :value="ns">{{ ns }}</option>
        </select>
        <div class="tb-right">
          <label v-if="tab === 'events'" class="ck" title="Normal 事件大多是调度器与拉镜像的成功心跳，默认不取">
            <input v-model="showNormalEvents" type="checkbox" /><span>含 Normal</span>
          </label>
          <button class="btn" :disabled="loading" @click="refresh">刷新</button>
        </div>
      </div>

      <div v-if="hostCopy" class="err-band" :class="{ quiet: notDetected }" :role="notDetected ? 'status' : 'alert'">
        <div class="eb-text">
          <div class="eb-msg">{{ hostCopy.title }}</div>
          <div v-if="hostCopy.hint" class="eb-hint">{{ hostCopy.hint }}</div>
          <div v-if="probeNote" class="eb-note">{{ probeNote }}</div>
        </div>
        <button v-if="hostError?.retryable" class="btn btn-sm" @click="refresh">重试</button>
      </div>
      <div v-else-if="loading && !currentRows.length" class="load-band">
        <svg class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"/><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"/></svg>
        正在经 SSH 会话执行 {{ activeLabel }} …
      </div>
      <div v-else class="body">
        <div class="list-col">
          <K8sNodesTab
            v-if="tab === 'nodes'"
            :rows="filteredNodes" :error="nodeList.error.value"
            :selected-name="selected?.name ?? null"
            :empty-text="emptyText('节点', false)"
            @open="openDetail(detailOfNode($event), nodeRowKey($event))"
          />
          <K8sWorkloadsTab
            v-else-if="tab === 'workloads'"
            :rows="filteredWorkloads" :error="workloadList.error.value"
            :selected-key="selKey"
            :empty-text="emptyText('负载')"
            @open="openWorkload"
            @restart="doRestart"
            @scale="openScale"
            @remove="doDeleteJob"
          />
          <K8sPodsTab
            v-else-if="tab === 'pods'"
            :rows="filteredPods" :error="podList.error.value"
            :selected-key="selKey"
            :empty-text="emptyText('Pod')"
            @open="openDetail(detailOfPod($event), podRowKey($event))"
            @remove="doDeletePod"
          />
          <K8sServicesTab
            v-else-if="tab === 'services'"
            :rows="filteredServices" :error="serviceList.error.value"
            :selected-key="selKey"
            :empty-text="emptyText('服务')"
            @open="openService"
          />
          <K8sEventsTab
            v-else
            :rows="filteredEvents" :error="eventList.error.value"
            :empty-text="eventsEmptyText"
          />
        </div>
      </div>

      <!-- 状态栏：计数跟着当前 tab 走，永远报「这一屏是什么」。
           取数失败时不许报 0 —— 「0 个负载」和「没读到」是两回事，后者读成前者就是谎报空集。 -->
      <div class="statusbar">
        <span class="sb-name" :title="targetName">{{ targetName }}</span>
        <span>远程 · SSH · {{ scopeLabel }}</span>
        <span v-if="activeListFailed">{{ tabLabel }} <span class="bad">读取失败</span></span>
        <template v-else>
          <span v-if="tab === 'nodes'">节点 <span class="mono">{{ nodes.length }}</span>（<span class="ok">{{ readyNodes }}</span> Ready / <span class="bad">{{ nodes.length - readyNodes }}</span> NotReady）</span>
          <span v-else-if="tab === 'workloads'">负载 <span class="mono">{{ workloads.length }}</span><template v-if="degradedWorkloads">（<span class="bad">{{ degradedWorkloads }}</span> 未就绪）</template></span>
          <span v-else-if="tab === 'pods'">Pod <span class="mono">{{ pods.length }}</span>（<span class="ok">{{ runningPods }}</span> 运行 / <span class="bad">{{ abnormalPods }}</span> 异常）</span>
          <span v-else-if="tab === 'services'">服务 <span class="mono">{{ services.length }}</span><template v-if="ingressCount">（<span class="mono">{{ ingressCount }}</span> Ingress）</template></span>
          <span v-else>事件 <span class="mono">{{ events.length }}</span><template v-if="showNormalEvents && warningCount">（<span class="bad">{{ warningCount }}</span> Warning）</template></span>
        </template>
        <span v-if="truncated" class="sb-warn" title="单次 SSH 命令输出达到 1M 字符上限，列表可能缺行；用搜索框按名称或命名空间收窄">输出已截断</span>
        <span class="sb-right mono">{{ lastSync }}</span>
      </div>
    </template>

    <Teleport to="body">
      <DockerConnectionPicker
        v-if="showConnPicker"
        :connections="sshStore.connections"
        :connecting="connecting"
        :pending-id="pendingConnId"
        @close="showConnPicker = false"
        @pick="handleConnect"
      />
    </Teleport>

    <K8sResourceDetail
      v-if="selected"
      :target="selected"
      :session-id="targetId"
      @close="closeDetail"
    />

    <K8sScaleDialog
      v-if="scaleRow"
      :row="scaleRow"
      :submitting="!!busy && busy.startsWith('scale:')"
      @close="scaleRow = null"
      @submit="doScale"
    />
  </div>
</template>

<script setup lang="ts">
// K8sView.vue — K8s 页的编排层：五个分段的取数、四个窄动作的下发、弹窗的开关。
// 命令拼接与解析一律在 stores/k8s-core.ts，执行底座在 k8s/context.ts，页签只画不跑。
import { computed, onUnmounted, ref, watch } from 'vue'
import { useToastStore } from '@/stores/toast'
import { useConfirm } from '@/composables/useConfirm'
import {
  EVENTS_CMD, EVENTS_CMD_WITH_NORMAL, NODES_CMD, TOP_NODES_CMD, PODS_CMD, SERVICES_CMD, WORKLOADS_CMD,
  ACTION_CONSEQUENCE,
  deleteJobCmd, deletePodCmd, rolloutRestartCmd, scaleCmd,
  isAbnormalPod, parseEvents, parseNodes, parsePods, parseServices,
  parseTopNodes, parseWorkloads, isHostLevelError, mergeNodeUsage, isNotDetectedState, hostErrorCopy,
  envProbeNote,
  workloadRowKey, podRowKey, serviceRowKey, nodeRowKey,
  detailOfNode, detailOfPod, detailOfService, detailOfWorkload,
  type DetailTarget,
  type EventRow, type K8sError, type NodeRow, type PodRow, type ServiceRow, type WorkloadRow,
} from '@/stores/k8s-core'
import { provideK8sHost, readK8sContext, probeK8sEnv, TABS, type ListPin, type TabKey } from './k8s/context'
import IconK8s from '@/components/icons/IconK8s.vue'
import DockerConnectionPicker from './docker/DockerConnectionPicker.vue'
import K8sNodesTab from './k8s/K8sNodesTab.vue'
import K8sWorkloadsTab from './k8s/K8sWorkloadsTab.vue'
import K8sPodsTab from './k8s/K8sPodsTab.vue'
import K8sServicesTab from './k8s/K8sServicesTab.vue'
import K8sEventsTab from './k8s/K8sEventsTab.vue'
import K8sResourceDetail from './k8s/K8sResourceDetail.vue'
import K8sScaleDialog from './k8s/K8sScaleDialog.vue'

// 页面隐藏时收起弹窗：与 Docker 页同一约定（独立页是 v-show 常驻，弹窗不然会跨页留着）
const props = defineProps<{ active?: boolean }>()

const host = provideK8sHost()
const toast = useToastStore()
const { confirm } = useConfirm()
const {
  sshStore, remoteSessions, targetId, targetName, targetHost, targetSession,
  showConnPicker, connecting, pendingConnId, handleConnect, disconnectTarget,
  busy, tab, showNormalEvents, truncated, lastSync, contextName, envProbe,
} = host

const query = ref('')
const nsFilter = ref('')
const loading = ref(false)
const selected = ref<DetailTarget | null>(null)
const selKey = ref<string | null>(null)
const scaleRow = ref<WorkloadRow | null>(null)
let loadGen = 0
onUnmounted(() => { loadGen++ })

// ── 五个分段：一律 -A；命名空间是表里的一列，收窄靠搜索框与命名空间选择（纯客户端） ──
const nodeList = host.createList<NodeRow>({
  kind: 'nodes',
  cmd: () => NODES_CMD,
  parse: parseNodes,
  label: 'kubectl get nodes',
  // 实时用量是第二条腿（kubectl top，依赖 metrics-server）：只在节点列表刚写回过时合并，
  // 失败静默留空 —— 「取不到用量」和「没有用量」是两回事，但都不值得为它红一条带。
  after: (id, pin) => { void enrichNodeUsage(id, pin) },
})
const workloadList = host.createList<WorkloadRow>({
  kind: 'workloads', cmd: () => WORKLOADS_CMD, parse: parseWorkloads, label: 'kubectl get deploy,sts,ds,job,cj',
})
const podList = host.createList<PodRow>({
  kind: 'pods', cmd: () => PODS_CMD, parse: parsePods, label: 'kubectl get pods',
})
const serviceList = host.createList<ServiceRow>({
  kind: 'services', cmd: () => SERVICES_CMD, parse: parseServices, label: 'kubectl get svc,ing',
})
const eventList = host.createList<EventRow>({
  kind: 'events', cmd: () => (showNormalEvents.value ? EVENTS_CMD_WITH_NORMAL : EVENTS_CMD),
  parse: parseEvents, label: 'kubectl get events',
})

const nodes = computed(() => nodeList.rows.value)
const workloads = computed(() => workloadList.rows.value)
const pods = computed(() => podList.rows.value)
const services = computed(() => serviceList.rows.value)
const events = computed(() => eventList.rows.value)

/** 异常置顶：正在崩溃的 Pod 排第一行，而不是埋在字母序第 14 行 */
const sortedPods = computed(() => [...pods.value].sort((a, b) =>
  (isAbnormalPod(b) ? 1 : 0) - (isAbnormalPod(a) ? 1 : 0) || a.namespace.localeCompare(b.namespace) || a.name.localeCompare(b.name)))
const sortedWorkloads = computed(() => [...workloads.value].sort((a, b) =>
  (b.status === 'Degraded' || b.status === 'Failure' ? 1 : 0) - (a.status === 'Degraded' || a.status === 'Failure' ? 1 : 0)
  || a.kind.localeCompare(b.kind) || a.name.localeCompare(b.name)))

const q = computed(() => query.value.trim().toLowerCase())

/**
 * 命名空间选项从已取回的行里归纳：跨页签共用一份，选中后对每类资源都生效。
 * 它只是一层显示过滤 —— 列表仍 -A 取回，切命名空间不发命令。
 */
const namespaces = computed(() => {
  const set = new Set<string>()
  for (const r of [...workloads.value, ...pods.value, ...services.value, ...events.value]) {
    if (r.namespace) set.add(r.namespace)
  }
  return [...set].sort((a, b) => a.localeCompare(b))
})
// 刷新后选中的命名空间可能整个消失（资源被删 / 换了集群）：回落到全部，不留一个空选择
watch(namespaces, (list) => {
  if (nsFilter.value && !list.includes(nsFilter.value)) nsFilter.value = ''
})

function inNs<T extends { namespace?: string }>(rows: T[]): T[] {
  return nsFilter.value ? rows.filter(r => r.namespace === nsFilter.value) : rows
}

/** 列表全 -A 取回，命名空间成了表里的一列：过滤也跟着扩成「名称 / 命名空间」两格 */
function byName<T extends { name: string; namespace?: string }>(rows: T[], useNs = true): T[] {
  const scoped = useNs ? inNs(rows) : rows
  if (!q.value) return scoped
  return scoped.filter(r =>
    r.name.toLowerCase().includes(q.value) || (r.namespace ?? '').toLowerCase().includes(q.value))
}
// 节点是集群级资源，命名空间过滤对它不生效
const filteredNodes = computed(() => byName(nodes.value, false))
const filteredWorkloads = computed(() => byName(sortedWorkloads.value))
const filteredPods = computed(() => byName(sortedPods.value))
const filteredServices = computed(() => byName(services.value))
const filteredEvents = computed(() => {
  // Warning 的收窄交给服务端（--field-selector type=Warning），这里不再本地兜一层：
  // API server 认这个选择器是保证，重复过滤只会让「到底谁在过滤」变成两说
  const scoped = inNs(events.value)
  return q.value
    ? scoped.filter(e =>
        [e.namespace, e.object, e.reason, e.message].some(t => t.toLowerCase().includes(q.value)))
    : scoped
})

const currentRows = computed(() => {
  if (tab.value === 'nodes') return filteredNodes.value
  if (tab.value === 'workloads') return filteredWorkloads.value
  if (tab.value === 'pods') return filteredPods.value
  if (tab.value === 'services') return filteredServices.value
  return filteredEvents.value
})

// 整机级错误（未装 kubectl / kubeconfig 读不到 / 连不上 API server…）只在页顶说一次：
// 这时五个分段报的是同一句话，逐条列出来只是噪声。
const LIST_ERRS = computed<(K8sError | undefined)[]>(() => [
  nodeList.error.value, workloadList.error.value, podList.error.value,
  serviceList.error.value, eventList.error.value,
])
const hostError = computed<K8sError | undefined>(() => LIST_ERRS.value.find(e => isHostLevelError(e?.code)))
/** 这台主机压根没有 k8s（没装 / 没配 / 说不出 context）：页顶报状态，不报故障 */
const notDetected = computed(() => isNotDetectedState(hostError.value, contextName.value))
/** 页顶那两行由 k8s-core 定案（「回包是网页」要看 context 才说得清） */
const hostCopy = computed(() => hostErrorCopy(hostError.value, contextName.value))
/** 未检测到时补一句现场（哪个账号、HOME 在哪、KUBECONFIG 指着哪） */
const probeNote = computed(() => (notDetected.value ? envProbeNote(envProbe.value) : ''))
/** 当前这一屏自己读没读到：读失败时状态栏不许报 0，那会被看成「这里真的没有资源」 */
const ACTIVE_LISTS = {
  nodes: nodeList, workloads: workloadList, pods: podList, services: serviceList, events: eventList,
} as const
const activeListFailed = computed(() => !!ACTIVE_LISTS[tab.value].error.value)
const tabLabel = computed(() => TABS.find(t => t.key === tab.value)?.label || '资源')
const activeLabel = computed(() => {
  if (tab.value === 'nodes') return 'kubectl get nodes'
  if (tab.value === 'workloads') return 'kubectl get deployments,statefulsets,…'
  if (tab.value === 'pods') return 'kubectl get pods'
  if (tab.value === 'services') return 'kubectl get services,ingresses'
  return 'kubectl get events'
})

// 计数跟随当前这一屏（命名空间 / 搜索收窄后的行）：报了过滤范围却给全量数字，读起来就是假的
const readyNodes = computed(() => filteredNodes.value.filter(n => n.ready).length)
const degradedWorkloads = computed(() => filteredWorkloads.value.filter(w => w.status === 'Degraded' || w.status === 'Failure').length)
const runningPods = computed(() => filteredPods.value.filter(p => !isAbnormalPod(p)).length)
const abnormalPods = computed(() => filteredPods.value.filter(p => isAbnormalPod(p)).length)
const ingressCount = computed(() => filteredServices.value.filter(s => s.kind === 'Ingress').length)
const warningCount = computed(() => filteredEvents.value.filter(e => e.type === 'Warning').length)

/** 状态栏说明取数范围：节点页没有命名空间轴 */
const scopeLabel = computed(() =>
  tab.value !== 'nodes' && nsFilter.value ? `命名空间 ${nsFilter.value}` : '全命名空间')

function emptyText(what: string, useNs = true): string {
  if (q.value) return `没有匹配的${what}`
  if (useNs && nsFilter.value) return `命名空间 ${nsFilter.value} 里没有${what}`
  return `集群里没有${what}`
}

const eventsEmptyText = computed(() => {
  if (showNormalEvents.value) return emptyText('事件')
  if (q.value) return '没有匹配的 Warning 事件'
  return nsFilter.value ? `命名空间 ${nsFilter.value} 里没有 Warning 事件` : '这个集群没有 Warning 事件'
})

async function enrichNodeUsage(id: string, pin: ListPin): Promise<void> {
  if (!nodeList.rows.value.length) return
  try {
    const usage = parseTopNodes(await host.k8sExec(TOP_NODES_CMD, id))
    // 回包必须仍然新鲜：中途切了主机时，旧主机的节点名（k1/k2）完全可能在新集群里同名，
    // 不校验就会把 A 的用量并进 B 的节点行。
    if (!host.listFresh('nodes', pin)) return
    nodeList.rows.value = mergeNodeUsage(nodeList.rows.value, usage)
  } catch { /* metrics-server 缺席：用量列留空，列表照常 */ }
}

async function loadAll(): Promise<void> {
  const id = targetId.value
  const gen = ++loadGen
  if (!id) {
    // 刚断开：后面不会再有哪一轮替这一轮关灯，自己复位
    loading.value = false
    return
  }
  loading.value = true
  truncated.value = false
  envProbe.value = null
  try {
    // context 单独先取：它是本地命令（不碰网络），而页顶那句要靠「说不说得出 context」
    // 决定报「未检测到 k8s」还是「集群出错」。塞进并行里会出现列表先失败、context 后落定
    // 的窗口，同一屏换两次措辞。
    await readK8sContext(host)
    if (gen !== loadGen) return
    await Promise.allSettled([
      workloadList.reload(), podList.reload(), serviceList.reload(), eventList.reload(), nodeList.reload(),
    ])
    // 失败后单独补一次现场：换账号重试、KUBECONFIG 指错、exec PATH 缺 kubectl，
    // 这三种在用户自己的终端里都看不出来。
    if (gen === loadGen && notDetected.value) await probeK8sEnv(host)
  } finally {
    // 只有最新一代关灯：切主机 / 卸载时这一轮提前返回，也不能把 loading 留在 true，
    // 否则页面永远停在「正在经 SSH 会话执行 kubectl …」
    if (gen === loadGen) loading.value = false
  }
}

function refresh(): void { void loadAll() }

function selectTab(next: TabKey): void {
  tab.value = next
  // 分段只切显示；数据在刷新时已全取过一轮，切页不该再发命令（与 Docker 页一致）
}

// ── 详情 ──
// selKey 存下来而不是从 target 反推：负载/服务用 Kind 原名做键，Pod 只用 ns/name，
// 反推一次就要在两处维护同一个字符串形状，错一格表现为「选中的行不再高亮」这种难查的毛病。
function openDetail(t: DetailTarget, key: string): void {
  selected.value = t
  selKey.value = key
}
function closeDetail(): void {
  selected.value = null
  selKey.value = null
}
const openWorkload = (w: WorkloadRow) => openDetail(detailOfWorkload(w), workloadRowKey(w))
const openService = (s: ServiceRow) => openDetail(detailOfService(s), serviceRowKey(s))
function openScale(w: WorkloadRow): void { scaleRow.value = w }

// ── 四个窄动作：全部先确认、全部经 actionExec（60s 预算 + 审计）、全部按 kind/ns 定死目标 ──
async function runAction(key: string, cmd: string, okMsg: string, reloads: (() => Promise<void>)[]): Promise<void> {
  if (busy.value) return
  await host.withBusyKey(key, async () => {
    try {
      await host.actionExec(cmd)
      toast.success(okMsg)
      await Promise.allSettled(reloads.map(fn => fn()))
    } catch (e) {
      host.toastErr(e)
    }
  })
}

async function doRestart(w: WorkloadRow): Promise<void> {
  const ok = await confirm({
    title: '滚动重启',
    message: `${w.kind}／${w.name}（${w.namespace}）\n${ACTION_CONSEQUENCE.rolloutRestart}`,
    danger: true, confirmText: '重启',
  })
  if (!ok) return
  await runAction(
    workloadRowKey(w),
    rolloutRestartCmd(w.kind, w.name, w.namespace),
    `已下发重启：${w.name}`,
    [workloadList.reload, podList.reload],
  )
}

async function doScale(replicas: number): Promise<void> {
  const w = scaleRow.value
  if (!w) return
  scaleRow.value = null
  await runAction(
    `scale:${workloadRowKey(w)}`,
    scaleCmd(w.kind, w.name, w.namespace, replicas),
    `已把 ${w.name} 缩放到 ${replicas}`,
    [workloadList.reload, podList.reload],
  )
}

async function doDeletePod(p: PodRow): Promise<void> {
  const ok = await confirm({
    title: '删除 Pod',
    message: `${p.namespace}／${p.name}\n${ACTION_CONSEQUENCE.deletePod}`,
    danger: true, confirmText: '删除',
  })
  if (!ok) return
  await runAction(
    podRowKey(p),
    deletePodCmd(p.name, p.namespace),
    `已删除 ${p.name}`,
    [podList.reload, workloadList.reload],
  )
}

async function doDeleteJob(w: WorkloadRow): Promise<void> {
  const ok = await confirm({
    title: '删除 Job',
    message: `${w.namespace}／${w.name}\n${ACTION_CONSEQUENCE.deleteJob}`,
    danger: true, confirmText: '删除',
  })
  if (!ok) return
  await runAction(
    workloadRowKey(w),
    deleteJobCmd(w.name, w.namespace),
    `已删除 Job ${w.name}`,
    [workloadList.reload, podList.reload],
  )
}

// 换主机要整轮重取；命名空间选择在新集群里没有意义，先回落到全部
watch(targetId, () => { nsFilter.value = ''; void loadAll() }, { immediate: true })
// 「含 Normal」是取数范围而不是筛选：改了就得重新问服务端
watch(showNormalEvents, () => { void eventList.reload() })
// 页面隐藏：收起弹窗，别让它们盖在别的页面上
watch(() => props.active, (a) => {
  if (a === false) { closeDetail(); scaleRow.value = null; showConnPicker.value = false }
})
</script>

<style scoped>
.k8s-view {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--canvas-bg);
}

/* 连接状态条：与 Docker 页同一尺寸口径（44px / 16px 侧距 / 底部分隔线） */
.page-head {
  display: flex; align-items: center; gap: var(--spacing-12);
  flex-shrink: 0; height: var(--page-head-h); padding: 0 var(--spacing-16);
  border-bottom: 1px solid var(--border-subtle);
}
.conn-left { display: flex; align-items: center; gap: var(--spacing-8); min-width: 0; overflow: hidden; }
.conn-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--text-tertiary); }
.conn-dot.online { background: var(--status-success-text); }
.conn-dot.offline { background: var(--text-tertiary); }
.conn-name { font-size: var(--page-title-size); font-weight: 600; color: var(--text-primary); }
.conn-host { font-size: 0.9231rem; font-weight: 400; color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.head-spacer { flex: 1; }
.conn-right { display: flex; align-items: center; gap: var(--spacing-10); flex-shrink: 0; }
.host-sel {
  height: 26px; padding: 0 var(--spacing-8); max-width: 200px;
  background: var(--bg-input); border: 1px solid var(--border-input); border-radius: var(--radius-md);
  color: var(--text-primary); font-size: 0.9231rem; font-family: inherit; cursor: pointer;
}
.host-sel option { background: var(--bg-elevated); color: var(--text-primary); }
/* context 名可能很长（gke_项目_区域_集群）：让它先截断，别把切主机和断开按钮挤出页面 */
.ctx-chip {
  font-size: 0.7692rem; color: var(--text-tertiary); padding: 1px var(--spacing-6);
  border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-input);
  max-width: 220px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* 工具条 */
.toolbar {
  display: flex; align-items: center; gap: var(--spacing-10);
  padding: var(--spacing-10) var(--spacing-16);
  border-bottom: 1px solid var(--divider-primary); flex-shrink: 0;
}
.seg { display: inline-flex; background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); padding: var(--spacing-2); gap: var(--spacing-2); }
.seg-btn {
  border: none; background: transparent; color: var(--text-secondary); font-size: 0.9231rem;
  padding: var(--spacing-4) var(--spacing-14); border-radius: var(--radius-sm);
  transition: background var(--transition-fast), color var(--transition-fast);
}
.seg-btn:hover { color: var(--text-primary); background: var(--bg-hover); }
.seg-btn.on { background: var(--bg-elevated); color: var(--accent-text); }
/* 分段控件是纯按钮：键盘焦点用内嵌柔光，不画硬描边 */
.seg-btn:focus-visible { outline: none; box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent-primary) 55%, transparent); }
.search {
  display: inline-flex; align-items: center; gap: 7px; height: 26px; padding: 0 var(--spacing-10); width: 210px;
  background: var(--bg-input); border: 1px solid var(--border-input); border-radius: var(--radius-md); color: var(--text-tertiary);
}
.search input { background: transparent; border: none; color: var(--text-primary); font-size: 0.9231rem; flex: 1; }
.search input::placeholder { color: var(--text-tertiary); }
.tb-right { margin-left: auto; display: flex; align-items: center; gap: var(--spacing-8); }
.ck { display: inline-flex; align-items: center; gap: var(--spacing-6); font-size: 0.8462rem; color: var(--text-secondary); cursor: pointer; user-select: none; }

.body { flex: 1; display: flex; min-height: 0; }
.list-col { flex: 1; display: flex; flex-direction: column; min-width: 0; }

/* 取数错误带 */
.err-band {
  display: flex; align-items: flex-start; gap: var(--spacing-10);
  margin: var(--spacing-12) var(--spacing-16); padding: var(--spacing-12) var(--spacing-14); flex-shrink: 0;
  background: color-mix(in srgb, var(--status-error-text) 8%, var(--bg-elevated));
  border: 1px solid color-mix(in srgb, var(--status-error-text) 28%, transparent);
  border-radius: var(--radius-md);
}
.err-band .eb-text { flex: 1; min-width: 0; word-break: break-word; }
/* 「未检测到 k8s」是状态不是故障：一台没装 k8s 的机器不该看着像出了事 */
.err-band.quiet { background: var(--bg-elevated); border-color: var(--divider-primary); }
.err-band .eb-msg { font-size: 1rem; color: var(--text-primary); line-height: 1.5; }
.err-band .eb-hint { font-size: 0.9231rem; color: var(--text-secondary); margin-top: var(--spacing-4); line-height: 1.5; }
/* 现场事实（哪个账号 / HOME / KUBECONFIG）排在建议之后、再弱一档，不和建议抢读 */
.err-band .eb-note { font-size: 0.8462rem; color: var(--text-tertiary); margin-top: var(--spacing-2); line-height: 1.5; }
.load-band {
  display: flex; align-items: center; justify-content: center; gap: var(--spacing-8); flex: 1;
  color: var(--text-tertiary); font-size: 0.9231rem;
}

/* 状态栏 */
.statusbar {
  height: 24px; flex-shrink: 0; display: flex; align-items: center; gap: var(--spacing-16); padding: 0 var(--spacing-16);
  border-top: 1px solid var(--divider-primary); font-size: 0.8462rem; color: var(--text-tertiary); user-select: none;
  overflow: hidden;
}
/* 只有主机名可收缩：计数与时间宁可留白，也不能被长主机名挤出去 */
.statusbar > span { white-space: nowrap; flex-shrink: 0; }
.statusbar .sb-name { flex-shrink: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.statusbar .mono { color: var(--text-secondary); }
.statusbar .ok { color: var(--status-success-text); }
.statusbar .bad { color: var(--status-error-text); }
.statusbar .sb-warn { color: var(--status-warn-text); }
.sb-right { margin-left: auto; }

.empty {
  flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: var(--spacing-10); color: var(--text-tertiary); font-size: 1rem;
}
</style>
