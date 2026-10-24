<template>
  <div class="git-repos">
    <!-- 工具条：过滤 / 位置切换 / 连接 / 登记 / 刷新（全部只属于仓库子页） -->
    <div class="toolbar">
      <label class="search">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        <input v-model="query" placeholder="按名称或路径过滤…" aria-label="过滤仓库" />
      </label>
      <span class="tb-spacer"></span>
      <select v-model="scopeKey" class="host-sel" aria-label="仓库位置">
        <option value="local">本地</option>
        <option v-for="s in remoteSessions" :key="s.id" :value="s.id">{{ s.name }}</option>
      </select>
      <button class="btn" type="button" @click="showConnPicker = true">连接远程</button>
      <button class="btn btn-primary" type="button" :disabled="!!busy" @click="onAdd">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><line x1="12" y1="11" x2="12" y2="17"/><line x1="9" y1="14" x2="15" y2="14"/></svg>
        添加仓库
      </button>
      <button class="btn icon" type="button" :disabled="loading || !!busy" title="刷新：重新读取各仓库状态" aria-label="刷新" @click="refresh()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/></svg>
      </button>
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
        <div class="empty-actions">
          <button class="btn btn-primary" type="button" @click="onAdd">选择目录登记</button>
        </div>
      </template>
      <template v-else>
        <span>{{ scopeSession?.name || '该主机' }} 尚未登记仓库路径</span>
        <div class="empty-actions">
          <button class="btn btn-primary" type="button" @click="showAddRemote = true">登记仓库路径</button>
        </div>
      </template>
    </div>

    <div v-else class="body">
      <div class="tbl-wrap">
        <table class="tbl">
          <!-- table-layout: fixed + 显式列宽：列宽不随内容/刷新漂移，表头与数据严格对齐 -->
          <colgroup>
            <col /><col class="col-branch" /><col class="col-status" /><col class="col-remote" /><col class="col-when" /><col class="col-act" />
          </colgroup>
          <thead>
            <tr>
              <th>仓库</th>
              <th>分支</th>
              <th>状态</th>
              <th>远程</th>
              <th>最近提交</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="r in filteredRows"
              :key="r.id"
              class="row"
              @click="detailRepo = r"
            >
              <td>
                <div class="r-name">{{ r.name }}</div>
                <div class="r-path" :title="r.path">{{ r.path }}</div>
              </td>
              <td class="r-branch">{{ branchText(r) }}</td>
              <td>
                <span v-if="r.error" class="pill k-err" :title="r.error">取数失败</span>
                <span v-else class="pill" :class="'k-' + repoRowKind(r)">{{ kindText(r) }}</span>
              </td>
              <td>
                <span v-if="r.originHost">
                  <span class="r-host">{{ r.originHost }}</span>
                  <span class="r-slug">{{ r.originSlug }}</span>
                  <span v-if="accelTag(r)" class="r-acc" title="git 读取已走全局镜像加速（insteadOf）">已加速</span>
                </span>
                <span v-else class="r-slug">—</span>
              </td>
              <td class="r-when" :title="r.lastCommit?.subject">
                <template v-if="r.lastCommit">{{ r.lastCommit.relTime }} · {{ r.lastCommit.author }}</template>
                <template v-else-if="!r.error">无提交</template>
              </td>
              <td @click.stop>
                <div class="r-act">
                  <button
                    class="ibtn"
                    type="button"
                    :disabled="!!busy || !!r.error"
                    title="fetch：仅获取远端状态（--all --prune）"
                    aria-label="fetch"
                    @click="onFetch(r)"                  >
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/></svg>
                  </button>
                  <button
                    class="ibtn"
                    type="button"
                    :disabled="!!busy"
                    title="在终端中打开（本地终端 / 新 SSH 会话）"
                    aria-label="在终端中打开"
                    @click="onTerm(r)"
                  >
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m6 8 4 4-4 4"/><line x1="12" y1="16" x2="18" y2="16"/></svg>
                  </button>
                </div>
              </td>
            </tr>
            <tr v-if="!filteredRows.length"><td colspan="6" class="tbl-empty">没有匹配的仓库</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 状态栏：计数跟着当前范围走（Docker 惯例） -->
    <div class="statusbar">
      <span>{{ scopeLabel }}</span>
      <span>仓库 {{ rows.length }}<template v-if="statusParts.length">（{{ statusParts.join(' / ') }}）</template><template v-else>（全部干净）</template></span>
      <span v-if="hostSummary">{{ hostSummary }}</span>
      <span class="sb-right mono">{{ lastSync ? `已同步 ${lastSync}` : '' }}</span>
    </div>

    <!-- 仓库详情：自取数，动作回抛（确认 / 审计 / 锁都在本组件） -->
    <Teleport to="body">
      <GitRepoDetailDialog
        v-if="detailRepo"
        :key="detailRepo.id"
        :repo="detailRepo"
        :accel-active="accelActive"
        :busy="busy"
        @close="detailRepo = null"
        @fetch="detailRepo && onFetch(detailRepo)"
        @pull="detailRepo && onPull(detailRepo)"
        @term="detailRepo && onTerm(detailRepo)"
        @remove="detailRepo && onRemove(detailRepo)"
        @switch="onSwitchCurrent"
        @commit="onCommitCurrent"
        @push="detailRepo && onPush(detailRepo)"
      />
    </Teleport>

    <!-- 连接选择器：复用 Docker 页的同款组件（连接列表是通用形态） -->
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
// GitReposPanel.vue — Git 工具页的「仓库」子页（设计稿 2026-10-24，变体一：分域列表）。
// 位置 = 本地或某台已连接远程会话；本地经 gitrepos:* IPC（主进程 execFile），
// 远程构造 shared/git-repos-core 的命令经 sshExec 下发（同 Docker 路线）。
// 动作锁 / pull 确认 / 远程审计收在本组件；详情弹窗只取数不执行。
import { computed, onMounted, ref, watch } from 'vue'
import { useSSHStore, type SSHConnection, type TerminalSession } from '@/stores/ssh'
import { useToastStore } from '@/stores/toast'
import { useConfirm } from '@/composables/useConfirm'
import { toFriendlyMessage, unwrapIpcErrorEnvelope } from '@/api/client'
import { useGithubAccelStore } from '@/stores/github-accel'
import DockerConnectionPicker from '../docker/DockerConnectionPicker.vue'
import GitRepoDetailDialog from './GitRepoDetailDialog.vue'
import {
  AUDIT_ACTION_GIT,
  auditEnvironmentFor,
  auditOutcomeForDispatchError,
  auditResultForExit,
  buildRemoteAuditRecord,
  writeRemoteAudit,
  type RemoteAuditConclusion,
} from '@/composables/remoteAudit'
import {
  buildRemoteCommitCmd,
  buildRemoteGitActionCmd,
  buildRemotePushCmd,
  buildRemoteRepoStatusCmd,
  buildRemoteRepoVerifyCmd,
  buildRemoteSwitchCmd,
  firstLine,
  friendlyGitStderr,
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
    // 详情弹窗开着时让概要行跟上新数据（弹窗自身取的文件/分支/提交不重取）
    if (detailRepo.value) detailRepo.value = rows.value.find(r => r.id === detailRepo.value?.id) ?? null
  } catch (e) {
    if (pin !== epoch) return
    loadError.value = errMsg(e, '仓库读取失败')
    rows.value = []
  } finally {
    if (pin === epoch) loading.value = false
  }
}

// ── 过滤 / 排序 / 状态栏 ──
const query = ref('')
const filteredRows = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return rows.value
  return rows.value.filter(r => (r.name + ' ' + r.path).toLowerCase().includes(q))
})

function kindText(r: RepoRow): string {
  const k = repoRowKind(r)
  if (k === 'dirty') return `未提交变更 ${r.dirtyFiles}`
  if (k === 'diverge') return `已分叉 ↑${r.ahead}↓${r.behind}`
  if (k === 'behind') return `落后 ↓${r.behind}`
  if (k === 'ahead') return `领先 ↑${r.ahead}`
  if (k === 'none') return '无远程'
  return '干净'
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
        if (r.code !== 0) throw new Error(firstLine(friendlyGitStderr(r.stderr)) || `fetch 退出码 ${r.code}`)
      } else {
        if (!row.sessionId) throw new Error('远端会话不可用')
        const r = await api.sshExec(row.sessionId, buildRemoteGitActionCmd(row.path, 'fetch'), REMOTE_FETCH_SEC)
        if (r.exitCode !== 0) throw new Error(firstLine(r.stderr) || `fetch 退出码 ${r.exitCode}`)
      }
      toast.success(`已 fetch ${row.name}：远端状态已更新`)
      await refresh()
    } catch (e) {
      toast.error(errMsg(e, 'fetch 失败'))
    }
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

// ── 详情弹窗动作的桥接：弹窗只回抛，行对象从 detailRepo 取（模板内不写 TS 标注） ──
function onSwitchCurrent(branch: string): void {
  if (detailRepo.value) void onSwitch(detailRepo.value, branch)
}
function onCommitCurrent(payload: { files: string[]; msg: string }): void {
  if (detailRepo.value) void onCommit(detailRepo.value, payload)
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
        if (r.code !== 0) throw new Error(firstLine(friendlyGitStderr(r.stderr)) || `switch 退出码 ${r.code}`)
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
        if (exitCode !== 0) throw new Error(firstLine(stderr) || `switch 退出码 ${exitCode}`)
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
    const api = window.electronAPI
    if (!api) return
    try {
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
        await refresh()
      } else {
        toast.error(`${row.name}：${out.message}`, 6000)
      }
    } catch (e) {
      toast.error(errMsg(e, '提交失败'))
    }
  })
}

async function onPush(row: RepoRow): Promise<void> {
  const isRemote = row.scope === 'remote'
  if (!row.upstream && !(row.remoteName && row.branch && !row.detached)) {
    toast.error('当前分支没有上游且无法确定远程仓库，无法推送（可先在终端 git push -u 设置）')
    return
  }
  const target = row.upstream || `${row.remoteName}/${row.branch}`
  const ok = await confirm({
    title: '推送到远程（git push）',
    message: `将把分支 ${row.branch} 推送到 ${target}${row.upstream ? '' : '（push -u 建立上游跟踪）'}。推送到共享或受保护分支会影响其他人，请确认。${isRemote ? '操作在远程主机上执行，计入审计。' : ''}`,
    confirmText: '推送',
    danger: isRemote,
  })
  if (!ok) return
  await withBusy(`push:${row.id}`, async () => {
    const api = window.electronAPI
    if (!api) return
    try {
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
        await refresh()
      } else if (out.kind === 'rejected') {
        toast.warning(`${row.name}：${out.message}`, 8000)
      } else {
        toast.error(`${row.name}：${out.message}`, 6000)
      }
    } catch (e) {
      toast.error(errMsg(e, '推送失败'))
    }
  })
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
    // DockerView.doExec 同款：新开一条会话（不占用用户正在敲的 shell），等登录
    // shell 就绪再写 cd；早写会被登录流程吞掉
    const newId = await sshStore.connectRemote(connId, 80, 24)
    emit('exec-terminal', newId)
    await new Promise(r => setTimeout(r, 500))
    await api.sshWrite(newId, `cd ${shSingleQuote(row.path)}\r`)
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
      detailRepo.value = null
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
        throw new Error(firstLine(r.stderr) || '该路径不是 git 工作区')
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

// ── 连接远程（复用 Docker 页的连接选择器） ──
const showConnPicker = ref(false)
const connecting = ref(false)
const pendingConnId = ref('')
async function handleConnect(conn: SSHConnection): Promise<void> {
  if (connecting.value) return
  connecting.value = true
  pendingConnId.value = conn.id
  try {
    // 用 connectRemote 返回的会话 id 直接落为范围（同 Docker：不按 connectionId 回查最早会话）
    scopeKey.value = await sshStore.connectRemote(conn.id, 80, 24)
    showConnPicker.value = false
    toast.success(`已连接 ${conn.name}`)
  } catch (e) {
    toast.error(toFriendlyMessage(e, 'SSH 连接失败'))
  } finally {
    connecting.value = false
    pendingConnId.value = ''
  }
}

// ── 详情弹窗 ──
const detailRepo = ref<RepoRow | null>(null)
// 页面可见性：被 v-show 隐藏时收起弹窗（Teleport 到 body 的浮层会盖到别的页面上，
// Docker 同款处理）；回到前台时刷新一次 —— 状态是快照不是实时，离开期间远端可能已变
watch(() => props.active, (active) => {
  if (active === false) {
    detailRepo.value = null
    return
  }
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
.tb-spacer { flex: 1; }
.search {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  height: 26px;
  padding: 0 10px;
  width: 210px;
  max-width: 40vw;
  background: var(--bg-input);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-md);
  color: var(--text-tertiary);
  cursor: text;
}
.search input { background: transparent; border: none; color: var(--text-primary); font-size: 12px; font-family: var(--font-sans); flex: 1; min-width: 0; outline: none; }
.search input::placeholder { color: var(--text-tertiary); }
.host-sel {
  height: 26px;
  padding: 0 8px;
  max-width: 220px;
  background: var(--bg-input);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-md);
  color: var(--text-primary);
  font-size: 12px;
  font-family: var(--font-sans);
  cursor: pointer;
}
.host-sel option { background: var(--bg-elevated); color: var(--text-primary); }

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
.btn.icon { width: 26px; padding: 0; justify-content: center; }
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

/* ── 表格（table-layout: fixed + colgroup 定宽：列不随内容/刷新漂移） ── */
.body { flex: 1; min-height: 0; display: flex; }
.tbl-wrap { flex: 1; overflow: auto; }
.tbl { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 12px; min-width: 780px; }
.col-branch { width: 120px; }
.col-status { width: 104px; }
.col-remote { width: 200px; }
.col-when { width: 150px; }
.col-act { width: 64px; }
.tbl th {
  text-align: left;
  font-weight: 400;
  color: var(--text-tertiary);
  padding: 7px 12px;
  border-bottom: 1px solid var(--border-subtle);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
}
.tbl td { padding: 7px 12px; border-bottom: 1px solid var(--divider-primary); vertical-align: middle; overflow: hidden; white-space: nowrap; }
.tbl tbody tr.row { cursor: pointer; }
.tbl tbody tr.row:hover td { background: var(--bg-hover); }
.tbl-empty { color: var(--text-tertiary); text-align: center; padding: 28px 12px !important; white-space: normal !important; }

.r-name { color: var(--text-primary); font-weight: 500; line-height: 1.4; }
.r-path {
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  font-size: 11px;
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.r-branch {
  font-family: var(--font-mono);
  color: var(--accent-cyan);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.pill {
  display: inline-flex;
  align-items: center;
  height: 18px;
  padding: 0 8px;
  border-radius: 9px;
  font-size: 11px;
  white-space: nowrap;
}
.k-dirty { background: var(--status-warn-bg); color: var(--status-warn-text); }
.k-diverge { background: color-mix(in srgb, var(--accent-orange) 12%, transparent); color: var(--accent-orange); }
.k-behind { background: color-mix(in srgb, var(--status-info-text) 12%, transparent); color: var(--status-info-text); }
.k-ahead { background: color-mix(in srgb, var(--accent-purple) 12%, transparent); color: var(--accent-purple); }
.k-clean { background: var(--status-success-bg); color: var(--status-success-text); }
.k-none { background: var(--bg-hover); color: var(--text-tertiary); }
.k-err { background: var(--status-error-bg); color: var(--status-error-text); }

.r-host {
  display: inline-flex;
  align-items: center;
  font-size: 11px;
  color: var(--text-secondary);
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  padding: 1px 6px;
  white-space: nowrap;
}
.r-slug { font-family: var(--font-mono); font-size: 11px; color: var(--text-tertiary); white-space: nowrap; }
.r-acc {
  display: inline-block;
  margin-left: 6px;
  font-size: 10px;
  color: var(--accent-green);
  border: 1px solid color-mix(in srgb, var(--accent-green) 35%, transparent);
  border-radius: 3px;
  padding: 0 4px;
  white-space: nowrap;
  vertical-align: 1px;
}
.r-when { color: var(--text-secondary); font-size: 11px; white-space: nowrap; }

.r-act { display: flex; gap: 4px; }
.ibtn {
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  border-radius: var(--radius-sm);
  cursor: pointer;
}
.ibtn:hover:not(:disabled) { background: var(--bg-active); color: var(--text-primary); }
.ibtn:disabled { opacity: .45; cursor: default; }

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
