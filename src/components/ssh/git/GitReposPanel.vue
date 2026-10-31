<template>
  <div class="git-repos">
    <!-- 工具条：过滤 / 添加 / 刷新 / 私人令牌（全部只属于仓库子页），内容整体居左 -->
    <div class="toolbar">
      <label class="search">
        <input v-model="query" placeholder="过滤仓库…" aria-label="过滤仓库" />
      </label>
      <button class="btn btn-primary" type="button" :disabled="!!busy" @click="onAdd">添加仓库</button>
      <button class="btn" type="button" :disabled="loading || !!busy" title="刷新：重新读取各仓库状态" @click="refresh()">刷新</button>
      <button class="btn" type="button" title="私人令牌（PAT）：HTTPS 推拉私有仓库的鉴权凭据，按平台/域名保存" @click="openTokens">私人令牌</button>
    </div>

    <!-- 巡检失败（不遮住列表：仓库状态本身读得到，只是远端一致性这一层缺数据） -->
    <div v-if="probeError" class="probe-band" role="alert">
      <span class="pb-text">{{ probeError }}</span>
      <button class="btn btn-sm" type="button" :disabled="probing" @click="probeEndpoints()">重试巡检</button>
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

    <!-- 双栏主体：左 = 状态分组仓库列表；右 = 常驻详情面板（变更 / 远端 / 分支 / 图谱） -->
    <div v-else class="body">
      <aside class="vlist" aria-label="仓库列表">
        <div ref="listEl" class="vlist-scroll" @keydown="onListKeydown">
          <template v-for="g in groupedRows" :key="g.key">
            <div class="vgroup" :class="{ 'g-needs': g.key === 'needs' || g.key === 'sync' }">
              <span>{{ g.label }}</span>
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
      <!-- 详情：自取数，动作回抛（确认 / 审计 / 锁都在本组件）；端点快照也由本组件巡检持有 -->
      <GitRepoDetailPanel
        v-if="detailRepo"
        :key="detailRepo.id"
        :repo="detailRepo"
        :accel-active="accelActive"
        :busy="busy"
        :endpoints="endpointsOf(detailRepo)"
        :sync-available="syncAvailable"
        :probing="probing"
        :probe-label="probeLabel"
        @fetch="detailRepo && onFetch(detailRepo)"
        @pull="detailRepo && onPull(detailRepo)"
        @push="detailRepo && onPush(detailRepo)"
        @term="detailRepo && onTerm(detailRepo)"
        @remove="detailRepo && onRemove(detailRepo)"
        @refresh="refresh()"
        @switch="onSwitchCurrent"
        @commit="onCommitCurrent"
        @commit-push="onCommitPushCurrent"
        @merge="(remote: string) => detailRepo && onMerge(detailRepo, remote)"
        @probe="probeEndpoints()"
      />
      <div v-else class="vdetail-empty">
        <span>在左侧选择一个仓库</span>
        <span class="vdetail-sub">   </span>
      </div>
    </div>

    <!-- 登记远程仓库路径 -->
    <Teleport to="body">
      <div v-if="showAddRemote" class="arm-overlay" @click.self="showAddRemote = false">
        <div ref="addRemoteRef" class="arm-dialog" role="dialog" aria-modal="true" aria-label="登记远程仓库路径" tabindex="-1">
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

    <!--
      私人令牌（PAT）管理：自「加速」子页迁入 —— PAT 是 HTTPS 鉴权能力，不是加速能力。
      配置存储（github-accel.json）与 IPC（ghaccel:getTokens/saveTokens）不变；
      弹窗复用本页 arm-overlay/arm-dialog 模态框样式（登记远程路径同款）。
    -->
    <Teleport to="body">
      <div v-if="showTokens" class="arm-overlay" @click.self="showTokens = false">
        <div ref="tokenDialogRef" class="arm-dialog wide" role="dialog" aria-modal="true" aria-label="私人令牌" tabindex="-1">
          <div class="arm-title-row">
            <div class="arm-title">私人令牌（PAT）</div>
            <span v-if="accelStore.privateTokens.length" class="token-count" title="已配置的令牌数">{{ accelStore.privateTokens.length }}</span>
            <span class="grow"></span>
            <button v-if="!showTokenForm" class="btn btn-primary btn-sm" type="button" @click="toggleTokenForm">添加令牌</button>
          </div>
          <div class="arm-sub">按平台/域名保存，HTTPS 推拉私有仓库时自动注入鉴权；仅存本地 github-accel.json，切勿把 PAT 发给公共代理。</div>

          <!-- 现有令牌列表（空 token 显示「未配置」而非真实内容；label 默认取平台名，与平台重复时不重复展示） -->
          <div v-if="accelStore.privateTokens.length" class="token-list">
            <div v-for="tok in accelStore.privateTokens" :key="tok.id" class="token-row">
              <span class="token-provider" :class="{ custom: tok.provider === 'custom' }">{{ providerLabel(tok.provider) }}</span>
              <span class="token-id">
                <code class="token-host mono" :title="tok.host">{{ tok.host }}</code>
                <span v-if="tok.label && tok.label !== providerLabel(tok.provider)" class="token-title" :title="tok.label">{{ tok.label }}</span>
              </span>
              <code class="token-value mono">{{ tok.token ? accelStore.maskToken(tok.token) : '未配置' }}</code>
              <span class="token-status" :class="tok.token ? 'on' : 'off'"><i class="status-dot"></i>{{ tok.token ? '已配置' : '未启用' }}</span>
              <span class="token-acts">
                <button class="btn btn-sm btn-ghost" type="button" @click="startTokenEdit(tok)">编辑</button>
                <button v-if="tok.token" class="btn btn-sm btn-ghost" type="button" @click="copyToken(tok)">复制</button>
                <button class="btn btn-sm btn-danger" type="button" @click="removeToken(tok.id)">删除</button>
              </span>
            </div>
          </div>
          <div v-else class="token-empty">
            <svg class="token-empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>
            <div class="token-empty-title">尚未配置任何令牌</div>
            <div class="token-empty-sub">HTTPS 推拉私有仓库时会因 401 失败；点右上角「添加令牌」填入 PAT 即可自动鉴权。</div>
          </div>

          <!-- 添加/编辑表单（同一表单双向复用：tokenEditingId 非空为编辑，否则为新增） -->
          <div v-if="showTokenForm" class="token-form">
            <div class="form-row">
              <label class="form-label">平台</label>
              <div class="form-field">
                <select v-model="tokenDraft.provider" class="arm-input" @change="onProviderChange">
                  <option v-for="p in PROVIDERS" :key="p" :value="p">{{ providerLabel(p) }}</option>
                </select>
              </div>
            </div>
            <div class="form-row">
              <label class="form-label">域名</label>
              <div class="form-field">
                <input
                  v-model="tokenDraft.host"
                  class="arm-input mono"
                  :disabled="tokenDraft.provider !== 'custom'"
                  placeholder="如 git.example.com"
                  spellcheck="false"
                />
                <span v-if="tokenDraft.provider !== 'custom'" class="field-note">预设平台域名不可改，需要自定义请选「自定义」</span>
              </div>
            </div>
            <div class="form-row">
              <label class="form-label">标签</label>
              <div class="form-field">
                <input v-model="tokenDraft.label" class="arm-input" placeholder="默认为平台名" spellcheck="false" />
              </div>
            </div>
            <div class="form-row">
              <label class="form-label">令牌</label>
              <div class="form-field">
                <div class="token-input-wrap">
                  <input
                    v-model="tokenDraft.token"
                    class="arm-input mono"
                    :type="showTokenPlain ? 'text' : 'password'"
                    placeholder="PAT（如 ByytV7oivReuuWcsPJKF-oqX）"
                    autocomplete="off"
                    spellcheck="false"
                  />
                  <button class="btn btn-sm btn-ghost" type="button" tabindex="-1" @click="showTokenPlain = !showTokenPlain">{{ showTokenPlain ? '隐藏' : '显示' }}</button>
                </div>
              </div>
            </div>
            <div class="form-hint">各平台私人令牌可在「设置 → 开发者 → Personal access tokens」生成；仅本地保存，不落 git。</div>
            <div v-if="tokenError" class="token-error">{{ tokenError }}</div>
            <div class="form-actions">
              <button class="btn btn-primary btn-sm" type="button" :disabled="!canSubmitToken" @click="submitToken">{{ tokenEditingId ? '保存修改' : '添加令牌' }}</button>
              <button class="btn btn-sm btn-ghost" type="button" @click="cancelTokenEdit">取消</button>
            </div>
          </div>

          <div class="arm-foot">
            <span class="foot-note">凭据仅存本机 github-accel.json，不进 git 仓库</span>
            <span class="grow"></span>
            <button class="btn" type="button" @click="showTokens = false">关闭</button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
// GitReposPanel.vue — Git 页的「源码管理」子页（方案 A 双栏工作台，设计稿 2026-10-25 / 2026-10-30 v1）。
// 左列 = 状态分组仓库列表（远端待同步 / 待处理 / 无远程 / 干净），
// 右列 = 常驻详情面板 GitRepoDetailPanel（变更 / 远端 / 分支 / 图谱四页签）。
// 位置 = 本地或某台已连接远程会话；本地经 gitrepos:* IPC（主进程 execFile），
// 远程构造 shared/git-repos-core 的命令经 sshExec 下发（同 Docker 路线）。
// 2026-10-30：原「同步」子页并入本页 —— 列表行远端摘要 + 详情远端条/远端页签，
// 巡检（gitrepos:syncProbe）与补齐/合并动作收在本组件，与仓库动作共用一把 busy 锁。
// 动作锁 / pull 确认 / 远程审计收在本组件；详情面板只取数不执行。
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useModalFocusTrap } from '../../../composables/useModalFocusTrap'
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
  type SyncEndpoint,
} from '../../../../shared/git-repos-core'
import type { GitPrivateToken } from '../../../../shared/github-accel-core'

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
      if (!localPaths.value.length) { rows.value = []; pruneEndpoints(); markSynced(); return }
      // ref 深层 reactive 后是 Proxy，过不了 contextBridge 的结构化克隆
      // （"An object could not be cloned"）——IPC 传参一律先拷成普通数组
      const results = await api.gitreposStatusAll([...localPaths.value])
      if (pin !== epoch) return
      rows.value = sortRepoRows(results.map(r => toLocalRepoRow(r)))
      // 仓库已从注册表移出时，旧端点快照会变成幽灵（状态栏仍计数）——本地范围即时剪掉
      pruneEndpoints()
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

/** 左列分组：待处理（失败/脏区/分叉/待推送/待拉取）> 无远程 > 干净（档序 = REPO_KIND_RANK）。
 * 原「待拉取 / 待推送」两个分组已移除：本地与上游的 ahead/behind（未推送/未拉取）
 * 由详情「变更 / 分支」页签承载，列表不再单列——避免与远端巡检（主仓+镜像是否同步）混淆。
 * ahead/behind 的行并入「待处理」组，既不丢行、也不再单独喊「待推送 n」。
 * 本地范围另有「远端待同步」置顶：仓库本身干净（没有要拉/要推的），但主仓或镜像没追上时
 * 只有巡检能发现——这组让它从「干净」里浮上来（进组即从原分组摘掉，不重复列）。 */
const ROW_GROUPS: Array<{ key: string; label: string; kinds: RepoRowKind[] }> = [
  { key: 'needs', label: '待处理', kinds: ['error', 'dirty', 'diverge', 'behind', 'ahead'] },
  { key: 'none', label: '无远程', kinds: ['none'] },
  { key: 'clean', label: '干净', kinds: ['clean'] },
]
const groupedRows = computed(() => {
  const syncRows = syncAvailable.value
    ? filteredRows.value.filter(r => !r.error && repoRowKind(r) === 'clean' && pendingOf(r) > 0)
    : []
  const inSync = new Set(syncRows.map(r => r.id))
  const rest = ROW_GROUPS
    .map(g => ({
      ...g,
      rows: filteredRows.value.filter(r => !inSync.has(r.id) && g.kinds.includes(r.error ? 'error' : repoRowKind(r))),
    }))
    .filter(g => g.rows.length)
  return [
    ...(syncRows.length ? [{ key: 'sync', label: '远端待同步', rows: syncRows }] : []),
    ...rest,
  ]
})

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
  // 行内不再挂 upstream 的 ↑↓（待推送/落后）：未提交与未推送由详情「变更 / 分支」页签承载，
  // 列表只关心分组归属，避免与远端巡检（主仓+镜像是否同步）的语义混淆
  return r.branch || '—'
}

// 失败行不走 repoRowKind（error 是行字段不是状态档），这里单独数。
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

// ── 远端一致性（2026-10-30 原「同步」子页并入本页） ──
// 数据 = gitrepos:syncProbe（逐远端 ls-remote + rev-list 分型，只读不动工作区）。
// 该通道走**本地注册表**，因此只有本地范围有巡检能力 —— 远程范围的远端一致性
// 以「获取」后的 ↑↓ 为准，列表不给远端摘要、详情远端条改给提示。
// 纪律（沿用同步子页）：镜像只允许快进；分叉绝不强推；动作共用一把 busy 锁。
const endpointsByPath = ref<Record<string, SyncEndpoint[]>>({})
const probedAt = ref(0)
const probing = ref(false)
/** 巡检在途时又被请求了一次（动作后的复检）→ 本轮结束后补跑 */
let probeQueued = false
const probeError = ref<string | null>(null)
const syncAvailable = computed(() => isLocal.value)

function endpointsOf(r: RepoRow): SyncEndpoint[] {
  return syncAvailable.value ? (endpointsByPath.value[r.path] ?? []) : []
}
/**
 * 「远端待同步」的入场券：只认 behind / diverged。
 * unknown（探测失败）不是待同步——远端状态未知，把仓库塞进同步组只会给出点不动的按钮。
 */
function pendingOf(r: RepoRow): number {
  return endpointsOf(r).filter(e => e.state === 'behind' || e.state === 'diverged').length
}

function relTime(ms: number): string {
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000))
  if (s < 60) return '刚刚'
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`
  return `${Math.floor(s / 3600)} 小时前`
}
const probeLabel = computed(() => (probedAt.value ? `上次巡检 ${relTime(probedAt.value)}` : '未巡检'))

/** 巡检频率：会话内记忆（不落盘 —— 落盘要进设置存储，另议）；默认自动 5 分，
 * 切换频率的入口随状态栏移除，定时巡检仍按此默认节奏运行。 */
type Freq = 'auto5' | 'auto15' | 'manual'
const freq = ref<Freq>('auto5')
let timer: ReturnType<typeof setInterval> | null = null
function armTimer(): void {
  if (timer) { clearInterval(timer); timer = null }
  const ms = freq.value === 'auto5' ? 5 * 60_000 : freq.value === 'auto15' ? 15 * 60_000 : 0
  // 定时巡检连带刷新仓库状态：远端变了，↑↓ 也常常跟着变
  if (ms) timer = setInterval(() => { if (props.active) { void refresh(); void probeEndpoints() } }, ms)
}

/** 全量巡检：一次 syncProbe 覆盖全部本地仓库（主进程内部逐仓逐远端） */
async function probeEndpoints(): Promise<void> {
  const api = window.electronAPI
  if (!api || !syncAvailable.value || !localPaths.value.length) return
  // 在跑时排队再跑一轮：补齐/合并/批量推送结束后紧跟的巡检如果直接丢弃，
  // 界面就会停在「动作前」的快照上（推完了仍显示落后）
  if (probing.value) { probeQueued = true; return }
  probing.value = true
  probeError.value = null
  try {
    // ref 深层 Proxy 过不了 contextBridge 结构化克隆 —— 传参先拷普通数组
    const results = await api.gitreposSyncProbe([...localPaths.value])
    const map: Record<string, SyncEndpoint[]> = {}
    for (const r of results) map[r.path] = r.endpoints
    endpointsByPath.value = map
    probedAt.value = Math.max(0, ...results.map(r => r.probedAt || 0)) || Date.now()
  } catch (e) {
    probeError.value = errMsg(e, '远端巡检失败')
  } finally {
    probing.value = false
    if (probeQueued) { probeQueued = false; void probeEndpoints() }
  }
}

/** 切回本页时复用新鲜快照：一分钟内已经巡检过就不再打一遍全量 ls-remote */
const PROBE_FRESH_MS = 60_000
async function probeIfStale(): Promise<void> {
  if (probedAt.value && Date.now() - probedAt.value < PROBE_FRESH_MS) return
  await probeEndpoints()
}

/** 单仓重探：补齐/合并后只更新这一仓的端点，避免整页闪烁与二次全量 ls-remote */
async function reprobeOne(path: string): Promise<void> {
  const api = window.electronAPI
  if (!api) return
  try {
    const results = await api.gitreposSyncProbe([path])
    const hit = results[0]
    if (!hit) return
    endpointsByPath.value = { ...endpointsByPath.value, [path]: hit.endpoints }
    probedAt.value = hit.probedAt || Date.now()
  } catch (e) {
    // 补齐/合并已经动过仓库了，复检失败不能反向判成动作失败（toast 会误导用户重复操作）；
    // 只把巡检层的问题记进错误带，端点快照保持动作前那条，由用户点「重试巡检」重取
    probeError.value = errMsg(e, '远端巡检失败')
  }
}

/** 只纠正已有快照的仓库：没巡检过的仓库不在这里开小灶，否则状态栏会出现「1/1」的假全局结论 */
async function reprobeIfTracked(path: string): Promise<void> {
  if (endpointsByPath.value[path]) await reprobeOne(path)
}

/** 剪掉已不在当前列表里的端点快照：仓库被移出后，旧快照会让状态栏继续计数（幽灵端点） */
function pruneEndpoints(): void {
  const live = new Set(rows.value.map(r => r.path))
  const next: Record<string, SyncEndpoint[]> = {}
  for (const [path, eps] of Object.entries(endpointsByPath.value)) {
    if (live.has(path)) next[path] = eps
  }
  endpointsByPath.value = next
}


/** 分叉：先 fetch 再 --ff-only 合并；真分叉时 git 拒绝，交给用户手动处理 */
async function onMerge(r: RepoRow, remote: string): Promise<void> {
  await withBusy(`merge:${r.id}:${remote}`, async () => {
    const api = window.electronAPI
    if (!api) return
    try {
      const res = await api.gitreposSyncMerge(r.path, remote)
      const verdict = interpretPull(res.stdout, res.stderr, res.code)
      if (verdict.kind === 'failed') toast.error(`合并失败：${verdict.message}`)
      else toast.success(verdict.message)
      await reprobeOne(r.path)
      await refresh()
    } catch (e) {
      toast.error(errMsg(e, '合并失败'))
    }
  })
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
      // HEAD 可能前移：端点的落后/分叉判断是跟着本地 HEAD 算的，旧快照已过期
      if (out.kind !== 'failed' && row.scope === 'local') await reprobeIfTracked(row.path)
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
      // 端点快照描述的是上一个分支的远端 SHA，换分支后必须重探，否则远端摘要全在说谎
      if (row.scope === 'local') await reprobeIfTracked(row.path)
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

/** 推送目标：主仓(gitcode 优先) > 上游 > 远程名/当前分支（push -u 兜底）；无法确定时为 null */
function pushTarget(row: RepoRow): string | null {
  if (row.primaryRemote && row.branch && !row.detached) return `${row.primaryRemote}/${row.branch}`
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
      const pushed = await execPush(row)
      await refresh()
      if (pushed && row.scope === 'local') await reprobeIfTracked(row.path)
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
      const pushed = await execPush(row)
      await refresh()
      // 推上去了 = 主仓那个端点已经不是「落后」，不重探就要等到下一轮巡检才更正
      if (pushed && row.scope === 'local') await reprobeIfTracked(row.path)
    } catch (e) {
      toast.error(errMsg(e, '推送失败'))
    }
  })
}

/** 推送执行体（确认 / 动作锁 / 上游判据之外的部分）：结果分级 toast，返回是否真的推上去了 */
async function execPush(row: RepoRow): Promise<boolean> {
  const api = window.electronAPI
  if (!api) return false
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
  return out.kind === 'pushed'
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
      // 新仓库还没有端点快照：补一次单仓探测，否则它要等到下一轮巡检才有远端摘要。
      // 登记的可能是仓库子目录（注册表存原样），所以按「路径前缀最长」反查所属仓库行
      const added = rows.value
        .filter(r => r.path === dir || dir.startsWith(r.path.replace(/\/+$/, '') + '/'))
        .sort((a, b) => b.path.length - a.path.length)[0]
      if (added) void reprobeOne(added.path)
      toast.success(`已登记 ${repoNameOf(dir)}`)
    } catch (e) {
      toast.error(errMsg(e, '登记失败'))
    }
  })
}

const showAddRemote = ref(false)
const addRemotePath = ref('')

const addRemoteRef = ref<HTMLElement | null>(null)
// 打开即聚焦输入框：这个弹窗存在的意义就是敲一行路径
const addRemoteTrap = useModalFocusTrap(addRemoteRef, { initialFocus: '.arm-input' })
watch(showAddRemote, (v) => { if (v) addRemoteTrap.activate(); else addRemoteTrap.deactivate() })
// Esc 与「取消」同一出口（此前这个弹窗没有任何键盘退出路径）
function onAddRemoteKey(e: KeyboardEvent): void {
  if (e.key === 'Escape' && showAddRemote.value) showAddRemote.value = false
}
onMounted(() => window.addEventListener('keydown', onAddRemoteKey))
onUnmounted(() => window.removeEventListener('keydown', onAddRemoteKey))

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

// ── 私人令牌（PAT）管理：自「加速」子页迁入 —— PAT 是 HTTPS 鉴权能力，不是加速能力。
// 配置存储（github-accel.json）与 IPC（ghaccel:getTokens/saveTokens）不变；
// store 的 init() 有 loaded 幂等守卫，首次打开弹窗时补拉一次令牌清单。
const PROVIDERS = ['gitcode', 'github', 'gitee', 'gitea', 'gitlab', 'custom'] as const
type ProviderKind = (typeof PROVIDERS)[number]

function providerLabel(p: string): string {
  const map: Record<string, string> = { github: 'GitHub', gitcode: 'GitCode', gitee: 'Gitee', gitea: 'Gitea', gitlab: 'GitLab', custom: '自定义' }
  return map[p] ?? p
}

const showTokens = ref(false)
const showTokenForm = ref(false)
const showTokenPlain = ref(false)
const tokenError = ref('')
const tokenEditingId = ref<string | null>(null)
const tokenDraft = ref<{ provider: ProviderKind; host: string; label: string; token: string }>({ provider: 'gitcode', host: 'gitcode.com', label: '', token: '' })

const tokenDialogRef = ref<HTMLElement | null>(null)
const tokenTrap = useModalFocusTrap(tokenDialogRef)
watch(showTokens, (v) => { if (v) tokenTrap.activate(); else tokenTrap.deactivate() })
// Esc 与「关闭」同一出口（登记远程路径弹窗同款键盘退出路径）
function onTokenKey(e: KeyboardEvent): void {
  if (e.key === 'Escape' && showTokens.value) showTokens.value = false
}
onMounted(() => window.addEventListener('keydown', onTokenKey))
onUnmounted(() => window.removeEventListener('keydown', onTokenKey))

async function openTokens(): Promise<void> {
  showTokens.value = true
  showTokenForm.value = false
  try {
    await accelStore.init()
  } catch { /* 配置读失败时保留现有缓存；保存动作会带错误反馈 */ }
}

const canSubmitToken = computed(() => tokenDraft.value.host.trim().length > 0 && tokenDraft.value.token.trim().length > 0)

function resetTokenDraft(): void {
  tokenDraft.value = { provider: 'gitcode', host: 'gitcode.com', label: '', token: '' }
  tokenEditingId.value = null
  tokenError.value = ''
  showTokenPlain.value = false
}

function onProviderChange(): void {
  // 切到预设平台时自动填 host；切到 custom 保留旧 host（便于从预设改过去微调）
  if (tokenDraft.value.provider !== 'custom') {
    tokenDraft.value.host = { github: 'github.com', gitcode: 'gitcode.com', gitee: 'gitee.com', gitea: 'gitea.com', gitlab: 'gitlab.com' }[tokenDraft.value.provider] || ''
  }
}

function toggleTokenForm(): void {
  resetTokenDraft()
  showTokenForm.value = true
}

function startTokenEdit(tok: GitPrivateToken): void {
  tokenEditingId.value = tok.id
  tokenDraft.value = { provider: tok.provider as ProviderKind, host: tok.host, label: tok.label, token: tok.token }
  showTokenForm.value = true
  showTokenPlain.value = false
  tokenError.value = ''
}

function cancelTokenEdit(): void {
  showTokenForm.value = false
  resetTokenDraft()
}

async function submitToken(): Promise<void> {
  tokenError.value = ''
  try {
    const id = tokenEditingId.value ?? `tok-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
    const provider = tokenDraft.value.provider
    const host = provider === 'custom' ? tokenDraft.value.host : ({ github: 'github.com', gitcode: 'gitcode.com', gitee: 'gitee.com', gitea: 'gitea.com', gitlab: 'gitlab.com' }[provider] || tokenDraft.value.host)
    const next = accelStore.privateTokens.map(t => {
      if (t.id === id) {
        return { ...t, provider, host, label: tokenDraft.value.label || providerLabel(provider), token: tokenDraft.value.token.trim(), updatedAt: Date.now() }
      }
      return t
    })
    const exists = accelStore.privateTokens.some(t => t.id === id)
    if (!exists) {
      next.push({ id, provider, host, label: tokenDraft.value.label || providerLabel(provider), token: tokenDraft.value.token.trim(), updatedAt: Date.now() })
    }
    await accelStore.saveTokens(next)
    toast.success(exists ? '令牌已更新' : '令牌已添加')
    showTokenForm.value = false
    resetTokenDraft()
  } catch (e: any) {
    tokenError.value = e?.message || '保存失败'
  }
}

async function removeToken(id: string): Promise<void> {
  try {
    await accelStore.saveTokens(accelStore.privateTokens.filter(t => t.id !== id))
    if (tokenEditingId.value === id) cancelTokenEdit()
    toast.success('令牌已删除')
  } catch (e: any) {
    toast.error(e?.message || '删除失败')
  }
}

async function copyToken(tok: { token: string; label: string }): Promise<void> {
  try {
    await navigator.clipboard.writeText(tok.token)
    toast.success(`${tok.label || '令牌'} 已复制到剪贴板`)
  } catch {
    toast.error('复制失败（剪贴板不可用）')
  }
}

// ── 详情面板 ──
// 页面可见性：面板是页内布局（不 Teleport），切走不会盖到别的页面；
// 回到前台时刷新一次 —— 状态是快照不是实时，离开期间远端可能已变（远端一致性同理）
watch(() => props.active, (active) => {
  if (active === false) return
  void refresh()
  // 页面来回切一次就打一遍全量 ls-remote 太重：新鲜快照直接复用，过期才重探
  void probeIfStale()
})

// ── 生命周期 ──
onMounted(() => {
  void accelStore.initHealth()
  armTimer()
  void loadRegistry()
    .then(() => refresh())
    .then(() => probeEndpoints())
    .catch(e => {
      loadError.value = errMsg(e, '注册表读取失败')
    })
})
onUnmounted(() => { if (timer) clearInterval(timer) })
// 切范围即重取；范围变了，登记表单对着旧主机就没有意义了（巡检只在本地范围跑）
watch(scopeKey, () => {
  showAddRemote.value = false
  void refresh().then(() => probeEndpoints())
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
  gap: var(--spacing-10);
  padding: var(--spacing-10) var(--spacing-16);
  border-bottom: 1px solid var(--divider-primary);
  flex-shrink: 0;
  flex-wrap: wrap;
}
.search {
  display: inline-flex;
  align-items: center;
  height: 26px;
  padding: 0 var(--spacing-10);
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
.search input { background: transparent; border: none; color: var(--text-primary); font-size: 0.9231rem; font-family: var(--font-sans); flex: 1; min-width: 0; outline: none; }
.search input::placeholder { color: var(--text-tertiary); }

.btn {
  display: inline-flex;
  align-items: center;
  gap: var(--spacing-6);
  height: 26px;
  padding: 0 var(--spacing-10);
  border: 1px solid var(--border-default);
  background: transparent;
  color: var(--text-primary);
  font-size: 0.9231rem;
  font-family: var(--font-sans);
  border-radius: var(--radius-md);
  cursor: pointer;
  white-space: nowrap;
}
.btn:hover:not(:disabled) { background: var(--bg-hover); }
.btn:disabled { opacity: .45; cursor: default; }
.btn.btn-primary { background: var(--accent-primary); border-color: var(--accent-primary); color: var(--accent-on); }
.btn.btn-primary:hover:not(:disabled) { filter: brightness(1.15); }
.btn.btn-sm { height: 22px; padding: 0 var(--spacing-8); font-size: 0.8462rem; }

/* ── 错误带 / 加载带 / 空态（对齐 Docker） ── */
.err-band {
  display: flex;
  align-items: flex-start;
  gap: var(--spacing-10);
  margin: var(--spacing-12) var(--spacing-16);
  padding: var(--spacing-12) var(--spacing-14);
  flex-shrink: 0;
  background: color-mix(in srgb, var(--status-error-text) 8%, var(--bg-elevated));
  border: 1px solid color-mix(in srgb, var(--status-error-text) 28%, transparent);
  border-radius: var(--radius-md);
}
.err-band .eb-text { flex: 1; min-width: 0; }
.err-band .eb-msg { font-size: 1rem; color: var(--text-primary); line-height: 1.5; }
.err-band .eb-hint { font-size: 0.9231rem; color: var(--text-secondary); margin-top: var(--spacing-4); line-height: 1.5; }

.load-band {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--spacing-8);
  flex: 1;
  color: var(--text-tertiary);
  font-size: 0.9231rem;
}
.spinner { width: 14px; height: 14px; animation: gi-rot 1s linear infinite; }
@keyframes gi-rot { to { transform: rotate(360deg); } }

.empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--spacing-12);
  color: var(--text-tertiary);
  font-size: 1rem;
}
.empty-actions { display: flex; gap: var(--spacing-8); }

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
.vlist-scroll { flex: 1; overflow-y: auto; padding-bottom: var(--spacing-8); }
.vgroup {
  position: sticky;
  top: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: var(--spacing-6);
  padding: 7px var(--spacing-10) 3px var(--spacing-10);
  font-size: 0.8462rem;
  color: var(--text-tertiary);
  background: var(--bg-base);
}
.vgroup.g-needs { color: var(--status-warn-text); }
.vrow {
  display: flex;
  flex-direction: column;
  gap: 3px;
  width: 100%;
  padding: 7px var(--spacing-10);
  border: none;
  border-left: 2px solid transparent;
  background: transparent;
  font-family: var(--font-sans);
  text-align: left;
  cursor: pointer;
}
.vrow:hover { background: var(--bg-hover); }
.vrow.sel { background: var(--bg-active); border-left-color: var(--accent-primary); }
.vr-l1 { display: flex; align-items: center; gap: var(--spacing-6); min-width: 0; }
.vr-name {
  flex: 1;
  min-width: 0;
  font-size: 0.9231rem;
  font-weight: 500;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.vr-acc {
  flex-shrink: 0;
  font-size: 0.7692rem;
  color: var(--accent-green);
  border: 1px solid color-mix(in srgb, var(--accent-green) 35%, transparent);
  border-radius: var(--radius-sm);
  padding: 0 var(--spacing-4);
  white-space: nowrap;
}
.vr-l2 { display: flex; align-items: center; gap: 7px; min-width: 0; font-size: 0.8462rem; }
.vr-branch {
  font-family: var(--font-mono);
  color: var(--accent-cyan);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.vr-dirty { color: var(--status-warn-text); white-space: nowrap; }
.vr-err {
  font-size: 0.8462rem;
  color: var(--status-error-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 巡检失败带：比取数失败轻一档（列表照常可用，只是缺远端那一层） */
.probe-band {
  display: flex;
  align-items: center;
  gap: var(--spacing-10);
  margin: var(--spacing-8) var(--spacing-16) 0;
  padding: var(--spacing-8) var(--spacing-12);
  flex-shrink: 0;
  font-size: 0.9231rem;
  color: var(--text-primary);
  background: color-mix(in srgb, var(--status-warn-text) 10%, var(--bg-elevated));
  border: 1px solid color-mix(in srgb, var(--status-warn-text) 30%, transparent);
  border-radius: var(--radius-md);
}
.probe-band .pb-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.vlist-empty { padding: 28px var(--spacing-12); text-align: center; color: var(--text-tertiary); font-size: 0.9231rem; }

/* 详情未选中空态：双向居中（DESIGN.md 空状态规范） */
.vdetail-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--spacing-6);
  color: var(--text-tertiary);
  font-size: 1rem;
}
.vdetail-sub { font-size: 0.8462rem; }

/* ── 登记远程路径弹窗 ── */
.arm-overlay {
  position: fixed;
  inset: 0;
  z-index: 90;
  background: var(--scrim);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--spacing-24);
}
.arm-dialog {
  width: min(420px, 100%);
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  /* 容器只是初始焦点落点（tabindex=-1），描边交给里面的输入框 */
  outline: none;
  padding: var(--spacing-16) 18px;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-10);
}
.arm-title { font-size: 1.0769rem; font-weight: 600; color: var(--text-primary); }
.arm-sub { font-size: 0.9231rem; color: var(--text-secondary); line-height: 1.5; }
.arm-input {
  height: 28px;
  padding: 0 var(--spacing-10);
  background: var(--bg-input);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-md);
  color: var(--text-primary);
  font-size: 0.9231rem;
  outline: none;
}
.arm-input:focus { border-color: var(--accent-primary); }
.arm-foot { display: flex; gap: var(--spacing-8); justify-content: flex-end; }

/* ── 私人令牌弹窗（宽版 arm-dialog；列表卡片化 + 表单浅底容器，颜色全部走主题令牌） ── */
.arm-dialog.wide { width: min(680px, 100%); }

.arm-title-row { display: flex; align-items: center; gap: var(--spacing-8); }
.arm-title-row .grow { flex: 1; }

.token-count {
  font-size: 0.7692rem;
  font-family: var(--font-mono);
  color: var(--text-secondary);
  background: var(--bg-hover);
  border-radius: var(--radius-pill);
  padding: 1px var(--spacing-8);
}

/* 列表：有边框容器 + 行分隔线 + hover 反馈（同步子页远端卡同款形态） */
.token-list {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  overflow-y: auto;
  max-height: 280px;
}

.token-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-10);
  padding: var(--spacing-8) var(--spacing-12);
  transition: background 0.12s ease;
}
.token-row + .token-row { border-top: 1px solid var(--divider-primary); }
.token-row:hover { background: var(--bg-hover); }

/* 平台章：状态 tag 同源令牌；custom 域名走 indigo 区分 */
.token-provider {
  flex-shrink: 0;
  min-width: 60px;
  text-align: center;
  font-size: 0.7692rem;
  font-weight: 500;
  color: var(--status-tag-text);
  background: var(--status-tag-bg);
  border: 1px solid var(--status-tag-border);
  border-radius: var(--radius-pill);
  padding: 1px var(--spacing-8);
  line-height: 18px;
}
.token-provider.custom {
  color: var(--accent-indigo);
  background: var(--accent-indigo-bg);
  border-color: color-mix(in srgb, var(--accent-indigo) 32%, transparent);
}

/* host 是身份、label 是别名（默认与平台名重复时不重复展示） */
.token-id { display: flex; align-items: baseline; gap: var(--spacing-8); flex: 0 1 auto; min-width: 0; }
.token-host {
  font-size: 0.8462rem;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.token-title {
  flex-shrink: 1;
  font-size: 0.7692rem;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 脱敏值：mono 胶囊（bg-input 底），不抢视觉 */
.token-value {
  flex: 1;
  min-width: 120px;
  font-size: 0.8462rem;
  color: var(--text-tertiary);
  background: var(--bg-input);
  border-radius: var(--radius-pill);
  padding: 2px var(--spacing-10);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 状态：点 + 文案，替代原来的描边小签（降噪） */
.token-status {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: var(--spacing-6);
  font-size: 0.7692rem;
  color: var(--text-tertiary);
  white-space: nowrap;
}
.token-status.on { color: var(--status-success-text); }
.status-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--text-tertiary); opacity: .45; }
.token-status.on .status-dot { background: var(--status-success-text); opacity: 1; }

.token-acts { display: flex; gap: var(--spacing-4); flex-shrink: 0; }

/* 空态：锁图标 + 主句 + 副句，居中留白 */
.token-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--spacing-6);
  padding: var(--spacing-24) var(--spacing-16);
  text-align: center;
}
.token-empty-icon { width: 28px; height: 28px; color: var(--text-tertiary); opacity: .7; }
.token-empty-title { font-size: 0.9231rem; color: var(--text-secondary); }
.token-empty-sub {
  font-size: 0.8462rem;
  color: var(--text-tertiary);
  line-height: 1.6;
  max-width: 360px;
}

/* 表单：浅底卡片化容器，与列表容器同圆角 */
.token-form {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-10);
  padding: var(--spacing-12);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--bg-base) 45%, transparent);
}

.form-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-10);
  min-height: 30px;
}

.form-label {
  flex: 0 0 56px;
  font-size: 0.8462rem;
  color: var(--text-secondary);
}

.form-field { flex: 1; min-width: 0; display: flex; align-items: center; gap: var(--spacing-8); }
.form-field .arm-input { flex: 1; min-width: 0; width: auto; }
.form-field select.arm-input { font-family: var(--font-sans); }
.field-note { flex-shrink: 0; font-size: 0.7692rem; color: var(--text-tertiary); }

.token-input-wrap { flex: 1; display: flex; align-items: center; gap: var(--spacing-6); min-width: 0; }
.token-input-wrap .arm-input { flex: 1; min-width: 0; }

.form-hint {
  font-size: 0.7692rem;
  color: var(--text-tertiary);
  line-height: 1.5;
  padding-left: calc(56px + var(--spacing-10));
}

/* 校验错误：danger 令牌色带（替代裸文本） */
.token-error {
  padding: var(--spacing-6) var(--spacing-10);
  border: 1px solid var(--danger-border);
  background: var(--danger-bg);
  border-radius: var(--radius-sm);
  font-size: 0.8462rem;
  color: var(--danger-text);
  line-height: 1.5;
}

.form-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--spacing-8);
}

/* 底栏：顶部分隔线 + 左侧说明 + 右侧关闭（只作用于令牌弹窗，不碰登记远程路径弹窗） */
.arm-dialog.wide .arm-foot {
  align-items: center;
  padding-top: var(--spacing-10);
  border-top: 1px solid var(--divider-primary);
}
.arm-dialog.wide .arm-foot .grow { flex: 1; }
.foot-note { font-size: 0.7692rem; color: var(--text-tertiary); }

.mono { font-family: var(--font-mono); }
</style>
