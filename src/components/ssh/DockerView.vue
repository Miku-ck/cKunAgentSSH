<template>
  <div class="docker-view">
    <!-- 连接状态条：仿监控仪表盘。未连接时给“连接远程”引导；已连接则显示目标主机，可切换/断开 -->
    <div class="page-head">
      <div class="conn-left">
        <div class="conn-dot" :class="targetSession ? 'online' : 'offline'"></div>
        <span class="conn-name">Docker</span>
        <span v-if="targetHost" class="conn-host mono" :title="targetHost">{{ targetHost }}</span>
      </div>

      <!-- 多台已连接远程会话：在此切换目标主机 -->
      <select v-if="remoteSessions.length > 1" v-model="targetId" class="host-sel" aria-label="切换目标主机">
        <option v-for="s in remoteSessions" :key="s.id" :value="s.id">{{ s.name }}</option>
      </select>

      <span class="head-spacer"></span>
      <div class="conn-right">
        <button v-if="targetSession" class="btn" @click="disconnectTarget">断开</button>
        <button class="btn btn-primary" @click="showConnPicker = true">连接远程</button>
      </div>
    </div>

    <!-- 无远程会话：Docker 依附远程主机，空态画与顶栏同一枚鲸鱼（连接动作头部一直有） -->
    <div v-if="!remoteSessions.length" class="empty">
      <IconDocker :width="36" :height="36" :stroke-width="1.5" />
      <span>尚未连接远程主机</span>
    </div>

    <!-- 有目标主机：工具条 + 正文 -->
    <template v-else>
      <div class="toolbar">
        <div class="seg" role="tablist" aria-label="Docker 资源">
          <button v-for="t in TABS" :key="t.key" class="seg-btn" role="tab" :aria-selected="tab === t.key" :class="{ on: tab === t.key }" @click="selectTab(t.key)">{{ t.label }}</button>
        </div>
        <div class="search">
          <input v-model="query" placeholder="按名称过滤…" aria-label="过滤" />
        </div>
        <div class="tb-right">
          <template v-if="tab === 'images'">
            <button class="btn" title="从仓库拉取镜像（docker pull）" @click="showPull = true">
              拉取镜像
            </button>
            <button class="btn" title="从远端 tar 加载镜像（docker load -i）" @click="openImport">
              加载镜像
            </button>
            <button
              class="btn btn-danger-ghost"
              :disabled="!!busy || !images.length"
              title="删除悬空镜像（docker image prune -f）：无标签且未被引用的中间层；有标签的镜像不受影响"
              @click="doPruneImages"
            >
              <svg v-if="busy === 'prune:images'" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
              清理悬空
            </button>
            <button
              class="btn btn-danger-ghost"
              :disabled="!!busy || !images.length"
              title="删除所有未被容器引用的镜像（docker image prune -a -f）：带标签镜像也在范围内，删除后需重新 pull"
              @click="doPruneAllImages"
            >
              <svg v-if="busy === 'prune:images-all'" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
              清理未使用
            </button>
          </template>
          <button
            v-if="tab === 'volumes'"
            class="btn btn-danger-ghost"
            :disabled="!!busy || !volumes.length"
            title="删除未被容器引用的卷（docker volume prune -f）；Docker 23+ 默认只清匿名卷，具名卷需逐条删除"
            @click="doPruneVolumes"
          >
            <svg v-if="busy === 'prune:volumes'" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
            清理未使用
          </button>
          <button
            v-else-if="tab === 'networks'"
            class="btn btn-danger-ghost"
            :disabled="!!busy || !networks.length"
            title="删除所有未被容器使用的自定义网络（docker network prune -f）"
            @click="doPruneNetworks"
          >
            <svg v-if="busy === 'prune:networks'" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
            清理未使用
          </button>
          <button class="btn" @click="refresh">
            刷新
          </button>
        </div>
      </div>

      <div v-if="loadError" class="err-band" role="alert">
        <div class="eb-text">
          <div class="eb-msg">{{ loadError.message }}</div>
          <div v-if="loadError.hint" class="eb-hint">{{ loadError.hint }}</div>
        </div>
        <button v-if="loadError.retryable" class="btn btn-sm" @click="refresh">重试</button>
      </div>
      <div v-else-if="loading && !containers.length" class="load-band">
        <svg class="dk-spinner" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"/><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"/></svg>
        正在经 SSH 会话执行 docker ps …
      </div>
      <div v-else class="body">
        <!-- 左列：按页签换成对应列表（四个子组件只画不跑命令） -->
        <div class="list-col">
          <DockerContainersTab
            v-if="tab === 'containers'"
            :rows="filteredContainers"
            :selected="selected"
            :empty-text="containers.length ? '没有匹配的容器' : '该主机没有容器'"
            @select="selected = $event"
            @start="doStart"
            @stop="doStop"
            @restart="doRestart"
            @remove="doRemove"
            @exec="doExec"
          />
          <DockerImagesTab
            v-else-if="tab === 'images'"
            :rows="filteredImages"
            :error="imageList.error.value"
            :empty-text="images.length ? '没有匹配的镜像' : '该主机没有镜像'"
            :usage-known="unusedCountKnown"
            :containers="containers"
            @export="openExport"
            @remove="doRemoveImage"
          />
          <DockerVolumesTab
            v-else-if="tab === 'volumes'"
            :rows="filteredVolumes"
            :error="volumeList.error.value"
            :selected-name="selectedVolume?.name ?? null"
            :empty-text="volumes.length ? '没有匹配的卷' : '该主机没有卷'"
            @open="selectedVolume = $event"
            @remove="doRemoveVolume"
          />
          <DockerNetworksTab
            v-else
            :rows="filteredNetworks"
            :error="networkList.error.value"
            :selected-id="selectedNetwork?.id ?? null"
            :empty-text="networks.length ? '没有匹配的网络' : '该主机没有网络'"
            @open="selectedNetwork = $event"
            @remove="doRemoveNetwork"
          />
        </div>

        <!-- 容器详情已改为居中弹窗（见下方 Teleport），列表列独享整幅宽度 -->
      </div>

      <!-- 状态栏：计数必须跟着当前 tab 走。此前这里不分页永远报容器，切到镜像/卷/网络后
           数字对、对象错，比空着更容易误信。 -->
      <div class="statusbar">
        <span>{{ targetName }}</span>
        <span>远程 · SSH</span>
        <span v-if="tab === 'containers'">容器 <span class="mono">{{ containers.length }}</span>（<span class="ok">{{ runningCount }}</span> 运行 / <span class="bad">{{ abnormalCount }}</span> 异常 / <span class="mono">{{ stoppedCount }}</span> 停止）</span>
        <span v-else-if="tab === 'images'">镜像 <span class="mono">{{ images.length }}</span><template v-if="unusedCountKnown && unusedImageCount">（<span class="warn">{{ unusedImageCount }}</span> 未使用<template v-if="danglingCount">，其中 <span class="warn">{{ danglingCount }}</span> 悬空</template>）</template><template v-else-if="danglingCount">（<span class="warn">{{ danglingCount }}</span> 悬空）</template></span>
        <span v-else-if="tab === 'volumes'">卷 <span class="mono">{{ volumes.length }}</span><template v-if="unusedVolumeCount">（<span class="warn">{{ unusedVolumeCount }}</span> 未使用）</template></span>
        <span v-else>网络 <span class="mono">{{ networks.length }}</span><template v-if="builtinNetworkCount">（<span class="mono">{{ builtinNetworkCount }}</span> 内置）</template></span>
        <span v-if="truncated" class="sb-warn" title="单次 SSH 命令输出达到 1MB 上限，列表可能不完整">输出已截断</span>
        <span class="sb-right mono">{{ lastSync }}</span>
      </div>
    </template>

    <!-- 连接选择模态：列出所有已保存 SSH 连接，点选即建会话并设为目标（Teleport 到 body，Esc 关闭） -->
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

    <!-- 容器详情：inspect / stats / logs 三条取数腿在子组件内自持（含轮询与陈旧回包守卫） -->
    <DockerContainerDetail
      v-if="tab === 'containers' && selected"
      :container="selected"
      :session-id="targetId"
      @close="selected = null"
    />

    <!-- 镜像拉取：引用先校验；docker pull 的层下载远超读命令，走 600s 预算 -->
    <Teleport to="body">
      <DockerPullDialog v-if="showPull" @close="showPull = false" @pulled="onPulled" />
    </Teleport>

    <!-- 镜像导出/加载（纯远端）：tar 只落在远端主机，两端都不经过本地 -->
    <Teleport to="body">
      <DockerImageTransferModal
        v-if="transferMode"
        :mode="transferMode"
        :session-id="targetId"
        :image="transferImage"
        :running="transferRunning"
        :error="transferError"
        :elapsed="transferElapsed"
        :initial-dir="lastTransferDir"
        @close="closeTransfer"
        @submit="onTransferSubmit"
      />
    </Teleport>

    <!-- 卷/网络详情弹窗：inspect 自持；删除动作回抛给父级统一走确认 + 审计 -->
    <Teleport to="body">
      <DockerVolumeDetailDialog
        v-if="tab === 'volumes' && selectedVolume"
        :key="targetId + ':' + selectedVolume.name"
        :volume="selectedVolume"
        @close="selectedVolume = null"
        @remove="doRemoveVolume"
      />
    </Teleport>
    <Teleport to="body">
      <DockerNetworkDetailDialog
        v-if="tab === 'networks' && selectedNetwork"
        :key="targetId + ':' + selectedNetwork.id"
        :network="selectedNetwork"
        @close="selectedNetwork = null"
        @remove="doRemoveNetwork"
      />
    </Teleport>
  </div>
</template>

<script setup lang="ts">
// DockerView.vue — 远程 Docker 管理页
// 定位：依附已连接的 SSH 会话，经 sshExec 在其远程主机上真执行 docker 命令。
// 分工：命令构造/解析/错误归类 → docker-core.ts；执行/审计/动作锁/陈旧回包守卫 → ./docker/context.ts；
//       四个页签的列表与全部弹窗 → ./docker/*；本文件只做 sshExec 编排与页面级状态。
import { computed, ref, watch, onUnmounted } from 'vue'
import { formatBytes } from '@/utils/format'
import { sshSessionIo, writeAfterShellReady } from '@/utils/ssh-handshake'
import DockerContainerDetail from './docker/DockerContainerDetail.vue'
import DockerImageTransferModal from './docker/DockerImageTransferModal.vue'
import IconDocker from '@/components/icons/IconDocker.vue'
import { useSSHStore } from '@/stores/ssh'
import { useToastStore } from '@/stores/toast'
import { useConfirm } from '@/composables/useConfirm'
import {
  AUDIT_ACTION_DOCKER,
  auditOutcomeForDispatchError,
  auditResultForExit,
} from '@/utils/remote-audit'
import {
  PS_CMD, IMAGES_CMD, VOLUMES_CMD, VOLUME_USAGE_CMD, NETWORKS_CMD,
  startCmd, stopCmd, restartCmd, rmCmd, rmiCmd, execCmd, imagePruneCmd, imagePruneAllCmd,
  dockerSaveCmd, dockerLoadCmd, saveTempPath,
  volumeRmCmd, networkRmCmd, volumePruneCmd, networkPruneCmd,
  parsePs, parseImages, parseVolumes, parseNetworks, imageLabel, imageHaystack, imageUsageCount,
  parseVolumeUsage, mergeVolumeUsage, isOutputTruncated,
  countPrunedLines, isAbnormal, isBuiltinNetwork,
  describeThrownDockerError,
  type ContainerRow, type ImageRow, type VolumeRow, type NetworkRow, type DockerError,
} from '@/stores/docker-core'
// exec 三态判据与主进程 ssh-manager 同源（判据源 shared/exec-outcome-core.ts）
import { classifyExit } from '../../../shared/exec-outcome-core'
import { provideDockerHost, TABS, ACTION_TIMEOUT_SEC, errText, type TabKey, type ListPin } from './docker/context'
import DockerContainersTab from './docker/DockerContainersTab.vue'
import DockerImagesTab from './docker/DockerImagesTab.vue'
import DockerVolumesTab from './docker/DockerVolumesTab.vue'
import DockerNetworksTab from './docker/DockerNetworksTab.vue'
import DockerVolumeDetailDialog from './docker/DockerVolumeDetailDialog.vue'
import DockerNetworkDetailDialog from './docker/DockerNetworkDetailDialog.vue'
import DockerPullDialog from './docker/DockerPullDialog.vue'
import DockerConnectionPicker from './docker/DockerConnectionPicker.vue'

const props = defineProps<{ active?: boolean }>()
const sshStore = useSSHStore()
const toast = useToastStore()
const { confirm } = useConfirm()

// 目标主机、动作锁、审计、陈旧回包守卫都收在这一份底座里，页签与弹窗共用同一实例
const host = provideDockerHost()
const {
  remoteSessions, targetId, targetName, targetHost, targetSession,
  showConnPicker, connecting, pendingConnId, handleConnect, disconnectTarget,
  busy, withBusyKey, withBusy, truncated, psTruncated, tab, lastSync, markSynced,
  dockerExec, actionExec, dockerAuditTarget, writeDockerAudit, toastErr,
  bumpEpoch, pinList, listFresh, createList,
} = host

// exec 进入终端：命令写进目标会话的交互 shell 后，请求父级（SshView）切到该会话终端
const emit = defineEmits<{ 'exec-terminal': [sessionId: string] }>()

const query = ref('')
const q = () => query.value.toLowerCase()

// ── 四类资源：取数同构（pin 宿主与序号 → 取 → 只在仍新鲜时写回），故交给同一个工厂 ──
const containerList = createList<ContainerRow>({
  kind: 'containers',
  cmd: PS_CMD,
  // docker ps 单独记账：镜像「引用」列的准确性只取决于容器列表是否完整，
  // 不能因为 docker images 被截断就把所有镜像的引用状态都降级成「—」。
  parse: (raw) => { psTruncated.value = isOutputTruncated(raw); return parsePs(raw) },
  label: 'docker ps',
})
const imageList = createList<ImageRow>({
  kind: 'images', cmd: IMAGES_CMD, parse: parseImages, label: 'docker images',
})
const volumeList = createList<VolumeRow>({
  kind: 'volumes', cmd: VOLUMES_CMD, parse: parseVolumes, label: 'docker volume ls',
  // 用量来自 docker system df -v，可能比列表慢一个量级：不挡首屏，单独回填
  after: (id, pin) => { void enrichVolumeUsage(id, pin) },
})
const networkList = createList<NetworkRow>({
  kind: 'networks', cmd: NETWORKS_CMD, parse: parseNetworks, label: 'docker network ls',
})
const containers = containerList.rows
const images = imageList.rows
const volumes = volumeList.rows
const networks = networkList.rows

const selected = ref<ContainerRow | null>(null)
// 卷/网络详情弹窗：互斥开关（同一时间只会开一个），inspect 由弹窗自持
const selectedVolume = ref<VolumeRow | null>(null)
const selectedNetwork = ref<NetworkRow | null>(null)
// 拉取弹窗的开合由页面管（按钮在工具条上），表单状态留在弹窗里
const showPull = ref(false)

const loading = ref(false)
const loadError = ref<DockerError | null>(null)

/** 异常优先排序：exited / health=unhealthy → running → stopped，出事容器一眼可见 */
const filteredContainers = computed(() => {
  const list = containers.value.filter(c => c.name.toLowerCase().includes(q()))
  const rank = (c: ContainerRow): number => (isAbnormal(c) ? 0 : c.state === 'running' ? 1 : 2)
  return [...list].sort((a, b) => rank(a) - rank(b))
})
// 过滤串与行内展示同一个来源：悬空/按摘要拉取的行显示的是短 ID，原先只搜 repo:tag，
// 搜屏幕上真正写着的那串 ID 反而一条都不命中。
const filteredImages = computed(() =>
  images.value.filter(im => imageHaystack(im).includes(q())))
const filteredVolumes = computed(() =>
  volumes.value.filter(v => v.name.toLowerCase().includes(q())))
const filteredNetworks = computed(() =>
  networks.value.filter(n => n.name.toLowerCase().includes(q())))
const runningCount = computed(() =>
  containers.value.filter(c => c.state === 'running' && !isAbnormal(c)).length)
const stoppedCount = computed(() => containers.value.filter(c => c.state === 'stopped').length)
const abnormalCount = computed(() => containers.value.filter(isAbnormal).length)
// 状态栏副标题用的三组细分计数：都是「清理」按钮的作用面，有值才显示（缺数时不报假的 0）。
// 悬空镜像 = 无标签层（列表里显示的是短 ID），正是 image prune 的目标集合。
const danglingCount = computed(() =>
  images.value.filter(im => im.repo === '<none>' || im.tag === '<none>').length)
// 「未使用」= 没有任何容器（含已停止）引用。docker ps 被 1MB 截断时列表不完整，
// 此时一律不给计数/状态（显示「—」），宁可少报也不要把在用的镜像说成可删。
const unusedCountKnown = computed(() => !psTruncated.value)
const unusedImageCount = computed(() =>
  unusedCountKnown.value
    ? images.value.filter(im => imageUsageCount(im, containers.value) === 0).length
    : 0)
// 未使用卷 = 引用容器数为 0；老版本 docker 不给 Links 时 linkCount 缺失，不凭空断言
const unusedVolumeCount = computed(() => volumes.value.filter(v => v.linkCount === 0).length)
// 内置网络（bridge/host/none）删不掉，计数在状态栏里解释那几枚置灰的删除按钮
const builtinNetworkCount = computed(() => networks.value.filter(n => isBuiltinNetwork(n.name)).length)

// 跨宿主竞态守卫只在 loadAll 用到代次：pinList/listFresh 已覆盖「切宿主即作废」
let loadGen = 0

/**
 * 卷用量富集：docker volume ls 的 JSON 没有 UsageData（那是 GET /system/df 专有字段），
 * 只能再跑一条 docker system df -v。失败/超时保持「—」，不影响卷管理主流程；
 * 回包仍须通过列表序号校验——等待期间用户可能已切宿主或重拉，同名卷会跨宿主撞名。
 */
async function enrichVolumeUsage(id: string, pin: ListPin): Promise<void> {
  try {
    const usage = parseVolumeUsage(await dockerExec(VOLUME_USAGE_CMD, id, ACTION_TIMEOUT_SEC))
    if (!listFresh('volumes', pin)) return
    volumes.value = mergeVolumeUsage(volumes.value, usage)
    const cur = selectedVolume.value
    if (cur) {
      const merged = volumes.value.find(v => v.name === cur.name)
      if (merged) selectedVolume.value = merged
    }
  } catch { /* 增强项：保持列显示「—」 */ }
}

async function loadAll(): Promise<void> {
  const id = targetId.value
  if (!id) return
  const gen = ++loadGen
  const pins = {
    containers: pinList('containers', id),
    images: pinList('images', id),
    volumes: pinList('volumes', id),
    networks: pinList('networks', id),
  }
  loading.value = true
  loadError.value = null
  imageList.error.value = undefined
  volumeList.error.value = undefined
  networkList.error.value = undefined
  truncated.value = false
  psTruncated.value = false
  try {
    // 容器为主视图；其失败即整体错误（未装 / 无权限都在此暴露）
    const psRaw = await dockerExec(PS_CMD, id)
    const ps = parsePs(psRaw)
    if (gen !== loadGen || !listFresh('containers', pins.containers)) return
    containers.value = ps
    psTruncated.value = isOutputTruncated(psRaw)
    // 其余尽力而为：单个子命令失败不阻断整页，但要按 tab 如实上浮，不伪装成「没有资源」
    const [im, vo, ne] = await Promise.allSettled([
      dockerExec(IMAGES_CMD, id), dockerExec(VOLUMES_CMD, id), dockerExec(NETWORKS_CMD, id),
    ])
    if (gen !== loadGen) return
    if (listFresh('images', pins.images)) {
      images.value = im.status === 'fulfilled' ? parseImages(im.value) : []
      imageList.error.value = im.status === 'rejected' ? errText(im.reason) : undefined
    }
    if (listFresh('volumes', pins.volumes)) {
      volumes.value = vo.status === 'fulfilled' ? parseVolumes(vo.value) : []
      volumeList.error.value = vo.status === 'rejected' ? errText(vo.reason) : undefined
      // 用量来自 docker system df -v，可能比列表慢一个量级：不挡首屏，单独回填
      if (vo.status === 'fulfilled') void enrichVolumeUsage(id, pins.volumes)
    }
    if (listFresh('networks', pins.networks)) {
      networks.value = ne.status === 'fulfilled' ? parseNetworks(ne.value) : []
      networkList.error.value = ne.status === 'rejected' ? errText(ne.reason) : undefined
    }
    markSynced(TABS.map(t => t.key), '已同步')
  } catch (e) {
    if (gen !== loadGen || !listFresh('containers', pins.containers)) return
    loadError.value = e as DockerError
    containers.value = []; images.value = []; volumes.value = []; networks.value = []
    imageList.error.value = undefined
    volumeList.error.value = undefined
    networkList.error.value = undefined
    selected.value = null
  } finally {
    if (gen === loadGen) loading.value = false
  }
}

// 详情已改弹窗：不再自动选中首个容器（否则进页面/刷新就弹模态）；仅当选中项已不在列表才清空。
// 一轮刷新只可能从两处改动容器列表（loadAll / reloadContainers），用监听表达一次即可。
watch(containers, (list) => {
  const cur = selected.value
  if (cur && !list.some(c => c.id === cur.id)) selected.value = null
})

// 页面被 v-show 隐藏时组件仍挂载：Teleport 到 body 的弹窗会继续悬浮在别的页面上。
// 隐藏即收起全部弹窗；拉取弹窗不阻塞关闭，在途 pull 由自身 finally 收尾。
watch(() => props.active, (active) => {
  if (active !== false) return
  // 执行中的导出/加载不强收：关面板不等于停掉远端进程；其余情况跟随页面一起收起。
  if (!transferRunning.value) transferMode.value = ''
  showConnPicker.value = false
  showPull.value = false
  selected.value = null
  selectedVolume.value = null
  selectedNetwork.value = null
})

function refresh(): void { void loadAll() }

// 切离当前 tab 时清空对应选中：避免残留选中项在切回时重新弹出上次的详情大窗
function selectTab(key: TabKey): void {
  tab.value = key
  if (key !== 'containers') selected.value = null
  if (key !== 'volumes') selectedVolume.value = null
  if (key !== 'networks') selectedNetwork.value = null
}

// 容器动作只重跑 docker ps（镜像/卷/网络不受容器启停影响），避免每次动作全量刷新 4 条命令。
function doStart(c: ContainerRow) { return withBusy(c, async () => {
  try { await actionExec(startCmd(c.name)); toast.success(`已启动 ${c.name}`); await containerList.reload() } catch (e) { toastErr(e) }
}) }
function doStop(c: ContainerRow) { return withBusy(c, async () => {
  try { await actionExec(stopCmd(c.name)); toast.info(`已停止 ${c.name}`); await containerList.reload() } catch (e) { toastErr(e) }
}) }
function doRestart(c: ContainerRow) { return withBusy(c, async () => {
  try { await actionExec(restartCmd(c.name)); toast.success(`已重启 ${c.name}`); await containerList.reload() } catch (e) { toastErr(e) }
}) }

async function doRemove(c: ContainerRow) {
  const ok = await confirm({
    title: '删除容器',
    message: `容器 ${c.name} 的可写层数据将永久丢失（挂载卷保留）。`,
    confirmText: '确认删除',
    danger: true,
  })
  if (!ok) return
  await withBusy(c, async () => {
    try { await actionExec(rmCmd(c.name)); toast.success(`已删除 ${c.name}`); await containerList.reload() } catch (e) { toastErr(e) }
  })
}

async function doRemoveImage(im: ImageRow) {
  // 按「这一行的引用」删，不按镜像 ID 删：docker rmi <ID> 会把该 ID 上的**所有标签**
  // 一起摘掉，而弹窗只承诺了当前这一行 —— 同一镜像多标签时用户会以为只删了一个，
  // 实际把另一个也卸了。多标签行各自 untag，只有最后一个标签才带走层数据。
  const ref = imageLabel(im)
  const ok = await confirm({
    title: '删除镜像',
    message: `将取消标签 ${ref}（${im.id}）；若它是该镜像上的最后一个标签，层数据一并删除。有容器（含已停止容器）引用该镜像时，删除最后一个标签会失败。`,
    confirmText: '确认删除',
    danger: true,
  })
  if (!ok) return
  await withBusyKey(ref, async () => {
    try { await actionExec(rmiCmd(ref)); toast.success(`已删除镜像 ${ref}`); await imageList.reload() } catch (e) { toastErr(e) }
  })
}

// ── 镜像导出 / 加载（纯远端）──
// tar 只落在远端主机：docker save 的 stdout 是二进制流，走不了 sshExec 的文本通道，
// 所以必须 -o 落盘；加载侧对称地 -i 读盘。两端都不经过本地，没有传输链路。
const TRANSFER_TIMEOUT_SEC = 600

type TransferMode = 'export' | 'import'
const transferMode = ref<TransferMode | ''>('')
const transferImage = ref<ImageRow | null>(null)
const transferRunning = ref(false)
const transferError = ref('')
const transferElapsed = ref(0)
/** 上次落盘目录：重开弹窗直接停在那儿，不用每次从 / 重新找 */
const lastTransferDir = ref('')
let transferClock: ReturnType<typeof setInterval> | null = null

function startTransferClock(): void {
  stopTransferClock()
  transferElapsed.value = 0
  transferClock = setInterval(() => { transferElapsed.value += 1 }, 1000)
}
function stopTransferClock(): void {
  if (transferClock) { clearInterval(transferClock); transferClock = null }
}

/**
 * save/load 专用执行：不复用 dockerExec 的 dockerErrorFromResult 归类。
 * 后者把「有输出但 exit-status 缺失」（exitCode === null）判成 DOCKER_UNKNOWN 并抛出，
 * 而 docker save 写完文件却没回 exit-status 是真会发生的：此时文件可能是完整的，
 * 报失败会让人删掉一份好备份重导一遍。这里原样回包，由调用方按 classifyExit 三态
 * 结合产物大小决定说法；审计照写。
 */
async function transferExec(cmd: string): Promise<{ stdout: string; stderr: string; exitCode: number | null }> {
  const api = window.electronAPI
  const id = targetId.value
  if (!id || !api) throw { code: 'NO_SESSION', message: '未选择目标主机', retryable: false } as DockerError
  const ctx = { action: AUDIT_ACTION_DOCKER, ...dockerAuditTarget(id) }
  const started = Date.now()
  try {
    const res = await api.sshExec(id, cmd, TRANSFER_TIMEOUT_SEC)
    writeDockerAudit(ctx, cmd, 'executed', auditResultForExit(res.exitCode, Date.now() - started))
    return res
  } catch (err) {
    const dErr = describeThrownDockerError(err)
    const outcome = auditOutcomeForDispatchError(err, dErr.message, TRANSFER_TIMEOUT_SEC)
    writeDockerAudit(ctx, cmd, outcome.conclusion, outcome.result)
    throw dErr
  }
}

/** 产物大小，仅用于文案；读不到（SFTP 不可用 / 无权限）返回 0 */
async function statSize(id: string, path: string): Promise<number> {
  const api = window.electronAPI
  if (!api || !id) return 0
  try {
    await api.sftpOpen(id)
    return (await api.sftpStat(id, path)).size || 0
  } catch {
    return 0
  }
}

/** 首行非空输出：docker 的报错主体几乎都在第一行，后面是建议动作 */
function firstLine(s: string): string {
  return (s || '').split('\n').map(l => l.trim()).find(Boolean) || ''
}

/** 失败原因：docker 的原话优先；没有就用退出码，unknown 不能写成「退出码 null」 */
function execWhy(res: { stdout: string; stderr: string; exitCode: number | null }): string {
  const detail = firstLine(res.stderr) || firstLine(res.stdout)
  if (detail) return detail
  return classifyExit(res.exitCode) === 'unknown' ? '命令未回退出码' : `退出码 ${res.exitCode}`
}

function transferFail(head: string, why: string, path: string, tmp = ''): DockerError {
  // 超时只关 channel：远端 docker 大概率已死，但写了一半的 tar 会留下，
  // 不说清楚用户会以为目录是干净的。gz 还多一个临时文件要指。
  const where = tmp ? `${path}；临时文件：${tmp}` : path
  return { code: 'DOCKER_EXEC_FAILED', message: `${head}：${why}`, hint: `可能残留半截文件：${where}`, retryable: true }
}

function dirOf(path: string): string {
  const i = path.lastIndexOf('/')
  return i <= 0 ? '/' : path.slice(0, i)
}

function openExport(im: ImageRow): void {
  if (transferRunning.value) return
  transferImage.value = im
  transferError.value = ''
  transferMode.value = 'export'
}

function openImport(): void {
  if (transferRunning.value) return
  transferImage.value = null
  transferError.value = ''
  transferMode.value = 'import'
}

function closeTransfer(): void {
  if (transferRunning.value) return
  transferMode.value = ''
}

async function onTransferSubmit(
  p: { mode: 'export'; ref: string; path: string; gz: boolean } | { mode: 'import'; path: string },
): Promise<void> {
  if (transferRunning.value) return
  const id = targetId.value
  transferRunning.value = true
  transferError.value = ''
  startTransferClock()
  try {
    if (p.mode === 'export') {
      const res = await transferExec(dockerSaveCmd(p.ref, p.path, p.gz))
      const outcome = classifyExit(res.exitCode)
      const tmp = p.gz ? saveTempPath(p.path) : ''
      if (outcome === 'failed') throw transferFail('打包失败', execWhy(res), p.path, tmp)
      // gz 由 `&&` 串在 save 之后、脚本以 $? 退出：exit 0 意味着 save 与 gzip 都成功，
      // 产物必定完整 —— 不再用大小猜（scratch 镜像压完可能只有几百字节）。
      // 大小只进文案：读不到不该把一次成功导出报成失败。
      const size = await statSize(id, p.path)
      if (outcome === 'unknown') {
        toast.warning(size
          ? `命令未回退出码；已生成 ${formatBytes(size)}，建议用加载验证完整性`
          : `命令未回退出码，无法确认 ${p.path} 是否完整，建议用加载验证`)
      } else {
        toast.success(size ? `已导出 ${formatBytes(size)} 到 ${p.path}` : `已导出到 ${p.path}`)
      }
    } else {
      const res = await transferExec(dockerLoadCmd(p.path))
      const outcome = classifyExit(res.exitCode)
      if (outcome === 'failed') throw transferFail('加载失败', execWhy(res), p.path)
      if (outcome === 'unknown') {
        toast.warning('命令未回退出码：镜像可能已载入，请刷新列表核对')
      } else {
        // docker load 成功会打印 Loaded image: <ref>，比自编文案有用
        toast.success(firstLine(res.stderr) || firstLine(res.stdout) || '镜像已加载')
      }
    }
    lastTransferDir.value = dirOf(p.path)
    transferMode.value = ''
    await imageList.reload()
  } catch (e) {
    // 错误文案单一来源：这里把 hint（含「可能残留」路径）拼进来，弹窗只负责显示，
    // 不自己再猜一次失败原因。
    const d = e as DockerError
    const base = d?.hint ? `${d.message}；${d.hint}` : (d?.message || String(e))
    // 超时/断链是 reject，拿不到 exitCode，也就拿不到 transferFail 的 hint：
    // 残留路径得在这里补，否则最可能留下半截文件的那条路径反而不提。
    const tail = p.mode !== 'export'
      ? ''
      : p.gz
        ? `可能残留半截文件：${p.path}；临时文件：${saveTempPath(p.path)}`
        : `可能残留半截文件：${p.path}`
    transferError.value = tail && !base.includes(p.path) ? `${base}；${tail}` : base
  } finally {
    transferRunning.value = false
    stopTransferClock()
  }
}

/** 拉取成功：发起时的宿主仍是当前目标才重拉，否则会拿 A 的镜像盖到 B 的列表上 */
function onPulled(id: string): void {
  showPull.value = false
  if (targetId.value === id) void imageList.reload()
}

async function doPruneImages(): Promise<void> {
  const ok = await confirm({
    title: '清理悬空镜像',
    message: '将删除所有悬空镜像（无标签、也未被其它镜像引用的中间层）；有标签的镜像不受影响。',
    confirmText: '确认清理',
    danger: true,
  })
  if (!ok) return
  await withBusyKey('prune:images', async () => {
    try {
      const out = await actionExec(imagePruneCmd())
      const reclaimed = parseReclaimed(out)
      const n = countPrunedLines(out)
      toast.success(reclaimed ? `已清理 ${n} 个悬空镜像，回收 ${reclaimed}` : `已清理 ${n} 个悬空镜像`)
      await imageList.reload()
    } catch (e) { toastErr(e) }
  })
}

async function doPruneAllImages(): Promise<void> {
  // 作用面以 Docker 判定为准；列表计数只用于让用户预先知道大概删多少。
  // docker ps 被截断时容器集合不全，计数会高估，此时不给数字。
  const known = unusedCountKnown.value
  const n = unusedImageCount.value
  const k = danglingCount.value
  const scope = known
    ? `当前 ${n} 个未使用镜像${k ? `（其中 ${k} 个悬空）` : ''}。`
    : '容器列表不完整，实际删除数由 Docker 判定。'
  const ok = await confirm({
    title: '清理未使用镜像',
    message: `将删除所有未被任何容器（包括已停止容器）引用的镜像；带标签的镜像也在范围内，删除后需重新 pull，不可恢复。${scope}`,
    confirmText: '确认清理',
    danger: true,
  })
  if (!ok) return
  await withBusyKey('prune:images-all', async () => {
    try {
      const out = await actionExec(imagePruneAllCmd())
      const reclaimed = parseReclaimed(out)
      const pruned = countPrunedLines(out)
      toast.success(reclaimed ? `已清理 ${pruned} 个未使用镜像，回收 ${reclaimed}` : `已清理 ${pruned} 个未使用镜像`)
      await imageList.reload()
    } catch (e) { toastErr(e) }
  })
}

async function doRemoveVolume(v: VolumeRow) {
  const ok = await confirm({
    title: '删除卷',
    message: `卷 ${v.name} 中的数据将永久丢失，不可恢复。若仍被容器引用（含已停止容器），删除会失败。`,
    confirmText: '确认删除',
    danger: true,
  })
  if (!ok) return
  await withBusyKey('v:' + v.name, async () => {
    try {
      await actionExec(volumeRmCmd(v.name))
      toast.success(`已删除卷 ${v.name}`)
      if (selectedVolume.value?.name === v.name) selectedVolume.value = null
      await volumeList.reload()
    } catch (e) { toastErr(e) }
  })
}
async function doRemoveNetwork(n: NetworkRow) {
  const ok = await confirm({
    title: '删除网络',
    message: `网络 ${n.name}（${n.driver}）将被移除；若仍有容器连接则删除失败。默认 bridge/host/none 不可删除。`,
    confirmText: '确认删除',
    danger: true,
  })
  if (!ok) return
  await withBusyKey('n:' + n.name, async () => {
    try {
      await actionExec(networkRmCmd(n.name))
      toast.success(`已删除网络 ${n.name}`)
      if (selectedNetwork.value?.name === n.name) selectedNetwork.value = null
      await networkList.reload()
    } catch (e) { toastErr(e) }
  })
}

// prune 行名回收已收敛到 docker-core.countPrunedLines（表头与回收空间行不算个数）
function parseReclaimed(out: string): string | null {
  const m = out.match(/Total reclaimed space:\s*(\S+)/i)
  return m ? m[1] : null
}

async function doPruneVolumes() {
  const ok = await confirm({
    title: '清理未使用的卷',
    // 口径诚实：Docker 23+（API 1.42 起）`volume prune` 默认只清匿名卷，具名卷要显式
    // --all。这里不发 --all（老版本 docker 不识别该 flag，参数解析阶段就整条失败，
    // 与 VOLUMES_CMD 不用 --verbose 同一个理由），所以把实际范围写在确认框里，
    // 而不是让用户以为一键收走了全部孤儿卷。
    message: '将删除未被任何容器（包括已停止容器）引用的卷，数据不可恢复。注意：Docker 23+ 默认只清匿名卷、保留具名卷（实际范围以本机版本为准），具名卷请用行内「删除卷」逐个处理。',
    confirmText: '确认清理',
    danger: true,
  })
  if (!ok) return
  await withBusyKey('prune:volumes', async () => {
    try {
      const out = await actionExec(volumePruneCmd())
      const reclaimed = parseReclaimed(out)
      const n = countPrunedLines(out)
      toast.success(reclaimed ? `已清理 ${n} 个未使用卷，回收 ${reclaimed}` : `已清理 ${n} 个未使用卷`)
      selectedVolume.value = null
      await volumeList.reload()
    } catch (e) { toastErr(e) }
  })
}
async function doPruneNetworks() {
  const ok = await confirm({
    title: '清理未使用的网络',
    message: '将删除所有未被容器使用的自定义网络（不影响默认 bridge/host/none）。已停止但仍在的容器所接网络不会被删。',
    confirmText: '确认清理',
    danger: true,
  })
  if (!ok) return
  await withBusyKey('prune:networks', async () => {
    try {
      const out = await actionExec(networkPruneCmd())
      const n = countPrunedLines(out)
      toast.success(`已清理 ${n} 个未使用网络`)
      selectedNetwork.value = null
      await networkList.reload()
    } catch (e) { toastErr(e) }
  })
}

// exec 进终端：命令写进目标会话的交互 shell（自带 PTY，docker exec -it 可直接附着），再请求切到该终端
// 走 withBusy(整行)：建会话 + 等 shell 就绪 + 写命令共三跳、约 0.5s 以上，期间连点会开出
// 第二个终端标签（第一条远端 PTY 也挂在会话列表里没人认领）；同一行的启停/删除一并锁住，
// 免得「exec 附着中」与「docker stop」打到同一个容器。
async function doExec(c: ContainerRow) {
  const s = targetSession.value
  const api = window.electronAPI
  if (!s || !api) { toast.error('未连接目标主机'); return }
  // 先把 connectionId 落成局部量：属性收窄进不了 withBusy 的回调闭包，
  // 直接在回调里用 s.connectionId 会被判成 string | undefined。
  const connId = s.connectionId
  if (!connId) { toast.error('当前会话缺少连接配置，无法新开终端标签'); return }
  await withBusy(c, async () => {
    try {
      // 新开一个终端标签（复用同一连接配置、独立会话），不再把 docker exec 写进用户当前
      // shell —— 那会盖掉他正敲的上下文。connectRemote 会 push 会话并置为 active。
      const newId = await sshStore.connectRemote(connId, 80, 24)
      emit('exec-terminal', newId)
      // GitReposPanel.onTerm 同款确认式握手（utils/ssh-handshake）：先写带一次性
      // 标记的探针，等回显里出现标记（登录 shell 真正执行了探针）再写 docker exec
      // —— 固定延时在慢主机（横幅/profile 超 500ms）上纯靠猜，命令会与横幅交错、
      // 结果静默不可辨。握手内保留 500ms 地板等新标签挂载订阅（组件只收实时流，
      // 早到的输出丢帧）；探针超时回落为固定延时照常写入，并如实提示用户。
      const io = sshSessionIo(newId)
      // api 已在上文校验，判空只为类型收窄
      if (!io) throw new Error('Electron API 未就绪，无法写入终端')
      const { confirmed } = await writeAfterShellReady(io, execCmd(c.name) + '\r')
      if (!confirmed) toast.warning('远程主机登录较慢，命令已按固定延时写入；若终端未见执行请重试')
    } catch (err) {
      toast.error(describeThrownDockerError(err).message)
    }
  })
}

// 目标主机变化：清空重取（不同主机容器集合不同）。epoch 先递增，作废所有在途回包。
watch(targetId, () => {
  bumpEpoch()
  showPull.value = false
  selected.value = null
  selectedVolume.value = null
  selectedNetwork.value = null
  void loadAll()
}, { immediate: true })
// 任一详情弹窗打开时挂 Esc；三个选中均为 null 时卸监。容器详情的日志/轮询在子组件内自理。
function onDetailKey(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  if (selected.value) selected.value = null
  else if (selectedVolume.value) selectedVolume.value = null
  else if (selectedNetwork.value) selectedNetwork.value = null
}
watch([selected, selectedVolume, selectedNetwork], ([c, v, n]) => {
  if (c || v || n) document.addEventListener('keydown', onDetailKey)
  else document.removeEventListener('keydown', onDetailKey)
})

onUnmounted(() => {
  stopTransferClock()
  document.removeEventListener('keydown', onDetailKey)
})
</script>

<style scoped>
.docker-view {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  /* 整页结构画布：走 --canvas-bg（默认=--bg-base；详见 DashboardView 同注）。 */
  background: var(--canvas-bg);
}

/* 连接状态条：仿仪表盘，44px / 16px 侧距 / 底部分隔线 */
.page-head {
  display: flex;
  align-items: center;
  gap: var(--spacing-12);
  flex-shrink: 0;
  height: var(--page-head-h);
  padding: 0 var(--spacing-16);
  border-bottom: 1px solid var(--border-subtle);
}
.conn-left { display: flex; align-items: center; gap: var(--spacing-8); min-width: 0; }
.conn-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--text-tertiary); }
.conn-dot.online { background: var(--status-success-text); }
.conn-dot.offline { background: var(--text-tertiary); }
.conn-name { font-size: var(--page-title-size); font-weight: 600; color: var(--text-primary); }
.conn-host { font-size: 0.9231rem; font-weight: 400; color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.head-spacer { flex: 1; }
.conn-right { display: flex; align-items: center; gap: var(--spacing-10); flex-shrink: 0; }
.host-sel {
  height: 26px; padding: 0 var(--spacing-8); max-width: 200px;
  background: var(--bg-input); border: 1px solid var(--border-input); border-radius: var(--radius-md);
  color: var(--text-primary); font-size: 0.9231rem; font-family: inherit; cursor: pointer;
}
.host-sel option { background: var(--bg-elevated); color: var(--text-primary); }

/* 工具条 */
.toolbar {
  display: flex;
  align-items: center;
  gap: var(--spacing-10);
  padding: var(--spacing-10) var(--spacing-16);
  border-bottom: 1px solid var(--divider-primary);
  flex-shrink: 0;
}
.seg { display: inline-flex; background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); padding: var(--spacing-2); gap: var(--spacing-2); }
.seg-btn {
  border: none; background: transparent; color: var(--text-secondary); font-size: 0.9231rem;
  padding: var(--spacing-4) var(--spacing-14); border-radius: var(--radius-sm);
  transition: background var(--transition-fast), color var(--transition-fast);
}
.seg-btn:hover { color: var(--text-primary); background: var(--bg-hover); }
.seg-btn.on { background: var(--bg-elevated); color: var(--accent-text); }
.search {
  display: inline-flex; align-items: center; gap: 7px; height: 26px; padding: 0 var(--spacing-10); width: 210px;
  background: var(--bg-input); border: 1px solid var(--border-input); border-radius: var(--radius-md); color: var(--text-tertiary);
}
.search input { background: transparent; border: none; color: var(--text-primary); font-size: 0.9231rem; flex: 1; }
.search input::placeholder { color: var(--text-tertiary); }
.tb-right { margin-left: auto; display: flex; align-items: center; gap: var(--spacing-8); }

/* 正文：列表列独享整幅宽度（详情已改居中弹窗） */
.body { flex: 1; display: flex; min-height: 0; }
.list-col { flex: 1; display: flex; flex-direction: column; min-width: 0; }

.btn:disabled { opacity: .45; cursor: default; }
/* 危险 ghost：prune 按钮 —— 基线透明 + 危险色描边，hover 不反白而是淡入危险底色 */
.btn-danger-ghost { color: var(--status-error-text); border: 1px solid color-mix(in srgb, var(--status-error-text) 35%, transparent); }
.btn-danger-ghost:hover:not(:disabled) { background: color-mix(in srgb, var(--status-error-text) 10%, transparent); color: var(--status-error-text); }
/* .spin 与 @keyframes dk-rot 统一由 docker-page.css 提供（四个页签共用同一枚转圈） */

/* 取数错误带：docker 未装 / 无权限 / 会话异常，集中一处如实上浮（对齐仪表盘错误态） */
.err-band {
  display: flex; align-items: flex-start; gap: var(--spacing-10);
  margin: var(--spacing-12) var(--spacing-16); padding: var(--spacing-12) var(--spacing-14); flex-shrink: 0;
  background: color-mix(in srgb, var(--status-error-text) 8%, var(--bg-elevated));
  border: 1px solid color-mix(in srgb, var(--status-error-text) 28%, transparent);
  border-radius: var(--radius-md);
}
.err-band .eb-text { flex: 1; min-width: 0; }
.err-band .eb-msg { font-size: 1rem; color: var(--text-primary); line-height: 1.5; }
.err-band .eb-hint { font-size: 0.9231rem; color: var(--text-secondary); margin-top: var(--spacing-4); line-height: 1.5; }
.load-band {
  display: flex; align-items: center; justify-content: center; gap: var(--spacing-8); flex: 1;
  color: var(--text-tertiary); font-size: 0.9231rem;
}

/* 状态栏 */
.statusbar {
  height: 24px; flex-shrink: 0; display: flex; align-items: center; gap: var(--spacing-16); padding: 0 var(--spacing-16);
  border-top: 1px solid var(--divider-primary); font-size: 0.8462rem; color: var(--text-tertiary); user-select: none;
}
.statusbar .mono { color: var(--text-secondary); }
.statusbar .ok { color: var(--status-success-text); }
.statusbar .bad { color: var(--status-error-text); }
.statusbar .sb-warn { color: var(--status-warn-text); }
/* 悬空/未使用计数：可回收的量，用 warn 色与上方 danger-ghost 清理按钮呼应 */
.statusbar .warn { color: var(--status-warn-text); }
.sb-right { margin-left: auto; }

/* 空态 */
.empty {
  flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: var(--spacing-10); color: var(--text-tertiary); font-size: 1rem;
}
</style>
