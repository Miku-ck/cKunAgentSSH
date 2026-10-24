<template>
  <div class="grd-overlay" @click.self="emit('close')">
    <div class="grd-dialog" role="dialog" aria-modal="true" :aria-label="'仓库详情：' + repo.name">
      <div class="grd-head">
        <span class="grd-name">{{ repo.name }}</span>
        <span class="grd-pill" :class="kindClass">{{ kindText }}</span>
        <span v-if="repo.scope === 'remote'" class="grd-scope">远程 · SSH 会话</span>
        <button class="grd-icon-btn" title="关闭" aria-label="关闭" @click="emit('close')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="grd-path mono" :title="repo.path">{{ repo.path }}</div>

      <!-- 概要条：分支 / 上游 / 工作区 / 远程仓库（行级读取失败时这些字段无意义，不展示） -->
      <div v-if="!repo.error" class="grd-sum">
        <span>分支 <b class="mono">{{ repo.detached ? '(HEAD 游离)' : (repo.branch || '—') }}</b></span>
        <span>上游分支 <span class="mono">{{ repo.upstream || '未设置上游' }}<template v-if="repo.ahead || repo.behind"> ↑{{ repo.ahead }} ↓{{ repo.behind }}</template></span></span>
        <span>未提交变更 <b>{{ repo.dirtyFiles }}</b></span>
        <span v-if="repo.originHost">远程仓库 <span class="mono">{{ repo.originHost }}/{{ repo.originSlug }}</span><span v-if="accelTag" class="grd-acc">已加速</span></span>
        <span v-else>无远程仓库</span>
      </div>
      <div v-if="repo.scope === 'remote'" class="grd-warn">
        远程仓库：拉取将直接更新服务器上的工作区（部署语义），操作计入审计。
      </div>

      <div class="grd-body">
        <div v-if="loading" class="grd-load">正在读取仓库详情…</div>
        <div v-else-if="error" class="grd-err" role="alert">{{ error }}</div>
        <template v-else-if="detail">
          <section>
            <h4>工作区变更（未提交） <span v-if="detail.files.length" class="grd-count">{{ detail.files.length }}</span></h4>
            <div v-if="!detail.files.length" class="grd-empty">工作区干净（clean），无未提交变更</div>
            <template v-else>
              <label v-for="f in detail.files" :key="f.path" class="grd-file mono" :title="'状态 ' + f.status">
                <input v-model="checkedPaths" type="checkbox" class="grd-fcheck" :value="f.path" />
                <span class="grd-fstat" :class="statClass(f.status)">{{ f.status }}</span>
                <span class="grd-fpath">{{ f.path }}</span>
              </label>
              <div class="grd-commit-box">
                <input
                  v-model="commitMsg"
                  class="grd-commit-msg"
                  placeholder="提交信息（commit message）…"
                  maxlength="200"
                  spellcheck="false"
                />
                <button class="btn btn-primary grd-commit-btn" type="button" :disabled="!canCommit" @click="submitCommit">
                  提交所选 {{ checkedPaths.length }}
                </button>
              </div>
              <div class="grd-hint">只提交勾选的文件（git add + commit -- pathspec），未勾选的改动保留在工作区</div>
            </template>
          </section>
          <section>
            <h4>分支 <span v-if="detail.branches.length" class="grd-count">{{ detail.branches.length }}</span></h4>
            <div v-if="!detail.branches.length" class="grd-empty">没有本地分支</div>
            <div v-for="b in detail.branches" :key="b.name" class="grd-branch">
              <span class="grd-bname mono" :title="b.name">{{ b.name }}</span>
              <span class="grd-bup mono">{{ b.upstream || '未设置上游分支' }}</span>
              <span class="grd-bab">{{ aheadBehindText(b) }}</span>
              <span v-if="b.current" class="grd-bcur">✓ 当前分支</span>
              <button v-else class="btn btn-sm grd-bswitch" type="button" :disabled="!!busy" title="git switch：切换到该分支（冲突时 git 拒绝）" @click="emit('switch', b.name)">切换</button>
            </div>
          </section>
          <!-- 提交图谱：默认折叠（折叠时不发起取数，首次展开才读取）；图谱失败回退平铺提交列表 -->
          <section>
            <button type="button" class="grd-sechead" :aria-expanded="graphOpen" @click="toggleGraph">
              <span class="grd-caret mono">{{ graphOpen ? '▾' : '▸' }}</span>
              <span class="grd-sectitle">提交图谱</span>
              <span v-if="graphOpen && graphCommitCount" class="grd-count">{{ graphCommitCount }}</span>
            </button>
            <template v-if="graphOpen">
              <template v-if="graphRows.length">
                <div v-for="(row, i) in graphRows" :key="i" class="grd-grow">
                  <!-- white-space:pre 保留对齐空格，固定行高保证 │ 跨行连续 -->
                  <span class="grd-gcol mono">{{ row.graph }}</span>
                  <template v-if="row.commit">
                    <span class="grd-chash mono">{{ row.commit.hash.slice(0, 7) }}</span>
                    <span
                      v-for="ref in row.refs"
                      :key="ref"
                      class="grd-ref"
                      :class="'r-' + graphRefKind(ref)"
                    >{{ ref.replace('tag: ', '') }}</span>
                    <span class="grd-cmsg" :title="row.commit.subject">{{ row.commit.subject }}</span>
                    <span class="grd-cmeta">{{ row.commit.author }} · {{ row.commit.relTime }}</span>
                  </template>
                </div>
                <div class="grd-gfoot">
                  <button v-if="canLoadMore" class="btn btn-sm" type="button" :disabled="graphLoading" @click="loadMore">
                    {{ graphLoading ? '加载中…' : '加载更多 30 条' }}
                  </button>
                  <span v-else-if="graphLimit >= GRAPH_MAX_LIMIT" class="grd-gend">已达 {{ GRAPH_MAX_LIMIT }} 条上限</span>
                  <span v-else class="grd-gend">已到最早提交</span>
                </div>
              </template>
              <div v-else-if="graphLoading" class="grd-empty">正在读取提交图谱…</div>
              <template v-else-if="graphError">
                <div class="grd-err" role="alert">{{ graphError }}（回退为平铺列表）</div>
                <div v-for="c in detail?.commits || []" :key="c.hash" class="grd-commit">
                  <span class="grd-chash mono">{{ c.hash.slice(0, 7) }}</span>
                  <span class="grd-cmsg" :title="c.subject">{{ c.subject }}</span>
                  <span class="grd-cmeta">{{ c.author }} · {{ c.relTime }}</span>
                </div>
              </template>
              <div v-else class="grd-empty">空仓库，尚无提交</div>
            </template>
            <div v-else class="grd-empty">默认折叠：点击标题展开（展开时才读取图谱）</div>
          </section>
        </template>
      </div>

      <!-- 动作统一回抛父级：确认框 / 审计 / 动作锁都收在 GitReposPanel -->
      <div class="grd-foot">
        <button class="btn grd-remove" type="button" :disabled="!!busy" @click="emit('remove')">移出列表</button>
        <span class="grd-foot-spacer"></span>
        <button
          v-if="canPush"
          class="btn"
          type="button"
          :disabled="!!busy || !!repo.error"
          title="push：推送当前分支到远程（无上游时 git push -u 建立跟踪）"
          @click="emit('push')"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><path d="M12 21V9"/><path d="m7 14 5-5 5 5"/><path d="M5 3h14"/></svg>
          推送
        </button>
        <button class="btn" type="button" :disabled="!!busy || !!repo.error" title="fetch：更新远程跟踪引用（--all --prune，不改动工作区）" @click="emit('fetch')">获取</button>
        <button class="btn btn-primary" type="button" :disabled="!!busy || !!repo.error" @click="emit('pull')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>
          拉取
        </button>
        <button class="btn" type="button" :disabled="!!busy" @click="emit('term')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m6 8 4 4-4 4"/><line x1="12" y1="16" x2="18" y2="16"/></svg>
          打开终端
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// GitRepoDetailDialog.vue — 仓库详情弹窗（自取数，动作回抛）。
// 取数：本地走 gitrepos:detail IPC；远程经 sshExec 下发 buildRemoteRepoDetailCmd
// 的三段复合命令。解析共用 shared/git-repos-core.parseRepoDetailSections（单测同源）。
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  GRAPH_MAX_LIMIT,
  buildRemoteGraphCmd,
  buildRemoteRepoDetailCmd,
  firstLine,
  friendlyGitStderr,
  graphHasMore,
  graphRefKind,
  parseGraphLog,
  parseRepoDetailSections,
  splitBySep,
  repoRowKind,
  type GitBranchInfo,
  type GraphCommitRow,
  type RepoDetail,
  type RepoRow,
} from '../../../../shared/git-repos-core'

const props = defineProps<{
  repo: RepoRow
  /** git insteadOf 是否生效（加速联动标记，来自加速 store 的健康状态） */
  accelActive: boolean
  /** 父级动作锁（fetch/pull 在途时禁用按钮，与列表行内按钮同一把锁） */
  busy?: string
}>()
const emit = defineEmits<{
  close: []
  fetch: []
  pull: []
  term: []
  remove: []
  /** V2 写操作：分支切换（分支名经父级与主进程双重复检） */
  switch: [branch: string]
  /** V2 写操作：提交所选文件（pathspec + 单行提交信息） */
  commit: [payload: { files: string[]; msg: string }]
  push: []
}>()

const loading = ref(false)
const error = ref('')
const detail = ref<RepoDetail | null>(null)

const DETAIL_TIMEOUT_SEC = 20

// 取数失败的行不走 repoRowKind（error 是行字段不是状态档），先于状态档判定
const kindClass = computed(() => props.repo.error ? 'k-err' : `k-${repoRowKind(props.repo)}`)
const kindText = computed(() => {
  if (props.repo.error) return '读取失败'
  const k = repoRowKind(props.repo)
  const map: Record<string, string> = {
    dirty: `未提交变更 ${props.repo.dirtyFiles}`,
    diverge: `已分叉 ↑${props.repo.ahead}↓${props.repo.behind}`,
    behind: `落后 ↓${props.repo.behind}`,
    ahead: `领先 ↑${props.repo.ahead}`,
    none: '无远程仓库',
    clean: '干净',
  }
  return map[k]
})
/** GitHub 仓库 + 全局 insteadOf 生效 → 本机 fetch/pull 自动走镜像 */
const accelTag = computed(() => props.repo.originHost === 'github.com' && props.accelActive)

function statClass(status: string): string {
  if (status.startsWith('A') || status.startsWith('?')) return 'st-add'
  if (status.startsWith('D')) return 'st-del'
  return 'st-mod'
}

function aheadBehindText(b: GitBranchInfo): string {
  if (b.ahead === null || b.behind === null) return ''
  return b.ahead || b.behind ? `↑${b.ahead} ↓${b.behind}` : ''
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  detail.value = null
  try {
    if (props.repo.scope === 'local') {
      const res = await window.electronAPI?.gitreposDetail(props.repo.path)
      if (!res) throw new Error('Electron API 不可用')
      if (!res.ok) throw new Error(res.error || '取数失败')
      detail.value = parseRepoDetailSections(res.statusRaw ?? '', res.branchRaw ?? '', res.logRaw ?? '')
    } else {
      const sid = props.repo.sessionId
      const api = window.electronAPI
      if (!sid || !api) throw new Error('远端会话不可用')
      const r = await api.sshExec(sid, buildRemoteRepoDetailCmd(props.repo.path), DETAIL_TIMEOUT_SEC)
      const parts = splitBySep(r.stdout)
      if (parts.length < 3) throw new Error(firstLine(r.stderr) || '远端取数失败（回包分段缺失）')
      detail.value = parseRepoDetailSections(parts[0], parts[1], parts[2])
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
    // 勾选默认全选（= 提交所选的初值）；重取详情后提交信息清空，防止旧信息复用
    checkedPaths.value = detail.value?.files.map(f => f.path) ?? []
    commitMsg.value = ''
  }
}

function onKey(e: KeyboardEvent): void {
  if (e.key === 'Escape') emit('close')
}

// ── 提交所选（V2）：勾选 = pathspec，单行信息；动作回抛父级执行 ──
const checkedPaths = ref<string[]>([])
const commitMsg = ref('')

const canCommit = computed(() =>
  checkedPaths.value.length > 0 && !!commitMsg.value.trim() && !props.busy && !loading.value,
)

function submitCommit(): void {
  if (!canCommit.value) return
  emit('commit', { files: [...checkedPaths.value], msg: commitMsg.value.trim() })
}

// ── 提交图谱：默认折叠，折叠时不取数（首次展开才读取）；失败重开重试 ──
const GRAPH_PAGE = 30
const graphOpen = ref(false)
const graphRows = ref<GraphCommitRow[]>([])
const graphLimit = ref(GRAPH_PAGE)
const graphLoading = ref(false)
const graphError = ref('')

const graphCommitCount = computed(() => graphRows.value.filter(r => r.commit).length)
const canLoadMore = computed(() => graphHasMore(graphRows.value, graphLimit.value))

function toggleGraph(): void {
  graphOpen.value = !graphOpen.value
  if (graphOpen.value && !graphRows.value.length) void loadGraph()
}

async function loadGraph(): Promise<void> {
  const api = window.electronAPI
  if (!api) return
  graphLoading.value = true
  try {
    let raw = ''
    if (props.repo.scope === 'local') {
      const r = await api.gitreposGraphLog(props.repo.path, graphLimit.value)
      if (r.code !== 0) throw new Error(firstLine(friendlyGitStderr(r.stderr)) || `图谱取数退出码 ${r.code}`)
      raw = r.stdout
    } else {
      const sid = props.repo.sessionId
      if (!sid) throw new Error('远端会话不可用')
      const r = await api.sshExec(sid, buildRemoteGraphCmd(props.repo.path, graphLimit.value), DETAIL_TIMEOUT_SEC)
      // 空仓库 log 也退出 0；只在既无输出又非零时才算失败
      if (r.exitCode !== 0 && !r.stdout.trim()) throw new Error(firstLine(r.stderr) || '远端图谱取数失败')
      raw = r.stdout
    }
    graphRows.value = parseGraphLog(raw)
    graphError.value = ''
  } catch (e) {
    graphError.value = e instanceof Error ? e.message : String(e)
  } finally {
    graphLoading.value = false
  }
}

function loadMore(): void {
  graphLimit.value += GRAPH_PAGE
  void loadGraph()
}

// ── 推送（V2）：有上游或已知远程名+当前分支才可推 ──
const canPush = computed(() =>
  !props.repo.error && !props.repo.detached && !!(props.repo.upstream || (props.repo.remoteName && props.repo.branch)),
)

/** 父级动作完成并 refresh 后行数据会变（分支 / 未提交数 / 最新提交），据此自动重取详情与图谱 */
watch(
  () => [props.repo.branch, props.repo.dirtyFiles, props.repo.lastCommit?.hash, props.repo.upstream] as const,
  () => {
    if (!detail.value || loading.value) return
    void load()
    if (graphOpen.value) void loadGraph()
  },
)

onMounted(() => {
  document.addEventListener('keydown', onKey)
  void load()
})
onUnmounted(() => document.removeEventListener('keydown', onKey))
</script>

<style scoped>
.grd-overlay {
  position: fixed;
  inset: 0;
  z-index: 90;
  background: var(--scrim);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.grd-dialog {
  width: min(640px, 100%);
  max-height: 86vh;
  display: flex;
  flex-direction: column;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}

.grd-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px 0;
  flex-wrap: wrap;
}

.grd-name { font-size: 14px; font-weight: 600; color: var(--text-primary); }

.grd-scope { font-size: 11px; color: var(--text-tertiary); }

/* 状态徽标：与 GitReposPanel 表格同一套色（按行状态） */
.grd-pill {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 9px;
  white-space: nowrap;
}
.k-dirty { background: var(--status-warn-bg); color: var(--status-warn-text); }
.k-diverge { background: color-mix(in srgb, var(--accent-orange) 12%, transparent); color: var(--accent-orange); }
.k-behind { background: color-mix(in srgb, var(--status-info-text) 12%, transparent); color: var(--status-info-text); }
.k-ahead { background: color-mix(in srgb, var(--accent-purple) 12%, transparent); color: var(--accent-purple); }
.k-clean { background: var(--status-success-bg); color: var(--status-success-text); }
.k-none { background: var(--bg-hover); color: var(--text-tertiary); }
.k-err { background: var(--status-error-bg); color: var(--status-error-text); }

.grd-icon-btn {
  margin-left: auto;
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
.grd-icon-btn:hover { background: var(--bg-hover); color: var(--text-primary); }

.grd-path {
  padding: 2px 16px 0;
  font-size: 11px;
  color: var(--text-tertiary);
  word-break: break-all;
}

.grd-sum {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  margin: 10px 16px 0;
  padding: 7px 10px;
  background: var(--bg-input);
  border-radius: var(--radius-md);
  font-size: 11px;
  color: var(--text-secondary);
}
.grd-sum b { font-weight: 500; color: var(--text-primary); }

.grd-acc {
  margin-left: 5px;
  font-size: 10px;
  color: var(--accent-green);
  border: 1px solid color-mix(in srgb, var(--accent-green) 35%, transparent);
  border-radius: 3px;
  padding: 0 4px;
}

.grd-warn {
  margin: 10px 16px 0;
  font-size: 11px;
  color: var(--status-warn-text);
  line-height: 1.5;
  padding: 5px 8px;
  border-left: 2px solid var(--status-warn-text);
  border-radius: 3px;
  background: var(--status-warn-bg);
}

.grd-body {
  flex: 1;
  min-height: 120px;
  overflow-y: auto;
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.grd-body h4 {
  font-size: 11px;
  font-weight: 500;
  color: var(--text-tertiary);
  margin-bottom: 4px;
}
.grd-count { margin-left: 4px; color: var(--text-tertiary); }

.grd-load, .grd-empty {
  font-size: 12px;
  color: var(--text-tertiary);
  padding: 8px 0;
}

.grd-err {
  font-size: 12px;
  color: var(--danger-text);
  padding: 8px 0;
  line-height: 1.5;
}

.grd-file {
  display: flex;
  gap: 8px;
  padding: 2px 0;
  font-size: 11px;
  color: var(--text-secondary);
  min-width: 0;
  cursor: pointer;
  align-items: center;
}
.grd-fcheck { flex-shrink: 0; accent-color: var(--accent-primary); }
.grd-commit-box {
  display: flex;
  gap: 8px;
  margin-top: 8px;
  min-width: 0;
}
.grd-commit-msg {
  flex: 1;
  min-width: 0;
  height: 26px;
  padding: 0 10px;
  background: var(--bg-input);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-md);
  color: var(--text-primary);
  font-size: 12px;
  font-family: var(--font-sans);
  outline: none;
}
.grd-commit-msg:focus { border-color: var(--accent-primary); }
.grd-commit-btn { white-space: nowrap; flex-shrink: 0; }
.grd-hint {
  margin-top: 5px;
  font-size: 11px;
  color: var(--text-tertiary);
  line-height: 1.5;
}
.grd-fstat { width: 22px; flex-shrink: 0; font-weight: 600; }
.st-mod { color: var(--status-warn-text); }
.st-add { color: var(--status-success-text); }
.st-del { color: var(--danger-text); }
.grd-fpath {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.grd-branch {
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 3px 0;
  font-size: 12px;
  min-width: 0;
}
.grd-bswitch { flex-shrink: 0; }
.btn-sm { height: 22px; padding: 0 8px; font-size: 11px; }

/* 可折叠小节头（提交图谱默认折叠） */
.grd-sechead {
  display: flex;
  align-items: center;
  gap: 4px;
  width: 100%;
  padding: 0;
  margin-bottom: 4px;
  border: none;
  background: transparent;
  cursor: pointer;
  text-align: left;
}
.grd-sechead:hover .grd-sectitle { color: var(--text-primary); }
.grd-caret { font-size: 11px; color: var(--text-tertiary); }
.grd-sectitle {
  font-size: 11px;
  font-weight: 500;
  color: var(--text-tertiary);
}
.grd-bname {
  width: 160px;
  flex-shrink: 0;
  color: var(--accent-cyan);
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.grd-bup {
  flex: 1;
  min-width: 0;
  font-size: 11px;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.grd-bab { font-size: 11px; color: var(--text-secondary); white-space: nowrap; }
.grd-bcur { font-size: 11px; color: var(--status-success-text); white-space: nowrap; }

.grd-commit {
  display: flex;
  gap: 8px;
  align-items: baseline;
  padding: 3px 0;
  min-width: 0;
}

/* ── 提交图谱行：图谱列 pre 保留空格、行高固定，│ 才能跨行连成线 ── */
.grd-grow {
  display: flex;
  gap: 8px;
  align-items: center;
  min-height: 20px;
  line-height: 20px;
  font-size: 11px;
  min-width: 0;
}
.grd-gcol {
  white-space: pre;
  color: var(--text-tertiary);
  flex-shrink: 0;
}

/* 引用徽章：HEAD 实底强调，本地/远端/标签描边色分（不单靠颜色：文字本身可辨） */
.grd-ref {
  flex-shrink: 0;
  font-size: 10px;
  line-height: 14px;
  padding: 0 5px;
  border-radius: 8px;
  border: 1px solid var(--border-subtle);
  color: var(--text-secondary);
  white-space: nowrap;
}
.r-head { background: var(--accent-primary); border-color: var(--accent-primary); color: var(--accent-on); }
.r-local { color: var(--accent-purple); border-color: color-mix(in srgb, var(--accent-purple) 45%, transparent); }
.r-remote { color: var(--accent-cyan); border-color: color-mix(in srgb, var(--accent-cyan) 45%, transparent); }
.r-tag { color: var(--accent-yellow); border-color: color-mix(in srgb, var(--accent-yellow) 45%, transparent); }

.grd-gfoot {
  display: flex;
  justify-content: center;
  padding-top: 6px;
}
.grd-gend { font-size: 11px; color: var(--text-tertiary); }
.grd-chash { font-size: 11px; color: var(--accent-orange); flex-shrink: 0; }
.grd-cmsg {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.grd-cmeta { font-size: 11px; color: var(--text-tertiary); white-space: nowrap; flex-shrink: 0; }

.grd-foot {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 12px 16px;
  border-top: 1px solid var(--border-subtle);
}
.grd-foot-spacer { flex: 1; }
/* 移出列表：仅动注册表不动文件，克制地放低位 */
.grd-remove { color: var(--status-error-text); border-color: color-mix(in srgb, var(--status-error-text) 35%, transparent); }
.grd-remove:hover { background: color-mix(in srgb, var(--status-error-text) 10%, transparent); }

/* 按钮与 Docker 页同款基础形态（弹窗独立文件不能吃父级 scoped 样式） */
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
}
.btn:hover { background: var(--bg-hover); }
.btn:disabled { opacity: 0.45; cursor: default; }
.btn-primary { background: var(--accent-primary); border-color: var(--accent-primary); color: var(--accent-on); }
.btn-primary:hover { filter: brightness(1.15); }

.mono { font-family: var(--font-mono); }
</style>
