<template>
  <div class="gh-view">
    <div class="gh-body">
      <!-- ── 加速总控：一键开关 + 巡检红灯（自检与措施状态在下方「加速措施」面板） ── -->
      <section class="panel master">
        <div class="master-row">
          <span class="dot" :class="masterOn ? 'on' : 'off'"></span>
          <span class="master-title">加速{{ masterStateText }}</span>
          <span class="head-spacer"></span>
          <button
            class="btn"
            :class="masterOn ? 'btn-danger' : 'btn-primary'"
            :disabled="masterBusy"
            @click="masterOn ? onMasterDisable() : onMasterEnable()"
          >{{ masterButtonLabel }}</button>
        </div>
        <div v-if="masterNote" class="master-note" :class="{ warn: masterNoteWarn }">
          <span class="master-note-main">{{ masterNote }}</span>
          <span v-for="t in masterTags" :key="t" class="master-tag">{{ t }}</span>
        </div>

        <!-- 巡检红灯：镜像失效与 hosts 失效分开归因，各自给修复入口（绝不自动改配置） -->
        <div v-if="accel.health?.state === 'degraded'" class="health-alert">
          <template v-if="mirrorFailureText">
            <span class="health-alert-text">{{ mirrorFailureText }}</span>
            <button
              v-if="accel.health.fallback"
              class="btn btn-sm btn-primary"
              :disabled="accel.gitBusy"
              @click="switchMirror(accel.health.fallback.prefix)"
            >切换到 {{ accel.health.fallback.name }}</button>
          </template>
          <template v-if="accel.health.hosts && !accel.health.hosts.ok">
            <span class="health-alert-text">hosts 未生效：{{ accel.health.hosts.message }}</span>
            <button class="btn btn-sm" :disabled="accel.hostsPicking || accel.hostsBusy" @click="refreshStaleHosts">重新优选并写入</button>
          </template>
        </div>
        <!-- 探测走没走代理：开着代理时直连探测的结论与真实 clone 体验是两回事 -->
        <div v-if="accel.health?.proxy" class="hint">镜像探测经代理：{{ accel.health.proxy }}</div>
      </section>

      <!-- ── 下载任务（有任务才出现） ── -->
      <section v-if="accel.downloads.length" class="panel">
        <div class="panel-title">下载任务</div>
        <div v-for="d in visibleDownloads" :key="d.id" class="dl-row">
          <div class="dl-head">
            <span class="dl-name" :title="d.fileName">{{ d.fileName }}</span>
            <span class="dl-size">{{ d.state === 'downloading' ? `${formatBytes(d.transferred)}${d.total ? ' / ' + formatBytes(d.total) : ''}` : (d.path ? formatBytes(d.transferred) : '') }}</span>
            <span class="dl-state" :class="d.state">{{ dlStateText(d) }}</span>
            <button v-if="d.state === 'downloading'" class="btn btn-sm btn-ghost" @click="cancelDownload(d.id)">取消</button>
            <button v-if="d.state === 'done'" class="btn btn-sm btn-ghost" @click="revealDownload(d.id)">打开位置</button>
          </div>
          <div v-if="d.state === 'downloading'" class="dl-bar">
            <div class="dl-bar-fill" :style="{ width: d.total ? Math.min(100, (d.transferred / d.total) * 100) + '%' : '30%' }"></div>
          </div>
          <div v-if="d.state === 'error' && d.error" class="hint warn">{{ d.error }}</div>
        </div>
      </section>

      <!-- ── 加速措施：卡片行（左：状态点 + 名称 + 状态章 + 事实；右：行内动作；
           说明收进 title 提示，副行只放从属配置）── -->
      <section class="panel">
        <div class="panel-title">
          加速措施
          <span class="head-spacer"></span>
          <!-- 自检是「怀疑措施失效时」的按需诊断（被动措施不会自己报错），
               跟着措施走而不是占总控的一级按钮位 -->
          <button
            class="btn btn-sm"
            :disabled="masterBusy || accel.selfChecking || accel.activeMeasureCount === 0"
            :title="accel.activeMeasureCount === 0 ? '先开启至少一项加速措施' : '端到端诊断：git 读取 / 镜像 / SSH 443 / hosts'"
            @click="onSelfCheck"
          >{{ accel.selfChecking ? '自检中…' : '自检' }}</button>
        </div>

        <!-- git 全局加速（含 push 去向从属行） -->
        <div class="measure-item" :class="{ active: accel.git?.active }">
          <div class="measure-main">
            <span class="dot" :class="accel.git?.active ? 'on' : 'off'"></span>
            <span class="measure-name" title="开启后 git clone / fetch / 下载按原地址自动走镜像；改写的是全局 git 配置，关闭时还原。只代理读取：同步写入 pushInsteadOf 保证 push 直连，HTTPS push 不受镜像影响">git 全局加速</span>
            <span class="title-chip" :class="gitChip.cls">{{ gitChip.text }}</span>
            <span v-if="accel.git?.active && !accel.git.managed" class="title-chip warn" title="当前 insteadOf 规则不是本应用写入的，关闭加速不会移除它">非本应用写入</span>
            <span class="m-detail mono" :title="accel.git?.active ? accel.git.activeSubsection : undefined">{{ gitDetailText }}</span>
            <span class="head-spacer"></span>
            <template v-if="accel.git?.available">
              <template v-if="accel.git.active">
                <select
                  class="text-input select"
                  :value="accel.activeGitPrefix"
                  :disabled="accel.gitBusy"
                  title="切换生效镜像（只改 git 全局 insteadOf，不碰仓库 remote）"
                  @change="switchMirror(($event.target as HTMLSelectElement).value)"
                >
                  <option v-for="p in proxyOptions" :key="p.id" :value="p.prefix">{{ p.name }}</option>
                </select>
                <button class="btn btn-danger btn-sm" :disabled="accel.gitBusy" @click="disableGit">关闭并还原</button>
              </template>
              <template v-else>
                <select v-model="gitPrefix" class="text-input select" :disabled="accel.gitBusy">
                  <option v-for="p in accel.enabledProxies" :key="p.id" :value="p.prefix">{{ p.name }}</option>
                </select>
                <button class="btn btn-primary btn-sm" :disabled="accel.gitBusy || !gitPrefix" @click="enableGit">开启</button>
              </template>
            </template>
          </div>
          <div v-if="accel.git && !accel.git.available" class="m-note warn">{{ accel.git.message || '未检测到 git' }}</div>
          <div v-else-if="accel.git?.active && accel.git.backup" class="m-note">已备份接管前的规则，关闭时自动还原。</div>

          <!-- push 策略：镜像只代理读取，push 去向单独可配（直连 / SSH 443） -->
          <div v-if="accel.git?.available" class="measure-sub-row">
            <span class="measure-sub-name">push 去向</span>
            <select
              class="text-input select"
              :value="pushModeValue"
              :disabled="accel.gitBusy || accel.git?.pushRewrite === 'user'"
              :title="accel.git?.pushRewrite === 'user' ? '检测到你自建的 push 规则，本应用不接管' : 'clone/fetch 永远走镜像；push 要么直连 GitHub，要么改写成 SSH（配合 SSH 443 更稳）'"
              @change="onPushMode(($event.target as HTMLSelectElement).value)"
            >
              <option value="direct">直连 GitHub（HTTPS）</option>
              <option value="ssh">走 SSH（git@github.com）</option>
              <option v-if="accel.git?.pushRewrite === 'user'" value="user">用户自建规则（不接管）</option>
            </select>
            <span class="m-detail">{{ pushDetailText }}</span>
          </div>
        </div>

        <!-- SSH 443 -->
        <div class="measure-item" :class="{ active: accel.ssh443?.applied }">
          <div class="measure-main">
            <span class="dot" :class="accel.ssh443?.applied ? 'on' : 'off'"></span>
            <span class="measure-name" title="写入 ~/.ssh/config 托管块：Host github.com → ssh.github.com:443（GitHub 官方支持），用 git@github.com 推拉时绕开被限速的 22 端口">SSH 443</span>
            <span class="title-chip" :class="accel.ssh443?.applied ? 'on' : 'off'">{{ accel.ssh443?.applied ? '已写入' : '未写入' }}</span>
            <span class="m-detail mono">ssh.github.com:443 · ~/.ssh/config</span>
            <span class="head-spacer"></span>
            <button v-if="accel.ssh443 && !accel.ssh443.applied" class="btn btn-primary btn-sm" :disabled="accel.sshBusy" @click="applySsh">写入</button>
            <button v-else-if="accel.ssh443" class="btn btn-danger btn-sm" :disabled="accel.sshBusy" @click="removeSsh">移除</button>
          </div>
        </div>

        <!-- hosts 优选 IP -->
        <div class="measure-item" :class="{ active: accel.hosts?.applied }">
          <div class="measure-main">
            <span class="dot" :class="accel.hosts?.applied ? 'on' : 'off'"></span>
            <span class="measure-name" title="解析 GitHub 系域名的优选 IP 写入系统 hosts，解决镜像覆盖不了的登录态网页与 API；写入与移除各弹一次 UAC">hosts 优选 IP</span>
            <span class="title-chip" :class="hostsAgeChip?.cls || 'off'">{{ hostsAgeChip?.text || '未写入' }}</span>
            <span class="m-detail mono">{{ hostsDetailText }}</span>
            <span class="head-spacer"></span>
            <!-- 时效告警时的一键更新：重新优选 + 写入（一次 UAC），不用记「hosts 要维护」 -->
            <button
              v-if="hostsAgeChip?.cls === 'warn'"
              class="btn btn-sm"
              :disabled="accel.hostsPicking || accel.hostsBusy"
              @click="refreshStaleHosts"
            >{{ accel.hostsPicking ? '优选中…' : '更新' }}</button>
            <button class="btn btn-sm" :disabled="accel.hostsPicking" @click="pickHosts">
              {{ accel.hostsPicking ? '优选中…' : (accel.hostsPicks.length ? '重新优选' : '优选 IP') }}
            </button>
            <button
              v-if="accel.hosts && !accel.hosts.applied"
              class="btn btn-primary btn-sm"
              :disabled="accel.hostsBusy || accel.hostsPicking || accel.hostsReadyCount === 0"
              @click="applyHosts"
            >写入 hosts</button>
            <button v-else-if="accel.hosts" class="btn btn-danger btn-sm" :disabled="accel.hostsBusy" @click="removeHosts">移除托管块</button>
          </div>
          <div class="measure-foot">
            <label class="auto-refresh" title="hosts 快照过期或解析失效时，巡检自动重新优选并写回（只在无需管理员授权时静默生效，不会后台弹 UAC）">
              <input
                type="checkbox"
                :checked="accel.hostsAutoRefresh"
                @change="onHostsAutoRefresh(($event.target as HTMLInputElement).checked)"
              />
              <span>失效自动重选</span>
            </label>
            <span v-if="accel.hosts?.message" class="m-note warn">{{ accel.hosts.message }}</span>
          </div>
          <!-- 优选候选：带实测延迟，仅在测出候选时出现（写入前的比选依据） -->
          <div v-if="accel.hostsPicks.length" class="hosts-table">
            <div v-for="r in accel.hostsPicks" :key="r.domain" class="hosts-row">
              <code class="link-url">{{ r.domain }}</code>
              <span class="hosts-ip" :class="{ none: !r.ip }">{{ r.ip ?? (r.error || '无可用 IP') }}</span>
              <span class="latency" :class="r.latencyMs !== null && r.latencyMs < 300 ? 'good' : r.ip ? 'mid' : 'bad'">
                {{ r.latencyMs !== null ? `${r.latencyMs}ms` : '' }}
              </span>
            </div>
          </div>
        </div>

        <!-- 自检结果：只读端到端验证，skip 灰显表示该措施未开启 -->
        <div v-if="accel.selfCheckRows.length" class="selfcheck">
          <div v-for="row in accel.selfCheckRows" :key="row.key" class="selfcheck-row">
            <span class="dot" :class="row.state === 'ok' ? 'on' : row.state === 'fail' ? 'fail' : 'off'"></span>
            <span class="selfcheck-name">{{ SELF_CHECK_LABELS[row.key] }}</span>
            <span class="selfcheck-msg" :class="{ warn: row.state === 'fail' }">{{ row.message }}</span>
            <span class="selfcheck-ms">{{ row.latencyMs != null ? formatLatency(row.latencyMs) : '' }}</span>
          </div>
        </div>
      </section>

      <!-- ── 镜像源 ── -->
      <section class="panel">
        <div class="panel-title">
          镜像源
          <span v-if="accel.probedAt" class="hint">测速于 {{ probeTimeText }}</span>
          <span class="head-spacer"></span>
          <button class="btn btn-sm" :disabled="accel.probing" @click="runProbe">{{ accel.probing ? '测速中…' : '全部测速' }}</button>
        </div>

        <div v-for="p in accel.proxies" :key="p.id" class="mirror-row">
          <label class="mirror-check">
            <input type="checkbox" :checked="p.enabled" @change="toggleProxy(p.id, ($event.target as HTMLInputElement).checked)" />
          </label>
          <span class="mirror-name">{{ p.name }}</span>
          <code class="link-url">{{ p.prefix }}</code>
          <span class="latency" :class="latencyClass(p.id)">{{ latencyText(p.id) }}</span>
          <span v-if="accel.bestProxyId === p.id" class="badge-best">最快</span>
          <button v-if="!p.builtin" class="btn btn-sm btn-danger" @click="removeProxy(p.id)">删除</button>
        </div>
      </section>

    </div>
  </div>
</template>

<script setup lang="ts">
// GitAccelPanel.vue — Git 页的「加速」子页。
// 由原 GithubAccelView.vue 主体原样搬入（页面标题上移到 GitView 壳），
// store（github-accel.ts）/ IPC（ghaccel:*）/ 主进程（electron/github-accel.ts）零改动。
// 私人令牌（PAT）管理已迁至「仓库」子页（GitReposPanel 弹窗）—— PAT 是 HTTPS 鉴权能力，
// 不是加速能力；配置存储（github-accel.json）与 IPC（ghaccel:getTokens/saveTokens）不变。
import { computed, onMounted, ref } from 'vue'
import { useGithubAccelStore } from '@/stores/github-accel'
import { useToastStore } from '@/stores/toast'
import { formatBytes } from '@/utils/format'

const accel = useGithubAccelStore()
const toast = useToastStore()

const gitPrefix = ref('')

const masterBusy = ref(false)
/** 本次总控动作的方向（执行中按钮文案用；与实时状态解耦，避免措施陆续生效时按钮来回翻转） */
const masterMode = ref<'enable' | 'disable'>('enable')
const masterNote = ref('')
const masterNoteWarn = ref(false)
/** 结果细节：从步骤 message 拆成独立标签（已选 X / 自动加入 Y / 淘汰 N 个） */
const masterTags = ref<string[]>([])

/** 「自动调优：已选 X，自动加入 Y」→ ['已选 X', '自动加入 Y'] */
function toTags(msg: string): string[] {
  return msg.replace(/^自动调优：/, '').split('，').map(s => s.trim()).filter(Boolean)
}

/** 总控开关状态：任一措施生效即视为"加速已开启"，按钮相应切换为「关闭加速」 */
const masterOn = computed(() => accel.activeMeasureCount > 0)

/** 状态文案：三项全开/全关一笔带过，部分生效时明确 n/3 */
const masterStateText = computed(() => {
  const n = accel.activeMeasureCount
  if (n === 0) return '未开启'
  return n === 3 ? '已开启' : `部分开启（${n}/3）`
})

const masterButtonLabel = computed(() => {
  // 启动时会先跑一轮实测调优（十几秒），按钮得说清在做什么，否则像卡死
  if (masterBusy.value) return masterMode.value === 'enable' ? '正在自动选镜像并启动…' : '正在关闭加速…'
  return masterOn.value ? '关闭加速' : '启动加速'
})

const SELF_CHECK_LABELS: Record<'git' | 'proxy' | 'ssh443' | 'hosts', string> = {
  git: 'git 读取',
  proxy: '镜像探测',
  ssh443: 'SSH 443',
  hosts: 'hosts 解析',
}

function formatLatency(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`
}

/** 巡检红灯的镜像归因：error 只由镜像失效写入（hosts 问题走 health.hosts.message） */
const mirrorFailureText = computed(() => {
  const h = accel.health
  if (!h || h.state !== 'degraded' || !h.error || !h.activePrefix) return ''
  return `当前镜像 ${h.activeName || h.activePrefix} 不可用（${h.error}）`
})

/** 生效中的镜像可能已被停用：下拉里补一条，避免 select 显示空白 */
const proxyOptions = computed(() => {
  const list = [...accel.enabledProxies]
  const active = accel.activeGitPrefix
  if (active && !list.some(p => p.prefix === active)) {
    list.unshift({
      id: `active:${active}`,
      name: `${accel.health?.activeName || active}（已停用）`,
      prefix: active,
      builtin: false,
      enabled: false,
    })
  }
  return list
})

function proxyName(prefix: string): string {
  return accel.proxies.find(p => p.prefix === prefix)?.name || accel.health?.activeName || prefix
}

async function switchMirror(prefix: string): Promise<void> {
  if (!prefix || prefix === accel.activeGitPrefix) return
  try {
    await accel.switchActiveMirror(prefix)
    toast.success(`已切换到 ${proxyName(prefix)}`)
  } catch (e: any) {
    toast.error(e?.message || '切换失败')
    // 主进程先卸旧规则再写新规则：失败时可能已无生效项，回读真实状态而非停在旧子节
    await accel.refreshGitStatus().catch(() => undefined)
  }
}

async function onSelfCheck(): Promise<void> {
  try {
    await accel.runSelfCheck()
  } catch (e: any) {
    toast.error(e?.message || '自检失败')
  }
}

const probeTimeText = computed(() => {
  if (!accel.probedAt) return ''
  const d = new Date(accel.probedAt)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`
})

/**
 * hosts 托管块的时效徽标。优选 IP 是某一次测速的快照，写进系统 hosts 后不会自己
 * 更新；GitHub 轮换接入 IP 后它会从加速器变成阻断器，而界面上看不出任何异常。
 * staleDays 为 null（旧版本或手改的块，无时间戳可比对）同样按 stale 处理 ——
 * 判"未知=新鲜"才是风险，多提示一次点选的代价只是几个点击。
 */
const hostsAgeChip = computed(() => {
  const h = accel.hosts
  if (!h?.applied) return null
  if (!h.stale) return { cls: 'on', text: '已生效' }
  return { cls: 'warn', text: h.staleDays === null ? '时效未知' : `${h.staleDays} 天未更新` }
})

/** git 措施的状态章：读取中 / 不可用 / 未开启 / 已接管 */
const gitChip = computed(() => {
  if (!accel.git) return { cls: 'off', text: '读取中…' }
  if (!accel.git.available) return { cls: 'off', text: '不可用' }
  return accel.git.active ? { cls: 'on', text: '已接管' } : { cls: 'off', text: '未开启' }
})

const gitDetailText = computed(() => {
  if (!accel.git?.available) return ''
  if (!accel.git.active) return 'clone / fetch 直连 GitHub'
  // insteadOf 子节形如 https://ghfast.top/https://github.com：省掉协议，只留「原地址 → 镜像」
  const sub = accel.git.activeSubsection || ''
  const [mirror, ...rest] = sub.replace(/^https?:\/\//, '').split('/https://')
  if (!rest.length) return sub
  return `${rest.join('/https://')} → ${mirror.replace(/\/$/, '')}`
})

/** push 下拉当前值：用户自建规则时显示“不接管”（下拉置灰） */
const pushModeValue = computed(() => (accel.git?.pushRewrite === 'user' ? 'user' : accel.pushMode))

const pushDetailText = computed(() => {
  const g = accel.git
  if (!g?.available) return ''
  if (g.pushRewrite === 'user') return `你自建的 push 规则（${g.pushSubsection || 'pushInsteadOf'}），本应用不改`
  if (g.pushRewrite === 'ssh') return 'push → git@github.com:…（走 SSH）'
  if (g.pushRewrite === 'direct') return 'push → 直连 github.com（不经镜像）'
  return '未接管 push'
})

/** hosts 详情行：已写入的映射概览 / 待写入候选 / 未接管 */
const hostsDetailText = computed(() => {
  if (accel.hosts?.applied) {
    const n = accel.hosts.entries.length
    const first = accel.hosts.entries[0]?.ip
    return n ? `${first} 等 ${n} 条映射` : '已写入托管块（无映射条目）'
  }
  return accel.hostsPicks.length ? '已测出候选，待写入' : 'GitHub 域名走系统 DNS 直连'
})

/** 界面只回看最近 8 个任务（主进程 5 分钟后从内存清终态） */
const visibleDownloads = computed(() => accel.downloads.slice(-8))

onMounted(async () => {
  try {
    await accel.init()
    // 上次选择的镜像若已被停用/删除，回退到第一个启用项（否则 select 空白）
    gitPrefix.value = accel.enabledProxies.find(p => p.prefix === accel.lastProxyPrefix)?.prefix
      ?? accel.enabledProxies[0]?.prefix
      ?? ''
    // 首次进入自动测速：没有延迟数据时「最快」徽标、镜像推荐全部失效，
    // 用户面对一排「未测速」无从选起。仅在本会话从未测过时触发，不重复打扰。
    if (!accel.probedAt && !accel.probing) void runProbe()
  } catch (e: any) {
    toast.error(e?.message || 'GitHub 加速配置加载失败')
  }
})

async function runProbe(): Promise<void> {
  try {
    await accel.probeAll()
  } catch (e: any) {
    toast.error(e?.message || '测速失败')
  }
}

function dlStateText(d: { state: string }): string {
  return ({ downloading: '下载中', done: '完成', error: '失败', cancelled: '已取消' } as Record<string, string>)[d.state] ?? d.state
}

async function cancelDownload(id: string): Promise<void> {
  try { await accel.downloadCancel(id) } catch { /* 已终态 */ }
}

async function revealDownload(id: string): Promise<void> {
  try { await accel.showInFolder(id) } catch (e: any) { toast.error(e?.message || '打开失败') }
}

function latencyText(id: string): string {
  const r = accel.probeOf(id)
  if (!r) return '—'
  if (!r.ok) return '失效'
  const parts: string[] = []
  if (r.latencyMs !== null) parts.push(`${r.latencyMs}ms`)
  if (typeof r.speedBps === 'number' && r.speedBps > 0) parts.push(formatSpeed(r.speedBps))
  return parts.length ? parts.join(' · ') : '—'
}

/** 带宽样本（archive 维度）：镜像排序看的就是它 */
function formatSpeed(bps: number): string {
  if (bps >= 1024 * 1024) return `${(bps / 1024 / 1024).toFixed(1)}MB/s`
  return `${Math.max(1, Math.round(bps / 1024))}KB/s`
}

function latencyClass(id: string): string {
  const r = accel.probeOf(id)
  if (!r) return ''
  if (!r.ok) return 'bad'
  if (r.latencyMs === null) return ''
  return r.latencyMs < 800 ? 'good' : r.latencyMs < 2000 ? 'mid' : 'bad'
}

async function toggleProxy(id: string, enabled: boolean): Promise<void> {
  try { await accel.toggleProxy(id, enabled) } catch (e: any) { toast.error(e?.message || '保存失败') }
}
async function removeProxy(id: string): Promise<void> {
  try { await accel.removeProxy(id) } catch (e: any) { toast.error(e?.message || '删除失败') }
}

async function onPushMode(mode: string): Promise<void> {
  if (mode !== 'direct' && mode !== 'ssh') return
  try {
    await accel.setPushMode(mode)
    toast.success(mode === 'ssh' ? 'push 已改走 SSH' : 'push 已改为直连 GitHub')
  } catch (e: any) { toast.error(e?.message || '切换失败') }
}

async function onHostsAutoRefresh(on: boolean): Promise<void> {
  try {
    await accel.setHostsAutoRefresh(on)
  } catch (e: any) { toast.error(e?.message || '保存失败') }
}

async function enableGit(): Promise<void> {
  try {
    await accel.gitEnable(gitPrefix.value)
    await accel.saveOptions({ lastProxyPrefix: gitPrefix.value })
    toast.success('已开启 git 全局加速')
  } catch (e: any) { toast.error(e?.message || '开启失败') }
}
async function disableGit(): Promise<void> {
  try {
    await accel.gitDisable()
    toast.success('已关闭并还原')
  } catch (e: any) { toast.error(e?.message || '关闭失败') }
}
async function applySsh(): Promise<void> {
  try {
    await accel.ssh443Apply()
    toast.success('已写入 ~/.ssh/config 托管块')
  } catch (e: any) { toast.error(e?.message || '写入失败') }
}
async function removeSsh(): Promise<void> {
  try {
    await accel.ssh443Remove()
    toast.success('已移除托管块')
  } catch (e: any) { toast.error(e?.message || '移除失败') }
}
async function pickHosts(): Promise<void> {
  try {
    await accel.hostsPick()
  } catch (e: any) { toast.error(e?.message || '优选失败') }
}
async function applyHosts(): Promise<void> {
  try {
    await accel.hostsApply()
    toast.success('hosts 托管块已写入')
  } catch (e: any) { toast.error(e?.message || '写入失败') }
}
async function removeHosts(): Promise<void> {
  try {
    await accel.hostsRemove()
    toast.success('hosts 托管块已移除')
  } catch (e: any) { toast.error(e?.message || '移除失败') }
}

// ─── 总控 ───
const STEP_LABEL: Record<string, string> = { git: 'git', ssh443: 'SSH 443', hosts: 'hosts' }

/** hosts 行的一键更新：重新优选 + 写入（一次 UAC）。时效告警（chip warn）与
 *  巡检 hosts 未生效两条路径都走这里，用户不需要记住「hosts 要维护」。 */
async function refreshStaleHosts(): Promise<void> {
  try {
    await accel.hostsPick()
    await accel.hostsApply()
    toast.success('hosts 已重新优选并写入')
  } catch (e: any) {
    toast.error(e?.message || 'hosts 更新失败')
  }
}

async function onMasterEnable(): Promise<void> {
  masterBusy.value = true
  masterMode.value = 'enable'
  masterNote.value = ''
  masterTags.value = []
  try {
    const steps = await accel.masterEnable()
    const failed = steps.filter(s => !s.ok)
    if (failed.length === 0) {
      // 主句只留结论，步骤里的细节（换了哪个镜像、淘汰了几个）拆成标签跟在后面
      const notes = steps.map(s => s.message).filter((m): m is string => !!m)
      masterNoteWarn.value = false
      masterNote.value = '加速已全部开启'
      masterTags.value = notes.flatMap(toTags)
    } else {
      masterNoteWarn.value = true
      masterTags.value = []
      masterNote.value = `部分未生效：${failed.map(s => `${STEP_LABEL[s.key]}（${s.message || '失败'}）`).join('；')}`
    }
    // 有任一项生效就异步自检一次，不阻塞总控按钮状态
    if (steps.some(s => s.ok)) void accel.runSelfCheck()
  } catch (e: any) {
    masterNoteWarn.value = true
    masterTags.value = []
    masterNote.value = e?.message || '启动失败'
  } finally {
    masterBusy.value = false
  }
}

async function onMasterDisable(): Promise<void> {
  masterBusy.value = true
  masterMode.value = 'disable'
  masterNote.value = ''
  masterTags.value = []
  try {
    const steps = await accel.masterDisable()
    const failed = steps.filter(s => !s.ok)
    masterTags.value = []
    if (failed.length === 0) {
      masterNoteWarn.value = false
      masterNote.value = '加速已全部关闭'
    } else {
      masterNoteWarn.value = true
      masterNote.value = `部分未撤销：${failed.map(s => `${STEP_LABEL[s.key]}（${s.message || '失败'}）`).join('；')}`
    }
  } catch (e: any) {
    masterNoteWarn.value = true
    masterNote.value = e?.message || '关闭失败'
  } finally {
    masterBusy.value = false
  }
}

</script>

<style scoped>
.gh-view {
  display: flex;
  flex-direction: column;
  flex: 1;
  overflow: hidden;
  min-height: 0;
  min-width: 0;
  font-family: var(--font-sans);
}

.head-spacer { flex: 1; }

.gh-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--spacing-12) var(--spacing-16) var(--spacing-24);
  display: flex;
  flex-direction: column;
  gap: var(--spacing-10);
  min-height: 0;
}

/* ── 卡片统一规格 ── */
.panel {
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: var(--spacing-12) var(--spacing-14);
  display: flex;
  flex-direction: column;
  gap: var(--spacing-8);
}

.panel-title {
  font-size: 0.9231rem;
  font-weight: 500;
  color: var(--text-primary);
  display: flex;
  align-items: center;
  gap: var(--spacing-8);
  min-height: 22px;
}

/* 标题/行内状态章：胶囊形淡底，亮态与告警各自带同色系背景 */
.title-chip {
  font-size: 0.7692rem;
  font-weight: 400;
  padding: 0 var(--spacing-6);
  line-height: 16px;
  border-radius: var(--radius-pill);
  border: 1px solid var(--border-subtle);
  background: color-mix(in srgb, var(--text-secondary) 6%, transparent);
  color: var(--text-secondary);
  white-space: nowrap;
}
.title-chip.on {
  color: var(--status-success-text);
  border-color: color-mix(in srgb, var(--status-success-text) 32%, transparent);
  background: color-mix(in srgb, var(--status-success-text) 10%, transparent);
}
/* 时效告警：优选 IP 是测速快照，超阈值/未知都走这里催一次重选 */
.title-chip.warn {
  color: var(--status-warn-text);
  border-color: color-mix(in srgb, var(--status-warn-text) 32%, transparent);
  background: color-mix(in srgb, var(--status-warn-text) 10%, transparent);
}

.hint {
  font-size: 0.8462rem;
  color: var(--text-secondary);
  line-height: 1.6;
}

.hint.warn { color: var(--danger-text); }

.hint code {
  font-family: var(--font-mono);
  font-size: 0.8462rem;
}

/* ── 总控卡（单行紧凑） ── */
.master-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-10);
  flex-wrap: wrap;
}

.master-title {
  font-size: 1rem;
  font-weight: 600;
  color: var(--text-primary);
}

/* 结果提示：主句一行 + 细节标签（已选 X / 淘汰 N 个），不再拼成一长串 */
.master-note {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--spacing-6);
  font-size: 0.8462rem;
}

.master-note-main {
  color: var(--status-success-text);
  font-weight: 500;
}

.master-note.warn .master-note-main {
  flex: 1 1 100%;
  color: var(--danger-text);
  font-weight: 400;
  line-height: 1.6;
}

.master-tag {
  font-size: 0.7692rem;
  color: var(--text-secondary);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  padding: 0 var(--spacing-6);
  line-height: 18px;
  white-space: nowrap;
}

/* ── 巡检红灯（总控卡内） ── */
.health-alert {
  display: flex;
  align-items: center;
  gap: var(--spacing-8);
  flex-wrap: wrap;
  padding: var(--spacing-6) var(--spacing-8);
  border-left: 2px solid var(--danger-text);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--danger-text) 8%, transparent);
}

.health-alert-text {
  font-size: 0.8462rem;
  color: var(--danger-text);
  line-height: 1.5;
}

/* ── 自检结果（总控卡内，只读） ── */
.selfcheck {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-2);
  padding-top: var(--spacing-2);
  border-top: 1px dashed var(--border-subtle);
}

.selfcheck-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-8);
  min-height: 20px;
}

.selfcheck-name {
  flex: 0 0 64px;
  font-size: 0.8462rem;
  color: var(--text-secondary);
}

.selfcheck-msg {
  flex: 1;
  min-width: 0;
  font-size: 0.8462rem;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.selfcheck-msg.warn { color: var(--danger-text); }

.selfcheck-ms {
  flex: 0 0 52px;
  font-size: 0.8462rem;
  font-family: var(--font-mono);
  color: var(--text-secondary);
  text-align: right;
}

/* ── 加速措施：卡片行 ── */
.measure-item {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-4);
  padding: var(--spacing-8) var(--spacing-10);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--bg-elevated) 55%, transparent);
}

/* 生效中：淡绿底 + 同色描边，扫一眼能数出哪几项在跑 */
.measure-item.active {
  border-color: color-mix(in srgb, var(--status-success-text) 28%, var(--border-subtle));
  background: color-mix(in srgb, var(--status-success-text) 4%, transparent);
}

.measure-main {
  display: flex;
  align-items: center;
  gap: var(--spacing-8);
  flex-wrap: wrap;
  min-height: 26px;
}

.measure-name {
  font-size: 0.9231rem;
  font-weight: 600;
  color: var(--text-primary);
  flex-shrink: 0;
  cursor: help;
}

/* 行内事实：等宽、三级色，与状态章互补（章说状态，事实说细节） */
.m-detail {
  flex: 0 1 auto;
  font-size: 0.8462rem;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.m-detail.mono { font-family: var(--font-mono); }
.m-detail.warn { color: var(--danger-text); }

/* 副说明（备份提示、失败原因）：比事实再弱一档，不跟主行抢视线 */
.m-note {
  font-size: 0.8462rem;
  color: var(--text-secondary);
  line-height: 1.5;
}
.m-note.warn { color: var(--danger-text); }

/* 从属栏（push 去向）：卡片内一根细虚线挂到 git 项下 */
.measure-sub-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-8);
  flex-wrap: wrap;
  padding: var(--spacing-6) 0 0;
  border-top: 1px dashed var(--border-subtle);
}

.measure-sub-name {
  flex: 0 0 56px;
  font-size: 0.8462rem;
  color: var(--text-secondary);
}

/* hosts 的副行：自动重选开关与告警文案同行，单独一行不拥挤 */
.measure-foot {
  display: flex;
  align-items: center;
  gap: var(--spacing-12);
  flex-wrap: wrap;
}

/* 卡片内下拉与按钮同高，避免同一行高低错落 */
.measure-main .text-input.select,
.measure-main .btn {
  height: 24px;
}

/* hosts 自动重选：跟按钮同排的轻量开关 */
.auto-refresh {
  display: flex;
  align-items: center;
  gap: var(--spacing-4);
  font-size: 0.8462rem;
  color: var(--text-secondary);
  cursor: pointer;
  user-select: none;
}
.auto-refresh input { accent-color: var(--accent-primary); }

.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
}
.dot.on { background: var(--status-success-text); }
.dot.off { background: var(--text-secondary); opacity: .5; }
.dot.fail { background: var(--danger-text); }

.text-input {
  flex: 1;
  height: 26px;
  padding: 0 var(--spacing-8);
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-subtle);
  background: var(--bg-input);
  color: var(--text-primary);
  font-size: 0.9231rem;
  font-family: var(--font-mono);
  outline: none;
  min-width: 0;
}

.text-input:focus { border-color: var(--accent-primary); }
.text-input.select { flex: 0 1 auto; font-family: var(--font-sans); }

/* ── 下载任务 ── */
.dl-row { display: flex; flex-direction: column; gap: var(--spacing-4); padding: var(--spacing-4) 0; }

.dl-head { display: flex; align-items: center; gap: var(--spacing-8); min-height: 22px; }

.dl-name {
  flex: 1;
  font-size: 0.9231rem;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.dl-size { font-size: 0.8462rem; color: var(--text-secondary); font-family: var(--font-mono); }

.dl-state { font-size: 0.8462rem; color: var(--text-secondary); flex: 0 0 48px; text-align: right; }
.dl-state.done { color: var(--status-success-text); }
.dl-state.error { color: var(--danger-text); }

.dl-bar {
  height: 4px;
  border-radius: var(--radius-xs);
  background: var(--bg-slider);
  overflow: hidden;
}

.dl-bar-fill {
  height: 100%;
  background: var(--accent-primary);
  border-radius: var(--radius-xs);
  transition: width 0.25s ease;
}

.link-url {
  flex: 1;
  font-family: var(--font-mono);
  font-size: 0.8462rem;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

/* ── hosts 优选候选表（优选中出现，写入前的比选依据）── */
.hosts-table { display: flex; flex-direction: column; gap: var(--spacing-2); margin-top: var(--spacing-4); }

.hosts-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-8);
  min-height: 22px;
}

.hosts-ip {
  flex: 0 0 120px;
  font-size: 0.8462rem;
  font-family: var(--font-mono);
  color: var(--text-primary);
}
.hosts-ip.none { color: var(--danger-text); }

/* ── 镜像源 ── */

.mirror-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-8);
  min-height: 26px;
}

.mirror-check { display: flex; align-items: center; }
.mirror-check input { accent-color: var(--accent-primary); }

.mirror-name {
  flex: 0 0 110px;
  font-size: 0.9231rem;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 延迟 + 带宽同栏（“123ms · 1.2MB/s”），窄了会挤成省略号 */
.latency {
  flex: 0 0 108px;
  font-size: 0.8462rem;
  font-family: var(--font-mono);
  color: var(--text-secondary);
  text-align: right;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.latency.good { color: var(--status-success-text); }
.latency.mid { color: var(--status-warn-text); }
.latency.bad { color: var(--danger-text); }

.badge-best {
  font-size: 0.7692rem;
  color: var(--status-success-text);
  border: 1px solid color-mix(in srgb, var(--status-success-text) 40%, transparent);
  border-radius: var(--radius-sm);
  padding: 0 var(--spacing-4);
  line-height: 16px;
}
</style>
