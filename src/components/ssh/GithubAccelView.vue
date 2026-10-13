<template>
  <div class="gh-view">
    <div class="page-head">
      <div class="page-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="16" height="16"><path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"/></svg>
        <span>GitHub 加速</span>
      </div>
    </div>

    <div class="gh-body">
      <!-- ── 加速总控：一键启动 / 关闭 ── -->
      <section class="panel master">
        <div class="master-row">
          <span class="dot" :class="accel.activeMeasureCount > 0 ? 'on' : 'off'"></span>
          <span class="master-title">加速{{ accel.activeMeasureCount > 0 ? '运行中' : '未启动' }}</span>
          <span class="measure-chips">
            <span v-for="m in measureChips" :key="m.key" class="m-chip" :class="m.on ? 'on' : 'off'">{{ m.label }}</span>
          </span>
          <span class="head-spacer"></span>
          <button class="btn btn-primary" :disabled="masterBusy" @click="onMasterEnable">启动加速</button>
          <button class="btn" :disabled="masterBusy" @click="onMasterDisable">关闭加速</button>
        </div>
        <div v-if="masterNote" class="hint" :class="{ warn: masterNoteWarn }">{{ masterNote }}</div>
      </section>

      <!-- ── 痛点①②③的日常入口：链接转换 ── -->
      <section class="panel">
        <div class="panel-title">链接转换</div>
        <div class="convert-row">
          <input
            v-model="inputUrl"
            class="text-input"
            placeholder="粘贴 GitHub 地址：仓库 / Release 附件 / raw / git@github.com:…"
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
              <div v-for="link in group.links" :key="link.url" class="link-row">
                <span class="link-mirror">{{ link.mirrorName }}</span>
                <code class="link-url" :title="link.url">{{ displayUrl(link) }}</code>
                <button class="btn btn-sm" @click="copyText(displayUrl(link), link.url)">{{ copiedKey === link.url ? '已复制' : '复制' }}</button>
                <button v-if="link.kind === 'web'" class="btn btn-sm btn-ghost" @click="openExternal(link.url)">打开</button>
                <button v-if="link.kind === 'download'" class="btn btn-sm btn-primary" @click="startDownload(link)">下载</button>
              </div>
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

      <!-- ── 措施一：clone 加速 ── -->
      <section class="panel">
        <div class="panel-title">
          git 全局加速
          <span v-if="accel.git?.active" class="title-chip on">已开启</span>
          <span v-else class="title-chip off">未开启</span>
        </div>
        <div class="hint">开启后 <code>git clone https://github.com/…</code> 原样敲即自动走镜像；关闭时自动还原。</div>
        <div v-if="accel.git" class="status-block">
          <div v-if="!accel.git.available" class="hint warn">{{ accel.git.message || '未检测到 git' }}</div>
          <template v-else>
            <div v-if="accel.git.active" class="status-line">
              <code class="link-url">{{ accel.git.activeSubsection }}</code>
              <span v-if="!accel.git.managed" class="hint warn">（非本应用写入）</span>
            </div>
            <div v-if="accel.git.backup" class="hint">已备份接管前规则，关闭时还原。</div>
            <div class="action-row">
              <template v-if="!accel.git.active">
                <select v-model="gitPrefix" class="text-input select">
                  <option v-for="p in accel.enabledProxies" :key="p.id" :value="p.prefix">{{ p.name }}</option>
                </select>
                <button class="btn btn-primary" :disabled="accel.gitBusy || !gitPrefix" @click="enableGit">开启</button>
              </template>
              <button v-else class="btn btn-danger" :disabled="accel.gitBusy" @click="disableGit">关闭并还原</button>
            </div>
          </template>
        </div>
      </section>

      <!-- ── 措施二：SSH 推拉加速 ── -->
      <section class="panel">
        <div class="panel-title">
          SSH 走 443 端口
          <span v-if="accel.ssh443?.applied" class="title-chip on">已写入</span>
          <span v-else class="title-chip off">未写入</span>
        </div>
        <div class="hint">用 <code>git@github.com:…</code> 推拉时绕开被限速的 22 端口（GitHub 官方支持，写入 <code>~/.ssh/config</code> 托管块，可一键移除）。</div>
        <div v-if="accel.ssh443" class="status-block">
          <div class="action-row">
            <button v-if="!accel.ssh443.applied" class="btn btn-primary" :disabled="accel.sshBusy" @click="applySsh">写入</button>
            <button v-else class="btn btn-danger" :disabled="accel.sshBusy" @click="removeSsh">移除</button>
          </div>
        </div>
      </section>

      <!-- ── 措施三：hosts 优选（登录态网页） ── -->
      <section class="panel">
        <div class="panel-title">
          hosts 优选 IP
          <span v-if="accel.hosts?.applied" class="title-chip on">已生效</span>
          <span v-else class="title-chip off">未写入</span>
        </div>
        <div class="hint">解决镜像覆盖不了的登录态网页与 API：解析 GitHub 官方最优 IP 写入系统 hosts（写入时弹 UAC 提权框，确认即生效）。</div>
        <div v-if="accel.hosts" class="status-block">
          <div v-if="accel.hosts.applied && accel.hosts.entries.length" class="hosts-table">
            <div v-for="e in accel.hosts.entries" :key="e.domain" class="hosts-row">
              <code class="link-url">{{ e.domain }}</code>
              <span class="hosts-ip">{{ e.ip }}</span>
            </div>
          </div>
          <div v-if="accel.hostsPicks.length" class="hosts-table">
            <div v-for="r in accel.hostsPicks" :key="r.domain" class="hosts-row">
              <code class="link-url">{{ r.domain }}</code>
              <span class="hosts-ip" :class="{ none: !r.ip }">{{ r.ip ?? (r.error || '无可用 IP') }}</span>
              <span class="latency" :class="r.latencyMs !== null && r.latencyMs < 300 ? 'good' : r.ip ? 'mid' : 'bad'">
                {{ r.latencyMs !== null ? `${r.latencyMs}ms` : '' }}
              </span>
            </div>
          </div>
          <div class="action-row">
            <button class="btn" :disabled="accel.hostsPicking" @click="pickHosts">
              {{ accel.hostsPicking ? '优选中…' : (accel.hostsPicks.length ? '重新优选' : '优选 IP') }}
            </button>
            <button
              v-if="!accel.hosts.applied"
              class="btn btn-primary"
              :disabled="accel.hostsBusy || accel.hostsPicking || accel.hostsReadyCount === 0"
              @click="applyHosts"
            >写入 hosts</button>
            <button v-else class="btn btn-danger" :disabled="accel.hostsBusy" @click="removeHosts">移除托管块</button>
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

      <div class="risk-note">公共镜像属于不可信中继：经其 clone / 下载的代码理论上可被篡改，敏感项目请核对 commit hash。</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useGithubAccelStore } from '@/stores/github-accel'
import { useToastStore } from '@/stores/toast'
import { formatBytes } from '@/utils/format'
import { type AccelLink } from '../../../shared/github-accel-core'

const accel = useGithubAccelStore()
const toast = useToastStore()

const inputUrl = ref('')
const converted = ref<ReturnType<typeof accel.convert> | null>(null)
const copiedKey = ref('')
const gitPrefix = ref('')
const newProxyPrefix = ref('')
const newWebHost = ref('')

const masterBusy = ref(false)
const masterNote = ref('')
const masterNoteWarn = ref(false)

const KIND_TITLES: Record<AccelLink['kind'], string> = {
  clone: 'git clone',
  download: '文件下载',
  web: '网页浏览（镜像站，不能登录）',
}

const measureChips = computed(() => [
  { key: 'git', label: 'git', on: !!accel.git?.active },
  { key: 'ssh443', label: 'SSH 443', on: !!accel.ssh443?.applied },
  { key: 'hosts', label: 'hosts', on: !!accel.hosts?.applied },
])

const linkGroups = computed(() => {
  if (!converted.value) return []
  const order: AccelLink['kind'][] = ['clone', 'download', 'web']
  return order
    .map(kind => ({ kind, title: KIND_TITLES[kind], links: converted.value!.result.links.filter(l => l.kind === kind) }))
    .filter(g => g.links.length > 0)
})

const probeTimeText = computed(() => {
  if (!accel.probedAt) return ''
  const d = new Date(accel.probedAt)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`
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
  } catch (e: any) {
    toast.error(e?.message || 'GitHub 加速配置加载失败')
  }
})

function doConvert(): void {
  converted.value = accel.convert(inputUrl.value)
}

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

async function onMasterEnable(): Promise<void> {
  masterBusy.value = true
  masterNote.value = ''
  try {
    const steps = await accel.masterEnable()
    const failed = steps.filter(s => !s.ok)
    if (failed.length === 0) {
      masterNoteWarn.value = false
      masterNote.value = '加速已全部启动'
    } else {
      masterNoteWarn.value = true
      masterNote.value = `部分未生效：${failed.map(s => `${STEP_LABEL[s.key]}（${s.message || '失败'}）`).join('；')}`
    }
  } catch (e: any) {
    masterNoteWarn.value = true
    masterNote.value = e?.message || '启动失败'
  } finally {
    masterBusy.value = false
  }
}

async function onMasterDisable(): Promise<void> {
  masterBusy.value = true
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
  font-family: var(--font-sans);
}

.page-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}

.page-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
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

.hint {
  font-size: 11px;
  color: var(--text-secondary);
  line-height: 1.6;
}

.hint.warn { color: var(--danger-text); }

.hint code, .status-line code {
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

.measure-chips { display: flex; gap: 6px; }

.m-chip {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
  border: 1px solid var(--border-subtle);
  color: var(--text-secondary);
}
.m-chip.on {
  color: var(--status-success-text);
  border-color: color-mix(in srgb, var(--status-success-text) 40%, transparent);
}

/* ── 链接转换 ── */
.convert-row { display: flex; gap: 8px; }
.convert-result { display: flex; flex-direction: column; gap: 10px; }

.link-group { display: flex; flex-direction: column; gap: 4px; }
.group-title { font-size: 11px; color: var(--text-secondary); }

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

/* ── 状态与操作 ── */
.status-block { display: flex; flex-direction: column; gap: 8px; }

.status-line {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-primary);
  flex-wrap: wrap;
}

.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
}
.dot.on { background: var(--status-success-text); }
.dot.off { background: var(--text-secondary); opacity: .5; }

.action-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }

.text-input {
  flex: 1;
  height: 26px;
  padding: 0 8px;
  border-radius: 4px;
  border: 1px solid var(--border-subtle);
  background: var(--bg-base);
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
  background: var(--bg-base);
  overflow: hidden;
}

.dl-bar-fill {
  height: 100%;
  background: var(--accent-primary);
  border-radius: 2px;
  transition: width 0.25s ease;
}

/* ── hosts 优选 ── */
.hosts-table { display: flex; flex-direction: column; gap: 2px; }

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

.risk-note {
  font-size: 11px;
  color: var(--text-secondary);
  line-height: 1.6;
  padding: 0 2px;
}
</style>
