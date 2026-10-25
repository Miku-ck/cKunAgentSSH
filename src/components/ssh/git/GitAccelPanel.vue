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
        <div class="hint">
          一键开关三项措施；hosts 写入与移除各弹一次 UAC。
          git 加速只代理读取（clone / fetch / 下载），HTTPS push 不走镜像，请用 SSH 443。
        </div>
        <div v-if="masterNote" class="hint" :class="{ warn: masterNoteWarn }">{{ masterNote }}</div>

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
      </section>

      <!-- ── 痛点①②③的日常入口：链接转换 ── -->
      <section class="panel">
        <div class="panel-title">链接转换</div>
        <div class="convert-row">
          <input
            v-model="inputUrl"
            class="text-input"
            spellcheck="false"
            @keyup.enter="doConvert"
          />
          <button class="btn btn-primary" :disabled="!inputUrl.trim()" @click="doConvert">转换</button>
        </div>

        <div v-if="converted" class="convert-result">
          <div v-if="converted.parsed.type === 'unknown'" class="hint warn">
            无法识别这个地址。支持：github.com 仓库/页面、releases/download 附件、archive、raw.githubusercontent.com、git@github.com 形式。
          </div>
          <div v-else-if="converted.result.links.length === 0" class="hint warn">
            没有可用的加速目标——请在下方「镜像源」里启用至少一个。
          </div>
          <template v-else>
            <div v-for="group in linkGroups" :key="group.kind" class="link-group">
              <div class="group-title">{{ group.title }}</div>
              <div v-for="link in visibleLinks(group)" :key="link.url" class="link-row">
                <span class="link-mirror">{{ link.mirrorName }}</span>
                <code class="link-url" :title="link.url">{{ displayUrl(link) }}</code>
                <button class="btn btn-sm" @click="copyText(displayUrl(link), link.url)">{{ copiedKey === link.url ? '已复制' : '复制' }}</button>
                <button v-if="link.kind === 'web'" class="btn btn-sm btn-ghost" @click="openExternal(link.url)">打开</button>
                <button v-if="link.kind === 'download'" class="btn btn-sm btn-primary" @click="startDownload(link)">下载</button>
              </div>
              <!-- 有测速数据时默认只推最快一条，其余折叠；无数据则全量平铺（未测速的第一条不构成推荐） -->
              <button v-if="canCollapseGroup(group)" class="btn btn-sm btn-ghost fold-toggle" @click="toggleGroup(group.kind)">
                {{ expandedGroups[group.kind] ? '收起其他镜像' : `其他 ${group.links.length - 1} 个镜像 ▾` }}
              </button>
            </div>
          </template>
        </div>
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

      <!-- ── 加速措施：三行一表（原型稿形态：图标 + 状态章 + 详情 + 行内动作；
           说明收进 title 提示，正文只留状态与详情）── -->
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

        <!-- git 全局加速 -->
        <div class="measure-row">
          <svg class="m-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="13" height="13"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>
          <span class="measure-name" title="开启后 git clone / fetch / 下载按原地址自动走镜像；改写的是全局 git 配置，关闭时还原。只代理读取，HTTPS push 不走镜像">git 全局加速</span>
          <span class="title-chip" :class="gitChip.cls">{{ gitChip.text }}</span>
          <span v-if="accel.git?.active && !accel.git.managed" class="title-chip warn" title="当前 insteadOf 规则不是本应用写入的，关闭加速不会移除它">非本应用写入</span>
          <span class="m-detail mono">{{ gitDetailText }}</span>
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
        <div v-if="accel.git && !accel.git.available" class="m-detail">{{ accel.git.message || '未检测到 git' }}</div>
        <div v-else-if="accel.git?.active && accel.git.backup" class="m-detail">已备份接管前规则，关闭时还原。</div>

        <div class="measure-sep"></div>

        <!-- SSH 443 -->
        <div class="measure-row">
          <svg class="m-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="13" height="13"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
          <span class="measure-name" title="写入 ~/.ssh/config 托管块：Host github.com → ssh.github.com:443（GitHub 官方支持），用 git@github.com 推拉时绕开被限速的 22 端口">SSH 443</span>
          <span class="title-chip" :class="accel.ssh443?.applied ? 'on' : 'off'">{{ accel.ssh443?.applied ? '已写入' : '未写入' }}</span>
          <span class="m-detail mono">ssh.github.com:443 · ~/.ssh/config</span>
          <span class="head-spacer"></span>
          <button v-if="accel.ssh443 && !accel.ssh443.applied" class="btn btn-primary btn-sm" :disabled="accel.sshBusy" @click="applySsh">写入</button>
          <button v-else-if="accel.ssh443" class="btn btn-danger btn-sm" :disabled="accel.sshBusy" @click="removeSsh">移除</button>
        </div>

        <div class="measure-sep"></div>

        <!-- hosts 优选 IP -->
        <div class="measure-row">
          <svg class="m-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="13" height="13"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
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
        <div v-if="accel.hosts?.message" class="m-detail warn">{{ accel.hosts.message }}</div>
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
        <div class="add-row">
          <input v-model="newProxyPrefix" class="text-input" placeholder="添加代理前缀：https://mirror.example/" spellcheck="false" @keyup.enter="addProxy" />
          <button class="btn btn-sm" :disabled="!newProxyPrefix.trim()" @click="addProxy">添加</button>
        </div>

        <div class="mirror-sep"></div>

        <div v-for="w in accel.webMirrors" :key="w.id" class="mirror-row">
          <label class="mirror-check">
            <input type="checkbox" :checked="w.enabled" @change="toggleWeb(w.id, ($event.target as HTMLInputElement).checked)" />
          </label>
          <span class="mirror-name">{{ w.name }}</span>
          <code class="link-url">{{ w.host }}</code>
          <span class="latency" :class="latencyClass(w.id)">{{ latencyText(w.id) }}</span>
          <button v-if="!w.builtin" class="btn btn-sm btn-danger" @click="removeWeb(w.id)">删除</button>
        </div>
        <div class="add-row">
          <input v-model="newWebHost" class="text-input" placeholder="添加网页镜像站：mirror.example.com" spellcheck="false" @keyup.enter="addWeb" />
          <button class="btn btn-sm" :disabled="!newWebHost.trim()" @click="addWeb">添加</button>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
// GitAccelPanel.vue — Git 页的「加速」子页。
// 由原 GithubAccelView.vue 主体原样搬入（页面标题上移到 GitView 壳），
// store（github-accel.ts）/ IPC（ghaccel:*）/ 主进程（electron/github-accel.ts）零改动。
import { computed, onMounted, ref, watch } from 'vue'
import { useGithubAccelStore } from '@/stores/github-accel'
import { useToastStore } from '@/stores/toast'
import { formatBytes } from '@/utils/format'
import { type AccelLink } from '../../../../shared/github-accel-core'

const accel = useGithubAccelStore()
const toast = useToastStore()

const inputUrl = ref('')
const converted = ref<ReturnType<typeof accel.convert> | null>(null)
const copiedKey = ref('')
const gitPrefix = ref('')
const newProxyPrefix = ref('')
const newWebHost = ref('')

const masterBusy = ref(false)
/** 本次总控动作的方向（执行中按钮文案用；与实时状态解耦，避免措施陆续生效时按钮来回翻转） */
const masterMode = ref<'enable' | 'disable'>('enable')
const masterNote = ref('')
const masterNoteWarn = ref(false)

/** 总控开关状态：任一措施生效即视为"加速已开启"，按钮相应切换为「关闭加速」 */
const masterOn = computed(() => accel.activeMeasureCount > 0)

/** 状态文案：三项全开/全关一笔带过，部分生效时明确 n/3 */
const masterStateText = computed(() => {
  const n = accel.activeMeasureCount
  if (n === 0) return '未开启'
  return n === 3 ? '已开启' : `部分开启（${n}/3）`
})

const masterButtonLabel = computed(() => {
  if (masterBusy.value) return masterMode.value === 'enable' ? '正在启动加速…' : '正在关闭加速…'
  return masterOn.value ? '关闭加速' : '启动加速'
})

const KIND_TITLES: Record<AccelLink['kind'], string> = {
  clone: 'git clone',
  download: '文件下载',
  web: '网页浏览（镜像站，不能登录）',
}

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

interface LinkGroup {
  kind: AccelLink['kind']
  title: string
  links: AccelLink[]
}

const linkGroups = computed<LinkGroup[]>(() => {
  if (!converted.value) return []
  const order: AccelLink['kind'][] = ['clone', 'download', 'web']
  return order
    .map(kind => ({ kind, title: KIND_TITLES[kind], links: sortLinks(converted.value!.result.links.filter(l => l.kind === kind)) }))
    .filter(g => g.links.length > 0)
})

/** 组内按实测延迟升序（未测速的排后面、保持原相对顺序）——
 *  测速完成后「最快」镜像自然排第一，与镜像源面板的「最快」徽标同一事实源 */
function sortLinks(links: AccelLink[]): AccelLink[] {
  return links
    .map((link, i) => ({ link, i, r: accel.probeOf(link.mirrorId) }))
    .sort((a, b) => {
      const av = a.r?.ok && a.r.latencyMs !== null ? a.r.latencyMs : Infinity
      const bv = b.r?.ok && b.r.latencyMs !== null ? b.r.latencyMs : Infinity
      return av === bv ? a.i - b.i : av - bv
    })
    .map(x => x.link)
}

// ── 转换结果收敛：默认每类只展示推荐（实测最快）一条，其余折叠 ──
// 启用 5 个镜像时全量平铺是 5 行 clone + 5 行下载，扫读成本远高于信息价值。
// 只在「有测速数据且不止一条」时才折叠 —— 没测过速时第一条并非推荐，不收。
const expandedGroups = ref<Record<string, boolean>>({})

function groupHasProbe(g: LinkGroup): boolean {
  return g.links.some(l => {
    const r = accel.probeOf(l.mirrorId)
    return !!r?.ok && r.latencyMs !== null
  })
}

function canCollapseGroup(g: LinkGroup): boolean {
  return g.links.length > 1 && groupHasProbe(g)
}

function visibleLinks(g: LinkGroup): AccelLink[] {
  return canCollapseGroup(g) && !expandedGroups.value[g.kind] ? g.links.slice(0, 1) : g.links
}

function toggleGroup(kind: AccelLink['kind']): void {
  expandedGroups.value[kind] = !expandedGroups.value[kind]
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
  return accel.git.active ? `insteadOf → ${accel.git.activeSubsection}` : 'clone / fetch 直连 GitHub'
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
    // 首次进入自动测速：没有延迟数据时「最快」徽标、转换结果排序、镜像推荐全部失效，
    // 用户面对一排「未测速」无从选起。仅在本会话从未测过时触发，不重复打扰。
    if (!accel.probedAt && !accel.probing) void runProbe()
  } catch (e: any) {
    toast.error(e?.message || 'GitHub 加速配置加载失败')
  }
})

function doConvert(): void {
  converted.value = accel.convert(inputUrl.value)
}

// 镜像清单（启停/增删/测速后）变化时，已转换的结果自动重算——否则新加的镜像不出现在结果里
watch(
  () => [accel.proxies, accel.webMirrors] as const,
  () => {
    if (inputUrl.value.trim() && converted.value) doConvert()
  },
  { deep: true },
)

/** clone 类链接展示为完整命令，下载/网页展示裸地址 */
function displayUrl(link: AccelLink): string {
  return link.kind === 'clone' ? `git clone ${link.url}` : link.url
}

let copyTimer: ReturnType<typeof setTimeout> | undefined
async function copyText(text: string, key: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    copiedKey.value = key
    clearTimeout(copyTimer)
    copyTimer = setTimeout(() => { copiedKey.value = '' }, 1500)
  } catch {
    toast.error('复制失败（剪贴板不可用）')
  }
}

async function openExternal(url: string): Promise<void> {
  try {
    await window.electronAPI?.ghaccelOpenExternal(url)
  } catch (e: any) {
    toast.error(e?.message || '打开失败')
  }
}

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

async function startDownload(link: AccelLink): Promise<void> {
  try {
    await accel.downloadStart(link.url)
    toast.success(`开始下载：${link.mirrorName}`)
  } catch (e: any) {
    toast.error(e?.message || '下载发起失败')
  }
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
  return r.latencyMs !== null ? `${r.latencyMs}ms` : '—'
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
async function toggleWeb(id: string, enabled: boolean): Promise<void> {
  try { await accel.toggleWebMirror(id, enabled) } catch (e: any) { toast.error(e?.message || '保存失败') }
}
async function addProxy(): Promise<void> {
  try {
    await accel.addProxy('', newProxyPrefix.value)
    newProxyPrefix.value = ''
  } catch (e: any) { toast.error(e?.message || '添加失败') }
}
async function addWeb(): Promise<void> {
  try {
    await accel.addWebMirror('', newWebHost.value)
    newWebHost.value = ''
  } catch (e: any) { toast.error(e?.message || '添加失败') }
}
async function removeProxy(id: string): Promise<void> {
  try { await accel.removeProxy(id) } catch (e: any) { toast.error(e?.message || '删除失败') }
}
async function removeWeb(id: string): Promise<void> {
  try { await accel.removeWebMirror(id) } catch (e: any) { toast.error(e?.message || '删除失败') }
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
  try {
    const steps = await accel.masterEnable()
    const failed = steps.filter(s => !s.ok)
    if (failed.length === 0) {
      masterNoteWarn.value = false
      masterNote.value = '加速已全部开启'
    } else {
      masterNoteWarn.value = true
      masterNote.value = `部分未生效：${failed.map(s => `${STEP_LABEL[s.key]}（${s.message || '失败'}）`).join('；')}`
    }
    // 有任一项生效就异步自检一次，不阻塞总控按钮状态
    if (steps.some(s => s.ok)) void accel.runSelfCheck()
  } catch (e: any) {
    masterNoteWarn.value = true
    masterNote.value = e?.message || '启动失败'
  } finally {
    masterBusy.value = false
  }
}

async function onMasterDisable(): Promise<void> {
  masterBusy.value = true
  masterMode.value = 'disable'
  masterNote.value = ''
  try {
    const steps = await accel.masterDisable()
    const failed = steps.filter(s => !s.ok)
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
  padding: 12px 16px 24px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-height: 0;
}

/* ── 卡片统一规格 ── */
.panel {
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.panel-title {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-primary);
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 22px;
}

/* 标题右侧状态小签（各措施卡通用） */
.title-chip {
  font-size: 10px;
  font-weight: 400;
  padding: 0 5px;
  line-height: 16px;
  border-radius: 3px;
  border: 1px solid var(--border-subtle);
  color: var(--text-secondary);
}
.title-chip.on {
  color: var(--status-success-text);
  border-color: color-mix(in srgb, var(--status-success-text) 40%, transparent);
}
/* 时效告警：优选 IP 是测速快照，超阈值/未知都走这里催一次重选 */
.title-chip.warn {
  color: var(--status-warn-text);
  border-color: color-mix(in srgb, var(--status-warn-text) 40%, transparent);
}

.hint {
  font-size: 11px;
  color: var(--text-secondary);
  line-height: 1.6;
}

.hint.warn { color: var(--danger-text); }

.hint code {
  font-family: var(--font-mono);
  font-size: 11px;
}

/* ── 总控卡（单行紧凑） ── */
.master-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.master-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

/* ── 巡检红灯（总控卡内） ── */
.health-alert {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 6px 8px;
  border-left: 2px solid var(--danger-text);
  border-radius: 3px;
  background: color-mix(in srgb, var(--danger-text) 8%, transparent);
}

.health-alert-text {
  font-size: 11px;
  color: var(--danger-text);
  line-height: 1.5;
}

/* ── 自检结果（总控卡内，只读） ── */
.selfcheck {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding-top: 2px;
  border-top: 1px dashed var(--border-subtle);
}

.selfcheck-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 20px;
}

.selfcheck-name {
  flex: 0 0 64px;
  font-size: 11px;
  color: var(--text-secondary);
}

.selfcheck-msg {
  flex: 1;
  min-width: 0;
  font-size: 11px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.selfcheck-msg.warn { color: var(--danger-text); }

.selfcheck-ms {
  flex: 0 0 52px;
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--text-secondary);
  text-align: right;
}

/* ── 链接转换 ── */
.convert-row { display: flex; gap: 8px; }
.convert-result { display: flex; flex-direction: column; gap: 10px; }

.link-group { display: flex; flex-direction: column; gap: 4px; }
.group-title { font-size: 11px; color: var(--text-secondary); }

/* 折叠切换：左对齐、小号，视觉上属于组内附注而非独立动作 */
.fold-toggle {
  margin-top: 2px;
  align-self: flex-start;
  font-size: 11px;
}

.link-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 24px;
}

.link-mirror {
  flex: 0 0 110px;
  font-size: 11px;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.link-url {
  flex: 1;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

/* ── 加速措施：三行一表（原型稿形态）── */
.measure-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 30px;
  flex-wrap: wrap;
}

.measure-name {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-primary);
  flex-shrink: 0;
  cursor: help;
}

.m-ic { color: var(--text-secondary); flex-shrink: 0; }

/* 行内详情：等宽、次级色，与状态章互补（章说状态，详情说事实） */
.m-detail {
  font-size: 11px;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.m-detail.warn { color: var(--danger-text); }

.measure-sep {
  border-top: 1px dashed var(--border-subtle);
  margin: 2px 0;
}

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
  padding: 0 8px;
  border-radius: 4px;
  border: 1px solid var(--border-subtle);
  background: var(--bg-input);
  color: var(--text-primary);
  font-size: 12px;
  font-family: var(--font-mono);
  outline: none;
  min-width: 0;
}

.text-input:focus { border-color: var(--accent-primary); }
.text-input.select { flex: 0 1 auto; font-family: var(--font-sans); }

/* ── 下载任务 ── */
.dl-row { display: flex; flex-direction: column; gap: 4px; padding: 4px 0; }

.dl-head { display: flex; align-items: center; gap: 8px; min-height: 22px; }

.dl-name {
  flex: 1;
  font-size: 12px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.dl-size { font-size: 11px; color: var(--text-secondary); font-family: var(--font-mono); }

.dl-state { font-size: 11px; color: var(--text-secondary); flex: 0 0 48px; text-align: right; }
.dl-state.done { color: var(--status-success-text); }
.dl-state.error { color: var(--danger-text); }

.dl-bar {
  height: 4px;
  border-radius: 2px;
  background: var(--bg-slider);
  overflow: hidden;
}

.dl-bar-fill {
  height: 100%;
  background: var(--accent-primary);
  border-radius: 2px;
  transition: width 0.25s ease;
}

/* ── hosts 优选候选表（优选中出现，写入前的比选依据）── */
.hosts-table { display: flex; flex-direction: column; gap: 2px; margin-top: 4px; }

.hosts-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 22px;
}

.hosts-ip {
  flex: 0 0 120px;
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--text-primary);
}
.hosts-ip.none { color: var(--danger-text); }

/* ── 镜像源 ── */
.mirror-sep { border-top: 1px dashed var(--border-subtle); margin: 4px 0; }

.mirror-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 26px;
}

.mirror-check { display: flex; align-items: center; }
.mirror-check input { accent-color: var(--accent-primary); }

.mirror-name {
  flex: 0 0 110px;
  font-size: 12px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.latency {
  flex: 0 0 64px;
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--text-secondary);
  text-align: right;
}
.latency.good { color: var(--status-success-text); }
.latency.mid { color: var(--status-warning-text, var(--text-secondary)); }
.latency.bad { color: var(--danger-text); }

.badge-best {
  font-size: 10px;
  color: var(--status-success-text);
  border: 1px solid color-mix(in srgb, var(--status-success-text) 40%, transparent);
  border-radius: 3px;
  padding: 0 4px;
  line-height: 16px;
}

.add-row {
  display: flex;
  gap: 8px;
  margin-top: 2px;
}

</style>
