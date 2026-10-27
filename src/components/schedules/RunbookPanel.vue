<template>
  <div class="runbook-panel">
    <!-- 头部（结构对齐 SchedulesView .schedules-header） -->
    <div class="rb-header">
      <div class="rb-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><path d="M8 6h13M8 12h13M8 18h13"/><path d="M3 6h.01M3 12h.01M3 18h.01"/></svg>
        <span>运维剧本</span>
        <span class="rb-sub" v-if="items.length">共 {{ items.length }} 个剧本</span>
      </div>
      <div class="rb-actions">
        <button class="btn btn-ghost" @click="load()" :disabled="loading">刷新</button>
        <button class="btn btn-primary" @click="openCreate">新建剧本</button>
      </div>
    </div>

    <!-- 剧本卡片 -->
    <div v-if="items.length === 0 && !loading" class="empty-state">暂无运维剧本</div>
    <div v-if="items.length" class="rb-list">
      <div v-for="r in items" :key="r.id" class="rb-card">
        <div class="rb-card-head">
          <div class="rb-name">
            {{ r.name }}
            <span class="toggle" :class="{ on: !!r.enabled }">
              {{ r.enabled ? '已启用' : '已停用' }}
            </span>
          </div>
          <div class="rb-card-actions">
            <button class="btn btn-sm" @click="toggle(r)" :title="r.enabled ? '停用' : '启用'">
              {{ r.enabled ? '停用' : '启用' }}
            </button>
            <button class="btn btn-sm" :disabled="isRunning(r)" @click="openEdit(r)">编辑</button>
            <button class="btn btn-sm" :disabled="isRunning(r)" @click="runNow(r)">执行</button>
            <button class="btn btn-sm" @click="openHistory(r)">历史</button>
            <button class="btn btn-sm btn-danger" @click="remove(r)">删除</button>
          </div>
        </div>
        <div class="rb-card-meta">
          <span class="meta-item" v-if="r.description">{{ r.description }}</span>
          <span class="meta-item">步骤：<b>{{ (r.steps || []).length }}</b></span>
          <span class="meta-item" v-if="r.target_host">目标：<b class="mono">{{ hostLabel(r.target_host) }}</b></span>
          <span class="meta-item">上次执行：{{ r.last_run_at ? formatBackendTs(r.last_run_at) : '—' }}</span>
          <span class="meta-item">
            状态：<span class="status" :class="r.last_status || ''">{{ statusLabel(r.last_status) }}</span>
          </span>
        </div>
        <div class="rb-card-body" v-if="(r.steps || []).length">
          <div class="k">步骤预览</div>
          <ol class="step-preview">
            <li v-for="(s, i) in r.steps" :key="i"><code class="mono">{{ s.command }}</code></li>
          </ol>
        </div>
      </div>
    </div>

    <!-- 执行历史 Modal -->
    <Teleport to="body">
      <div v-if="historyFor" class="modal-mask" @click.self="historyFor = null">
        <div class="modal wide" role="dialog" aria-modal="true">
          <div class="modal-title">{{ historyFor.name }} — 执行历史</div>
          <div class="modal-body">
            <div v-if="historyError" class="history-load-error">
              <span>{{ historyError }}</span>
              <button class="btn btn-sm" @click="fetchHistory()">重试</button>
            </div>
            <div v-else-if="historyRuns.length === 0" class="empty-small">暂无执行记录</div>
            <HistoryList v-else :key="historyFor?.id" :rows="historyRows" :label-map="STATUS_LABEL_RUNBOOK">
              <template #extra="{ row }">
                <!-- 步骤级状态徽章：success/failed/blocked/skipped -->
                <div class="hist-steps" v-if="(stepsOf(row.id) || []).length">
                  <div v-for="st in stepsOf(row.id)" :key="st.seq" class="hist-step">
                    <span class="step-badge" :class="st.status">{{ stepStatusLabel(st.status) }}</span>
                    <code class="mono hist-cmd">{{ st.command }}</code>
                    <span class="hist-verdict" v-if="st.error" :title="st.error">{{ st.error }}</span>
                  </div>
                </div>
                <!-- 自动诊断结论（诊断 run 的输出，失败执行才有） -->
                <div class="history-diagnosis" v-if="diagnosisOf(row.id)">
                  <div class="k">AI 诊断</div>
                  <div class="diagnosis-text">{{ diagnosisOf(row.id) }}</div>
                </div>
              </template>
            </HistoryList>
          </div>
          <div class="modal-actions">
            <button class="btn btn-ghost" @click="historyFor = null">关闭</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 新建/编辑 Modal（v1 保留：不提供编辑 modal 就无法定义剧本） -->
    <Teleport to="body">
      <div v-if="editing" class="modal-mask" @click.self="editing = null">
        <div class="modal wide" role="dialog" aria-modal="true">
          <div class="modal-title">{{ editing.id ? '编辑剧本' : '新建运维剧本' }}</div>
          <div class="modal-body">
            <label class="field">
              <span>剧本名称 *</span>
              <input v-model="form.name" maxlength="100" placeholder="例如：nginx 502 恢复" />
            </label>
            <label class="field">
              <span>说明（可选）</span>
              <input v-model="form.description" maxlength="500" placeholder="这个剧本做什么、什么时候用" />
            </label>
            <label class="field">
              <span>目标主机</span>
              <select v-model="form.target_host">
                <option value="">未指定（步骤命令将没有 SSH 会话可用）</option>
                <option v-for="c in connections" :key="c.id" :value="c.id">
                  {{ connLabel(c) }} — {{ c.username }}@{{ c.host }}
                </option>
              </select>
            </label>
            <div class="field">
              <span>步骤（≤ {{ MAX_STEPS }} 步；每步一条命令 + 可选验证命令）</span>
              <div class="step-editor">
                <div v-for="(s, i) in form.steps" :key="i" class="step-row">
                  <div class="step-row-head">
                    <b>第 {{ i + 1 }} 步</b>
                    <span class="step-ops">
                      <button class="btn btn-xs" :disabled="i === 0" @click="moveStep(i, -1)" title="上移">↑</button>
                      <button class="btn btn-xs" :disabled="i === form.steps.length - 1" @click="moveStep(i, 1)" title="下移">↓</button>
                      <button class="btn btn-xs btn-danger" @click="removeStep(i)">删除</button>
                    </span>
                  </div>
                  <input v-model="s.command" class="mono" maxlength="2000" placeholder="命令（必填，≤2000 字）" />
                  <input v-model="s.assertCommand" class="mono" maxlength="2000" placeholder="验证命令（可选，≤2000 字，用于输出 [步骤结论]）" />
                  <input v-model="s.note" maxlength="500" placeholder="说明（可选，≤500 字）" />
                </div>
                <button class="btn btn-sm" @click="addStep" :disabled="form.steps.length >= MAX_STEPS">
                  + 添加步骤（{{ form.steps.length }}/{{ MAX_STEPS }}）
                </button>
              </div>
            </div>
            <label class="field check">
              <input v-model="form.enabled" type="checkbox" />
              <span>启用</span>
            </label>
            <!-- 表单级校验错误（command 必填 / 长度 / ≤20 步），非空时禁用保存 -->
            <div class="form-err" v-if="formError">{{ formError }}</div>
          </div>
          <div class="modal-actions">
            <button class="btn btn-ghost" @click="editing = null">取消</button>
            <button class="btn btn-primary" @click="save" :disabled="saving || !!formError">保存</button>
          </div>
        </div>
      </div>
    </Teleport>

    <div v-if="loading" class="rb-loading">加载中…</div>
    <div v-if="error" class="rb-error">{{ error }}</div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, computed, watch, onMounted, onUnmounted } from 'vue'
import {
  runbookApi,
  type RunbookRecord,
  type RunbookRunRecord,
  type RunbookStep,
} from '@/api/runbook'
import { toFriendlyMessage } from '@/api/base'
import { useToastStore } from '@/stores/toast'
import { useConfirm } from '@/composables/useConfirm'
import { formatBackendTs } from '@/utils/format'
import { connNote, useSSHStore } from '@/stores/ssh'
import HistoryList from '@/components/schedules/HistoryList.vue'
// shortId/durationText/parseBackendTs 不再引入：历史行渲染已下沉 HistoryList，本面板仅卡片 meta 需 formatBackendTs/statusLabel（strict noUnusedLocals）
import { toRunbookRow, statusLabel as statusLabelOf, STATUS_LABEL_RUNBOOK, type HistoryRow } from '@/utils/history-view'

// SshView 里本面板随「运维剧本」tab 显示；active 由父级（SchedulesView 的分段 tab）传入
const props = defineProps<{ active?: boolean }>()

const toast = useToastStore()
const { confirm } = useConfirm()
const sshStore = useSSHStore()

/** 远程管理页保存的 SSH 连接列表（供目标主机下拉选择） */
const connections = computed(() => sshStore.connections)

function hostLabel(v: string): string {
  const c = sshStore.connections.find(x => x.id === v)
  return c ? `${connNote(c) ? c.name + '（' + connNote(c) + '）' : c.name} (${c.username}@${c.host})` : v
}

function connLabel(c: { name: string; note?: string; tags: string[] }): string {
  const note = connNote(c)
  return note ? `${c.name}（${note}）` : c.name
}

// 与后端 routes/runbooks.py 的上限同口径
const MAX_STEPS = 20
const COMMAND_MAX = 2000
const NOTE_MAX = 500

const loading = ref(false)
const error = ref('')
const items = ref<RunbookRecord[]>([])

const editing = ref<Partial<RunbookRecord> | null>(null)
const saving = ref(false)

interface RunbookForm {
  name: string
  description: string
  target_host: string
  steps: RunbookStep[]
  enabled: boolean
}
const FORM_DEFAULTS: RunbookForm = {
  name: '',
  description: '',
  target_host: '',
  steps: [],
  enabled: true,
}
const form = reactive<RunbookForm>({ ...FORM_DEFAULTS, steps: [] })

function resetForm(): void {
  Object.assign(form, { ...FORM_DEFAULTS, steps: [] })
}

// ── 表单校验（与后端 _validate_steps 同口径；返回第一条错误文案，空串=通过） ──
function stepError(s: RunbookStep, index: number): string {
  const cmd = (s.command || '').trim()
  if (!cmd) return `第 ${index + 1} 步的命令不能为空`
  if (cmd.length > COMMAND_MAX) return `第 ${index + 1} 步的命令需 ≤${COMMAND_MAX} 字`
  if ((s.assertCommand || '').trim().length > COMMAND_MAX) return `第 ${index + 1} 步的验证命令需 ≤${COMMAND_MAX} 字`
  if ((s.note || '').trim().length > NOTE_MAX) return `第 ${index + 1} 步的说明需 ≤${NOTE_MAX} 字`
  return ''
}

const formError = computed(() => {
  if (!form.name.trim()) return '剧本名称不能为空'
  if (form.steps.length === 0) return '剧本至少需要 1 个步骤'
  if (form.steps.length > MAX_STEPS) return `步骤数需 ≤${MAX_STEPS}`
  for (let i = 0; i < form.steps.length; i++) {
    const e = stepError(form.steps[i]!, i)
    if (e) return e
  }
  return ''
})

// ── 列表加载与轮询 ───────────────────────────────────────────────────────────
/** 列表请求序号：runNow 后 3s 轮询、激活期 30s 轮询与手动刷新可并发 */
let _loadSeq = 0

async function load(silent = false) {
  if (!silent) {
    loading.value = true
    error.value = ''
  }
  const seq = ++_loadSeq
  try {
    const rows = await runbookApi.list()
    // 只有最新一次响应能覆盖列表：慢的旧响应会把刚删除/刚改过的行还原成旧快照
    if (seq !== _loadSeq) return
    items.value = rows
    error.value = ''
  } catch (e) {
    if (seq !== _loadSeq) return
    error.value = toFriendlyMessage(e, '剧本加载失败')
  } finally {
    if (!silent && seq === _loadSeq) loading.value = false
  }
}

function isRunning(r: RunbookRecord): boolean {
  return r.last_status === 'running'
}

/** 激活期静默轮询（30s）：无头执行无流可看，状态变化只能靠列表刷新感知 */
const LIST_POLL_MS = 30000
let listPollTimer: ReturnType<typeof setInterval> | null = null

function startListPoll() {
  if (listPollTimer !== null) return
  listPollTimer = setInterval(() => { load(true) }, LIST_POLL_MS)
}

function stopListPoll() {
  if (listPollTimer === null) return
  clearInterval(listPollTimer)
  listPollTimer = null
}

// active 缺省（独立渲染场景）按活跃处理，保证至少加载一次
watch(() => props.active, (on) => {
  if (on || props.active === undefined) {
    load()
    startListPoll()
  } else {
    stopListPoll()
  }
}, { immediate: true })

// ── 执行与短轮询 ─────────────────────────────────────────────────────────────
/** 「执行」后的状态轮询定时器集合：组件卸载时统一清理 */
const runPollTimers = new Set<ReturnType<typeof setInterval>>()

function stopPoll(t: ReturnType<typeof setInterval>) {
  clearInterval(t)
  runPollTimers.delete(t)
}

const RUN_POLL_MS = 3000
const RUN_POLL_MAX = 100

async function runNow(r: RunbookRecord) {
  try {
    await runbookApi.runNow(r.id)
    toast.success('已触发执行')
    // 无头执行无流：3s 短轮询列表直到该剧本离开 running（删除/达到上限即停，
    // 防 setInterval 泄漏 —— SchedulesView runNow 同款约束）。
    // 定时器先登记再首拉：卸载落在下方 await 窗口内时也能被 onUnmounted 统一清理
    let polls = 0
    const t = setInterval(async () => {
      if (++polls > RUN_POLL_MAX) { stopPoll(t); return }
      try {
        await load(true)
        const latest = items.value.find((x) => x.id === r.id)
        if (!latest || !isRunning(latest)) stopPoll(t)
      } catch { /* 忽略轮询错误 */ }
    }, RUN_POLL_MS)
    runPollTimers.add(t)
    await load()
  } catch (e: any) {
    toast.error(toFriendlyMessage(e, '触发失败'))
  }
}

// ── 编辑 modal ───────────────────────────────────────────────────────────────
function openCreate() {
  resetForm()
  editing.value = {}
}

function openEdit(r: RunbookRecord) {
  resetForm()
  form.name = r.name
  form.description = r.description ?? ''
  form.target_host = r.target_host ?? ''
  form.enabled = !!r.enabled
  // 深拷贝步骤，编辑中途取消不污染列表数据
  form.steps = (r.steps || []).map(s => ({
    command: s.command || '',
    assertCommand: s.assertCommand || '',
    note: s.note || '',
  }))
  editing.value = r
}

function addStep() {
  if (form.steps.length >= MAX_STEPS) return
  form.steps.push({ command: '', assertCommand: '', note: '' })
}

function removeStep(i: number) {
  form.steps.splice(i, 1)
}

function moveStep(i: number, dir: -1 | 1) {
  const j = i + dir
  if (j < 0 || j >= form.steps.length) return
  const [s] = form.steps.splice(i, 1)
  form.steps.splice(j, 0, s!)
}

async function save() {
  if (formError.value) return
  saving.value = true
  try {
    const payload = {
      name: form.name.trim(),
      description: form.description.trim(),
      target_host: form.target_host.trim(),
      steps: form.steps.map(s => ({
        command: (s.command || '').trim(),
        assertCommand: (s.assertCommand || '').trim(),
        note: (s.note || '').trim(),
      })),
      enabled: form.enabled,
    }
    if (editing.value?.id) {
      await runbookApi.update(editing.value.id, payload)
      toast.success('剧本已更新')
    } else {
      await runbookApi.create(payload)
      toast.success('剧本已创建')
    }
    editing.value = null
    await load()
  } catch (e) {
    toast.error(toFriendlyMessage(e, '保存失败'))
  } finally {
    saving.value = false
  }
}

async function toggle(r: RunbookRecord) {
  try {
    await runbookApi.update(r.id, { enabled: !r.enabled })
    toast.success(r.enabled ? '已停用' : '已启用')
    await load()
  } catch (e) {
    toast.error(toFriendlyMessage(e, '更新失败'))
  }
}

async function remove(r: RunbookRecord) {
  const ok = await confirm({
    title: '删除运维剧本',
    message: `确认删除剧本「${r.name}」及其全部执行历史？`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  try {
    await runbookApi.remove(r.id)
    toast.success('已删除')
    await load()
  } catch (e) {
    toast.error(toFriendlyMessage(e, '删除失败'))
  }
}

// ── 历史 modal ───────────────────────────────────────────────────────────────
const historyFor = ref<RunbookRecord | null>(null)
const historyRuns = ref<RunbookRunRecord[]>([])
const historyError = ref('')
const historyRows = computed<HistoryRow[]>(() => historyRuns.value.map(toRunbookRow))
// HistoryRow 是窄契约：步骤/诊断全文按 id 反查原始记录（同 SchedulesView rawRunOf 模式）
function stepsOf(id: string) { return historyRuns.value.find((r) => r.id === id)?.steps_state }
function diagnosisOf(id: string) { return historyRuns.value.find((r) => r.id === id)?.diagnosis || '' }

// 历史请求序号：快速切换剧本时旧响应晚到会挂到新剧本的历史面板上
let _historySeq = 0

async function openHistory(r: RunbookRecord) {
  historyFor.value = r
  historyRuns.value = []
  historyError.value = ''
  await fetchHistory()
}

async function fetchHistory() {
  const r = historyFor.value
  if (!r) return
  historyError.value = ''
  const seq = ++_historySeq
  try {
    const runs = await runbookApi.runs(r.id)
    if (seq !== _historySeq) return
    historyRuns.value = runs
  } catch (e) {
    if (seq !== _historySeq) return
    historyError.value = toFriendlyMessage(e, '加载历史失败')
    toast.error(historyError.value)
  }
}

// ── 展示辅助 ─────────────────────────────────────────────────────────────────
// 卡片 meta 状态标签（历史列表标签已由 HistoryList 内部处理）；表/逻辑沉到 history-view 共享
function statusLabel(s?: string | null): string {
  return statusLabelOf(s, STATUS_LABEL_RUNBOOK, '未执行')
}

const STEP_STATUS_LABEL: Record<string, string> = {
  success: '已通过',
  failed: '失败',
  blocked: '已拦截',
  skipped: '已跳过',
  pending: '待执行',
}
function stepStatusLabel(s: string): string {
  return STEP_STATUS_LABEL[s] || s
}

// shortId/parseTs/durationText 已下沉 history-view（历史列表由组件内部调用）；本面板不再直接用

// Escape 关闭当前打开的弹窗
function handleKeydown(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  if (historyFor.value) { historyFor.value = null; return }
  if (editing.value) editing.value = null
}

onMounted(() => {
  if (sshStore.connections.length === 0) {
    sshStore.loadConnections()
  }
  window.addEventListener('keydown', handleKeydown)
})

onUnmounted(() => {
  window.removeEventListener('keydown', handleKeydown)
  stopListPoll()
  for (const t of runPollTimers) clearInterval(t)
  runPollTimers.clear()
})
</script>

<style scoped>
.runbook-panel {
  display: flex;
  flex-direction: column;
  flex: 1;
  overflow: hidden;
  background: var(--canvas-bg);
}
.rb-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: var(--page-head-h);
  padding: 0 16px;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.rb-title { display: flex; align-items: center; gap: 8px; font-size: var(--page-title-size); font-weight: 600; color: var(--text-primary); font-family: var(--font-sans); }
.rb-sub { font-size: 12px; color: var(--text-tertiary); font-weight: 400; }
.rb-actions { display: flex; gap: 8px; }

.rb-list { flex: 1; overflow: auto; display: flex; flex-direction: column; gap: 10px; padding: 12px 16px 16px; }
.rb-card {
  border: 1px solid var(--border-subtle);
  border-left: 3px solid var(--accent-primary);
  border-radius: 8px;
  padding: 12px 14px;
}
.rb-card-head { display: flex; align-items: center; justify-content: space-between; }
.rb-name { font-size: 14px; font-weight: 600; color: var(--text-primary); display: flex; align-items: center; gap: 8px; }
.toggle, .status { font-size: 11px; padding: 1px 8px; border-radius: 8px; border: 1px solid var(--border-subtle); }
.toggle.on { color: var(--accent-green); border-color: var(--accent-green); }
.rb-card-actions { display: flex; gap: 6px; }
.rb-card-meta { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: var(--text-secondary); margin-top: 8px; }
.meta-item { text-align: left; }
.meta-item b { color: var(--text-primary); font-weight: 600; }
.mono { font-family: var(--font-mono); }
.rb-card-body { margin-top: 8px; font-size: 12px; }
.rb-card-body .k { color: var(--text-tertiary); margin-bottom: 4px; text-align: left; }
.step-preview { margin: 0; padding-left: 20px; color: var(--text-secondary); }
.step-preview li { text-align: left; white-space: pre-wrap; word-break: break-all; }

.status.running { color: var(--accent-text); border-color: var(--accent-primary); }
.status.success, .status.completed { color: var(--accent-green); border-color: var(--accent-green); }
.status.failed { color: var(--accent-red); border-color: var(--accent-red); }
.status.stopped, .status.stale_reaped { color: var(--accent-orange); border-color: var(--accent-orange); }

.empty-state { flex: 1; display: flex; align-items: center; justify-content: center; color: var(--text-tertiary); font-size: 13px; padding: 16px; }
.rb-loading { color: var(--text-tertiary); font-size: 12px; padding: 0 16px 12px; }
.rb-error { color: var(--accent-red); font-size: 12px; padding: 0 16px 12px; }

.modal-mask { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.modal { width: 520px; max-width: 90vw; background: var(--bg-elevated); border: 1px solid var(--border-subtle); border-radius: 10px; padding: 16px; max-height: 80vh; overflow: auto; }
.modal.wide { width: 680px; }
.modal-title { font-size: 14px; font-weight: 600; color: var(--text-primary); margin-bottom: 12px; }
.modal-body { display: flex; flex-direction: column; gap: 10px; }
.field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--text-tertiary); }
.field input, .field select, .step-row input {
  font-size: 12px; padding: 6px 8px; border-radius: 6px;
  border: 1px solid var(--border-subtle); background: var(--bg-input); color: var(--text-primary);
}
.field.check { flex-direction: row; align-items: center; gap: 6px; }

.step-editor { display: flex; flex-direction: column; gap: 8px; }
.step-row { border: 1px solid var(--border-subtle); border-radius: 6px; padding: 8px; display: flex; flex-direction: column; gap: 6px; }
.step-row-head { display: flex; align-items: center; justify-content: space-between; font-size: 12px; color: var(--text-secondary); }
.step-ops { display: flex; gap: 4px; }
.btn-xs { padding: 1px 6px; font-size: 11px; }

.form-err { color: var(--accent-red); font-size: 12px; }

.history-load-error {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  font-size: 12px; color: var(--accent-red);
  border: 1px solid var(--accent-red); border-radius: 6px; padding: 8px 10px;
}
.empty-small { color: var(--text-tertiary); font-size: 12px; padding: 20px 0; text-align: center; }

.hist-steps { display: flex; flex-direction: column; gap: 4px; margin-top: 8px; }
.hist-step { display: flex; align-items: baseline; gap: 8px; font-size: 12px; }
.hist-step .hist-cmd { color: var(--text-secondary); white-space: pre-wrap; word-break: break-all; text-align: left; }
.hist-verdict { color: var(--text-tertiary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 40%; }
.step-badge { flex-shrink: 0; font-size: 11px; padding: 1px 8px; border-radius: 8px; border: 1px solid var(--border-subtle); }
.step-badge.success { color: var(--accent-green); border-color: var(--accent-green); }
.step-badge.failed { color: var(--accent-red); border-color: var(--accent-red); }
.step-badge.blocked { color: var(--accent-orange); border-color: var(--accent-orange); }
.step-badge.skipped { color: var(--text-tertiary); }
.step-badge.pending { color: var(--text-secondary); }

.history-diagnosis { margin-top: 8px; }
.history-diagnosis .k { color: var(--text-tertiary); font-size: 12px; margin-bottom: 4px; text-align: left; }
.diagnosis-text { font-size: 12px; color: var(--text-secondary); white-space: pre-wrap; max-height: 150px; overflow: auto; text-align: left; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
</style>
