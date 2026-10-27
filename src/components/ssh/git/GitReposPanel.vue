<template>
  <div class="git-repos">
    <!-- 工具条：过滤 / 登记 / 刷新（全部只属于仓库子页），内容整体居左 -->
    <div class="toolbar">
      <label class="search">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        <input v-model="query" placeholder="过滤仓库…" aria-label="过滤仓库" />
      </label>
      <button
        class="btn"
        type="button"
        :disabled="loading || !!busy || !rows.length"
        title="对当前范围的全部仓库执行 git fetch --all --prune：只更新远端引用，不改动任何工作区"
        @click="onFetchAll"
      >获取全部</button>
      <button class="btn btn-primary" type="button" :disabled="!!busy" @click="onAdd">添加仓库</button>
      <button class="btn" type="button" :disabled="loading || !!busy" title="刷新：重新读取各仓库状态" @click="refresh()">刷新</button>
    </div>

    <!-- 取数错误带（对齐 Docker 错误态） -->
    <div v-if="loadError" class="err-band" role="alert">
      <div class="eb-text">
        <div class="eb-msg">{{ loadError }}</div>
        <div class="eb-hint">本地仓库取数经主进程 execFile；请确认 git 已安装且在 PATH 中。</div>
      </div>
      <button class="btn btn-sm" type="button" @click="refresh()">重试</button>
    </div>

    <div v-else-if="loading && !rows.length" class="load-band">
      <svg class="spinner" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
      正在{{ isLocal ? '读取本地仓库状态' : '经 SSH 会话读取远端仓库状态' }}…
    </div>

    <!-- 空态引导：没有登记过任何仓库时给两个入口，比一行空表格有用 -->
    <div v-else-if="!rows.length && !registeredCount" class="empty">
      <template v-if="isLocal">
        <span>尚未登记本地仓库</span>
      </template>
      <template v-else>
        <span>{{ scopeSession?.name || '该主机' }} 尚未登记仓库路径</span>
        <div class="empty-actions">
          <button class="btn btn-primary" type="button" @click="showAddRemote = true">登记仓库路径</button>
        </div>
      </template>
    </div>

    <!-- 双栏主体：左 = 状态分组仓库列表；右 = 常驻详情面板（变更 / 分支 / 图谱） -->
    <div v-else class="body">
      <aside class="vlist" aria-label="仓库列表">
        <div ref="listEl" class="vlist-scroll" @keydown="onListKeydown">
          <template v-for="g in groupedRows" :key="g.key">
            <div class="vgroup" :class="{ 'g-needs': g.key === 'needs' }">
              <span>{{ g.label }}</span><span class="vgc">{{ g.rows.length }}</span>
            </div>
            <button
              v-for="r in g.rows"
              :key="r.id"
              type="button"
              class="vrow"
              :class="{ sel: r.id === selectedId }"
              :data-id="r.id"
              :aria-current="r.id === selectedId ? 'true' : undefined"
              :title="r.error || r.path"
              @click="selectedId = r.id"
            >
              <span class="vr-l1">
                <span class="vr-name">{{ r.name }}</span>
                <span v-if="accelTag(r)" class="vr-acc" title="git 读取已走全局镜像加速（insteadOf）">已加速</span>
              </span>
              <span v-if="r.error" class="vr-err" :title="r.error">{{ r.error }}</span>
              <span v-else class="vr-l2">
                <span class="vr-branch">{{ branchText(r) }}</span>
                <span v-if="r.dirtyFiles" class="vr-dirty">● {{ r.dirtyFiles }} 改</span>
              </span>
            </button>
          </template>
          <div v-if="!groupedRows.length" class="vlist-empty">没有匹配的仓库</div>
        </div>
      </aside>
      <!-- 详情：自取数，动作回抛（确认 / 审计 / 锁都在本组件） -->
      <GitRepoDetailPanel
        v-if="detailRepo"
        :key="detailRepo.id"
        :repo="detailRepo"
        :accel-active="accelActive"
        :busy="busy"
        @fetch="detailRepo && onFetch(detailRepo)"
        @pull="detailRepo && onPull(detailRepo)"
        @push="detailRepo && onPush(detailRepo)"
        @term="detailRepo && onTerm(detailRepo)"
        @remove="detailRepo && onRemove(detailRepo)"
        @refresh="refresh()"
        @switch="onSwitchCurrent"
        @commit="onCommitCurrent"
        @commit-push="onCommitPushCurrent"
      />
      <div v-else class="vdetail-empty">
        <span>在左侧选择一个仓库</span>
        <span class="vdetail-sub">   </span>
      </div>
    </div>

    <!-- 状态栏：计数跟着当前范围走（Docker 惯例） -->
    <div class="statusbar">
      <span>{{ scopeLabel }}</span>
      <span>仓库 {{ rows.length }}<template v-if="statusParts.length">（{{ statusParts.join(' / ') }}）</template><template v-else>（全部干净）</template></span>
      <span v-if="hostSummary">{{ hostSummary }}</span>
      <span class="sb-right mono">{{ lastSync ? `已同步 ${lastSync}` : '' }}</span>
    </div>

    <!-- 登记远程仓库路径 -->
    <Teleport to="body">
      <div v-if="showAddRemote" class="arm-overlay" @click.self="showAddRemote = false">
        <div class="arm-dialog" role="dialog" aria-modal="true" aria-label="登记远程仓库路径">
          <div class="arm-title">登记远程仓库路径</div>
          <div class="arm-sub">在 {{ scopeSession?.name || '当前主机' }} 上登记一个 git 工作区路径，登记前会先校验该路径。</div>
          <input
            v-model="addRemotePath"
            class="arm-input mono"
            placeholder="/var/www/app"
            spellcheck="false"
            @keyup.enter="onAddRemote"
          />
          <div class="arm-foot">
            <button class="btn" type="button" @click="showAddRemote = false">取消</button>
            <button class="btn btn-primary" type="button" :disabled="!!busy || !addRemotePath.trim()" @click="onAddRemote">校验并登记</button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
// GitReposPanel.vue — Git 页的「仓库」子页（方案 A 双栏工作台，设计稿 2026-10-25）。
// 左列 = 状态分组仓库列表（需处理 / 待拉取 / 待推送 / 无远程 / 干净），
// 右列 = 常驻详情面板 GitRepoDetailPanel（变更 / 分支 / 图谱三页签，替代 V2 的弹窗）。
// 位置 = 本地或某台已连接远程会话；本地经 gitrepos:* IPC（主进程 execFile），
// 远程构造 shared/git-repos-core 的命令经 sshExec 下发（同 Docker 路线）。
// 动作锁 / pull 确认 / 远程审计收在本组件；详情面板只取数不执行。
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useSSHStore, type TerminalSession } from '@/stores/ssh'
import { useToastStore } from '@/stores/toast'
import { useConfirm } from '@/composables/useConfirm'
import { unwrapIpcErrorEnvelope } from '@/api/client'
import { sshSessionIo, writeAfterShellReady } from '@/utils/ssh-handshake'
import { useGithubAccelStore } from '@/stores/github-accel'
import GitRepoDetailPanel from './GitRepoDetailPanel.vue'
import {
  AUDIT_ACTION_GIT,
  auditEnvironmentFor,
  auditOutcomeForDispatchError,
  auditResultForExit,
  buildRemoteAuditRecord,
  writeRemoteAudit,
  type RemoteAuditConclusion,
} from '@/utils/remote-audit'
import {
  buildRemoteCommitCmd,
  buildRemoteGitActionCmd,
  buildRemotePushCmd,
  buildRemoteRepoStatusCmd,
  buildRemoteRepoVerifyCmd,
  buildRemoteSwitchCmd,
  formatFetchFailures,
  friendlyGitError,
  interpretCommit,
  interpretPull,
  interpretPush,
  isValidRemoteRepoPath,
  repoNameOf,
  repoRowKind,
  shSingleQuote,
  sortRepoRows,
  toLocalRepoRow,
  toRemoteRepoRow,
  repoRowId,
  type CommitOutcome,
  type PushOutcome,
  type RepoRow,
  type RepoRowKind,
} from '../../../../shared/git-repos-core'

const props = defineProps<{ active?: boolean }>()
const emit = defineEmits<{ 'exec-terminal': [sessionId: string] }>()

const sshStore = useSSHStore()
const toast = useToastStore()
const { confirm } = useConfirm()
const accelStore = useGithubAccelStore()

// 取数读预算 20s；fetch 60s；pull 含 fetch + 合并给 120s（与主进程服务同口径）
const REMOTE_READ_SEC = 20
const REMOTE_FETCH_SEC = 60
const REMOTE_PULL_SEC = 120
/** 远程 switch / commit 是服务器本地盘操作；push 走网络给 pull 同预算 */
const REMOTE_ACTION_SEC = 60
/** 远程取数并发上限：一条 SSH 连接上的 exec channel 有上限，Docker 取数同款每批 4 条 */
const REMOTE_CONCURRENCY = 4

// ── 范围：'local' 或某个远程会话 id ──
const scopeKey = ref<'local' | string>('local')
const isLocal = computed(() => scopeKey.value === 'local')
const remoteSessions = computed<TerminalSession[]>(() => sshStore.sessions.filter(s => s.type === 'remote'))
const scopeSession = computed(() => remoteSessions.value.find(s => s.id === scopeKey.value) || null)
const scopeLabel = computed(() => isLocal.value ? '本地' : (scopeSession.value?.name || '远程主机'))
// 会话断开后回落本地（同 Docker 的目标校正），避免对着不存在的宿主取数
watch(remoteSessions, (list) => {
  if (scopeKey.value !== 'local' && !list.some(s => s.id === scopeKey.value)) scopeKey.value = 'local'
})

// ── 注册表（本地路径 + 按 connectionId 记忆的远端路径） ──
const localPaths = ref<string[]>([])
const remotePaths = ref<Record<string, string[]>>({})
const registeredCount = computed(() => isLocal.value
  ? localPaths.value.length
  : ((scopeSession.value?.connectionId && remotePaths.value[scopeSession.value.connectionId]) || []).length)

async function loadRegistry(): Promise<void> {
  const api = window.electronAPI
  if (!api) return
  const reg = await api.gitreposGetRegistry()
  localPaths.value = reg.local
  remotePaths.value = reg.remote
}

// ── 列表取数（epoch 守卫：切范围即作废在途回包） ──
const rows = ref<RepoRow[]>([])
const loading = ref(false)
const loadError = ref<string | null>(null)
const lastSync = ref('')
let epoch = 0

/**
 * 错误 → toast 文案：剥掉 IPC 信封（主进程 reject 到渲染端会裹一层
 * "Error invoking remote method …"）；「No handler registered」意味着运行中的
 * 主进程还是旧构建 —— 提示重启而不是让用户对着信封原文猜。
 */
const errMsg = (e: unknown, fallback: string): string => {
  const raw = e instanceof Error ? e.message : (String(e) || fallback)
  const msg = unwrapIpcErrorEnvelope(raw) || fallback
  if (/No handler registered/i.test(msg)) {
    return `${msg}（运行中的应用还是旧构建：请完全退出后重启应用或 npm run dev）`
  }
  return msg
}

function remoteErrorRow(sessionId: string, path: string, msg: string): RepoRow {
  return {
    id: repoRowId('remote', sessionId, path),
    scope: 'remote', sessionId, name: repoNameOf(path), path,
    branch: '', detached: false, upstream: null, ahead: 0, behind: 0, dirtyFiles: 0,
    originUrl: null, remoteName: null, originHost: null, originSlug: null, lastCommit: null, error: msg,
  }
}

async function refresh(): Promise<void> {
  const api = window.electronAPI
  if (!api) return
  const pin = ++epoch
  loading.value = true
  loadError.value = null
  const markSynced = (): void => { lastSync.value = new Date().toLocaleTimeString() }
  try {
    if (isLocal.value) {
      if (!localPaths.value.length) { rows.value = []; markSynced(); return }
      // ref 深层 reactive 后是 Proxy，过不了 contextBridge 的结构化克隆
      // （"An object could not be cloned"）——IPC 传参一律先拷成普通数组
      const results = await api.gitreposStatusAll([...localPaths.value])
      if (pin !== epoch) return
      rows.value = sortRepoRows(results.map(toLocalRepoRow))
    } else {
      const session = scopeSession.value
      if (!session) { rows.value = []; markSynced(); return }
      const paths = (session.connectionId && remotePaths.value[session.connectionId]) || []
      if (!paths.length) { rows.value = []; markSynced(); return }
      // 分批限流：整页仓库一次性全发会挤爆单条 SSH 连接的 exec channel 预算
      const settled: Array<PromiseSettledResult<{ stdout: string; stderr: string; exitCode: number | null }>> = []
      for (let i = 0; i < paths.length; i += REMOTE_CONCURRENCY) {
        const batch = await Promise.allSettled(
          paths.slice(i, i + REMOTE_CONCURRENCY).map(p => api.sshExec(session.id, buildRemoteRepoStatusCmd(p), REMOTE_READ_SEC)),
        )
        if (pin !== epoch) return
        settled.push(...batch)
      }
      rows.value = sortRepoRows(paths.map((p, i) => {
        const r = settled[i]
        return r.status === 'fulfilled'
          ? toRemoteRepoRow(session.id, p, r.value.stdout, r.value.stderr)
          : remoteErrorRow(session.id, p, errMsg(r.reason, '远端取数失败'))
      }))
    }
    markSynced()
  } catch (e) {
    if (pin !== epoch) return
    loadError.value = errMsg(e, '仓库读取失败')
    rows.value = []
  } finally {
    if (pin === epoch) loading.value = false
  }
}

// ── 过滤 / 分组 / 选中 / 状态栏 ──
const query = ref('')
const filteredRows = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return rows.value
  return rows.value.filter(r => (r.name + ' ' + r.path).toLowerCase().includes(q))
})

/** 左列分组：需处理（失败/脏区/分叉）> 待拉取 > 待推送 > 无远程 > 干净（档序 = REPO_KIND_RANK） */
const ROW_GROUPS: Array<{ key: string; label: string; kinds: RepoRowKind[] }> = [
  { key: 'needs', label: '需处理', kinds: ['error', 'dirty', 'diverge'] },
  { key: 'pull', label: '待拉取', kinds: ['behind'] },
  { key: 'push', label: '待推送', kinds: ['ahead'] },
  { key: 'none', label: '无远程', kinds: ['none'] },
  { key: 'clean', label: '干净', kinds: ['clean'] },
]
const groupedRows = computed(() => ROW_GROUPS
  .map(g => ({ ...g, rows: filteredRows.value.filter(r => g.kinds.includes(r.error ? 'error' : repoRowKind(r))) }))
  .filter(g => g.rows.length))

// ── 选中与详情：选中态只存 id，行数据跟着 rows 刷新走（refresh 后自动对齐新对象） ──
const selectedId = ref<string | null>(null)
const detailRepo = computed(() => rows.value.find(r => r.id === selectedId.value) ?? null)

// 列表键盘导航：↑↓ 在分组列表里移动选中（焦点随选中行走，Enter 即原生按钮激活）
const listEl = ref<HTMLElement | null>(null)
function onListKeydown(e: KeyboardEvent): void {
  if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
  const flat = groupedRows.value.flatMap(g => g.rows)
  if (!flat.length) return
  e.preventDefault()
  const i = flat.findIndex(r => r.id === selectedId.value)
  const target = i < 0
    ? (e.key === 'ArrowDown' ? flat[0] : flat[flat.length - 1])
    : flat[Math.min(flat.length - 1, Math.max(0, i + (e.key === 'ArrowDown' ? 1 : -1)))]
  selectedId.value = target.id
  void nextTick(() => listEl.value?.querySelector<HTMLElement>('.vrow.sel')?.focus())
}

function branchText(r: RepoRow): string {
  if (r.error) return '—'
  // detached HEAD：没有分支名可显示，用 git 术语标注游离状态
  if (r.detached) return '(HEAD 游离)'
  let t = r.branch || '—'
  if (r.ahead) t += ` ↑${r.ahead}`
  if (r.behind) t += ` ↓${r.behind}`
  return t
}

// 失败行不走 repoRowKind（error 是行字段不是状态档），这里单独数
const statusParts = computed(() => {
  const counts = { dirty: 0, diverge: 0, behind: 0 } as Record<string, number>
  let failed = 0
  for (const r of rows.value) {
    if (r.error) { failed++; continue }
    const k = repoRowKind(r)
    if (k in counts) counts[k]++
  }
  const parts: string[] = []
  if (failed) parts.push(`${failed} 失败`)
  if (counts.dirty) parts.push(`${counts.dirty} 脏区`)
  if (counts.diverge) parts.push(`${counts.diverge} 分歧`)
  if (counts.behind) parts.push(`${counts.behind} 落后`)
  return parts
})

const hostSummary = computed(() => {
  const hosts = new Map<string, number>()
  let none = 0
  for (const r of rows.value) {
    if (r.error) continue
    if (!r.originHost) { none++; continue }
    hosts.set(r.originHost, (hosts.get(r.originHost) || 0) + 1)
  }
  const parts = [...hosts.entries()].map(([h, n]) => `${h} ×${n}`)
  if (none) parts.push(`无远端 ×${none}`)
  return parts.join(' · ')
})

// ── 与加速子页联动：GitHub origin + 全局 insteadOf 生效 → 「已加速」──
// initHealth 是轻量初始化（TitleBar 同款）：不加载整份加速配置也能知道镜像接管状态
const accelActive = computed(() => !!accelStore.health?.activePrefix)
function accelTag(r: RepoRow): boolean {
  return r.originHost === 'github.com' && accelActive.value
}

// ── 动作锁：任一动作在途锁住全部动作按钮（Docker 同款） ──
const busy = ref('')
async function withBusy(key: string, fn: () => Promise<void>): Promise<void> {
  if (busy.value) return
  busy.value = key
  try { await fn() } finally { busy.value = '' }
}

// ── 审计（仅远程 pull：fetch 属读路径不写审计，与 Docker 读路径同口径） ──
function writeGitAudit(row: RepoRow, cmd: string, conclusion: RemoteAuditConclusion, result: string): void {
  const session = row.sessionId ? sshStore.sessions.find(s => s.id === row.sessionId) : null
  const conn = session?.connectionId ? sshStore.connections.find(c => c.id === session.connectionId) : undefined
  void writeRemoteAudit(buildRemoteAuditRecord({
    action: AUDIT_ACTION_GIT,
    conclusion,
    targetId: conn ? `${conn.name} (${conn.username}@${conn.host})` : (session?.shell || session?.name || row.sessionId || ''),
    environment: auditEnvironmentFor(session, sshStore.connections),
    command: cmd,
    result,
  })).then(ok => { if (!ok) toast.warning('审计记录写入失败：本次 Git 操作未进入审计链') })
}

// ── 动作 ──
async function onFetch(row: RepoRow): Promise<void> {
  await withBusy(`fetch:${row.id}`, async () => {
    const api = window.electronAPI
    if (!api) return
    try {
      if (row.scope === 'local') {
        const r = await api.gitreposFetchOne(row.path)
        if (r.code !== 0) throw new Error(friendlyGitError(r.stderr, `fetch 退出码 ${r.code}`))
      } else {
        if (!row.sessionId) throw new Error('远端会话不可用')
        const r = await api.sshExec(row.sessionId, buildRemoteGitActionCmd(row.path, 'fetch'), REMOTE_FETCH_SEC)
        if (r.exitCode !== 0) throw new Error(friendlyGitError(r.stderr, `fetch 退出码 ${r.exitCode}`))
      }
      toast.success(`已 fetch ${row.name}：远端状态已更新`)
      await refresh()
    } catch (e) {
      toast.error(errMsg(e, 'fetch 失败'))
    }
  })
}

/**
 * 获取全部：对当前范围全部仓库 fetch --all --prune。fetch 属读路径（只更新
 * 远端跟踪引用，不动工作区），无需确认也不写审计；本地并发发 IPC，远程沿用
 * 分批限流（同 refresh 口径）。失败按仓库名收进明细（stderr 经友好化翻译），
 * 完成一条汇总 toast，再刷新列表。
 */
async function onFetchAll(): Promise<void> {
  const targets = rows.value.filter(r => !r.error)
  if (!targets.length) return
  await withBusy('fetch-all', async () => {
    const api = window.electronAPI
    if (!api) return
    let ok = 0
    const fails: string[] = []
    const record = (name: string, good: boolean, err: string): void => {
      if (good) { ok++; return }
      fails.push(`${name}：${err || '失败原因未知'}`)
    }
    try {
      if (isLocal.value) {
        const settled = await Promise.allSettled(targets.map(r => api.gitreposFetchOne(r.path)))
        settled.forEach((s, i) => record(
          targets[i].name,
          s.status === 'fulfilled' && s.value.code === 0,
          s.status === 'rejected' ? errMsg(s.reason, 'fetch 失败') : friendlyGitError(s.value.stderr),
        ))
      } else {
        const session = scopeSession.value
        if (!session) return
        for (let i = 0; i < targets.length; i += REMOTE_CONCURRENCY) {
          const slice = targets.slice(i, i + REMOTE_CONCURRENCY)
          const batch = await Promise.allSettled(
            slice.map(r => r.sessionId
              ? api.sshExec(r.sessionId, buildRemoteGitActionCmd(r.path, 'fetch'), REMOTE_FETCH_SEC)
              : Promise.reject(new Error('远端会话不可用'))),
          )
          batch.forEach((s, bi) => record(
            slice[bi].name,
            s.status === 'fulfilled' && s.value.exitCode === 0,
            s.status === 'rejected' ? errMsg(s.reason, 'fetch 失败') : friendlyGitError(s.value.stderr),
          ))
        }
      }
    } catch (e) {
      toast.error(errMsg(e, '获取失败'))
      return
    }
    if (ok && !fails.length) toast.success(`已获取 ${ok} 个仓库：远端引用已更新`)
    else if (ok) toast.warning(`获取完成：成功 ${ok}，失败 ${fails.length} · ${formatFetchFailures(fails)}`, 8000)
    else toast.error(`获取失败：${fails.length} 个仓库 · ${formatFetchFailures(fails)}`, 8000)
    await refresh()
  })
}

async function onPull(row: RepoRow): Promise<void> {
  const isRemote = row.scope === 'remote'
  const ok = await confirm({
    title: '拉取仓库',
    message: isRemote
      ? `将在远程主机上执行 git pull --ff-only，快进合并 ${row.path} 的远端提交（部署语义，计入审计）。本地与远端分叉时不自动合并，会失败并提示手动处理。`
      : `将对 ${row.name} 执行 git pull --ff-only，快进合并远端${row.behind ? ` ${row.behind} 个提交` : '更新'}。本地与远端分叉时不自动合并，会失败并提示手动处理。`,
    confirmText: '确认拉取',
    danger: isRemote,
  })
  if (!ok) return
  await withBusy(`pull:${row.id}`, async () => {
    const api = window.electronAPI
    if (!api) return
    const cmd = `git -C ${shSingleQuote(row.path)} pull --ff-only`
    try {
      let stdout = '', stderr = '', code: number | null = null
      if (row.scope === 'local') {
        const r = await api.gitreposPullOne(row.path)
        stdout = r.stdout; stderr = r.stderr; code = r.code
      } else {
        if (!row.sessionId) throw new Error('远端会话不可用')
        const started = Date.now()
        try {
          const r = await api.sshExec(row.sessionId, buildRemoteGitActionCmd(row.path, 'pull'), REMOTE_PULL_SEC)
          stdout = r.stdout; stderr = r.stderr; code = r.exitCode
          writeGitAudit(row, cmd, 'executed', auditResultForExit(r.exitCode, Date.now() - started))
        } catch (e) {
          const outcome = auditOutcomeForDispatchError(e, errMsg(e, '拉取失败'), REMOTE_PULL_SEC)
          writeGitAudit(row, cmd, outcome.conclusion, outcome.result)
          throw e
        }
      }
      const out = interpretPull(stdout, stderr, code)
      if (out.kind === 'failed') toast.error(`${row.name}：${out.message}`, 6000)
      else if (out.kind === 'diverged') toast.warning(`${row.name}：${out.message}`, 8000)
      else toast.success(`${row.name}：${out.message}`)
      await refresh()
    } catch (e) {
      toast.error(errMsg(e, '拉取失败'))
    }
  })
}

// ── 详情面板动作的桥接：面板只回抛，行对象从 detailRepo 取（模板内不写 TS 标注） ──
function onSwitchCurrent(branch: string): void {
  if (detailRepo.value) void onSwitch(detailRepo.value, branch)
}
function onCommitCurrent(payload: { files: string[]; msg: string }): void {
  if (detailRepo.value) void onCommit(detailRepo.value, payload)
}
function onCommitPushCurrent(payload: { files: string[]; msg: string }): void {
  if (detailRepo.value) void onCommitPush(detailRepo.value, payload)
}

/**
 * V2 写动作：切换分支 / 提交所选 / 推送。远程执行 = 部署语义（确认框 danger +
 * 计入审计），本地执行只由 git 自身的安全边界把关（冲突拒绝、不 stash 不丢弃）。
 */
async function onSwitch(row: RepoRow, branch: string): Promise<void> {
  const isRemote = row.scope === 'remote'
  const ok = await confirm({
    title: '切换分支（git switch）',
    message: isRemote
      ? `将在远程主机上把 ${row.path} 切换到分支 ${branch}（git switch）。服务器工作区将切换到该分支，直接影响部署内容，计入审计。`
      : `将把 ${row.name} 切换到分支 ${branch}（git switch）。未提交变更与目标分支冲突时 git 会拒绝，不会丢弃任何改动。`,
    confirmText: '切换',
    danger: isRemote,
  })
  if (!ok) return
  await withBusy(`switch:${row.id}`, async () => {
    const api = window.electronAPI
    if (!api) return
    try {
      if (row.scope === 'local') {
        const r = await api.gitreposSwitchBranch(row.path, branch)
        if (r.code !== 0) throw new Error(friendlyGitError(r.stderr, `switch 退出码 ${r.code}`))
      } else {
        if (!row.sessionId) throw new Error('远端会话不可用')
        const remoteCmd = buildRemoteSwitchCmd(row.path, branch)
        const started = Date.now()
        let stderr = ''
        let exitCode: number | null = null
        try {
          const r = await api.sshExec(row.sessionId, remoteCmd, REMOTE_ACTION_SEC)
          stderr = r.stderr
          exitCode = r.exitCode
          writeGitAudit(row, remoteCmd, 'executed', auditResultForExit(r.exitCode, Date.now() - started))
        } catch (e) {
          const outcome = auditOutcomeForDispatchError(e, errMsg(e, '切换分支失败'), REMOTE_ACTION_SEC)
          writeGitAudit(row, remoteCmd, outcome.conclusion, outcome.result)
          throw e
        }
        if (exitCode !== 0) throw new Error(friendlyGitError(stderr, `switch 退出码 ${exitCode}`))
      }
      toast.success(`${row.name}：已切换到 ${branch}`)
      await refresh()
    } catch (e) {
      toast.error(errMsg(e, '切换分支失败'))
    }
  })
}

async function onCommit(row: RepoRow, payload: { files: string[]; msg: string }): Promise<void> {
  const isRemote = row.scope === 'remote'
  if (isRemote) {
    const ok = await confirm({
      title: '提交远程变更（git commit）',
      message: `将在远程主机上以「${payload.msg}」提交 ${payload.files.length} 个文件（git add + commit -- pathspec）。服务器工作区将产生新提交，计入审计。`,
      confirmText: '提交',
      danger: true,
    })
    if (!ok) return
  }
  await withBusy(`commit:${row.id}`, async () => {
    try {
      if (await execCommit(row, payload)) await refresh()
    } catch (e) {
      toast.error(errMsg(e, '提交失败'))
    }
  })
}

/** 提交执行体（确认 / 动作锁之外的部分）：成功 toast 并返回 true，失败 toast 后返回 false */
async function execCommit(row: RepoRow, payload: { files: string[]; msg: string }): Promise<boolean> {
  const api = window.electronAPI
  if (!api) return false
  let out: CommitOutcome
  if (row.scope === 'local') {
    const r = await api.gitreposCommit(row.path, payload.files, payload.msg)
    out = interpretCommit(r.stdout, r.stderr, r.code)
  } else {
    if (!row.sessionId) throw new Error('远端会话不可用')
    const remoteCmd = buildRemoteCommitCmd(row.path, payload.files, payload.msg)
    const started = Date.now()
    let stdout = '', stderr = '', exitCode: number | null = null
    try {
      const r = await api.sshExec(row.sessionId, remoteCmd, REMOTE_ACTION_SEC)
      stdout = r.stdout
      stderr = r.stderr
      exitCode = r.exitCode
      writeGitAudit(row, remoteCmd, 'executed', auditResultForExit(r.exitCode, Date.now() - started))
    } catch (e) {
      const outcome = auditOutcomeForDispatchError(e, errMsg(e, '提交失败'), REMOTE_ACTION_SEC)
      writeGitAudit(row, remoteCmd, outcome.conclusion, outcome.result)
      throw e
    }
    out = interpretCommit(stdout, stderr, exitCode)
  }
  if (out.kind === 'committed') {
    toast.success(`${row.name}：${out.message}`)
    return true
  }
  toast.error(`${row.name}：${out.message}`, 6000)
  return false
}

/** 推送目标：上游 > 远程名/当前分支（push -u 兜底）；无法确定时为 null */
function pushTarget(row: RepoRow): string | null {
  if (row.upstream) return row.upstream
  if (row.remoteName && row.branch && !row.detached) return `${row.remoteName}/${row.branch}`
  return null
}

/** 提交并推送：一次确认覆盖两步（提交成功才推送）；远程两步都走审计 */
async function onCommitPush(row: RepoRow, payload: { files: string[]; msg: string }): Promise<void> {
  const isRemote = row.scope === 'remote'
  const target = pushTarget(row)
  if (!target) {
    toast.error('当前分支没有上游且无法确定远程仓库，无法「提交并推送」；可先「提交所选」，或在终端 git push -u 设置')
    return
  }
  const ok = await confirm({
    title: '提交并推送',
    message: `将以「${payload.msg}」提交 ${payload.files.length} 个文件，随后把分支 ${row.branch} 推送到 ${target}${row.upstream ? '' : '（git push -u 建立上游跟踪）'}。提交成功才会推送。${isRemote ? '两步都在远程主机上执行，计入审计。' : ''}`,
    confirmText: '提交并推送',
    danger: isRemote,
  })
  if (!ok) return
  await withBusy(`commit-push:${row.id}`, async () => {
    try {
      if (!await execCommit(row, payload)) return
      await execPush(row)
      await refresh()
    } catch (e) {
      toast.error(errMsg(e, '提交并推送失败'))
    }
  })
}

async function onPush(row: RepoRow): Promise<void> {
  const isRemote = row.scope === 'remote'
  const target = pushTarget(row)
  if (!target) {
    toast.error('当前分支没有上游且无法确定远程仓库，无法推送（可先在终端 git push -u 设置）')
    return
  }
  const ok = await confirm({
    title: '推送到远程（git push）',
    message: `将把分支 ${row.branch} 推送到 ${target}${row.upstream ? '' : '（push -u 建立上游跟踪）'}。推送到共享或受保护分支会影响其他人，请确认。${isRemote ? '操作在远程主机上执行，计入审计。' : ''}`,
    confirmText: '推送',
    danger: isRemote,
  })
  if (!ok) return
  await withBusy(`push:${row.id}`, async () => {
    try {
      await execPush(row)
      await refresh()
    } catch (e) {
      toast.error(errMsg(e, '推送失败'))
    }
  })
}

/** 推送执行体（确认 / 动作锁 / 上游判据之外的部分）：结果分级 toast */
async function execPush(row: RepoRow): Promise<void> {
  const api = window.electronAPI
  if (!api) return
  let out: PushOutcome
  if (row.scope === 'local') {
    const r = await api.gitreposPush(row.path)
    out = interpretPush(r.stdout, r.stderr, r.code)
  } else {
    if (!row.sessionId || !row.remoteName || !row.branch) throw new Error('远端缺少远程名或分支信息，无法构造推送命令')
    const remoteCmd = row.upstream
      ? `git -C ${shSingleQuote(row.path)} push`
      : buildRemotePushCmd(row.path, { remoteName: row.remoteName, branch: row.branch })
    const started = Date.now()
    let stdout = '', stderr = '', exitCode: number | null = null
    try {
      const r = await api.sshExec(row.sessionId, remoteCmd, REMOTE_PULL_SEC)
      stdout = r.stdout
      stderr = r.stderr
      exitCode = r.exitCode
      writeGitAudit(row, remoteCmd, 'executed', auditResultForExit(r.exitCode, Date.now() - started))
    } catch (e) {
      const outcome = auditOutcomeForDispatchError(e, errMsg(e, '推送失败'), REMOTE_PULL_SEC)
      writeGitAudit(row, remoteCmd, outcome.conclusion, outcome.result)
      throw e
    }
    out = interpretPush(stdout, stderr, exitCode)
  }
  if (out.kind === 'pushed') {
    toast.success(`${row.name}：${out.message}`)
  } else if (out.kind === 'rejected') {
    toast.warning(`${row.name}：${out.message}`, 8000)
  } else {
    toast.error(`${row.name}：${out.message}`, 6000)
  }
}

async function onTerm(row: RepoRow): Promise<void> {
  try {
    if (row.scope === 'local') {
      // 本地终端直落仓库目录（createLocalTerminal 的 cwd 经 pty:create 原生支持）
      const id = await sshStore.createLocalTerminal('PowerShell', row.path)
      emit('exec-terminal', id)
      return
    }
    const session = row.sessionId ? sshStore.sessions.find(s => s.id === row.sessionId) : null
    const connId = session?.connectionId
    const api = window.electronAPI
    if (!session || !connId || !api) { toast.error('远端会话不可用'); return }
    // DockerView.doExec 同款：新开一条会话（不占用用户正在敲的 shell），确认式
    // 握手（utils/ssh-handshake）等登录 shell 真正就绪再写 cd —— 早写会被登录
    // 流程吞掉，固定延时在慢主机（横幅/profile 超 500ms）上纯靠猜；探针超时
    // 回落为固定延时照常写入并提示，握手内保留 500ms 地板等新标签挂载订阅
    const newId = await sshStore.connectRemote(connId, 80, 24)
    emit('exec-terminal', newId)
    const io = sshSessionIo(newId)
    if (!io) throw new Error('Electron API 未就绪，无法写入终端') // api 已在上文校验
    const { confirmed } = await writeAfterShellReady(io, `cd ${shSingleQuote(row.path)}\r`)
    if (!confirmed) toast.warning('远程主机登录较慢，命令已按固定延时写入；若终端未见执行请重试')
  } catch (e) {
    toast.error(errMsg(e, '打开终端失败'))
  }
}

async function onRemove(row: RepoRow): Promise<void> {
  const ok = await confirm({
    title: '移出仓库列表',
    message: `仅把 ${row.path} 从注册表移除，不删除、不改动任何文件。`,
    confirmText: '移出',
    danger: true,
  })
  if (!ok) return
  await withBusy(`remove:${row.id}`, async () => {
    const api = window.electronAPI
    if (!api) return
    try {
      if (row.scope === 'local') {
        await api.gitreposRemoveLocal(row.path)
      } else {
        const session = row.sessionId ? sshStore.sessions.find(s => s.id === row.sessionId) : null
        if (!session?.connectionId) throw new Error('会话缺少连接配置，无法更新注册表')
        const cur = remotePaths.value[session.connectionId] || []
        await api.gitreposSetRemotePaths(session.connectionId, cur.filter(p => p !== row.path))
      }
      selectedId.value = null
      await loadRegistry()
      await refresh()
      toast.success(`已移出 ${row.name}`)
    } catch (e) {
      toast.error(errMsg(e, '移出失败'))
    }
  })
}

// ── 登记：本地（目录选择器）与远程（路径表单） ──
async function onAdd(): Promise<void> {
  if (!isLocal.value) { addRemotePath.value = ''; showAddRemote.value = true; return }
  await withBusy('add', async () => {
    const api = window.electronAPI
    if (!api) return
    try {
      const dir = await api.gitreposPickDirectory()
      if (!dir) return
      await api.gitreposAddLocal(dir)
      await loadRegistry()
      await refresh()
      toast.success(`已登记 ${repoNameOf(dir)}`)
    } catch (e) {
      toast.error(errMsg(e, '登记失败'))
    }
  })
}

const showAddRemote = ref(false)
const addRemotePath = ref('')
async function onAddRemote(): Promise<void> {
  // 尾部斜杠归一：/var/www/app/ 与 /var/www/app 不该登记成两条
  const p = addRemotePath.value.trim().replace(/\/+$/, '') || '/'
  if (!isValidRemoteRepoPath(p)) {
    toast.error('路径须为服务器上的绝对路径（/ 开头），且不含引号或控制字符')
    return
  }
  await withBusy('add-remote', async () => {
    const api = window.electronAPI
    const session = scopeSession.value
    if (!api || !session) return
    try {
      if (!session.connectionId) throw new Error('当前会话缺少连接配置，无法记忆仓库路径')
      const r = await api.sshExec(session.id, buildRemoteRepoVerifyCmd(p), REMOTE_READ_SEC)
      if (r.exitCode !== 0 || r.stdout.trim() !== 'true') {
        throw new Error(friendlyGitError(r.stderr, '该路径不是 git 工作区'))
      }
      const next = [...new Set([...(remotePaths.value[session.connectionId] || []), p])]
      await api.gitreposSetRemotePaths(session.connectionId, next)
      showAddRemote.value = false
      addRemotePath.value = ''
      await loadRegistry()
      await refresh()
      toast.success(`已登记 ${repoNameOf(p)}`)
    } catch (e) {
      toast.error(errMsg(e, '登记失败'))
    }
  })
}

// ── 详情面板 ──
// 页面可见性：面板是页内布局（不 Teleport），切走不会盖到别的页面；
// 回到前台时刷新一次 —— 状态是快照不是实时，离开期间远端可能已变
watch(() => props.active, (active) => {
  if (active === false) return
  void refresh()
})

// ── 生命周期 ──
onMounted(() => {
  void accelStore.initHealth()
  void loadRegistry().then(() => refresh()).catch(e => {
    loadError.value = errMsg(e, '注册表读取失败')
  })
})
// 切范围即重取；范围变了，登记表单对着旧主机就没有意义了
watch(scopeKey, () => {
  showAddRemote.value = false
  void refresh()
})
</script>

<style scoped>
.git-repos {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  font-family: var(--font-sans);
}

/* ── 工具条（DockerView 同款形态） ── */
.toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--divider-primary);
  flex-shrink: 0;
  flex-wrap: wrap;
}
.search {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  height: 26px;
  padding: 0 10px;
  width: 100px;
  max-width: 40vw;
  background: var(--bg-input);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-md);
  color: var(--text-tertiary);
  cursor: text;
  transition: width var(--transition-fast);
}
/* 静置省地、聚焦展开：输入时回到全宽，长过滤词不用挤着看 */
.search:focus-within { width: 210px; }
.search input { background: transparent; border: none; color: var(--text-primary); font-size: 12px; font-family: var(--font-sans); flex: 1; min-width: 0; outline: none; }
.search input::placeholder { color: var(--text-tertiary); }

.btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 26px;
  padding: 0 10px;
  border: 1px solid var(--border-default);
  background: transparent;
  color: var(--text-primary);
  font-size: 12px;
  font-family: var(--font-sans);
  border-radius: var(--radius-md);
  cursor: pointer;
  white-space: nowrap;
}
.btn:hover:not(:disabled) { background: var(--bg-hover); }
.btn:disabled { opacity: .45; cursor: default; }
.btn.btn-primary { background: var(--accent-primary); border-color: var(--accent-primary); color: var(--accent-on); }
.btn.btn-primary:hover:not(:disabled) { filter: brightness(1.15); }
.btn.btn-sm { height: 22px; padding: 0 8px; font-size: 11px; }

/* ── 错误带 / 加载带 / 空态（对齐 Docker） ── */
.err-band {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  margin: 12px 16px;
  padding: 12px 14px;
  flex-shrink: 0;
  background: color-mix(in srgb, var(--status-error-text) 8%, var(--bg-elevated));
  border: 1px solid color-mix(in srgb, var(--status-error-text) 28%, transparent);
  border-radius: var(--radius-md);
}
.err-band .eb-text { flex: 1; min-width: 0; }
.err-band .eb-msg { font-size: 13px; color: var(--text-primary); line-height: 1.5; }
.err-band .eb-hint { font-size: 12px; color: var(--text-secondary); margin-top: 4px; line-height: 1.5; }

.load-band {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  flex: 1;
  color: var(--text-tertiary);
  font-size: 12px;
}
.spinner { width: 14px; height: 14px; animation: gi-rot 1s linear infinite; }
@keyframes gi-rot { to { transform: rotate(360deg); } }

.empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: var(--text-tertiary);
  font-size: 13px;
}
.empty-actions { display: flex; gap: 8px; }

/* ── 双栏主体：左 = 状态分组列表（粘性组头），右 = 详情面板 ── */
.body { flex: 1; min-height: 0; display: flex; }
.vlist {
  width: 300px;
  flex-shrink: 0;
  border-right: 1px solid var(--divider-primary);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.vlist-scroll { flex: 1; overflow-y: auto; padding-bottom: 8px; }
.vgroup {
  position: sticky;
  top: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 10px 3px 10px;
  font-size: 11px;
  color: var(--text-tertiary);
  background: var(--bg-base);
}
.vgroup.g-needs { color: var(--status-warn-text); }
.vgc {
  background: var(--bg-hover);
  border-radius: 8px;
  padding: 0 6px;
  font-size: 11px;
  color: var(--text-secondary);
}
.vgroup.g-needs .vgc { background: color-mix(in srgb, var(--status-warn-text) 14%, transparent); }

.vrow {
  display: flex;
  flex-direction: column;
  gap: 3px;
  width: 100%;
  padding: 7px 10px;
  border: none;
  border-left: 2px solid transparent;
  background: transparent;
  font-family: var(--font-sans);
  text-align: left;
  cursor: pointer;
}
.vrow:hover { background: var(--bg-hover); }
.vrow.sel { background: var(--bg-active); border-left-color: var(--accent-primary); }
.vr-l1 { display: flex; align-items: center; gap: 6px; min-width: 0; }
.vr-name {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.vr-acc {
  flex-shrink: 0;
  font-size: 10px;
  color: var(--accent-green);
  border: 1px solid color-mix(in srgb, var(--accent-green) 35%, transparent);
  border-radius: 3px;
  padding: 0 4px;
  white-space: nowrap;
}
.vr-l2 { display: flex; align-items: center; gap: 7px; min-width: 0; font-size: 11px; }
.vr-branch {
  font-family: var(--font-mono);
  color: var(--accent-cyan);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.vr-dirty { color: var(--status-warn-text); white-space: nowrap; }
.vr-err {
  font-size: 11px;
  color: var(--status-error-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.vlist-empty { padding: 28px 12px; text-align: center; color: var(--text-tertiary); font-size: 12px; }

/* 详情未选中空态：双向居中（DESIGN.md 空状态规范） */
.vdetail-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  color: var(--text-tertiary);
  font-size: 13px;
}
.vdetail-sub { font-size: 11px; }

/* ── 状态栏 ── */
.statusbar {
  height: 24px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 0 16px;
  border-top: 1px solid var(--divider-primary);
  font-size: 11px;
  color: var(--text-tertiary);
  user-select: none;
  white-space: nowrap;
  overflow: hidden;
}
.sb-right { margin-left: auto; color: var(--text-secondary); }

/* ── 登记远程路径弹窗 ── */
.arm-overlay {
  position: fixed;
  inset: 0;
  z-index: 90;
  background: var(--scrim);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.arm-dialog {
  width: min(420px, 100%);
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.arm-title { font-size: 14px; font-weight: 600; color: var(--text-primary); }
.arm-sub { font-size: 12px; color: var(--text-secondary); line-height: 1.5; }
.arm-input {
  height: 28px;
  padding: 0 10px;
  background: var(--bg-input);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-md);
  color: var(--text-primary);
  font-size: 12px;
  outline: none;
}
.arm-input:focus { border-color: var(--accent-primary); }
.arm-foot { display: flex; gap: 8px; justify-content: flex-end; }

.mono { font-family: var(--font-mono); }
</style>
