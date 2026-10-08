<template>
  <div class="bulk-modal-mask" @click.self="requestClose">
    <div class="bulk-modal">
      <div class="bulk-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
        多节点批量下发
        <span class="bulk-sub" v-if="!running && results.length">
          共 {{ okCount }} 台成功 · {{ failedCount }} 台失败
          <template v-if="results.length > 1 && !allConsistent"> · 存在差异</template>
        </span>
        <button class="bulk-close" title="关闭" @click="requestClose">×</button>
      </div>

      <!-- 下发前配置 -->
      <div v-if="!results.length && !running" class="bulk-config">
        <label class="bulk-field">
          <span>命令（对选中节点并行执行，建议只读命令）</span>
          <textarea v-model="command" rows="3" placeholder='例如：df -h / 或 uptime && free -m' @keydown.meta.enter="run" @keydown.ctrl.enter="run"></textarea>
        </label>
        <div class="bulk-quick">
          <button v-for="q in QUICK_CMDS" :key="q" class="bulk-quick-btn" @click="command = q">{{ q }}</button>
        </div>
        <label class="bulk-field row">
          <span>超时</span>
          <select v-model.number="timeoutSec">
            <option :value="10">10 秒</option>
            <option :value="30">30 秒</option>
            <option :value="60">60 秒</option>
            <option :value="120">120 秒</option>
          </select>
        </label>
        <div class="bulk-targets">
          <span>目标节点（{{ checkedCount }}/{{ remoteSessions.length }}）</span>
          <div class="bulk-target-list">
            <label v-for="s in remoteSessions" :key="s.id" class="bulk-target">
              <input type="checkbox" :value="s.id" v-model="checked" />
              <span class="mono">{{ hostOf(s) }}</span>
            </label>
          </div>
        </div>
      </div>

      <!-- 执行中 -->
      <div v-if="running" class="bulk-running">
        <div class="bulk-progress-bar"><div class="fill" :style="{ width: progressPct + '%' }"></div></div>
        <div class="bulk-progress-text">正在 {{ checkedCount }} 台节点上执行…（{{ doneCount }}/{{ checkedCount }} 完成）</div>
      </div>

      <!-- 结果对比 -->
      <div v-if="results.length && !running" class="bulk-results">
        <div class="bulk-results-toolbar">
          <button class="sc-btn" :disabled="running" @click="reRun">重新执行</button>
          <button v-if="results.length > 1" class="sc-btn" :class="{ active: showDiff }" @click="showDiff = !showDiff">
            {{ showDiff ? '返回逐节点视图' : '差异对比' }}
          </button>
          <button class="sc-btn" @click="askAi">⚡ AI 分析结果</button>
          <button class="sc-btn" @click="requestClose">关闭</button>
        </div>

        <!-- 差异对比视图 -->
        <div v-if="showDiff" class="bulk-diff">
          <div v-if="!diffLines.length" class="bulk-empty">无可对比的输出</div>
          <div v-else class="bulk-diff-lines">
            <div
              v-for="(dl, i) in diffLines"
              :key="i"
              class="bulk-diff-line"
              :class="{ inconsistent: dl.inconsistent }"
            >
              <span class="diff-marker">{{ dl.inconsistent ? '△' : '' }}</span>
              <span class="diff-text mono">{{ dl.line || '(空行)' }}</span>
              <span v-if="dl.inconsistent" class="diff-count" :title="`仅 ${dl.count}/${dl.total} 台输出此内容`">
                {{ dl.count }}/{{ dl.total }}
              </span>
            </div>
          </div>
        </div>

        <!-- 逐节点并排视图 -->
        <div v-else class="bulk-cols">
          <!-- key 用索引：同一连接开两个会话时 hostOf() 生成的 label 相同，
               重复 key 会让 Vue 复用错列（results 按完成顺序 push，每次执行都重建） -->
          <div v-for="(r, ri) in results" :key="ri" class="bulk-col" :class="{ failed: r.error || (r.exitCode !== null && r.exitCode !== 0) }">
            <div class="bulk-col-head">
              <span class="mono col-name">{{ r.label }}</span>
              <span class="col-badge" :class="r.error ? 'err' : (r.exitCode === 0 ? 'ok' : 'fail')">
                {{ r.error ? '错误' : (r.exitCode === 0 ? 'exit 0' : `exit ${r.exitCode}`) }}
              </span>
              <span class="col-dur">{{ (r.durationMs / 1000).toFixed(1) }}s</span>
            </div>
            <pre class="bulk-col-body mono">{{ r.error ? r.error : (r.lines.length ? r.lines.join('\n') : '(无输出)') }}</pre>
          </div>
        </div>
      </div>

      <!-- 底部按钮（配置态） -->
      <div v-if="!results.length && !running" class="bulk-footer">
        <button class="sc-btn" @click="requestClose">取消</button>
        <button class="sc-btn primary" :disabled="!canRun" @click="run">
          {{ checking ? '安全判定中…' : `执行（${checkedCount} 台并行）` }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * BulkCommandModal — 多节点批量下发 + 结果并排对比（F 阶段 MVP）
 *
 * 对当前所有活动远程终端会话并行下发一条命令（ssh:exec，独立 exec channel，
 * 不污染交互终端），结果并排展示；多节点时提供逐行差异对比（不一致行高亮）。
 * 结果可一键送 AI 分析。
 *
 * 这里是「一个人的动作 × N 台机器」的放大点，执行前必经后端风险判定：
 * hardline 直接中止（无确认按钮），dangerous 二次确认，判定不可用则拒绝下发。
 */
import { ref, computed, watch } from 'vue'
import { useSSHStore, type TerminalSession } from '@/stores/ssh'
import { useSshAiStore } from '@/stores/ssh-ai'
import { useSettingsStore } from '@/stores/settings'
import { useConfirm } from '@/composables/useConfirm'
import { agentApi, toFriendlyMessage, type SafetyClassifyResult } from '@/api/client'
import {
  diffResults, resultsConsistent, decideBulkDispatch, type BulkResult,
} from '@/composables/bulkCommandDiff'

/** 统一关闭入口：执行中关闭需确认——sshExec 不随关窗取消，且重开弹窗即"失忆"，
 *  可对同一批节点无感知地重复并发下发同一命令 */
async function requestClose() {
  if (running.value) {
    const { confirm } = useConfirm()
    const ok = await confirm({
      title: '命令仍在执行',
      message: `${liveChecked.value.length} 台节点的命令尚未全部返回，关闭后结果将被丢弃。确定关闭？`,
      confirmText: '仍要关闭',
      cancelText: '继续等待',
      danger: true,
    })
    if (!ok) return
  }
  emit('close')
}

const emit = defineEmits<{ close: [] }>()

const sshStore = useSSHStore()
const sshAiStore = useSshAiStore()

/** 当前活动远程会话（批量下发的可选目标） */
const remoteSessions = computed(() => sshStore.sessions.filter(s => s.type === 'remote'))

const checked = ref<string[]>([])
const command = ref('')
const timeoutSec = ref(30)
const results = ref<BulkResult[]>([])
const running = ref(false)
const checking = ref(false)
const showDiff = ref(false)

const QUICK_CMDS = ['uptime', 'free -m', 'df -h /', 'uname -a', 'cat /etc/os-release | head -3']

function hostOf(s: TerminalSession): string {
  const conn = s.connectionId ? sshStore.connections.find(c => c.id === s.connectionId) : null
  return conn ? `${conn.name} (${conn.username}@${conn.host})` : s.name
}

const checkedCount = computed(() => liveChecked.value.length)
// 执行期间目标会话可能断开：watch 对 checked 只增不减，若直接用 checked.length，
// 断开的目标会永远留在进度分母里（条到不了 100%）。取与活动会话的交集兜底。
const liveChecked = computed(() => checked.value.filter(id => remoteSessions.value.some(s => s.id === id)))
const canRun = computed(() => command.value.trim().length > 0 && liveChecked.value.length > 0 && !running.value && !checking.value)

const doneCount = computed(() => results.value.length)

// 进度 = 已完成节点数 / 总节点数
const progressPct = computed(() => {
  const total = checkedCount.value
  if (!total) return 0
  return Math.min(100, Math.round((doneCount.value / total) * 100))
})

const okCount = computed(() => results.value.filter(r => !r.error && r.exitCode === 0).length)
const failedCount = computed(() => results.value.filter(r => r.error || r.exitCode !== 0).length)
const allConsistent = computed(() => resultsConsistent(results.value))

const diffLines = computed(() => diffResults(results.value))

/**
 * 阻止执行并告知原因 —— 两个按钮都只是关闭，没有任何路径会下发命令。
 */
async function blockExecution(title: string, lines: string[]): Promise<false> {
  const { confirm } = useConfirm()
  await confirm({
    title,
    message: [...lines, '', '命令未下发到任何节点。'].join('\n'),
    confirmText: '知道了',
    cancelText: '关闭',
    danger: true,
  })
  return false
}

/**
 * 执行前防护：判定的唯一来源是后端 safety.classify_command（与 AI 的 shell_exec 同口径），
 * 渲染层不再自带词表 —— 曾经这里是第四份规则表，各判各的，导致 mkfs / rm -rf / 在这条
 * 路上只是"多问一句"。分支决策（hardline 中止 / dangerous 确认 / 判定不可达 fail-closed）
 * 在 decideBulkDispatch 里，可单测。
 */
async function confirmBeforeRun(cmd: string, targets: TerminalSession[]): Promise<boolean> {
  const { confirm } = useConfirm()
  let verdict: SafetyClassifyResult | null = null
  try {
    const res = await agentApi.classifyCommands([cmd])
    verdict = res.results.find(r => r.command === cmd) ?? null
  } catch {
    verdict = null
  }
  const prodHosts: string[] = []
  // E5：「生产环境额外确认」关掉时不再因为目标是 prod 就多问一句。
  // dangerous/hardline 判定与判定不可达的 fail-closed 仍在（decideBulkDispatch 那侧），
  // 这里撤掉的只是 prod 主机名带来的那一条警示。
  if (useSettingsStore().settings.agent.prodConfirm) {
    for (const s of targets) {
      const conn = s.connectionId ? sshStore.connections.find(c => c.id === s.connectionId) : null
      if (conn?.environment === 'prod') prodHosts.push(conn.name || conn.host)
    }
  }

  const gate = decideBulkDispatch(cmd, verdict, prodHosts)
  if (gate.action === 'block') return blockExecution(gate.title, gate.lines)
  if (gate.action === 'run') return true
  const ok = await confirm({
    title: '批量下发确认',
    message: [
      `即将对 ${targets.length} 台节点并行执行：`,
      cmd,
      '',
      ...gate.warnings.map(w => `⚠ ${w}`),
      '',
      '确认执行？',
    ].join('\n'),
    confirmText: '确认执行',
    cancelText: '取消',
    danger: true,
  })
  return ok
}

/** 执行：并行下发到所有选中节点（单台失败不影响其他；结果增量写入，进度实时推进） */
async function run() {
  if (!canRun.value) return
  const cmd = command.value.trim()
  const targets = remoteSessions.value.filter(s => liveChecked.value.includes(s.id))
  if (!targets.length) return

  // 判定要走一次网络往返，期间禁用入口避免重复点击堆叠确认框
  checking.value = true
  let ok = false
  try {
    ok = await confirmBeforeRun(cmd, targets)
  } finally {
    checking.value = false
  }
  if (!ok) return

  running.value = true
  results.value = []
  showDiff.value = false

  const api = window.electronAPI
  await Promise.all(
    targets.map(async (s) => {
      const label = hostOf(s)
      const start = Date.now()
      let r: BulkResult
      try {
        if (!api) throw new Error('Electron API 不可用')
        const res = await api.sshExec(s.id, cmd, timeoutSec.value)
        const out = res.stdout + (res.stderr ? (res.stdout ? '\n' : '') + res.stderr : '')
        r = {
          label,
          lines: out.replace(/\r/g, '').split('\n'),
          exitCode: res.exitCode,
          durationMs: Date.now() - start,
        }
      } catch (e: unknown) {
        r = {
          label,
          lines: [],
          exitCode: null,
          durationMs: Date.now() - start,
          error: toFriendlyMessage(e, '执行失败'),
        }
      }
      // 增量写入：进度条 / 已完成计数实时推进
      results.value.push(r)
    }),
  )
  running.value = false
}

/** 重新执行（同一命令、同一目标集） */
function reRun() {
  void run()
}

/** 结果送 AI 分析：纯文本入口（askAi），不采集当前活动终端的选区/命令历史——
 *  批量目标节点与活动终端未必同一台，且各节点输出已自包含在问题文本里
 *  （与 SchedulesView AI 诊断同约定） */
function askAi() {
  const parts = results.value.map(r => {
    const body = r.error ? `执行失败：${r.error}` : (r.lines.join('\n').slice(0, 1500) || '(无输出)')
    return `【${r.label}】exit ${r.exitCode ?? '?'}\n${body}`
  })
  const text = [
    `我在 ${results.value.length} 台节点上并行执行了命令：${command.value.trim()}`,
    '各节点输出如下，请帮我横向对比并指出差异、异常与潜在风险：',
    '',
    ...parts,
  ].join('\n')
  sshAiStore.askAi(text)
}

// 打开时默认全选
checked.value = remoteSessions.value.map(s => s.id)

// 弹窗打开期间新增远程会话 → 自动勾选（追加，不覆盖用户已取消勾选的节点）。
// 仅追加：已勾选的保持，用户手动取消的不再恢复。
watch(remoteSessions, (sessions) => {
  const known = new Set(checked.value)
  for (const s of sessions) {
    if (!known.has(s.id)) checked.value = [...checked.value, s.id]
  }
})
</script>

<style scoped>
.bulk-modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1100;
}
.bulk-modal {
  width: 860px;
  max-width: 94vw;
  max-height: 86vh;
  display: flex;
  flex-direction: column;
  background: var(--bg-base);
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  padding: 16px 18px;
}
.bulk-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 12px;
}
.bulk-sub {
  font-size: 12px;
  font-weight: 400;
  color: var(--text-tertiary);
}
.bulk-close {
  margin-left: auto;
  border: none;
  background: transparent;
  color: var(--text-tertiary);
  font-size: 20px;
  cursor: pointer;
  line-height: 1;
}
.bulk-close:hover { color: var(--text-primary); }

.bulk-config { display: flex; flex-direction: column; gap: 10px; }
.bulk-field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--text-tertiary); }
.bulk-field.row { flex-direction: row; align-items: center; gap: 8px; }
.bulk-field textarea, .bulk-field select {
  font-size: 12px;
  font-family: var(--font-mono);
  padding: 6px 8px;
  border-radius: 6px;
  border: 1px solid var(--border-subtle);
  background: var(--bg-base);
  color: var(--text-primary);
  resize: vertical;
}
.bulk-quick { display: flex; gap: 6px; flex-wrap: wrap; }
.bulk-quick-btn {
  font-size: 11px;
  font-family: var(--font-mono);
  padding: 3px 8px;
  border-radius: 5px;
  border: 1px dashed var(--border-subtle);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
}
.bulk-quick-btn:hover { color: var(--text-primary); border-color: var(--accent-primary); }

.bulk-targets { font-size: 12px; color: var(--text-tertiary); }
.bulk-target-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 6px;
  max-height: 160px;
  overflow: auto;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  padding: 8px;
}
.bulk-target { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text-secondary); cursor: pointer; }
.bulk-target:hover { color: var(--text-primary); }

.bulk-footer { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }

.sc-btn {
  padding: 4px 12px;
  font-size: 12px;
  border-radius: 6px;
  border: 1px solid var(--border-subtle);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
}
.sc-btn:hover:not(:disabled) { background: var(--bg-hover); color: var(--text-primary); }
.sc-btn:disabled { opacity: 0.5; cursor: default; }
.sc-btn.primary { color: var(--accent-primary); border-color: var(--accent-primary); }
.sc-btn.active { color: var(--accent-primary); border-color: var(--accent-primary); }

.bulk-running { padding: 30px 0; display: flex; flex-direction: column; gap: 10px; align-items: center; }
.bulk-progress-bar {
  width: 60%;
  height: 4px;
  border-radius: 2px;
  background: var(--bg-hover);
  overflow: hidden;
}
.bulk-progress-bar .fill {
  height: 100%;
  background: var(--accent-primary);
  transition: width 0.3s ease;
}
.bulk-progress-text { font-size: 12px; color: var(--text-tertiary); }

.bulk-results-toolbar { display: flex; gap: 8px; margin-bottom: 10px; }

.bulk-cols {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 10px;
  overflow: auto;
  max-height: 52vh;
}
.bulk-col {
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.bulk-col.failed { border-color: var(--accent-red); }
.bulk-col-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  background: var(--bg-hover);
  border-bottom: 1px solid var(--border-subtle);
}
.col-name { font-size: 12px; font-weight: 600; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.col-badge {
  font-size: 10px;
  padding: 0 6px;
  border-radius: 4px;
  flex-shrink: 0;
}
/* 底色用文字那枚令牌自己的色派生：原先的 rgba(63,185,80,.12)/rgba(229,83,75,.12) 是
   终端 ANSI 档，与主题无关，手记的墨绿/朱砂文字就压在一块 GitHub 绿的渍上 */
.col-badge.ok { color: var(--accent-green); background: color-mix(in srgb, var(--accent-green) 12%, transparent); }
.col-badge.fail { color: var(--accent-red); background: color-mix(in srgb, var(--accent-red) 12%, transparent); }
.col-badge.err { color: var(--accent-red); background: color-mix(in srgb, var(--accent-red) 12%, transparent); }
.col-dur { margin-left: auto; font-size: 10px; color: var(--text-tertiary); font-variant-numeric: tabular-nums; }
.bulk-col-body {
  margin: 0;
  padding: 8px 10px;
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-all;
  overflow: auto;
  flex: 1;
  max-height: 300px;
}
.mono { font-family: var(--font-mono); }

.bulk-diff {
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  max-height: 52vh;
  overflow: auto;
  padding: 6px 0;
}
.bulk-diff-line {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 1px 12px;
  font-size: 12px;
  line-height: 1.6;
}
.diff-marker { width: 12px; flex-shrink: 0; color: var(--accent-orange); }
.diff-text { color: var(--text-secondary); word-break: break-all; }
.diff-count {
  margin-left: auto;
  flex-shrink: 0;
  font-size: 10px;
  color: var(--accent-orange);
  background: color-mix(in srgb, var(--accent-orange) 10%, transparent);
  padding: 0 5px;
  border-radius: 4px;
}
.bulk-diff-line.inconsistent { background: color-mix(in srgb, var(--accent-orange) 7%, transparent); }
.bulk-diff-line.inconsistent .diff-text { color: var(--accent-orange); }
.bulk-empty { padding: 30px 0; text-align: center; color: var(--text-tertiary); font-size: 12px; }
</style>
