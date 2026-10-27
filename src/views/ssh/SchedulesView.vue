<template>
  <div class="schedules-view">
    <!-- 入口分段 tab：定时任务 | 运维剧本（C v1 收敛为本页内 tab，零共享导航文件改动） -->
    <div class="entry-tabs">
      <button :class="{ on: entryTab === 'schedules' }" @click="entryTab = 'schedules'">定时任务</button>
      <button :class="{ on: entryTab === 'runbooks' }" @click="entryTab = 'runbooks'">运维剧本</button>
    </div>
    <div v-show="entryTab !== 'runbooks'" class="schedules-pane">
    <!-- 头部 -->
    <div class="schedules-header">
      <div class="schedules-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 16 14"/></svg>
        <span>定时任务</span>
        <span class="schedules-sub" v-if="items.length">共 {{ items.length }} 个任务</span>
      </div>
      <div class="schedules-actions">
        <button class="btn btn-ghost" @click="load()" :disabled="loading">刷新</button>
        <button class="btn btn-primary" @click="openCreate">新建任务</button>
      </div>
    </div>

    <!-- 任务卡片 -->
    <div v-if="items.length === 0 && !loading" class="empty-state">暂无定时任务</div>
    <div v-if="items.length" class="schedule-list">
      <template v-for="section in sections" :key="section.type">
      <div v-if="section.items.length" class="schedule-section-title">
        {{ section.title }} <span class="cnt">{{ section.items.length }}</span>
      </div>
      <div v-for="s in section.items" :key="s.id" class="schedule-card" :class="{ daily: section.type === 'daily' }">
        <div class="schedule-card-head">
          <div class="schedule-name">
            {{ s.name }}
            <span class="toggle" :class="{ on: !!s.enabled }">
              {{ s.enabled ? '已启用' : '已停用' }}
            </span>
          </div>
          <div class="schedule-card-actions">
            <button class="btn btn-sm" @click="toggle(s)" :title="s.enabled ? '停用' : '启用'">
              {{ s.enabled ? '停用' : '启用' }}
            </button>
            <button class="btn btn-sm" :disabled="isRunning(s)" @click="openEdit(s)">编辑</button>
            <button class="btn btn-sm" @click="runNow(s)" :disabled="isRunning(s)">立即执行</button>
            <button class="btn btn-sm" @click="openHistory(s)">历史</button>
            <button class="btn btn-sm btn-danger" @click="remove(s)">删除</button>
          </div>
        </div>
        <div class="schedule-card-meta">
          <span class="meta-item">周期：{{ scheduleText(s) }}</span>
          <span class="meta-item" v-if="s.target_host">目标：<b class="mono">{{ hostLabel(s.target_host) }}</b></span>
          <span class="meta-item">上次执行：{{ s.last_run_at ? formatBackendTs(s.last_run_at) : '—' }}</span>
          <span class="meta-item">
            状态：<span class="status" :class="s.last_status">{{ statusLabel(s.last_status) }}</span>
          </span>
          <span class="meta-item" v-if="s.enabled && s.next_run_at">下次：{{ formatBackendTs(s.next_run_at) }}<span v-if="countdownText(s)" class="countdown">（{{ countdownText(s) }}后）</span></span>
        </div>
        <div class="schedule-card-body" v-if="s.task_prompt">
          <div class="k">任务提示</div>
          <div class="prompt">{{ s.task_prompt }}</div>
        </div>
      </div>
      </template>
    </div>

    <!-- 空历史 Modal -->
    <Teleport to="body">
    <div v-if="historyFor" class="modal-mask" @click.self="historyFor = null">
      <div class="modal wide" role="dialog" aria-modal="true">
        <div class="modal-title">{{ historyTitle }} — 执行历史</div>
        <div class="modal-body">
          <div v-if="historyRuns.length === 0" class="empty-small">暂无执行记录</div>
          <div v-else class="history-list">
            <div v-for="r in historyRuns" :key="r.id" class="history-item">
              <div class="history-head">
                <span class="mono">{{ formatBackendTs(r.started_at) }}</span>
                <span class="status" :class="r.status">{{ statusLabel(r.status) }}</span>
                <span class="mono" v-if="durationText(r)">{{ durationText(r) }}</span>
                <span class="mono" v-if="r.run_id">run {{ shortId(r.run_id) }}</span>
              </div>
              <div class="history-summary" v-if="r.summary">{{ r.summary }}</div>
              <div class="history-error" v-if="r.error">{{ r.error }}</div>
              <div class="history-actions" v-if="r.status === 'failed' && historyFor">
                <button class="btn btn-sm" @click="askAiDiagnose(historyFor, r)">⚡ AI 诊断此失败</button>
              </div>
            </div>
          </div>
        </div>
        <div class="modal-actions">
          <button class="btn btn-ghost" @click="historyFor = null">关闭</button>
        </div>
      </div>
    </div>
    </Teleport>

    <!-- 新建/编辑 Modal -->
    <Teleport to="body">
    <div v-if="editing" class="modal-mask" @click.self="editing = null">
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-title schedule-modal-title">
          <span>{{ editing.id ? '编辑任务' : '新建定时任务' }}</span>
          <div class="type-switch" :class="{ disabled: !!editing.id }" :title="editing.id ? '任务类型创建后不可修改' : '任务类型'">
            <button class="sched-option" :class="{ active: form.task_type === 'ops' }"
              :disabled="!!editing.id" @click="switchType('ops')">运维</button>
            <button class="sched-option" :class="{ active: form.task_type === 'daily' }"
              :disabled="!!editing.id" @click="switchType('daily')">日常</button>
          </div>
        </div>
        <div class="modal-body">
          <!-- 模板选择（仅新建时显示） -->
          <div v-if="!editing.id" class="field">
            <span>选择模板</span>
            <div class="template-list">
              <button
                v-for="t in visibleTemplates"
                :key="t.id"
                class="template-chip"
                :class="{ active: selectedTemplate === t.id }"
                @click="applyTemplate(t)"
              >
                <div class="template-name">{{ t.name }}</div>
                <div class="template-desc">{{ t.desc }}</div>
              </button>
            </div>
          </div>
          <label class="field">
            <span>任务名称 *</span>
            <!-- maxlength 与后端 Field(max_length=100) 同口径（S7-11）：超出去是 422，
                 而名称只是展示用串，静默截断不改变任何判定 —— 与 AgentPanel 那条
                 「截断会让授权键对不上」的顾虑不同族。 -->
            <input v-model="form.name" maxlength="100" placeholder="例如：每日系统健康巡检" />
          </label>
          <div class="field">
            <span>触发时间</span>
            <div class="sched-options">
              <button
                class="sched-option"
                :class="{ active: form.schedule_type === 'interval' }"
                @click="form.schedule_type = 'interval'"
              >每 N 分钟</button>
              <button
                class="sched-option"
                :class="{ active: form.schedule_type === 'daily' }"
                @click="form.schedule_type = 'daily'"
              >每天</button>
              <button
                class="sched-option"
                :class="{ active: form.schedule_type === 'weekly' }"
                @click="form.schedule_type = 'weekly'"
              >每周</button>
              <button
                class="sched-option"
                :class="{ active: form.schedule_type === 'monthly' }"
                @click="form.schedule_type = 'monthly'"
              >每月</button>
            </div>

            <!-- 每 N 分钟 -->
            <div v-if="form.schedule_type === 'interval'" class="sched-subrow">
              <span>每</span>
              <input v-model.number="form.interval_minutes" type="number" min="1" max="1440" class="num" />
              <span>分钟执行一次</span>
            </div>

            <!-- 每天 -->
            <div v-else-if="form.schedule_type === 'daily'" class="sched-subrow">
              <span>每天</span>
              <input v-model.number="form.time_hour" type="number" min="0" max="23" class="num" /> :
              <input v-model.number="form.time_minute" type="number" min="0" max="59" class="num" />
            </div>

            <!-- 每周 -->
            <div v-else-if="form.schedule_type === 'weekly'" class="sched-subrow">
              <span>每周</span>
              <select v-model.number="form.weekday" class="sel">
                <option v-for="(w, i) in WEEKDAYS" :key="w" :value="i">{{ w }}</option>
              </select>
              <input v-model.number="form.time_hour" type="number" min="0" max="23" class="num" /> :
              <input v-model.number="form.time_minute" type="number" min="0" max="59" class="num" />
            </div>

            <!-- 每月 -->
            <div v-else class="sched-subrow">
              <span>每月第</span>
              <input v-model.number="form.day_of_month" type="number" min="1" max="28" class="num" />
              <span>日</span>
              <input v-model.number="form.time_hour" type="number" min="0" max="23" class="num" /> :
              <input v-model.number="form.time_minute" type="number" min="0" max="59" class="num" />
            </div>
          </div>
          <label class="field">
            <span>目标主机（可选）</span>
            <select v-model="form.target_host">
              <option value="">未指定（不执行远程命令）</option>
              <option v-for="c in connections" :key="c.id" :value="c.id">
                {{ connLabel(c) }} — {{ c.username }}@{{ c.host }}
              </option>
            </select>
          </label>
          <label class="field">
            <span>任务提示（留空用内置模板）</span>
            <textarea
              v-model="form.task_prompt"
              rows="4"
              placeholder="描述要执行的任务，例如：对目标系统执行只读健康巡检：CPU/内存/磁盘/服务状态，输出异常与建议。"
            ></textarea>
          </label>
          <label class="field check">
            <input v-model="form.enabled" type="checkbox" />
            <span>启用定时执行</span>
          </label>
        </div>
        <div class="modal-actions">
          <button class="btn btn-ghost" @click="editing = null">取消</button>
          <button class="btn btn-primary" @click="save" :disabled="saving || !form.name.trim()">保存</button>
        </div>
      </div>
    </div>
    </Teleport>

    <div v-if="loading" class="sc-loading">加载中…</div>
    <div v-if="error" class="sc-error">{{ error }}</div>
    </div>
    <RunbookPanel v-if="entryTab === 'runbooks'" :active="entryTab === 'runbooks'" />
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, computed, watch, onMounted, onUnmounted } from 'vue'
import {
  scheduleApi,
  toFriendlyMessage,
  type ScheduleRecord,
  type ScheduleRun,
} from '@/api/client'
import { useToastStore } from '@/stores/toast'
import { useConfirm } from '@/composables/useConfirm'
import { formatBackendTs, parseBackendTs } from '@/utils/format'
import { pickNotices } from '@/utils/schedule-notify'
import {
  groupSchedules, templatesForType,
  type TaskType,
} from '@/utils/schedule-task-type'
import { connNote, useSSHStore } from '@/stores/ssh'
import { useSshAiStore } from '@/stores/ssh-ai'
import { useAgentStore } from '@/stores/agent'
import RunbookPanel from '@/components/schedules/RunbookPanel.vue'

// SshView 里本页 sticky-visited（首访挂载、之后保持），active 标记当前是否停留在定时任务页
const props = defineProps<{ active?: boolean }>()

// 入口分段：定时任务 | 运维剧本（运维剧本承载在 RunbookPanel，自身带加载/轮询）
const entryTab = ref<'schedules' | 'runbooks'>('schedules')

const toast = useToastStore()
const { confirm } = useConfirm()
const sshStore = useSSHStore()
const sshAiStore = useSshAiStore()
const agentStore = useAgentStore()

/** 远程管理页保存的 SSH 连接列表（供目标主机下拉选择） */
const connections = computed(() => sshStore.connections)

function hostLabel(v: string): string {
  const c = sshStore.connections.find(x => x.id === v)
  return c ? `${connLabel(c)} (${c.username}@${c.host})` : v
}

/** 连接显示名：备注存在时以「名称（备注）」呈现，与其他列表的备注展示同口径 */
function connLabel(c: { name: string; note?: string; tags: string[] }): string {
  const note = connNote(c)
  return note ? `${c.name}（${note}）` : c.name
}

const loading = ref(false)
const error = ref('')
const items = ref<ScheduleRecord[]>([])

// 列表分区（spec 2026-10-27-schedule-task-type）：存量/缺省归运维，空区整块隐藏
const grouped = computed(() => groupSchedules(items.value))
const opsItems = computed(() => grouped.value.ops)
const dailyItems = computed(() => grouped.value.daily)
// 单一卡片体遍历两个分区（运维上、日常下）；空区在模板层用 v-if 整块隐藏
const sections = computed(() => [
  { type: 'ops' as TaskType, title: '运维任务', items: opsItems.value },
  { type: 'daily' as TaskType, title: '日常任务', items: dailyItems.value },
])

const editing = ref<ScheduleRecord | null>(null)
const saving = ref(false)
const selectedTemplate = ref<string | null>(null)
type ScheduleType = 'interval' | 'daily' | 'weekly' | 'monthly'
interface ScheduleForm {
  name: string
  task_type: TaskType
  schedule_type: ScheduleType
  interval_minutes: number
  time_hour: number
  time_minute: number
  weekday: number
  day_of_month: number
  target_host: string
  task_prompt: string
  enabled: boolean
}
// 初始值只有一份：原先 reactive 的初值与 openCreate 的清空各抄一遍 10 个字段，
// 新增字段必然漏掉其中一处（新建表单带着上一个任务的字段值提交）。
const FORM_DEFAULTS: ScheduleForm = {
  name: '',
  task_type: 'ops',
  schedule_type: 'interval',
  interval_minutes: 60,
  time_hour: 9,
  time_minute: 0,
  weekday: 0,
  day_of_month: 1,
  target_host: '',
  task_prompt: '',
  enabled: true,
}
const form = reactive<ScheduleForm>({ ...FORM_DEFAULTS })

function resetForm(): void {
  Object.assign(form, FORM_DEFAULTS)
}

// 新建时切换类型：只换模板集、清模板选中态，不重置已填字段；编辑态锁定不可改
function switchType(t: TaskType): void {
  if (editing.value?.id) return
  if (form.task_type === t) return
  form.task_type = t
  selectedTemplate.value = null
}

const WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

const hh = (n: number) => String(n).padStart(2, '0')
const MINUTE_LABEL = (h: number, m: number) => `${hh(h)}:${hh(m)}`
/** 周期展示文案 */
function scheduleText(s: ScheduleRecord): string {
  switch (s.schedule_type) {
    case 'interval':
      return `每 ${s.interval_minutes} 分钟`
    case 'daily':
      return `每天 ${MINUTE_LABEL(s.time_hour, s.time_minute)}`
    case 'weekly':
      return `每周${WEEKDAYS[s.weekday] || s.weekday} ${MINUTE_LABEL(s.time_hour, s.time_minute)}`
    case 'monthly':
      return `每月${s.day_of_month}日 ${MINUTE_LABEL(s.time_hour, s.time_minute)}`
    default:
      return `每 ${s.interval_minutes} 分钟`
  }
}

interface Template {
  id: string
  name: string
  desc: string
  prompt: string
  task_type?: TaskType
  schedule_type?: ScheduleType
  defaultInterval?: number
  time_hour?: number
  weekday?: number
  day_of_month?: number
}
const templates: Template[] = [
  {
    id: 'system-patrol',
    name: '定时巡检',
    desc: '系统健康巡检（CPU/内存/磁盘/服务）',
    schedule_type: 'interval',
    defaultInterval: 60,
    prompt: '对目标主机系统做只读健康巡检：\n1. 检查 CPU、内存、磁盘使用率\n2. 检查关键系统服务状态\n3. 检查系统负载和连接数\n4. 输出异常指标和优化建议\n仅做信息采集，不执行修改操作。',
  },
  {
    id: 'log-cleanup',
    name: '日志清理',
    desc: '定时清理过期日志文件',
    schedule_type: 'weekly',
    weekday: 0,
    time_hour: 2,
    prompt: '检查目标主机上的日志目录，清理 7 天前的过期日志文件，\n报告清理前后磁盘空间变化。\n仅删除过期文件，确认安全后再执行。',
  },
  {
    id: 'backup-check',
    name: '备份检查',
    desc: '检查备份文件是否生成成功',
    schedule_type: 'daily',
    time_hour: 9,
    prompt: '检查备份目录下今日是否成功生成了完整的备份文件，\n验证文件大小和完整性，\n如发现备份缺失或异常立即报告。',
  },
  {
    id: 'bill-summary',
    name: '月度对账',
    desc: '每月初汇总资源用量并生成对账报告',
    schedule_type: 'monthly',
    day_of_month: 1,
    time_hour: 9,
    prompt: '汇总上月的资源使用与账单情况，比对配置与用量，\n生成结构化月度对账报告，标注异常扣费和优化建议。',
  },
  // ── 日常任务内置模板（spec 2026-10-27-schedule-task-type）──
  {
    id: 'daily-brief',
    name: '天气/资讯摘要提醒',
    desc: '每天生成当天天气与要点摘要',
    task_type: 'daily',
    schedule_type: 'daily',
    time_hour: 8,
    prompt: '汇总当天的天气、待办与要点信息，生成一条简洁的每日摘要提醒，面向个人日常使用。',
  },
  {
    id: 'weekly-report',
    name: '周报素材汇总',
    desc: '每周五收集本周要点形成周报素材',
    task_type: 'daily',
    schedule_type: 'weekly',
    weekday: 4,
    time_hour: 16,
    prompt: '收集本周完成的任务、进行中的事项与风险，整理成结构化周报素材草稿。',
  },
  {
    id: 'file-archive',
    name: '文件归档整理',
    desc: '按类型/日期归类整理目录',
    task_type: 'daily',
    schedule_type: 'monthly',
    day_of_month: 1,
    time_hour: 10,
    prompt: '对目标目录按文件类型与修改日期归类整理，先给出只读整理预览清单，确认后再执行。',
  },
]

// 模板 chips 按当前表单类型过滤（运维=现有模板，日常=上面 3 条）
const visibleTemplates = computed<Template[]>(() =>
  templatesForType(templates, form.task_type))

function applyTemplate(t: Template) {
  selectedTemplate.value = t.id
  // 切换模板时始终更新任务名称为模板名，便于一眼识别模板对应任务
  form.name = t.name
  if (t.schedule_type) form.schedule_type = t.schedule_type
  if (t.defaultInterval !== undefined) form.interval_minutes = t.defaultInterval
  if (t.time_hour !== undefined) form.time_hour = t.time_hour
  if (t.weekday !== undefined) form.weekday = t.weekday
  if (t.day_of_month !== undefined) form.day_of_month = t.day_of_month
  form.time_minute = FORM_DEFAULTS.time_minute
  form.task_prompt = t.prompt ?? ''
}

const historyFor = ref<ScheduleRecord | null>(null)
const historyRuns = ref<ScheduleRun[]>([])
const historyTitle = computed(() => historyFor.value?.name || '')

/** 列表请求序号：runNow 后 3s 轮询、页面激活期 30s 轮询与手动/操作后刷新可并发 */
let _loadSeq = 0

async function load(silent = false) {
  // silent：轮询刷新时不置 loading —— 否则每 3s 闪一次"加载中…"并禁用刷新按钮
  if (!silent) {
    loading.value = true
    error.value = ''
  }
  const seq = ++_loadSeq
  try {
    const rows = await scheduleApi.list()
    // 只有最新一次响应能覆盖列表：慢的旧响应会把刚删除/刚改过的行还原成旧快照
    if (seq !== _loadSeq) return
    // 先比对再覆盖：否则基线永远是新值，转态通知一次也发不出
    notifyTransitions(items.value, rows)
    items.value = rows
    error.value = ''
  } catch (e) {
    if (seq !== _loadSeq) return
    error.value = toFriendlyMessage(e, '巡检任务加载失败')
  } finally {
    if (!silent && seq === _loadSeq) loading.value = false
  }
}

/**
 * 轮询到新一次执行有结果时补桌面通知（G8）。判据是纯函数，见
 * `src/utils/schedule-notify.ts`（S7-08：按 last_run_id 去重，不是按状态字符串）；
 * 是否真弹由主进程判定（总开关 + 窗口是否聚焦 + 系统支持），这里只管发。
 */
function notifyTransitions(prev: ScheduleRecord[], next: ScheduleRecord[]) {
  const api = window.electronAPI
  if (!api?.notifyDesktop) return
  for (const notice of pickNotices(prev, next)) {
    // 桌面通知是 best-effort：ipcRenderer.invoke 会因主进程 assertTrustedSender/
    // normalizeIpcError 抛错而 reject，不吞则成未处理拒绝（同 ptyResize/ptyDestroy 类）。
    void api.notifyDesktop(notice.title, notice.body).catch(() => {})
  }
}

function isRunning(s: ScheduleRecord): boolean {
  return s.last_status === 'running'
}

/** 页面激活期静默轮询：与后端调度 tick 同周期（30s），覆盖引擎后台触发导致的
 *  last_status / next_run_at 变化；切走即停 */
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

// immediate：sticky-visited v-if 挂载即 active=true，无初始 false→true 跳变，
// 首访的加载与轮询自启必须由 immediate 回调完成（onMounted 不再重复 load）
watch(() => props.active, (on) => {
  // props 缺省（独立渲染场景）按活跃处理，保证至少加载一次
  if (on || props.active === undefined) {
    load()
    startListPoll()
  } else {
    stopListPoll()
  }
}, { immediate: true })

// AI 聊天里 schedule_op 直接写库（绕过 REST），结果事件驱动列表失效刷新
watch(() => agentStore.scheduleRev, () => { load(true) })

function openCreate() {
  resetForm()
  formBaseline = { ...form }
  selectedTemplate.value = null
  editing.value = {} as ScheduleRecord
}

/**
 * S7-12：进入表单时的那一份字段值，用于「只发改过的键」。
 *
 * 后端的 PATCH 契约是「不发 = 不改」，并按**键是否出现**判断调度参数变了
 * （`routes/schedules.py` 的 `_SCHEDULE_KEYS & set(patch.keys())`）⇒ 原实现每次保存
 * 都带上全部十个键，只改个错别字也会把 interval 任务的 `next_run_at` 重锚到
 * 「现在 + 一个周期」：本来马上要跑的那一次被整整推迟一个周期。
 * 基线取 `openEdit` 回填**之后**的表单（缺省值已在那一步归一），不再另抄一份默认值。
 */
let formBaseline: ScheduleForm = { ...FORM_DEFAULTS }

/** 编辑已有任务：把记录回填到表单并进入编辑态（模板选择在编辑态隐藏） */
function openEdit(s: ScheduleRecord) {
  // 缺省值取 FORM_DEFAULTS：原样字面量是第三份默认值副本，改一处就与新建表单分叉
  form.name = s.name
  form.task_type = s.task_type ?? 'ops'
  form.schedule_type = s.schedule_type
  form.interval_minutes = s.interval_minutes ?? FORM_DEFAULTS.interval_minutes
  form.time_hour = s.time_hour ?? FORM_DEFAULTS.time_hour
  form.time_minute = s.time_minute ?? FORM_DEFAULTS.time_minute
  form.weekday = s.weekday ?? FORM_DEFAULTS.weekday
  form.day_of_month = s.day_of_month ?? FORM_DEFAULTS.day_of_month
  form.target_host = s.target_host ?? ''
  form.task_prompt = s.task_prompt ?? ''
  form.enabled = !!s.enabled
  formBaseline = { ...form }
  selectedTemplate.value = null
  editing.value = s
}

function formPayload() {
  // number 输入被清空时 v-model.number 留下的是原始 ''（Vue 的 looseToNumber
  // 只在能转成数字时才转），`'' ?? 9` 不会兜底 → 后端 int 字段 422
  const numOr = (v: unknown, fallback: number): number =>
    typeof v === 'number' && Number.isFinite(v) ? v : fallback
  return {
    name: form.name.trim(),
    task_type: form.task_type,
    schedule_type: form.schedule_type,
    interval_minutes: numOr(form.interval_minutes, FORM_DEFAULTS.interval_minutes),
    time_hour: numOr(form.time_hour, FORM_DEFAULTS.time_hour),
    time_minute: numOr(form.time_minute, FORM_DEFAULTS.time_minute),
    weekday: numOr(form.weekday, FORM_DEFAULTS.weekday),
    day_of_month: numOr(form.day_of_month, FORM_DEFAULTS.day_of_month),
    target_host: form.target_host.trim(),
    task_prompt: form.task_prompt.trim(),
    enabled: form.enabled,
  }
}

/** 只保留与进入表单时不同的键（S7-12，见 formBaseline 的注释） */
function changedPayload(): Partial<ScheduleForm> {
  const current = formPayload()
  const out: Record<string, unknown> = {}
  for (const key of Object.keys(current) as (keyof ScheduleForm)[]) {
    if (current[key] !== formBaseline[key]) out[key] = current[key]
  }
  return out as Partial<ScheduleForm>
}

async function save() {
  if (!form.name.trim()) return
  saving.value = true
  try {
    if (editing.value?.id) {
      const patch = changedPayload()
      if (Object.keys(patch).length === 0) {
        // 一个键都没变就不发请求：空 PATCH 会走进 update_schedule 的「无 SET」分支，
        // 而这里真正想说的是「什么都没改，别动 next_run_at」
        editing.value = null
        return
      }
      await scheduleApi.update(editing.value.id, patch)
      toast.success('任务已更新')
    } else {
      await scheduleApi.create(formPayload())
      toast.success('定时任务已创建')
    }
    editing.value = null
    await load()
  } catch (e) {
    toast.error(toFriendlyMessage(e, '保存失败'))
  } finally {
    saving.value = false
  }
}

async function toggle(s: ScheduleRecord) {
  try {
    await scheduleApi.update(s.id, { enabled: !s.enabled })
    toast.success(s.enabled ? '已停用' : '已启用')
    await load()
  } catch (e) {
    toast.error(toFriendlyMessage(e, '更新失败'))
  }
}

/** 「立即执行」后的状态轮询定时器集合：组件卸载时统一清理 */
const runPollTimers = new Set<ReturnType<typeof setInterval>>()

function stopPoll(t: ReturnType<typeof setInterval>) {
  clearInterval(t)
  runPollTimers.delete(t)
}

async function runNow(s: ScheduleRecord) {
  try {
    await scheduleApi.runNow(s.id)
    toast.success('已触发立即执行')
    await load()
    // 后台执行，轮询刷新状态：任务被删除（latest 为 undefined）或达到最大次数
    // 时停止，防止 setInterval 泄漏（原实现在任务删除/异常不结束时永不 clearInterval）
    let polls = 0
    const t = setInterval(async () => {
      if (++polls > 100) { stopPoll(t); return }
      try {
        await load(true)
        const latest = items.value.find((x) => x.id === s.id)
        if (!latest || (latest.last_status !== 'running' && latest.last_status !== 'idle')) {
          stopPoll(t)
        }
      } catch { /* 忽略轮询错误 */ }
    }, 3000)
    runPollTimers.add(t)
  } catch (e: any) {
    toast.error(toFriendlyMessage(e, '触发失败'))
  }
}

// 审查R2修复：历史请求序号（与 load 的 _loadSeq 同模式）——快速切换任务时
// 旧响应晚到会挂到新任务的历史面板上，只有最新一次请求可写入
let _historySeq = 0

async function openHistory(s: ScheduleRecord) {
  historyFor.value = s
  historyRuns.value = []
  const seq = ++_historySeq
  try {
    const runs = await scheduleApi.runs(s.id)
    if (seq !== _historySeq) return
    historyRuns.value = runs
  } catch (e) {
    if (seq !== _historySeq) return
    toast.error(toFriendlyMessage(e, '加载历史失败'))
  }
}

async function remove(s: ScheduleRecord) {
  const ok = await confirm({
    title: '删除定时任务',
    message: `确认删除定时任务「${s.name}」及其全部历史？`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  try {
    await scheduleApi.remove(s.id)
    toast.success('已删除')
    await load()
  } catch (e) {
    toast.error(toFriendlyMessage(e, '删除失败'))
  }
}

const STATUS_LABEL: Record<string, string> = {
  idle: '未执行',
  running: '执行中',
  completed: '已完成',
  failed: '失败',
  waiting_permission: '待审批',
  pending: '待执行',
}
function statusLabel(s: string): string {
  return STATUS_LABEL[s] || s
}

function shortId(id: string): string {
  return id ? id.slice(0, 8) : ''
}

// ── 下次执行倒计时（D 阶段）：每秒 tick，仅在有启用任务时运行 ──
const nowTick = ref(Date.now())
let countdownTimer: ReturnType<typeof setInterval> | null = null

function parseSchedTs(ts?: string): number {
  if (!ts) return NaN
  return parseBackendTs(ts).getTime()
}

function countdownText(s: ScheduleRecord): string {
  if (!s.enabled || !s.next_run_at) return ''
  const t = parseSchedTs(s.next_run_at)
  if (isNaN(t)) return ''
  const ms = t - nowTick.value
  if (ms <= 0) return '即将'
  const m = Math.floor(ms / 60000)
  if (m < 60) return `${m} 分钟`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} 小时 ${m % 60} 分`
  return `${Math.floor(h / 24)} 天 ${h % 24} 小时`
}

/** 单次执行耗时（秒，保留 1 位小数） */
function durationText(r: ScheduleRun): string {
  if (!r.started_at || !r.finished_at) return ''
  const a = parseSchedTs(r.started_at)
  const b = parseSchedTs(r.finished_at)
  if (isNaN(a) || isNaN(b) || b <= a) return ''
  return `${Math.round((b - a) / 100) / 10}s`
}

function startCountdownTick() {
  if (countdownTimer) return
  countdownTimer = setInterval(() => {
    nowTick.value = Date.now()
  }, 1000)
}
function stopCountdownTick() {
  if (countdownTimer) {
    clearInterval(countdownTimer)
    countdownTimer = null
  }
}

// 按需启停：仅当存在启用且有 next_run_at 的任务时运行 1s 定时器。
// 视图是 v-show 常驻（SshView 不卸载），无条件 tick 会永久空转。
// 倒计时文案本身每 tick 重算（nowTick 变化触发），无任务时自然为空。
const hasCountdownTarget = computed(() =>
  items.value.some(s => s.enabled && !!s.next_run_at),
)
// 与上面的列表轮询同一道闸：本视图是 v-show 常驻（SshView 不卸载），只按"有没有任务"
// 启停的话，切回首页后这个 1s 定时器还在跑 —— nowTick 一变，整棵隐藏子树连每行的
// parseBackendTs().getTime() 都跟着重算，一直跑到应用退出。
watch([hasCountdownTarget, () => props.active], ([on, active]) => {
  if (on && (active || props.active === undefined)) startCountdownTick()
  else stopCountdownTick()
}, { immediate: true })

/** 失败 run → 预填任务台诊断草稿（复用 A 的 pendingPrefill 通路） */
function askAiDiagnose(s: ScheduleRecord, r: ScheduleRun) {
  const lines = [
    `定时任务「${s.name}」在 ${formatBackendTs(r.started_at)} 的执行失败了，帮我分析原因并给出修复建议。`,
    `任务提示：${s.task_prompt || '（使用内置模板）'}`,
    s.target_host ? `目标主机：${hostLabel(s.target_host)}` : '',
    r.summary ? `执行摘要：${r.summary}` : '',
    r.error ? `错误信息：${r.error}` : '',
    '请给出：1) 最可能的根因 2) 排查步骤 3) 修复方案。',
  ].filter(Boolean)
  // 纯文本入口，不采集终端上下文（本页面无终端，避免混入无关终端的选区/历史）
  sshAiStore.askAi(lines.join('\n'))
}

// Escape 关闭当前打开的弹窗
function handleKeydown(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  if (historyFor.value) { historyFor.value = null; return }
  if (editing.value) editing.value = null
}

onMounted(() => {
  // 首屏加载与列表轮询已由上方 watch(active, {immediate}) 承担，此处只补主机列表
  // 加载远程管理的主机列表，供「目标主机」下拉使用（仅在未加载时拉取）
  if (sshStore.connections.length === 0) {
    sshStore.loadConnections()
  }
  window.addEventListener('keydown', handleKeydown)
})

onUnmounted(() => {
  window.removeEventListener('keydown', handleKeydown)
  stopCountdownTick()
  stopListPoll()
  // 卸载前收起「立即执行」轮询：sticky-visited 下本页通常不触发，但 SshView 整体
  // 卸载/热更新后后台仍会 load() 数百次
  for (const t of runPollTimers) clearInterval(t)
  runPollTimers.clear()
})
</script>

<style scoped>
.schedules-view {
  display: flex;
  flex-direction: column;
  flex: 1;
  overflow: hidden;
  /* 整页结构画布：走 --canvas-bg（默认=--bg-base；详见 DashboardView 同注）。 */
  background: var(--canvas-bg);
}
/* 入口分段 tab（定时任务 | 运维剧本）：小段控件，选中态走 accent */
.entry-tabs { display: flex; gap: 4px; padding: 8px 16px 0; flex-shrink: 0; }
.entry-tabs button {
  padding: 5px 14px; font-size: 12px; border-radius: 6px 6px 0 0; cursor: pointer;
  border: 1px solid transparent; border-bottom: none; background: transparent;
  color: var(--text-secondary);
}
.entry-tabs button:hover { color: var(--text-primary); background: var(--bg-hover); }
.entry-tabs button.on {
  color: var(--accent-text); background: var(--bg-elevated);
  border-color: var(--border-subtle);
}
/* 分段内容壳：被 v-show 的子树要自己成为 flex 列容器，schedule-list 的 flex:1 才有效 */
.schedules-pane { display: flex; flex-direction: column; flex: 1; overflow: hidden; min-height: 0; }
/* 头部统一以 RemoteManager .rm-header 为准：44px 高 + 16px 侧距 + 底部分隔线 */
.schedules-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: var(--page-head-h);
  padding: 0 16px;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.schedules-title { display: flex; align-items: center; gap: 8px; font-size: var(--page-title-size); font-weight: 600; color: var(--text-primary); font-family: var(--font-sans); }
.schedules-sub { font-size: 12px; color: var(--text-tertiary); font-weight: 400; }
.schedules-actions { display: flex; gap: 8px; }


.schedule-list { flex: 1; overflow: auto; display: flex; flex-direction: column; gap: 10px; padding: 12px 16px 16px; }
.schedule-card {
  border: 1px solid var(--border-subtle);
  border-left: 3px solid var(--accent-primary);
  border-radius: 8px;
  padding: 12px 14px;
}
/* 日常任务色条：沿用成功/绿语义 token，不硬编码色值（spec §3） */
.schedule-card.daily { border-left-color: var(--accent-green); }
.schedule-section-title {
  display: flex; align-items: baseline; gap: 8px;
  font-size: 13px; font-weight: 600; color: var(--text-primary); margin-top: 4px;
}
.schedule-section-title .cnt { font-size: 11px; font-weight: 400; color: var(--text-tertiary); }
.schedule-card-head { display: flex; align-items: center; justify-content: space-between; }
.schedule-name { font-size: 14px; font-weight: 600; color: var(--text-primary); display: flex; align-items: center; gap: 8px; }
/* 启用标记与执行状态是同一种"小徽章"，形状只写一份；配色各自表述语义 */
.toggle, .status { font-size: 11px; padding: 1px 8px; border-radius: 8px; border: 1px solid var(--border-subtle); }
.toggle.on { color: var(--accent-green); border-color: var(--accent-green); }
.schedule-card-actions { display: flex; gap: 6px; }
.schedule-card-meta { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: var(--text-secondary); margin-top: 8px; }
.meta-item { text-align: left; }
.meta-item b { color: var(--text-primary); font-weight: 600; }
.mono { font-family: var(--font-mono); }
.schedule-card-body { margin-top: 8px; font-size: 12px; }
.schedule-card-body .k { color: var(--text-tertiary); margin-bottom: 4px; text-align: left; }
.prompt { color: var(--text-secondary); white-space: pre-wrap; text-align: left; }

.status.running { color: var(--accent-text); border-color: var(--accent-primary); }
.status.completed { color: var(--accent-green); border-color: var(--accent-green); }
.status.failed { color: var(--accent-red); border-color: var(--accent-red); }
.status.waiting_permission { color: var(--accent-orange); border-color: var(--accent-orange); }

.empty-state { flex: 1; display: flex; align-items: center; justify-content: center; color: var(--text-tertiary); font-size: 13px; padding: 16px; }
.sc-loading { color: var(--text-tertiary); font-size: 12px; padding: 0 16px 12px; }
.sc-error { color: var(--accent-red); font-size: 12px; padding: 0 16px 12px; }

.modal-mask { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.modal { width: 520px; max-width: 90vw; background: var(--bg-elevated); border: 1px solid var(--border-subtle); border-radius: 10px; padding: 16px; max-height: 80vh; overflow: auto; }
.modal.wide { width: 680px; }
.modal-title { font-size: 14px; font-weight: 600; color: var(--text-primary); margin-bottom: 12px; }
/* 弹窗标题栏：左侧标题、右侧类型分段开关（方案 B） */
.schedule-modal-title { display: flex; align-items: center; justify-content: space-between; }
.type-switch { display: inline-flex; gap: 6px; }
.type-switch .sched-option { padding: 3px 12px; }
.type-switch.disabled { opacity: .55; }
.type-switch .sched-option:disabled { cursor: not-allowed; }
.modal-body { display: flex; flex-direction: column; gap: 10px; }
.field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--text-tertiary); }
.field input, .field textarea, .field select {
  font-size: 12px; padding: 6px 8px; border-radius: 6px;
  border: 1px solid var(--border-subtle); background: var(--bg-input); color: var(--text-primary);
}
.field.check { flex-direction: row; align-items: center; gap: 6px; }
.template-list { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }
.template-chip {
  text-align: left; padding: 8px; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--border-subtle); background: var(--bg-hover);
}
.template-chip:hover { border-color: var(--text-tertiary); }
.template-chip.active { border-color: var(--accent-primary); color: var(--text-primary); background: color-mix(in srgb, var(--accent-primary) 8%, transparent); }
.template-name { font-size: 12px; font-weight: 600; color: var(--text-primary); }
.template-desc { font-size: 11px; color: var(--text-tertiary); margin-top: 2px; }
.sched-options { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; }
.sched-option {
  padding: 5px 0; font-size: 12px; border-radius: 6px; cursor: pointer;
  border: 1px solid var(--border-subtle); background: transparent; color: var(--text-secondary);
}
.sched-option:hover { color: var(--text-primary); background: var(--bg-hover); }
.sched-option.active { color: var(--accent-text); border-color: var(--accent-primary); background: color-mix(in srgb, var(--accent-primary) 8%, transparent); }
.sched-subrow { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-secondary); }
.sched-subrow .num { width: 56px; }
.sched-subrow .sel { width: 92px; }
.sched-subrow input.num, .sched-subrow select.sel {
  font-size: 12px; padding: 4px 6px; border-radius: 6px;
  border: 1px solid var(--border-subtle); background: var(--bg-input); color: var(--text-primary);
}
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }

.history-list { display: flex; flex-direction: column; gap: 8px; }
.history-item { border: 1px solid var(--border-subtle); border-radius: 6px; padding: 8px 10px; }
.history-head { display: flex; align-items: center; gap: 12px; font-size: 12px; color: var(--text-secondary); }
.history-summary { font-size: 12px; color: var(--text-secondary); margin-top: 6px; white-space: pre-wrap; max-height: 150px; overflow: auto; }
.history-error { font-size: 12px; color: var(--accent-red); margin-top: 4px; }
.empty-small { color: var(--text-tertiary); font-size: 12px; padding: 20px 0; text-align: center; }
.countdown { color: var(--accent-text); font-weight: 600; }
.history-actions { margin-top: 6px; }
</style>