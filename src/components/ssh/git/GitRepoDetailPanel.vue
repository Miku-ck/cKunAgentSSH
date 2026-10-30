<template>
  <section class="grdp" :aria-label="'仓库详情：' + repo.name">
    <!-- 头部：仓库标识 + 路径 + 动作条（动作统一回抛父级：确认/审计/锁都在 GitReposPanel） -->
    <div class="grdp-head">
      <span class="grdp-name">{{ repo.name }}</span>
      <span class="grdp-pill" :class="kindClass">{{ kindText }}</span>
      <span v-if="repo.scope === 'remote'" class="grdp-scope">远程 · SSH 会话</span>
      <span class="grdp-path mono" :title="repo.path">{{ repo.path }}</span>
      <span class="grdp-head-spacer"></span>
      <button class="btn" type="button" :disabled="!!busy || !!repo.error" :title="fetchHint" @click="emit('fetch')">获取</button>
      <button class="btn btn-primary" type="button" :disabled="!!busy || !!repo.error" :title="pullHint" @click="emit('pull')">拉取</button>
      <!-- 提交/推送合一：有未提交变更 → 提交（弹窗内默认提交并推送）；
           工作区干净但有待推提交 → 直接推送。任一时刻只有一个相关动作 -->
      <button
        v-if="repo.dirtyFiles"
        class="btn"
        type="button"
        :disabled="!!busy || !!repo.error || loading || !!error"
        title="提交变更：弹窗内勾选文件并填写信息，留空信息将自动生成"
        @click="openCommit"
      >提交</button>
      <button
        v-else-if="canPush"
        class="btn"
        type="button"
        :disabled="!!busy || !!repo.error || !repo.ahead"
        :title="pushHint"
        @click="emit('push')"
      >推送</button>
      <button class="btn" type="button" :disabled="!!busy" title="在终端中打开（本地终端 / 新 SSH 会话）" @click="emit('term')">终端</button>
      <button class="btn grdp-remove" type="button" :disabled="!!busy" title="仅从列表移除，不删除任何文件" @click="emit('remove')">移出</button>
    </div>

    <!-- 取数失败：错误框 + 重试（详情三段取数对失败行没有意义，不再尝试） -->
    <div v-if="repo.error" class="grdp-body">
      <div class="grdp-errbox" role="alert">
        <span>{{ repo.error }}</span>
        <button class="btn btn-sm" type="button" :disabled="loading" @click="emit('refresh')">重试读取</button>
      </div>
    </div>

    <template v-else>
      <!-- 远程部署警示：拉取/推送直接变更服务器工作区 -->
      <div v-if="repo.scope === 'remote'" class="grdp-warn">
        远程仓库：拉取 / 推送将直接更新服务器上的工作区（部署语义），操作计入审计。
      </div>

      <!-- 概要条：分支 / 上游 / 未提交 / 远程，标签一档、值一档 -->
      <div class="grdp-sum">
        <span class="sum-i">
          <span class="sum-k">分支</span>
          <span class="sum-v mono">{{ repo.detached ? '(HEAD 游离)' : (repo.branch || '—') }}</span>
        </span>
        <span class="sum-i">
          <span class="sum-k">上游</span>
          <span class="sum-v mono" :class="{ dim: !repo.upstream }">{{ repo.upstream || '未设置' }}</span>
          <span v-if="repo.ahead || repo.behind" class="sum-ab">
            <span v-if="repo.ahead" class="ab-up">↑{{ repo.ahead }}</span>
            <span v-if="repo.behind" class="ab-down">↓{{ repo.behind }}</span>
          </span>
        </span>
        <span class="sum-i">
          <span class="sum-k">未提交</span>
          <span class="sum-v" :class="{ warn: repo.dirtyFiles > 0 }">{{ repo.dirtyFiles }}</span>
        </span>
        <span v-if="repo.originHost" class="sum-i">
          <span class="sum-k">远程</span>
          <span class="sum-v mono">{{ repo.originHost }}/{{ repo.originSlug }}</span>
          <span v-if="accelTag" class="grdp-acc">已加速</span>
        </span>
        <span v-else class="sum-i">
          <span class="sum-k">远程</span>
          <span class="sum-v dim">未配置</span>
        </span>
      </div>

      <!-- 远端条：常驻一行看清 主仓 / 镜像 的落后与分叉（巡检快照；逐端点动作在「远端」页签）。
           无 remote 的仓库由概要条的「无远程仓库」表达，这里不再重复一句话。 -->
      <div v-if="showEpbar" class="grdp-epbar">
        <template v-if="syncAvailable">
          <span v-if="!endpoints.length" class="grdp-epnone">
            {{ probing ? '正在巡检远端…' : '尚未巡检该仓库的远端' }}
          </span>
          <span
            v-for="ep in endpoints"
            :key="ep.name"
            class="grdp-chip"
            :class="'s-' + ep.state"
            :title="`${ep.url} · ${epStateTitle(ep)}`"
          >
            <i class="grdp-cdot" :class="syncDotClass(ep)"></i>
            <span class="grdp-cname">{{ ep.name }}</span>
            <span class="grdp-crole" :class="ep.role === 'primary' ? 'pri' : 'mir'">{{ ep.role === 'primary' ? '主仓' : '镜像' }}</span>
            <span class="grdp-cstate">{{ syncStateText(ep) }}</span>
            <button
              v-if="ep.role === 'primary' && ep.state === 'behind'"
              class="grdp-cact"
              type="button"
              :disabled="!!busy"
              :title="`git push ${ep.name} ${repo.branch} --follow-tags（快进补齐主仓，非强推）`"
              @click="emit('catchup')"
            >补齐</button>
            <button
              v-else-if="ep.role === 'primary' && ep.state === 'diverged'"
              class="grdp-cact"
              type="button"
              :disabled="!!busy"
              :title="`git fetch ${ep.name} ${repo.branch} && git merge --ff-only FETCH_HEAD；真分叉时 git 拒绝，需手动处理`"
              @click="emit('merge', ep.name)"
            >合并</button>
          </span>
          <span class="grdp-epsp"></span>
          <span v-if="probeLabel" class="grdp-epmeta">{{ probeLabel }}</span>
          <button v-if="tab !== 'remotes'" class="grdp-eplink" type="button" @click="tab = 'remotes'">详情 ›</button>
        </template>
        <span v-else class="grdp-epnone">远端巡检只覆盖本地仓库：远程仓库的远端一致性以「获取」后的 ↑↓ 为准</span>
      </div>

      <!-- 四页签：变更（默认，日常工作流）/ 远端 / 分支 / 图谱（图谱首次激活才取数） -->
      <div class="grdp-tabs" role="tablist" aria-label="仓库详情分区">
        <button
          v-for="t in tabs"
          :key="t.key"
          type="button"
          class="grdp-tab"
          role="tab"
          :aria-selected="tab === t.key"
          :class="{ on: tab === t.key }"
          @click="tab = t.key"
        >{{ t.label }}<span v-if="t.key === 'changes'" class="grdp-tabct" :class="{ zero: !fileCount }">{{ fileCount }}</span><span v-else-if="t.key === 'remotes'" class="grdp-tabct" :class="{ zero: !endpoints.length }">{{ endpoints.length }}</span></button>
      </div>

      <div class="grdp-body">
        <!-- 远端：逐端点 URL / SHA / 状态 / 修复动作（不依赖详情取数，所以排在最前） -->
        <section v-if="tab === 'remotes'">
          <div v-if="!endpoints.length" class="grdp-empty">
            <span>{{ probing ? '正在巡检远端…' : (repo.originUrl ? '尚未巡检该仓库的远端' : '该仓库没有配置 remote') }}</span>
            <button v-if="repo.originUrl" class="grdp-eplink" type="button" :disabled="probing || !!busy" @click="emit('probe')">巡检</button>
          </div>
          <template v-else>
            <div v-if="!hasGitcodePrimary && !manualPrimary" class="grdp-epwarn">未配置 GitCode 主仓：本仓不走平台镜像，推送将回退其它远端</div>
            <div v-if="manualPrimary" class="grdp-epmanual">
              <span>主仓为手动指定（<b class="mono">{{ manualPrimary }}</b>），覆盖自动推导</span>
              <button
                class="btn btn-sm"
                type="button"
                :disabled="!!busy"
                title="清除手动设置，恢复 GitCode 优先的自动推导"
                @click="emit('setPrimary', null)"
              >恢复自动</button>
            </div>
            <div v-for="ep in endpoints" :key="ep.name" class="grdp-ep">
              <div class="grdp-ep-row">
                <span class="grdp-ep-name mono" :title="ep.url">{{ ep.name }}</span>
                <span class="grdp-ep-role" :class="ep.role === 'primary' ? 'pri' : 'mir'">{{ ep.role === 'primary' ? '主仓' : '镜像' }}</span>
                <span class="grdp-ep-url mono" :title="ep.url">{{ ep.url }}</span>
                <span class="grdp-ep-sha mono" :title="`远端 HEAD ${ep.sha ?? ''}`">{{ shortSha(ep.sha) }}</span>
                <span class="grdp-ep-state" :class="'s-' + ep.state" :title="epStateTitle(ep)">{{ syncStateText(ep) }}</span>
                <span class="grdp-ep-acts">
                  <button
                    v-if="ep.role === 'mirror'"
                    class="btn btn-sm grdp-ep-setprimary"
                    type="button"
                    :disabled="!!busy"
                    :title="`将 ${ep.name} 设为本仓主仓（手动覆盖自动推导）`"
                    @click="emit('setPrimary', ep.name)"
                  >设为主仓</button>
                  <button
                    v-if="ep.role === 'primary' && ep.state === 'behind'"
                    class="btn btn-sm"
                    type="button"
                    :disabled="!!busy"
                    :title="`git push ${ep.name} ${repo.branch} --follow-tags（快进补齐主仓，非强推）`"
                    @click="emit('fix', ep.name)"
                  >补齐</button>
                  <button
                    v-if="ep.role === 'primary' && ep.state === 'diverged'"
                    class="btn btn-sm"
                    type="button"
                    :disabled="!!busy"
                    :title="`git fetch ${ep.name} ${repo.branch} && git merge --ff-only FETCH_HEAD；真分叉时 git 拒绝，需手动处理`"
                    @click="emit('merge', ep.name)"
                  >合并</button>
                  <span v-if="ep.role === 'mirror' && ep.state === 'behind'" class="grdp-ep-wait">等待平台搬运</span>
                </span>
              </div>
              <div v-if="ep.state === 'diverged'" class="grdp-ep-note" :class="{ err: ep.role === 'mirror' }">
                <template v-if="ep.role === 'mirror'">镜像端出现本地没有的提交（可能有人直推镜像端），平台单向镜像不应如此；请 fetch 主仓后人工核对</template>
                <template v-else>两边各有提交：先合并（--ff-only），绝不强推覆盖</template>
              </div>
              <div v-else-if="ep.state === 'unknown' && ep.error" class="grdp-ep-note" :title="ep.error">{{ ep.error }}</div>
            </div>
            <div class="grdp-eptail">
              <button class="grdp-eplink" type="button" :disabled="probing || !!busy" title="对全部本地仓库重跑一次 ls-remote（只比对 SHA，不动工作区）" @click="emit('probe')">
                {{ probing ? '巡检中…' : '重新巡检' }}
              </button>
            </div>
            <details class="grdp-guide">
              <summary>平台镜像一次性配置（GitCode 侧）</summary>
              <div class="grdp-guide-b">
                <ol>
                  <li>GitCode 仓库 → 项目设置 → 仓库镜像 → Push → 添加镜像</li>
                  <li>目标填 GitHub 仓库 + classic 令牌（repo + admin:repo_hook）</li>
                  <li>再加一条指向 Gitee。此后本地只需推主仓，镜像自动搬运</li>
                </ol>
                <div class="grdp-guide-note">不启用双向镜像（Push + Pull 同开）：两端并发提交会互相覆盖。落后与分叉由本页巡检发现，不依赖平台回报。</div>
              </div>
            </details>
          </template>
        </section>

        <div v-else-if="loading" class="grdp-load">正在读取仓库详情…</div>
        <div v-else-if="error" class="grdp-err" role="alert">{{ error }}</div>
        <template v-else-if="detail">
          <!-- 变更：只读概览（勾选与提交动作在头部「提交」弹窗里，一处完成） -->
          <section v-if="tab === 'changes'">
            <div v-if="!detail.files.length" class="grdp-empty">工作区干净（clean），无未提交变更</div>
            <template v-else>
              <div v-for="f in detail.files" :key="f.path" class="grdp-frow mono">
                <FileTypeIcon :name="f.path" />
                <span class="grdp-fpath" :title="`${f.path} · ${statusTitle(f.status)}`">{{ f.path }}</span>
              </div>
              <div class="grdp-hint">   </div>
            </template>
          </section>

          <!-- 分支：当前 ✓ / 切换回抛（父级与主进程双重复检分支名） -->
          <section v-if="tab === 'branches'">
            <div v-if="!detail.branches.length" class="grdp-empty">没有本地分支</div>
            <div v-for="b in detail.branches" :key="b.name" class="grdp-branch">
              <span class="grdp-bname mono" :title="b.name">{{ b.name }}</span>
              <span class="grdp-bup mono">{{ b.upstream || '未设置上游分支' }}</span>
              <span class="grdp-bab">{{ aheadBehindText(b) }}</span>
              <span v-if="b.current" class="grdp-bcur">✓ 当前分支</span>
              <button v-else class="btn btn-sm" type="button" :disabled="!!busy" title="git switch：切换到该分支（冲突时 git 拒绝）" @click="emit('switch', b.name)">切换</button>
            </div>
          </section>

          <!-- 图谱：SVG 车道图；失败回退平铺提交列表 -->
          <section v-if="tab === 'graph'">
            <template v-if="graphRows.length">
              <GitLaneGraph :rows="graphRows" />
              <div class="grdp-gfoot">
                <button v-if="canLoadMore" class="btn btn-sm" type="button" :disabled="graphLoading" @click="loadMore">
                  {{ graphLoading ? '加载中…' : '加载更多 30 条' }}
                </button>
                <span v-else-if="graphLimit >= GRAPH_MAX_LIMIT" class="grdp-gend">已达 {{ GRAPH_MAX_LIMIT }} 条上限</span>
                <span v-else class="grdp-gend">已到最早提交</span>
              </div>
            </template>
            <div v-else-if="graphLoading" class="grdp-empty">正在读取提交图谱…</div>
            <template v-else-if="graphError">
              <div class="grdp-err" role="alert">{{ graphError }}（回退为平铺列表）</div>
              <div v-for="c in detail.commits" :key="c.hash" class="grdp-commit">
                <span class="grdp-chash mono">{{ c.hash.slice(0, 7) }}</span>
                <span class="grdp-cmsg" :title="c.subject">{{ c.subject }}</span>
                <span class="grdp-cmeta">{{ c.author }} · {{ c.relTime }}</span>
              </div>
            </template>
            <div v-else class="grdp-empty">空仓库，尚无提交</div>
          </section>
        </template>
      </div>
    </template>

    <!-- 提交弹窗（点击弹窗执行）：信息在上、文件清单在下、主按钮带分支名；
         分支徽标 / 字数计数 / 推送目标预览为本仓自己的增补 -->
    <Teleport to="body">
      <div v-if="showCommit" class="grdc-overlay" @click.self="closeCommit">
        <div ref="commitDialogRef" class="grdc-dialog" role="dialog" aria-modal="true" aria-label="提交变更" tabindex="-1">
          <div class="grdc-head">
            <span class="grdc-repo">{{ repo.name }}</span>
            <span v-if="!repo.detached && repo.branch" class="grdc-branch mono">{{ repo.branch }}</span>
            <span class="grdc-head-sp"></span>
            <button class="grdc-x" type="button" aria-label="关闭" title="关闭（Esc）" @click="closeCommit">✕</button>
          </div>
          <div v-if="repo.scope === 'remote'" class="grdc-warn">
            远程仓库：提交将写入服务器工作区（部署语义），执行前会再次确认并计入审计。
          </div>

          <div class="grdc-fieldlabel">
            <span>提交信息</span>
            <span class="grdc-head-sp"></span>
            <button
              class="grdc-spark"
              type="button"
              :class="{ busy: aiDrafting }"
              :disabled="aiDrafting || !checkedPaths.length"
              :title="aiDrafting ? 'AI 生成中…' : 'AI 按已勾选文件的 diff 生成提交信息'"
              aria-label="AI 生成提交信息"
              @click="aiGenerateDraft"
            >✦</button>
            <span class="grdc-counter mono">{{ commitMsg.length }}/200</span>
          </div>
          <textarea
            ref="commitInput"
            v-model="commitMsg"
            class="grdc-textarea"
            rows="2"
            maxlength="200"
            spellcheck="false"
            placeholder="留空将自动生成（Enter 确认执行）…"
            @keydown.enter.prevent="submitPrimary"
          />

          <div class="grdc-secbar">
            <span class="grdc-seclabel">变更文件 <b>{{ checkedPaths.length }}</b> / {{ (detail?.files || []).length }}</span>
            <span class="grdc-head-sp"></span>
            <button class="grdc-linkbtn" type="button" :disabled="checkedPaths.length === (detail?.files || []).length" @click="checkAll(true)">全选</button>
            <button class="grdc-linkbtn" type="button" :disabled="!checkedPaths.length" @click="checkAll(false)">清空</button>
          </div>
          <div class="grdc-list">
            <label v-for="f in detail?.files || []" :key="f.path" class="grdc-file mono">
              <input v-model="checkedPaths" type="checkbox" class="grdc-fcheck" :value="f.path" />
              <FileTypeIcon :name="f.path" />
              <span class="grdc-fpath" :title="`${f.path} · ${statusTitle(f.status)}`">{{ f.path }}</span>
            </label>
          </div>
          <div v-if="noiseSkipped" class="grdc-noisehint">
            已默认不勾选 {{ noiseSkipped }} 个疑似临时产物（日志 / 备份 / 系统文件），可勾回；长期忽略请写入 .gitignore
          </div>

          <div v-if="canPush" class="grdc-pushprev mono">
            推送目标 {{ repo.branch }} → {{ repo.primaryRemote || repo.upstream || `${repo.remoteName}/${repo.branch}` }}<template v-if="repo.primaryRemote"> · 主仓 GitCode</template><template v-if="repo.ahead">（本地已领先 ↑{{ repo.ahead }}{{ repo.primaryRemote ? '，将推到主仓' : '，将一并上送' }}）</template>
          </div>

          <div class="grdc-foot">
            <button class="btn" type="button" @click="closeCommit">取消</button>
            <span class="grdc-sp"></span>
            <!-- 主按钮 = 提交并推送（提交后必推是主流用法）；无推送目标的仓库退回仅提交 -->
            <button v-if="canPush" class="btn btn-primary" type="button" :disabled="!canCommit" title="提交所选后立即推送当前分支（提交成功才推送）" @click="submitCommitPush">
              提交并推送
            </button>
            <button v-else class="btn btn-primary" type="button" :disabled="!canCommit" @click="submitCommit">
              提交所选（{{ checkedPaths.length }}）
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </section>
</template>

<script setup lang="ts">
// GitRepoDetailPanel.vue — 仓库详情常驻面板（方案 A 双栏工作台的右栏，自取数，动作回抛）。
// 由 V2 的 GitRepoDetailDialog 弹窗改造而来：去掉遮罩/Esc/Teleport，改为
// 变更 / 分支 / 图谱 三页签常驻布局；图谱从 --graph 文字档升级为 SVG 车道图
// （GitLaneGraph + layoutGraphLanes），首次切到图谱页签才取数。
// 取数：本地走 gitrepos:detail IPC；远程经 sshExec 下发 buildRemoteRepoDetailCmd
// 的三段复合命令。解析共用 shared/git-repos-core.parseRepoDetailSections（单测同源）。
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import GitLaneGraph from './GitLaneGraph.vue'
import FileTypeIcon from '@/components/icons/FileTypeIcon.vue'
import { agentApi, toFriendlyMessage } from '@/api/client'
import { useToastStore } from '@/stores/toast'
import { useModalFocusTrap } from '@/composables/useModalFocusTrap'
import {
  GRAPH_MAX_LIMIT,
  buildRemoteDiffCmd,
  buildRemoteGraphCmd,
  buildRemoteRepoDetailCmd,
  clampDiffText,
  draftCommitMessage,
  friendlyGitError,
  graphHasMore,
  looksLikeNoiseFile,
  parseGraphLog,
  parseRepoDetailSections,
  remoteHostOf,
  splitBySep,
  syncDotClass,
  syncStateText,
  repoRowKind,
  type GitBranchInfo,
  type GraphCommitRow,
  type RepoDetail,
  type RepoRow,
  type SyncEndpoint,
} from '../../../../shared/git-repos-core'

const props = defineProps<{
  repo: RepoRow
  /** git insteadOf 是否生效（加速联动标记，来自加速 store 的健康状态） */
  accelActive: boolean
  /** 父级动作锁（fetch/pull 在途时禁用按钮，与列表行同一把锁） */
  busy?: string
  /** 端点快照（父级巡检持有；远程范围恒为空数组） */
  endpoints?: SyncEndpoint[]
  /** 巡检能力是否可用（syncProbe 走本地注册表，故只覆盖本地范围） */
  syncAvailable?: boolean
  /** 父级巡检在途 */
  probing?: boolean
  /** 上次巡检的展示文案（父级算好，面板不碰定时器） */
  probeLabel?: string
  /** 该仓库的手动主仓配置（来自注册表 reg.sync）；为空表示未手动指定、走自动推导 */
  manualPrimary?: string | null
}>()
const emit = defineEmits<{
  fetch: []
  pull: []
  push: []
  term: []
  remove: []
  /** 行级取数失败后的「重试读取」：父级 refresh 重读列表 */
  refresh: []
  /** 分支切换（分支名经父级与主进程双重复检） */
  switch: [branch: string]
  /** 提交所选文件（pathspec + 单行提交信息） */
  commit: [payload: { files: string[]; msg: string }]
  /** 提交并推送：一次确认覆盖两步（父级先提交、成功才推送） */
  'commit-push': [payload: { files: string[]; msg: string }]
  /** 补齐主仓（本地领先主仓的提交快进推上去） */
  catchup: []
  /** 补齐指定远端端点 */
  fix: [remote: string]
  /** 分叉合并指定端点（--ff-only） */
  merge: [remote: string]
  /** 重跑远端巡检 */
  probe: []
  /** 主仓设置：remote 为远端名 = 设为主仓；null = 恢复自动推导（清除手动配置） */
  setPrimary: [remote: string | null]
}>()

const TABS = [
  { key: 'changes', label: '变更' },
  { key: 'remotes', label: '远端' },
  { key: 'branches', label: '分支' },
  { key: 'graph', label: '图谱' },
] as const
type TabKey = typeof TABS[number]['key']

const tab = ref<TabKey>('changes')
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
const fileCount = computed(() => detail.value?.files.length ?? props.repo.dirtyFiles)

// ── 远端（巡检快照由父级传入：远端条与「远端」页签共用） ──
const endpoints = computed<SyncEndpoint[]>(() => props.endpoints ?? [])
const syncAvailable = computed(() => props.syncAvailable !== false)
/** 远程范围没有巡检能力（syncProbe 走本地注册表）→「远端」页签整条不出现，省掉一句「这里不可用」 */
const tabs = computed(() => (syncAvailable.value ? TABS : TABS.filter(t => t.key !== 'remotes')))
watch(syncAvailable, (v) => { if (!v && tab.value === 'remotes') tab.value = 'changes' })

function isGitcodeUrl(url: string): boolean {
  const h = remoteHostOf(url)
  return h === 'gitcode.com' || h === 'gitcode.net'
}
/** 没有 GitCode 主仓 = 不走平台镜像，推送退回其它远端（远端页签顶部提示） */
/** 远端条只在对得起一行信息时出现：无 remote 的仓库概要条已写明，不再重复 */
const showEpbar = computed(() => !syncAvailable.value || !!props.probing || endpoints.value.length > 0)
const hasGitcodePrimary = computed(() => endpoints.value.some(e => e.role === 'primary' && isGitcodeUrl(e.url)))

function epStateTitle(ep: SyncEndpoint): string {
  if (ep.state === 'unknown') return ep.error || 'ls-remote 失败（网络 / 认证）'
  if (ep.state === 'behind') {
    return ep.role === 'primary' ? '远端可快进：补齐 = 快进直推' : '镜像落后主仓：等 GitCode 平台自动搬运，本页不直推镜像'
  }
  if (ep.state === 'diverged') return '两边各有提交：先拉取合并，绝不强推'
  return '远端 HEAD 与本地一致'
}
function shortSha(sha: string | null): string {
  return sha ? sha.slice(0, 7) : '—'
}

/** 状态含义（悬浮提示用）：图标行不显示状态码，改/增/删的区分收进 title */
function statusTitle(status: string): string {
  if (status.startsWith('?')) return '未跟踪'
  const map: Record<string, string> = { M: '已修改', A: '已暂存新增', D: '已删除', R: '重命名' }
  const first = status.slice(0, 1)
  return map[first] || '状态变更'
}

function aheadBehindText(b: GitBranchInfo): string {
  if (b.ahead === null || b.behind === null) return ''
  return b.ahead || b.behind ? `↑${b.ahead} ↓${b.behind}` : ''
}

async function load(): Promise<void> {
  // 行级取数失败：不发起详情取数（三段命令同样会失败），由头部错误框 + 重试承接
  if (props.repo.error) { detail.value = null; return }
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
      if (parts.length < 3) throw new Error(friendlyGitError(r.stderr, '远端取数失败（回包分段缺失）'))
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

// ── 提交所选：勾选 = pathspec，单行信息；动作回抛父级执行（远程在父级再确认 + 审计） ──
const checkedPaths = ref<string[]>([])
const commitMsg = ref('')

const canCommit = computed(() =>
  checkedPaths.value.length > 0 && !props.busy && !loading.value,
)

// 提交弹窗（点击弹窗执行）：信息填写与动作选择都在弹窗里完成
const showCommit = ref(false)
const commitInput = ref<HTMLTextAreaElement | null>(null)
const commitDialogRef = ref<HTMLElement | null>(null)
// 打开即落在提交信息框（原先靠 openCommit 里的手工 focus，现在交给共用件统一做）
const { activate, deactivate } =
  useModalFocusTrap(commitDialogRef, { initialFocus: '.grdc-textarea' })
/** 打开弹窗时被「疑似临时产物」规则默认跳过的文件数（0 = 无，不显示提示行） */
const noiseSkipped = ref(0)
const toast = useToastStore()

function openCommit(): void {
  const files = detail.value?.files ?? []
  if (!files.length) return
  // 默认勾选 = 全部变更，排除疑似临时产物（日志/备份/系统文件——.gitignore 漏掉的兜底）
  const keep = files.filter(f => !looksLikeNoiseFile(f.path))
  checkedPaths.value = (keep.length ? keep : files).map(f => f.path)
  noiseSkipped.value = files.length - keep.length
  showCommit.value = true
}

/** 选中文件对 HEAD 的 diff 摘要（本地 IPC / 远程 sshExec 同口径）；失败返回空串——
 *  diff 只是提升描述质量的旁路，取不到就退回清单推断，不阻断 AI 生成 */
async function fetchCheckedDiff(): Promise<string> {
  const api = window.electronAPI
  const paths = checkedPaths.value
  if (!api || !paths.length) return ''
  try {
    if (props.repo.scope === 'local') {
      const r = await api.gitreposDiffFiles(props.repo.path, paths)
      return r.code === 0 ? clampDiffText(r.stdout) : ''
    }
    const sid = props.repo.sessionId
    if (!sid) return ''
    const r = await api.sshExec(sid, buildRemoteDiffCmd(props.repo.path, paths), DETAIL_TIMEOUT_SEC)
    return r.exitCode === 0 ? clampDiffText(r.stdout) : ''
  } catch {
    return ''
  }
}

/** ✦ AI 生成提交信息：先取选中文件的 diff（有内容看内容），再纯推理归纳；失败/为空降级规则草稿 */
const aiDrafting = ref(false)
async function aiGenerateDraft(): Promise<void> {
  if (aiDrafting.value || !checkedPaths.value.length) return
  const files = (detail.value?.files ?? [])
    .filter(f => checkedPaths.value.includes(f.path))
    .map(f => `${f.status} ${f.path}`)
  if (!files.length) return
  aiDrafting.value = true
  try {
    // 先取 diff（失败得空串，不阻断），再带着 diff 归纳——描述质量的关键就是这一步
    const diff = await fetchCheckedDiff()
    const r = await agentApi.generateCommitMessage({
      repo: props.repo.name,
      branch: props.repo.branch || '',
      files,
      ...(diff ? { diff } : {}),
    })
    const msg = (r.message || '').trim().slice(0, 200)
    if (msg) {
      commitMsg.value = msg
    } else {
      commitMsg.value = draftCommitMessage(checkedPaths.value)
      toast.warning('AI 返回为空，已填入规则草稿')
    }
    void nextTick(() => commitInput.value?.focus())
  } catch (e) {
    commitMsg.value = draftCommitMessage(checkedPaths.value)
    // 后端 detail 自带去处（设置 → AI 模型 / 重试或手写），给足阅读时长
    toast.warning(`AI 生成失败：${toFriendlyMessage(e, '服务不可用')}。已填入规则草稿`, 8000)
  } finally {
    aiDrafting.value = false
  }
}
function closeCommit(): void {
  showCommit.value = false
}
function onCommitDialogKey(e: KeyboardEvent): void {
  if (e.key === 'Escape') closeCommit()
}
watch(showCommit, (open) => {
  if (open) {
    document.addEventListener('keydown', onCommitDialogKey)
    activate()
  } else {
    document.removeEventListener('keydown', onCommitDialogKey)
    deactivate()
  }
})
onUnmounted(() => document.removeEventListener('keydown', onCommitDialogKey))

function submitCommit(): void {
  if (!canCommit.value) return
  closeCommit()
  // 留空自动生成：规则草稿由已勾选文件推出（确定性，主进程 isValidCommitMsg 仍会复核）
  const msg = commitMsg.value.trim() || draftCommitMessage(checkedPaths.value)
  emit('commit', { files: [...checkedPaths.value], msg })
}

/** Enter = 主动作：可推送 → 提交并推送（父级仍有确认框兜底防误触）；否则仅提交 */
function submitPrimary(): void {
  if (canPush.value) submitCommitPush()
  else submitCommit()
}

function submitCommitPush(): void {
  if (!canCommit.value || !canPush.value) return
  closeCommit()
  const msg = commitMsg.value.trim() || draftCommitMessage(checkedPaths.value)
  emit('commit-push', { files: [...checkedPaths.value], msg })
}

/** 全选 / 清空勾选（= 提交所选 pathspec 的初值控制） */
function checkAll(all: boolean): void {
  checkedPaths.value = all ? (detail.value?.files.map(f => f.path) ?? []) : []
}

// ── 图谱页签：首次激活才取数（原「默认折叠」的惰性策略平移到页签）；失败重开重试 ──
const GRAPH_PAGE = 30
const graphRows = ref<GraphCommitRow[]>([])
const graphLimit = ref(GRAPH_PAGE)
const graphLoading = ref(false)
const graphError = ref('')

const canLoadMore = computed(() => graphHasMore(graphRows.value, graphLimit.value))

watch(tab, (t) => {
  if (t === 'graph' && !graphRows.value.length) void loadGraph()
})

async function loadGraph(): Promise<void> {
  const api = window.electronAPI
  if (!api) return
  graphLoading.value = true
  try {
    let raw = ''
    if (props.repo.scope === 'local') {
      const r = await api.gitreposGraphLog(props.repo.path, graphLimit.value)
      if (r.code !== 0) throw new Error(friendlyGitError(r.stderr, `图谱取数退出码 ${r.code}`))
      raw = r.stdout
    } else {
      const sid = props.repo.sessionId
      if (!sid) throw new Error('远端会话不可用')
      const r = await api.sshExec(sid, buildRemoteGraphCmd(props.repo.path, graphLimit.value), DETAIL_TIMEOUT_SEC)
      // 空仓库 log 也退出 0；只在既无输出又非零时才算失败
      if (r.exitCode !== 0 && !r.stdout.trim()) throw new Error(friendlyGitError(r.stderr, '远端图谱取数失败'))
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

const fetchHint = computed(() => {
  const names = props.repo.remoteNames ?? []
  const where = names.length ? `全部远端（${names.join('、')}）` : '全部远端'
  return `fetch --all --prune：更新${where}的远端跟踪引用；不改动工作区与暂存区`
})
const pullHint = computed(() => {
  const up = props.repo.upstream
  return up
    ? `pull --ff-only：快进合并上游 ${up}；本地与上游分叉时 git 拒绝，不自动合并`
    : 'pull --ff-only：当前分支没有上游，git 会直接报错；先在终端 git branch -u 设置上游'
})

/**
 * 头部「推送」的目标不是 pickRemote 的结果，而是主仓（pushOne → primaryRemoteOf，GitCode 优先），
 * 上游没指向主仓时走 push -u。按钮文案只说「推送到远程」会让人以为推的是 origin（可能是镜像），
 * 所以把真实目标写进 title —— 与「远端」页签里逐端点的「补齐」同一套语义。
 */
const pushHint = computed(() => {
  const primary = props.repo.primaryRemote
  if (!primary) {
    return props.repo.upstream
      ? `push：推送当前分支到上游 ${props.repo.upstream}`
      : 'push：推送当前分支到远程（无上游时 git push -u 建立跟踪）'
  }
  return `push：推送当前分支到主仓 ${primary}（GitCode 优先；上游没指向主仓时 push -u 建立跟踪）`
})
// ── 推送：有上游或已知远程名+当前分支才可推 ──
const canPush = computed(() =>
  !props.repo.error && !props.repo.detached && !!(props.repo.upstream || (props.repo.remoteName && props.repo.branch)),
)

/** 父级动作完成并 refresh 后行数据会变（分支 / 未提交数 / 最新提交），据此自动重取详情与图谱 */
watch(
  () => [props.repo.branch, props.repo.dirtyFiles, props.repo.lastCommit?.hash, props.repo.upstream] as const,
  () => {
    if (!detail.value || loading.value) return
    void load()
    if (tab.value === 'graph') void loadGraph()
  },
)

onMounted(() => { void load() })
</script>

<style scoped>
.grdp {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  font-family: var(--font-sans);
}

/* ── 头部：标识 + 路径 + 动作条 ── */
.grdp-head {
  display: flex;
  align-items: center;
  gap: var(--spacing-8);
  padding: var(--spacing-8) var(--spacing-14);
  border-bottom: 1px solid var(--divider-primary);
  flex-shrink: 0;
  flex-wrap: wrap;
}
.grdp-name { font-size: 1rem; font-weight: 600; color: var(--text-primary); white-space: nowrap; }
.grdp-scope { font-size: 0.8462rem; color: var(--text-tertiary); white-space: nowrap; }
.grdp-path {
  font-size: 0.8462rem;
  color: var(--text-tertiary);
  min-width: 60px;
  flex: 0 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.grdp-head-spacer { flex: 1; }

/* 状态徽标：与左侧列表同一套色（按行状态） */
.grdp-pill {
  font-size: 0.8462rem;
  padding: 1px var(--spacing-8);
  border-radius: var(--radius-xl);
  white-space: nowrap;
}
.k-dirty { background: var(--status-warn-bg); color: var(--status-warn-text); }
.k-diverge { background: color-mix(in srgb, var(--accent-orange) 12%, transparent); color: var(--accent-orange); }
.k-behind { background: color-mix(in srgb, var(--status-info-text) 12%, transparent); color: var(--status-info-text); }
.k-ahead { background: color-mix(in srgb, var(--accent-purple) 12%, transparent); color: var(--accent-purple); }
.k-clean { background: var(--status-success-bg); color: var(--status-success-text); }
.k-none { background: var(--bg-hover); color: var(--text-tertiary); }
.k-err { background: var(--status-error-bg); color: var(--status-error-text); }

/* 远程部署警示条 */
.grdp-warn {
  margin: var(--spacing-8) var(--spacing-14) 0;
  font-size: 0.8462rem;
  color: var(--status-warn-text);
  line-height: 1.5;
  padding: 5px var(--spacing-8);
  border-left: 2px solid var(--status-warn-text);
  border-radius: var(--radius-sm);
  background: var(--status-warn-bg);
  flex-shrink: 0;
}

/* 概要条：标签一档（小字弱色）值一档（主色），字段之间拉大列距；换行不产生悬空分隔线 */
.grdp-sum {
  display: flex;
  align-items: baseline;
  column-gap: var(--spacing-12);
  row-gap: var(--spacing-6);
  flex-wrap: wrap;
  margin: var(--spacing-8) var(--spacing-14) 0;
  padding: 7px var(--spacing-10);
  background: var(--bg-input);
  border-radius: var(--radius-md);
  font-size: 0.8462rem;
  color: var(--text-secondary);
  flex-shrink: 0;
}
.sum-i {
  display: inline-flex;
  align-items: baseline;
  gap: var(--spacing-6);
  min-width: 0;
  white-space: nowrap;
}
.sum-k { font-size: 0.8462rem; color: var(--text-tertiary); }
.sum-v { font-size: 0.9231rem; color: var(--text-primary); }
.sum-v.dim { color: var(--text-tertiary); }
.sum-v.warn { color: var(--status-warn-text); font-weight: 500; }
.sum-ab { display: inline-flex; gap: var(--spacing-4); font-size: 0.8462rem; }
.ab-up { color: var(--accent-purple); }
.ab-down { color: var(--status-info-text); }
.grdp-acc {
  margin-left: var(--spacing-2);
  font-size: 0.7692rem;
  color: var(--accent-green);
  border: 1px solid color-mix(in srgb, var(--accent-green) 35%, transparent);
  border-radius: var(--radius-sm);
  padding: 0 var(--spacing-4);
}

/* ── 远端条：常驻一行看清 主仓/镜像 的状态，落后可原地补齐（逐端点动作在页签） ── */
.grdp-epbar {
  display: flex;
  align-items: center;
  gap: var(--spacing-8);
  flex-wrap: wrap;
  margin: var(--spacing-8) var(--spacing-14) 0;
  flex-shrink: 0;
  font-size: 0.8462rem;
  color: var(--text-tertiary);
}
.grdp-epnone { color: var(--text-tertiary); }
.grdp-epsp { flex: 1; }
.grdp-epmeta { color: var(--text-tertiary); white-space: nowrap; }
.grdp-eplink {
  border: none;
  background: transparent;
  color: var(--accent-text);
  font-size: 0.8462rem;
  font-family: var(--font-sans);
  cursor: pointer;
  padding: 0;
  white-space: nowrap;
}
.grdp-eplink:hover { text-decoration: underline; }
.grdp-eplink:disabled { opacity: .45; cursor: default; text-decoration: none; }
.grdp-eptail { margin-top: var(--spacing-8); display: flex; }
.grdp-empty .grdp-eplink { margin-left: var(--spacing-6); }

.grdp-chip {
  display: inline-flex;
  align-items: center;
  gap: var(--spacing-6);
  height: 22px;
  padding: 0 var(--spacing-8);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-pill);
  background: var(--bg-elevated);
  color: var(--text-secondary);
  white-space: nowrap;
}
.grdp-chip.s-behind { border-color: color-mix(in srgb, var(--status-warn-text) 38%, transparent); }
.grdp-chip.s-diverged { border-color: color-mix(in srgb, var(--status-error-text) 42%, transparent); }
.grdp-chip.s-unknown { border-style: dashed; }
.grdp-cdot { width: 7px; height: 7px; border-radius: 50%; background: var(--bg-slider); flex-shrink: 0; }
.grdp-cdot.ok { background: var(--status-success-text); }
.grdp-cdot.behind { background: var(--status-warn-text); }
.grdp-cdot.diverged { background: var(--status-error-text); }
.grdp-cdot.off { box-shadow: inset 0 0 0 1px var(--border-default); }
.grdp-cname { font-family: var(--font-mono); color: var(--text-primary); }
.grdp-crole {
  font-size: 0.7692rem;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  padding: 0 var(--spacing-4);
  color: var(--text-tertiary);
}
.grdp-crole.pri { color: var(--accent-text); border-color: color-mix(in srgb, var(--accent-primary) 40%, transparent); }
.grdp-crole.mir { color: var(--accent-indigo); border-color: color-mix(in srgb, var(--accent-indigo) 32%, transparent); }
.grdp-chip.s-ok .grdp-cstate { color: var(--status-success-text); }
.grdp-chip.s-behind .grdp-cstate { color: var(--status-warn-text); }
.grdp-chip.s-diverged .grdp-cstate { color: var(--status-error-text); }
.grdp-cact {
  border: none;
  border-left: 1px solid var(--divider-primary);
  background: transparent;
  color: var(--accent-text);
  font: inherit;
  font-family: var(--font-sans);
  cursor: pointer;
  margin-left: var(--spacing-2);
  padding: 0 0 0 var(--spacing-6);
}
.grdp-cact:hover:not(:disabled) { text-decoration: underline; }
.grdp-cact:disabled { opacity: .45; cursor: default; }

/* ── 远端页签：一远端一行（名称 + 角色 | URL | SHA | 状态 | 动作） ── */
.grdp-epwarn {
  margin-bottom: var(--spacing-8);
  padding: 5px var(--spacing-10);
  border-left: 2px solid var(--status-warn-text);
  border-radius: var(--radius-sm);
  background: var(--status-warn-bg);
  color: var(--text-primary);
  font-size: 0.8462rem;
  line-height: 1.5;
}
.grdp-epmanual {
  margin-bottom: var(--spacing-8);
  padding: 5px var(--spacing-10);
  border-left: 2px solid var(--accent-primary);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--accent-primary) 10%, transparent);
  color: var(--text-primary);
  font-size: 0.8462rem;
  line-height: 1.5;
  display: flex;
  align-items: center;
  gap: var(--spacing-8);
}
.grdp-epmanual .btn { margin-left: auto; flex-shrink: 0; }
.grdp-ep { padding: var(--spacing-8) 0; border-bottom: 1px solid var(--divider-primary); }
.grdp-ep-row {
  display: grid;
  grid-template-columns: auto auto 1fr auto auto auto;
  gap: var(--spacing-10);
  align-items: center;
  min-width: 0;
}
.grdp-ep-name { font-size: 0.9231rem; font-weight: 500; color: var(--text-primary); }
.grdp-ep-role {
  font-size: 0.7692rem;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  padding: 0 var(--spacing-6);
  color: var(--text-tertiary);
  white-space: nowrap;
  flex-shrink: 0;
}
.grdp-ep-role.pri { color: var(--accent-text); border-color: color-mix(in srgb, var(--accent-primary) 40%, transparent); }
.grdp-ep-role.mir { color: var(--accent-indigo); border-color: color-mix(in srgb, var(--accent-indigo) 32%, transparent); }
.grdp-ep-url {
  flex: 1;
  min-width: 0;
  font-size: 0.8462rem;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.grdp-ep-sha { width: 7ch; text-align: right; font-size: 0.8462rem; color: var(--text-tertiary); }
.grdp-ep-state { width: 5ch; text-align: left; font-size: 0.9231rem; white-space: nowrap; }
.grdp-ep-state.s-ok { color: var(--status-success-text); }
.grdp-ep-state.s-behind { color: var(--status-warn-text); }
.grdp-ep-state.s-diverged { color: var(--status-error-text); font-weight: 500; }
.grdp-ep-state.s-unknown { color: var(--text-tertiary); }
.grdp-ep-acts { display: flex; align-items: center; gap: var(--spacing-6); justify-content: flex-end; min-width: 10ch; }
/* 设为主仓：默认隐藏，悬停整行才显示（其余动作按状态始终可见） */
.grdp-ep-setprimary { opacity: 0; pointer-events: none; transition: opacity .12s ease; }
.grdp-ep-row:hover .grdp-ep-setprimary { opacity: 1; pointer-events: auto; }
.grdp-ep-wait { font-size: 0.8462rem; color: var(--text-tertiary); white-space: nowrap; }
.grdp-ep-note { margin-top: var(--spacing-4); font-size: 0.8462rem; color: var(--text-secondary); line-height: 1.5; }
.grdp-ep-note.err { color: var(--status-error-text); }

/* 平台镜像配置折叠：一次性说明，不占默认视野 */
.grdp-guide { margin-top: var(--spacing-12); border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); background: var(--bg-elevated); }
.grdp-guide summary {
  padding: var(--spacing-8) var(--spacing-12);
  font-size: 0.8462rem;
  color: var(--text-secondary);
  cursor: pointer;
  list-style: none;
}
.grdp-guide summary::-webkit-details-marker { display: none; }
.grdp-guide summary::before { content: "▸ "; color: var(--text-tertiary); }
.grdp-guide[open] summary::before { content: "▾ "; }
.grdp-guide-b { padding: 0 var(--spacing-12) var(--spacing-10); font-size: 0.8462rem; color: var(--text-secondary); }
.grdp-guide-b ol { margin: 0; padding-left: var(--spacing-20); }
.grdp-guide-b li { margin-bottom: var(--spacing-4); }
.grdp-guide-note { margin-top: var(--spacing-6); color: var(--text-tertiary); }

/* ── 页签 ── */
.grdp-tabs {
  display: flex;
  padding: var(--spacing-6) var(--spacing-14) 0;
  border-bottom: 1px solid var(--divider-primary);
  flex-shrink: 0;
  gap: var(--spacing-2);
}
.grdp-tab {
  display: inline-flex;
  align-items: center;
  gap: var(--spacing-6);
  height: 28px;
  padding: 0 var(--spacing-10);
  border: none;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--text-secondary);
  font-size: 0.9231rem;
  font-family: var(--font-sans);
  cursor: pointer;
}
.grdp-tab:hover { color: var(--text-primary); }
.grdp-tab.on { color: var(--text-primary); border-bottom-color: var(--accent-primary); }
.grdp-tabct {
  min-width: 16px;
  height: 15px;
  padding: 0 var(--spacing-4);
  border-radius: var(--radius-lg);
  background: color-mix(in srgb, var(--status-warn-text) 20%, transparent);
  color: var(--status-warn-text);
  font-size: 0.8462rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.grdp-tabct.zero { background: var(--bg-hover); color: var(--text-tertiary); }

/* ── 主体 ── */
.grdp-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--spacing-10) var(--spacing-14);
}
.grdp-load, .grdp-empty {
  font-size: 0.9231rem;
  color: var(--text-tertiary);
  padding: var(--spacing-8) 0;
}
.grdp-err {
  font-size: 0.9231rem;
  color: var(--danger-text);
  padding: var(--spacing-8) 0;
  line-height: 1.5;
}
.grdp-errbox {
  display: flex;
  align-items: center;
  gap: var(--spacing-10);
  justify-content: space-between;
  font-size: 0.9231rem;
  color: var(--danger-text);
  border: 1px solid color-mix(in srgb, var(--danger-text) 30%, transparent);
  border-radius: var(--radius-md);
  padding: var(--spacing-10) var(--spacing-12);
  line-height: 1.5;
}

/* 变更概览行（只读：图标 + 路径；勾选在提交弹窗里） */
.grdp-frow {
  display: flex;
  gap: var(--spacing-8);
  align-items: center;
  padding: var(--spacing-2) 0;
  font-size: 0.8462rem;
  color: var(--text-secondary);
  min-width: 0;
}
/* ── 提交弹窗（DESIGN.md 居中弹窗口径：--bg-elevated-solid + --scrim + Esc 关闭） ── */
.grdc-overlay {
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
.grdc-dialog {
  width: min(520px, 100%);
  max-height: min(600px, calc(100vh - 48px));
  overflow: hidden;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-10);
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  padding: var(--spacing-16) 18px;
  /* 容器只作为初始焦点落点（tabindex=-1），描边交给里面的控件 */
  outline: none;
}
.grdc-head { display: flex; align-items: center; gap: var(--spacing-8); flex-shrink: 0; min-width: 0; }
.grdc-head-sp { flex: 1; }
.grdc-repo { font-size: 1rem; font-weight: 600; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.grdc-branch {
  flex-shrink: 0;
  font-size: 0.8462rem;
  color: var(--accent-cyan);
  background: color-mix(in srgb, var(--accent-cyan) 10%, transparent);
  border-radius: var(--radius-xl);
  padding: 1px var(--spacing-8);
  white-space: nowrap;
}
.grdc-x {
  flex-shrink: 0;
  width: 22px;
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--text-tertiary);
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: 0.9231rem;
}
.grdc-x:hover { background: var(--bg-hover); color: var(--text-primary); }
.grdc-fieldlabel {
  display: flex;
  align-items: baseline;
  gap: var(--spacing-8);
  flex-shrink: 0;
  font-size: 0.8462rem;
  color: var(--text-tertiary);
}
.grdc-counter { color: var(--text-tertiary); }
.grdc-spark {
  flex-shrink: 0;
  width: 22px;
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--text-tertiary);
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: 1rem;
  line-height: 1;
}
.grdc-spark:hover:not(:disabled) { background: var(--bg-hover); color: var(--accent-text); }
.grdc-spark:disabled { opacity: .45; cursor: default; }
.grdc-spark.busy { color: var(--accent-text); animation: grdc-rot 1s linear infinite; }
@keyframes grdc-rot { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) {
  .grdc-spark.busy { animation: none; }
}
.grdc-noisehint {
  flex-shrink: 0;
  font-size: 0.8462rem;
  color: var(--status-warn-text);
  line-height: 1.5;
}
.grdc-textarea {
  flex-shrink: 0;
  min-height: 52px;
  padding: 7px var(--spacing-10);
  background: var(--bg-input);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-md);
  color: var(--text-primary);
  font-size: 0.9231rem;
  font-family: var(--font-sans);
  line-height: 1.5;
  resize: none;
  outline: none;
}
.grdc-textarea:focus { border-color: var(--accent-primary); }
.grdc-textarea::placeholder { color: var(--text-tertiary); }
.grdc-secbar { display: flex; align-items: center; gap: var(--spacing-8); flex-shrink: 0; }
.grdc-seclabel { font-size: 0.8462rem; color: var(--text-tertiary); }
.grdc-seclabel b { font-weight: 500; color: var(--text-secondary); }
.grdc-linkbtn {
  border: none;
  background: transparent;
  padding: 0;
  font-size: 0.8462rem;
  font-family: var(--font-sans);
  color: var(--text-tertiary);
  cursor: pointer;
}
.grdc-pushprev {
  flex-shrink: 0;
  font-size: 0.8462rem;
  color: var(--text-tertiary);
  border-left: 2px solid var(--border-default);
  padding: var(--spacing-2) 0 var(--spacing-2) var(--spacing-8);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.grdc-warn {
  flex-shrink: 0;
  font-size: 0.8462rem;
  color: var(--status-warn-text);
  line-height: 1.5;
  padding: 5px var(--spacing-8);
  border-left: 2px solid var(--status-warn-text);
  border-radius: var(--radius-sm);
  background: var(--status-warn-bg);
}
.grdc-list {
  min-height: 0;
  overflow-y: auto;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: var(--spacing-4) var(--spacing-8);
}
.grdc-file {
  display: flex;
  gap: var(--spacing-8);
  padding: var(--spacing-2) 0;
  font-size: 0.8462rem;
  color: var(--text-secondary);
  min-width: 0;
  cursor: pointer;
  align-items: center;
}
.grdc-fcheck { flex-shrink: 0; accent-color: var(--accent-primary); }
.grdc-fpath {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.grdc-linkbtn:hover:not(:disabled) { color: var(--text-primary); text-decoration: underline; }
.grdc-linkbtn:disabled { opacity: .45; cursor: default; }
.grdc-foot { display: flex; gap: var(--spacing-8); align-items: center; flex-shrink: 0; }
.grdc-sp { flex: 1; }
.grdp-fpath {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.grdp-hint {
  margin-top: var(--spacing-6);
  font-size: 0.8462rem;
  color: var(--text-tertiary);
  line-height: 1.5;
}

/* 分支行 */
.grdp-branch {
  display: flex;
  gap: var(--spacing-10);
  align-items: center;
  padding: 3px 0;
  font-size: 0.9231rem;
  min-width: 0;
}
.grdp-bname {
  width: 160px;
  flex-shrink: 0;
  color: var(--accent-cyan);
  font-size: 0.8462rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.grdp-bup {
  flex: 1;
  min-width: 0;
  font-size: 0.8462rem;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.grdp-bab { font-size: 0.8462rem; color: var(--text-secondary); white-space: nowrap; }
.grdp-bcur { font-size: 0.8462rem; color: var(--status-success-text); white-space: nowrap; }

/* 图谱回退平铺行 */
.grdp-commit {
  display: flex;
  gap: var(--spacing-8);
  align-items: baseline;
  padding: 3px 0;
  min-width: 0;
}
.grdp-gfoot {
  display: flex;
  justify-content: center;
  padding-top: var(--spacing-6);
}
.grdp-gend { font-size: 0.8462rem; color: var(--text-tertiary); }
.grdp-chash { font-size: 0.8462rem; color: var(--accent-orange); flex-shrink: 0; }
.grdp-cmsg {
  flex: 1;
  min-width: 0;
  font-size: 0.9231rem;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.grdp-cmeta { font-size: 0.8462rem; color: var(--text-tertiary); white-space: nowrap; flex-shrink: 0; }

/* 移出列表：仅动注册表不动文件，克制地放动作条末位 */
.grdp-remove { color: var(--status-error-text); border-color: color-mix(in srgb, var(--status-error-text) 35%, transparent); }
.grdp-remove:hover:not(:disabled) { background: color-mix(in srgb, var(--status-error-text) 10%, transparent); }

/* 按钮基础形态（面板是独立 scoped 文件，吃不到父级样式；与 GitReposPanel 同款） */
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
.btn:disabled { opacity: 0.45; cursor: default; }
.btn-primary { background: var(--accent-primary); border-color: var(--accent-primary); color: var(--accent-on); }
.btn-primary:hover:not(:disabled) { filter: brightness(1.15); }
.btn-sm { height: 22px; padding: 0 var(--spacing-8); font-size: 0.8462rem; }

.mono { font-family: var(--font-mono); }
</style>
