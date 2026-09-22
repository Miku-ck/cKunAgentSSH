<template>
  <div class="mem-view" @click.stop>
    <header class="mem-head">
      <span class="mem-title">Agent 记忆</span>
      <span class="mem-stat">{{ statText }}</span>
      <button class="mem-close" aria-label="关闭" @click="$emit('close')">
        <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </header>

    <nav class="mem-tabs">
      <button :class="{ on: tab === 'memories' }" @click="switchTab('memories')">记忆</button>
      <button :class="{ on: tab === 'jobs' }" @click="switchTab('jobs')">对账队列</button>
      <button :class="{ on: tab === 'blocks' }" @click="switchTab('blocks')">画像块</button>
    </nav>

    <!-- ── 记忆列表 ── -->
    <div v-if="tab === 'memories'" class="mem-filters">
      <select v-model="filters.kind" @change="loadMemories">
        <option value="">全部类型</option>
        <option value="semantic">事实</option>
        <option value="episodic">经历</option>
        <option value="procedural">规程</option>
      </select>
      <select v-model="filters.status" @change="loadMemories">
        <option value="active">生效中</option>
        <option value="">全部状态</option>
        <option value="pending_reconcile">待确认</option>
        <option value="invalidated">已失效</option>
        <option value="superseded">被取代</option>
        <option value="rejected">已拒绝</option>
      </select>
      <input v-model="filters.q" class="mem-search" placeholder="搜索内容/标题"
             @keydown.enter="loadMemories" />
      <button class="mem-btn" @click="loadMemories">搜索</button>
      <button class="mem-btn" @click="addingNew = !addingNew">{{ addingNew ? '收起' : '新增' }}</button>
    </div>

    <!-- ── 手动新增：直写 active 并置顶，不经对账管线 ── -->
    <div v-if="addingNew && tab === 'memories'" class="mem-new">
      <div class="mem-new-row">
        <select v-model="newMem.kind">
          <option value="semantic">事实</option>
          <option value="episodic">经历</option>
          <option value="procedural">规程</option>
        </select>
        <input v-model="newMem.scope" class="mem-new-scope" placeholder="global" />
        <input v-model="newMem.title" placeholder="标题（可空）" />
      </div>
      <textarea v-model="newMem.content" class="mem-edit" rows="3"
                placeholder="要记住的内容 —— 手写条目自动置顶，不会被池容量修剪掉" />
      <div class="mem-new-row end">
        <label class="mem-new-imp">
          重要度
          <input v-model.number="newMem.importance" type="range" min="0" max="1" step="0.05" />
          <span class="mem-importance">{{ newMem.importance.toFixed(2) }}</span>
        </label>
        <span class="mem-new-hint">直写生效并置顶，不经对账；不带向量，关键词检索照常命中</span>
        <button class="mem-btn sm" :disabled="!newMem.content.trim() || addingBusy"
                @click="onAdd">{{ addingBusy ? '写入中…' : '写入' }}</button>
      </div>
    </div>

    <div class="mem-body">
      <p v-if="loading" class="mem-hint">加载中…</p>
      <p v-else-if="error" class="mem-hint err">{{ error }}</p>

      <template v-else-if="tab === 'memories'">
        <p v-if="!memories.length" class="mem-hint">
          没有匹配的记忆。Agent 完成任务后会自动沉淀关键事实，经后台对账后可在此查看、编辑或作废。
        </p>
        <ul v-else class="mem-list">
          <li v-for="m in memories" :key="m.id" class="mem-item">
            <div class="mem-line">
              <span class="mem-badge kind" :class="m.kind">{{ kindText[m.kind] || m.kind }}</span>
              <span class="mem-badge status" :class="m.status">{{ statusText[m.status] || m.status }}</span>
              <span class="mem-badge scope" :title="m.scope">{{ scopeShort(m.scope) }}</span>
              <span v-if="m.pinned" class="mem-badge pin">置顶</span>
              <span class="mem-importance" :title="`重要度 ${fmtScore(m.importance)} · 命中 ${m.use_count} 次 · ${m.has_embedding ? '已向量' : '仅关键词'}`">
                {{ fmtScore(m.importance) }}
              </span>
            </div>

            <textarea v-if="editingId === m.id" v-model="editDraft" class="mem-edit" rows="3" />
            <p v-else class="mem-content" :title="m.title">{{ m.content }}</p>

            <div class="mem-meta">
              <span>{{ sourceText[m.source] || m.source }}</span>
              <span>· {{ fmtDate(m.created_at) }}</span>
              <span class="spacer" />
              <template v-if="editingId === m.id">
                <button class="mem-btn sm" :disabled="saving === m.id" @click="saveEdit(m)">保存</button>
                <button class="mem-btn sm" @click="editingId = ''">取消</button>
              </template>
              <template v-else>
                <button class="mem-lnk" @click="startEdit(m)">编辑</button>
                <button class="mem-lnk" @click="togglePin(m)">{{ m.pinned ? '取消置顶' : '置顶' }}</button>
                <button class="mem-lnk" @click="openAudit(m)">审计</button>
                <button v-if="m.status === 'active' || m.status === 'pending_reconcile'"
                        class="mem-lnk danger" :disabled="deleting === m.id" @click="onInvalidate(m)">作废</button>
              </template>
            </div>
          </li>
        </ul>
      </template>

      <!-- ── 对账队列 ── -->
      <template v-else-if="tab === 'jobs'">
        <div class="mem-jobs-bar">
          <span class="mem-new-hint">对账 / 池修剪 / 衰减按定时跑；这里可立刻催一轮</span>
          <button class="mem-btn sm" :disabled="tickBusy" @click="onTickNow">
            {{ tickBusy ? '整理中…' : '立即整理' }}
          </button>
        </div>
        <p v-if="!jobs.length" class="mem-hint">队列为空 —— 后台对账已消化所有待写入记忆。</p>
        <ul v-else class="mem-list">
          <li v-for="j in jobs" :key="j.id" class="mem-item job">
            <div class="mem-line">
              <span class="mem-badge">{{ jobKindText[j.kind] || j.kind }}</span>
              <span class="mem-badge status" :class="j.status">{{ jobStatusText[j.status] || j.status }}</span>
              <span class="mem-importance">尝试 {{ j.attempts }} · 优先级 {{ j.priority }}</span>
            </div>
            <p v-if="j.last_error" class="mem-content err-text">{{ j.last_error }}</p>
            <div class="mem-meta">
              <span>{{ fmtDate(j.created_at) }}</span>
              <span class="spacer" />
              <button v-if="j.status === 'failed'" class="mem-lnk" @click="retryJob(j)">重试</button>
            </div>
          </li>
        </ul>
      </template>

      <!-- ── 画像块 ── -->
      <template v-else>
        <p v-if="!blocks.length" class="mem-hint">还没有画像块。Agent 探测目标机后会自动写入主机画像。</p>
        <ul v-else class="mem-list">
          <li v-for="b in blocks" :key="b.key" class="mem-item">
            <div class="mem-line">
              <span class="mem-badge kind">{{ b.key }}</span>
              <span class="mem-importance">v{{ b.version }} · {{ b.value.length }}/{{ b.limit_chars }} 字 · {{ blockByText[b.updated_by] || b.updated_by }}</span>
            </div>
            <textarea v-if="blockEditKey === b.key" v-model="blockDraft" class="mem-edit" rows="6" />
            <div v-if="blockEditKey === b.key" class="mem-diff">
              <div v-for="(d, i) in blockDiff" :key="i" class="diff-line" :class="d.op">
                <span class="diff-mark">{{ d.op === 'add' ? '+' : d.op === 'del' ? '-' : ' ' }}</span>{{ d.text || ' ' }}
              </div>
            </div>
            <pre v-else class="mem-block-view">{{ b.value }}</pre>
            <div class="mem-meta">
              <span class="spacer" />
              <template v-if="blockEditKey === b.key">
                <button class="mem-btn sm" :disabled="saving === b.key" @click="saveBlock(b)">保存</button>
                <button class="mem-btn sm" @click="cancelBlockEdit">取消</button>
              </template>
              <template v-else>
                <button class="mem-lnk" @click="startBlockEdit(b)">编辑</button>
                <button class="mem-lnk" @click="toggleHistory(b.key)">
                  {{ historyFor === b.key ? '收起历史' : '历史' }}
                </button>
              </template>
            </div>

            <div v-if="historyFor === b.key" class="mem-history">
              <p v-if="historyLoading" class="mem-hint">历史加载中…</p>
              <p v-else-if="!historyRows.length" class="mem-hint">无历史版本。</p>
              <ul v-else>
                <li v-for="h in historyRows" :key="h.version">
                  <span class="mem-badge">v{{ h.version }}</span>
                  <span class="hist-meta">
                    {{ blockByText[h.updated_by] || h.updated_by }} · {{ fmtDate(h.created_at) }} · {{ h.value.length }} 字
                  </span>
                  <button class="mem-lnk" :disabled="saving === b.key || h.version === b.version"
                          @click="rollbackBlock(b, h)">
                    {{ h.version === b.version ? '当前版' : '回滚到此版' }}
                  </button>
                </li>
              </ul>
            </div>
          </li>
        </ul>
      </template>
    </div>

    <!-- ── 审计抽屉 ── -->
    <div v-if="auditFor" class="mem-drawer">
      <div class="drawer-head">
        <span>审计链 · {{ kindText[auditFor.kind] || auditFor.kind }}</span>
        <button class="mem-close" aria-label="关闭" @click="auditFor = null">✕</button>
      </div>
      <p v-if="!auditRows.length" class="mem-hint">无审计记录。</p>
      <ul v-else class="drawer-list">
        <li v-for="a in auditRows" :key="a.id">
          <span class="mem-badge" :class="a.action.toLowerCase()">{{ auditActionText[a.action] || a.action }}</span>
          <span class="drawer-reason">{{ a.reason || '—' }}</span>
          <span class="drawer-date">{{ fmtDate(a.created_at) }}</span>
        </li>
      </ul>
    </div>

    <footer class="mem-foot">
      <template v-if="clearArmed">
        <span class="prune-q">作废全部未置顶记忆？置顶项不受影响。</span>
        <button class="mem-btn danger" @click="onClear">确认</button>
        <button class="mem-btn" @click="clearArmed = false">取消</button>
      </template>
      <template v-else>
        <button class="mem-btn" @click="reload">刷新</button>
        <button v-if="tab === 'memories'" class="mem-btn danger" :disabled="!memories.length" @click="clearArmed = true">清空</button>
      </template>
    </footer>
  </div>
</template>

<script setup lang="ts">
/**
 * AgentMemoryView — v2 记忆管理面板（spec §9）
 *
 * 三视图：记忆（过滤/新增/编辑/置顶/作废/审计）、对账队列（状态/重试）、
 * 画像块（查看/编辑带逐行 diff 预览/历史回滚）。
 * 所有写入经后端唯一入口：POST=手写字段直写并置顶、PATCH=编辑、
 * clear=批量失效、job 重试重投队列、PUT block=整块新版本（回滚即回写历史值，历史不删）。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import {
  agentAdminApi, toFriendlyMessage,
  type MemoryRow, type MemoryJobRow, type MemoryBlockRow, type MemoryBlockHistoryRow,
  type MemoryAuditRow, type MemoryStats,
} from '@/api/client'
import { useToastStore } from '@/stores/toast'
import { parseBackendTs } from '@/utils/format'

defineEmits<{ close: [] }>()

const toast = useToastStore()
const tab = ref<'memories' | 'jobs' | 'blocks'>('memories')
const loading = ref(false)
const error = ref('')

const memories = ref<MemoryRow[]>([])
const jobs = ref<MemoryJobRow[]>([])
const blocks = ref<MemoryBlockRow[]>([])
const stats = ref<MemoryStats | null>(null)

const filters = ref({ kind: '', status: 'active', q: '' })
const editingId = ref('')
const editDraft = ref('')
const deleting = ref('')
const saving = ref('')
const clearArmed = ref(false)

const blockEditKey = ref('')
const blockDraft = ref('')
const blockEditBase = ref('')
const historyFor = ref('')
const historyRows = ref<MemoryBlockHistoryRow[]>([])
const historyLoading = ref(false)

const addingNew = ref(false)
const addingBusy = ref(false)
const tickBusy = ref(false)
const newMem = reactive({
  kind: 'semantic' as 'semantic' | 'episodic' | 'procedural',
  scope: 'global',
  title: '',
  content: '',
  importance: 0.7,
})

const auditFor = ref<MemoryRow | null>(null)
const auditRows = ref<MemoryAuditRow[]>([])

const kindText: Record<string, string> = { semantic: '事实', episodic: '经历', procedural: '规程' }
const statusText: Record<string, string> = {
  active: '生效', pending_reconcile: '待确认', invalidated: '已失效',
  superseded: '被取代', rejected: '已拒绝',
}
const sourceText: Record<string, string> = {
  chat_extract: '对话提取', agent_tool: 'Agent 写入', run_summary: '运行总结',
  user_ui: '手动', migration: '迁移', probe: '探测', test: '测试',
}
const jobKindText: Record<string, string> = {
  episodic_summary: '经历摘要', candidate_fact: '事实候选',
  agent_add: 'Agent 新增', backfill_reconcile: '存量回填',
}
const jobStatusText: Record<string, string> = { pending: '待处理', done: '完成', failed: '失败' }
const auditActionText: Record<string, string> = {
  ADD: '新增', UPDATE: '更新', DELETE: '作废', NOOP: '忽略', EDIT: '编辑', PIN: '置顶',
}
const blockByText: Record<string, string> = { user_ui: '用户', agent: 'Agent', probe: '探测', system: '系统' }

const statText = computed(() => {
  const s = stats.value
  if (!s) return ''
  const active = Object.values(s.memories).reduce((n, bySt) => n + (bySt.active || 0), 0)
  const pending = s.jobs.pending || 0
  const failed = s.jobs.failed || 0
  let t = `${active} 条生效`
  if (pending) t += ` · 队列 ${pending}`
  if (failed) t += ` · 失败 ${failed}`
  return t
})

function scopeShort(scope: string): string {
  if (scope === 'global') return '全局'
  if (scope.startsWith('host:')) return scope.slice(5, 11)
  if (scope.startsWith('run:')) return 'run'
  return scope
}

async function loadMemories() {
  loading.value = true
  error.value = ''
  try {
    const data = await agentAdminApi.listMemories({
      kind: filters.value.kind || undefined,
      status: filters.value.status || undefined,
      q: filters.value.q.trim() || undefined,
      limit: 100,
    })
    memories.value = data.memories
  } catch (e) {
    error.value = toFriendlyMessage(e, '记忆加载失败')
  } finally {
    loading.value = false
  }
}

async function loadStats() {
  try { stats.value = await agentAdminApi.memoryStats() } catch { /* 头部统计是增强项 */ }
}
async function loadJobs() {
  loading.value = true
  error.value = ''
  try { jobs.value = (await agentAdminApi.listMemoryJobs()).jobs }
  catch (e) { error.value = toFriendlyMessage(e, '队列加载失败') }
  finally { loading.value = false }
}
async function loadBlocks() {
  loading.value = true
  error.value = ''
  try { blocks.value = (await agentAdminApi.listMemoryBlocks()).blocks }
  catch (e) { error.value = toFriendlyMessage(e, '画像块加载失败') }
  finally { loading.value = false }
}

function switchTab(next: typeof tab.value) {
  tab.value = next
  clearArmed.value = false
  auditFor.value = null
  addingNew.value = false
  blockEditKey.value = ''
  historyFor.value = ''
  historyRows.value = []
  if (next === 'memories') loadMemories()
  else if (next === 'jobs') loadJobs()
  else loadBlocks()
}

function reload() {
  loadStats()
  if (tab.value === 'memories') loadMemories()
  else if (tab.value === 'jobs') loadJobs()
  else loadBlocks()
}

async function onAdd() {
  const content = newMem.content.trim()
  if (!content) { toast.error('内容不能为空'); return }
  addingBusy.value = true
  try {
    const r = await agentAdminApi.addMemory({
      kind: newMem.kind, content, title: newMem.title.trim(),
      scope: newMem.scope.trim() || 'global', importance: newMem.importance,
    })
    // 当前筛选条件可能容不下这条（如按类型过滤），失败也不影响写入本身
    if (!filters.value.kind && !filters.value.q.trim()) memories.value.unshift(r.memory)
    else await loadMemories()
    newMem.content = ''
    newMem.title = ''
    addingNew.value = false
    toast.success('已直写并置顶')
    loadStats()
  } catch (e) {
    toast.error(`新增失败：${toFriendlyMessage(e)}`)
  } finally { addingBusy.value = false }
}

function startEdit(m: MemoryRow) { editingId.value = m.id; editDraft.value = m.content }
async function saveEdit(m: MemoryRow) {
  const content = editDraft.value.trim()
  if (!content) { toast.error('内容不能为空'); return }
  saving.value = m.id
  try {
    const r = await agentAdminApi.editMemory(m.id, { content })
    Object.assign(m, r.memory)
    editingId.value = ''
    toast.success('已保存')
  } catch (e) {
    toast.error(`保存失败：${toFriendlyMessage(e)}`)
  } finally { saving.value = '' }
}

async function togglePin(m: MemoryRow) {
  try {
    const r = await agentAdminApi.editMemory(m.id, { pinned: !m.pinned })
    Object.assign(m, r.memory)
  } catch (e) { toast.error(`操作失败：${toFriendlyMessage(e)}`) }
}

async function onInvalidate(m: MemoryRow) {
  deleting.value = m.id
  try {
    await agentAdminApi.deleteMemory(m.id)
    if (filters.value.status === 'active') {
      memories.value = memories.value.filter((x) => x.id !== m.id)
    } else {
      m.status = 'invalidated'
    }
    loadStats()
  } catch (e) {
    toast.error(`作废失败：${toFriendlyMessage(e)}`)
  } finally { deleting.value = '' }
}

async function onClear() {
  clearArmed.value = false
  try {
    const r = await agentAdminApi.clearMemories()
    toast.success(`已作废 ${r.invalidated} 条`)
    await loadMemories(); loadStats()
  } catch (e) { toast.error(`清空失败：${toFriendlyMessage(e)}`) }
}

async function openAudit(m: MemoryRow) {
  auditFor.value = m
  auditRows.value = []
  try { auditRows.value = (await agentAdminApi.listMemoryAudit(m.id)).audit }
  catch (e) { toast.error(`审计加载失败：${toFriendlyMessage(e)}`) }
}

async function retryJob(j: MemoryJobRow) {
  try {
    await agentAdminApi.retryMemoryJob(j.id)
    j.status = 'pending'; j.attempts = 0
    toast.success('已重投队列')
  } catch (e) { toast.error(`重试失败：${toFriendlyMessage(e)}`) }
}

/** 手动催一轮 tick：后端 POST /agent/tick/run 跑同一套对账+维护，不改定时周期 */
async function onTickNow() {
  tickBusy.value = true
  try {
    const r = await agentAdminApi.runMemoryTickNow()
    toast.success(`本轮整理：${typeof r.summary === 'string' ? r.summary : '已执行'}`)
    await Promise.all([loadJobs(), loadStats()])
  } catch (e) { toast.error(`整理失败：${toFriendlyMessage(e)}`) }
  finally { tickBusy.value = false }
}

function startBlockEdit(b: MemoryBlockRow) {
  blockEditKey.value = b.key
  blockDraft.value = b.value
  blockEditBase.value = b.value
  historyFor.value = ''
}
function cancelBlockEdit() {
  blockEditKey.value = ''
  blockDraft.value = ''
  blockEditBase.value = ''
}

/** 逐行 diff 预览：掐掉公共前缀/后缀，中间整体算「旧行删 + 新行增」。
 *  块内容上限 2000 字符，成块显示足够看清改了什么，不值得为行级对齐上 LCS。 */
const blockDiff = computed<{ op: 'same' | 'del' | 'add', text: string }[]>(() => {
  if (!blockEditKey.value) return []
  return diffLineRanges(blockEditBase.value, blockDraft.value)
})

function diffLineRanges(oldText: string, newText: string) {
  const a = oldText.split('\n')
  const b = newText.split('\n')
  let head = 0
  while (head < a.length && head < b.length && a[head] === b[head]) head++
  let ae = a.length
  let be = b.length
  while (ae > head && be > head && a[ae - 1] === b[be - 1]) { ae--; be-- }
  const out: { op: 'same' | 'del' | 'add', text: string }[] = []
  for (let i = 0; i < head; i++) out.push({ op: 'same', text: a[i] })
  for (let i = head; i < ae; i++) out.push({ op: 'del', text: a[i] })
  for (let i = head; i < be; i++) out.push({ op: 'add', text: b[i] })
  for (let i = ae; i < a.length; i++) out.push({ op: 'same', text: a[i] })
  return out
}

async function toggleHistory(key: string) {
  if (historyFor.value === key) { historyFor.value = ''; return }
  historyFor.value = key
  historyRows.value = []
  historyLoading.value = true
  try { historyRows.value = (await agentAdminApi.listMemoryBlockHistory(key)).history }
  catch (e) { toast.error(`历史加载失败：${toFriendlyMessage(e)}`) }
  finally { historyLoading.value = false }
}

/** 回滚 = 把历史里的 value 再写一版：产生新版本号，历史本身不删（spec §6 失效不物理删） */
async function rollbackBlock(b: MemoryBlockRow, h: MemoryBlockHistoryRow) {
  if (h.version === b.version) return
  saving.value = b.key
  try {
    await agentAdminApi.putMemoryBlock(b.key, h.value)
    toast.success(`已回滚到 v${h.version}`)
    historyFor.value = ''
    blockEditKey.value = ''
    await loadBlocks()
  } catch (e) {
    toast.error(`回滚失败：${toFriendlyMessage(e)}`)
  } finally { saving.value = '' }
}

async function saveBlock(b: MemoryBlockRow) {
  saving.value = b.key
  try {
    await agentAdminApi.putMemoryBlock(b.key, blockDraft.value)
    toast.success('已保存')
    blockEditKey.value = ''
    await loadBlocks()
  } catch (e) {
    toast.error(`保存失败：${toFriendlyMessage(e)}`)
  } finally { saving.value = '' }
}

function fmtScore(v: number | null): string {
  return typeof v === 'number' ? v.toFixed(2) : '-'
}
function fmtDate(iso: string): string {
  try {
    const d = parseBackendTs(iso)
    return `${d.getMonth() + 1}/${d.getDate()}`
  } catch { return iso }
}

onMounted(() => { loadStats(); loadMemories() })
</script>

<style scoped>
.mem-view {
  /* 内嵌宿主是设置面板（旧的 AI 面板浮层入口已撤下），所以走文档流。
     高度必须确定：.mem-body 的 flex:1 + overflow 在内容撑高的盒子里不会生效。 */
  position: relative;
  height: min(60vh, 560px);
  min-height: 320px;
  display: flex; flex-direction: column;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: 8px; overflow: hidden;
}
.mem-head {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 12px; border-bottom: 1px solid var(--border-subtle); flex-shrink: 0;
}
.mem-title { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.mem-stat { font-size: 11px; color: var(--text-tertiary); margin-left: auto; }
.mem-close {
  background: none; border: none; cursor: pointer; color: var(--text-secondary);
  display: flex; align-items: center; padding: 2px;
}
.mem-close:hover { color: var(--text-primary); }
.mem-tabs {
  display: flex; gap: 2px; padding: 4px 8px; border-bottom: 1px solid var(--border-subtle);
}
.mem-tabs button {
  background: none; border: none; cursor: pointer; font-size: 12px; font-family: inherit;
  padding: 4px 10px; border-radius: 5px; color: var(--text-secondary);
}
.mem-tabs button.on { background: var(--bg-hover, rgba(255,255,255,.06)); color: var(--text-primary); }
.mem-filters {
  display: flex; gap: 6px; padding: 8px 12px; align-items: center; flex-wrap: wrap;
  border-bottom: 1px solid var(--border-subtle);
}
.mem-filters select, .mem-search {
  font-size: 11.5px; font-family: inherit; color: var(--text-primary);
  background: var(--bg-input, transparent); border: 1px solid var(--border-default);
  border-radius: 4px; padding: 3px 6px;
}
.mem-search { flex: 1; min-width: 90px; }
.mem-body { flex: 1; overflow-y: auto; padding: 4px 12px; }
.mem-hint { font-size: 12px; color: var(--text-secondary); line-height: 1.6; }
.mem-hint.err { color: #d98f86; }
.mem-list { list-style: none; margin: 0; padding: 0; }
.mem-item { padding: 8px 0; border-bottom: 1px solid var(--border-subtle); }
.mem-item:last-child { border-bottom: none; }
.mem-line { display: flex; align-items: center; gap: 6px; margin-bottom: 3px; flex-wrap: wrap; }
.mem-badge {
  font-size: 10px; padding: 0 6px; border-radius: 7px;
  border: 1px solid var(--border-default); color: var(--text-tertiary);
}
.mem-badge.kind.semantic { color: #7fbc92; border-color: rgba(127,188,146,.35); }
.mem-badge.kind.episodic { color: #7aa2d0; border-color: rgba(122,162,208,.35); }
.mem-badge.kind.procedural { color: #c9a4d6; border-color: rgba(201,164,214,.35); }
.mem-badge.status.active { color: #7fbc92; border-color: rgba(127,188,146,.4); }
.mem-badge.status.pending_reconcile { color: #d8b567; border-color: rgba(216,181,103,.4); }
.mem-badge.status.invalidated,
.mem-badge.status.superseded,
.mem-badge.status.rejected,
.mem-badge.status.failed { color: #cf8a80; border-color: rgba(207,138,128,.4); }
.mem-badge.status.done { color: var(--text-tertiary); }
.mem-badge.pin { color: #d8b567; border-color: rgba(216,181,103,.5); }
.mem-importance { margin-left: auto; font-size: 10px; color: var(--text-tertiary); font-family: ui-monospace, Consolas, monospace; }
.mem-content {
  margin: 0 0 4px; font-size: 12px; color: var(--text-primary); line-height: 1.5;
  word-break: break-word;
  display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden;
}
.mem-content.err-text { color: #cf8a80; }
.mem-edit {
  width: 100%; box-sizing: border-box; margin-bottom: 4px; resize: vertical;
  font-size: 12px; font-family: inherit; color: var(--text-primary);
  background: var(--bg-input, transparent); border: 1px solid var(--accent-primary); border-radius: 4px; padding: 4px 6px;
}
.mem-block-view {
  margin: 0 0 4px; font-size: 11.5px; line-height: 1.5; color: var(--text-primary);
  white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, Consolas, monospace;
  max-height: 160px; overflow-y: auto;
}
.mem-meta { display: flex; align-items: center; gap: 8px; font-size: 10.5px; color: var(--text-tertiary); }
.mem-meta .spacer { flex: 1; }
.mem-lnk {
  background: none; border: none; cursor: pointer; padding: 0;
  color: var(--text-secondary); font-size: 11px; font-family: inherit;
}
.mem-lnk:hover { color: var(--accent-primary); }
.mem-lnk.danger:hover { color: #cf8a80; }
.mem-lnk:disabled { opacity: .5; cursor: default; }
.mem-drawer {
  border-top: 1px solid var(--border-default); padding: 8px 12px; max-height: 180px; overflow-y: auto;
  background: var(--bg-sunken, rgba(0,0,0,.12));
}
.drawer-head { display: flex; align-items: center; font-size: 11.5px; color: var(--text-primary); margin-bottom: 6px; }
.drawer-head .mem-close { margin-left: auto; }
.drawer-list { list-style: none; margin: 0; padding: 0; }
.drawer-list li { display: flex; align-items: center; gap: 8px; font-size: 11px; padding: 3px 0; }
.drawer-reason { flex: 1; color: var(--text-secondary); word-break: break-word; }
.drawer-date { color: var(--text-tertiary); font-family: ui-monospace, Consolas, monospace; }
.mem-foot {
  display: flex; align-items: center; gap: 8px; padding: 8px 12px;
  border-top: 1px solid var(--border-subtle);
  font-size: 11.5px; color: var(--text-secondary); flex-shrink: 0;
}
.prune-q { flex: 1; }
.mem-btn {
  height: 24px; padding: 0 10px; border-radius: 4px; font-size: 11.5px; font-family: inherit;
  background: none; border: 1px solid var(--border-default); color: var(--text-primary); cursor: pointer;
}
.mem-btn:hover:not(:disabled) { border-color: var(--accent-primary); }
.mem-btn:disabled { opacity: .45; cursor: default; }
.mem-btn.danger { color: #cf8a80; border-color: rgba(207,138,128,.4); }
.mem-btn.sm { height: 20px; padding: 0 8px; font-size: 11px; }
.mem-new {
  display: flex; flex-direction: column; gap: 5px; padding: 8px 12px;
  border-bottom: 1px solid var(--border-subtle);
}
.mem-jobs-bar {
  display: flex; align-items: center; gap: 8px; margin-bottom: 6px; padding: 5px 8px;
  border: 1px solid var(--border-subtle); border-radius: 4px;
}
.mem-new-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.mem-new-row.end { justify-content: flex-end; }
.mem-new-row select, .mem-new-row input {
  font-size: 11.5px; font-family: inherit; color: var(--text-primary);
  background: var(--bg-input, transparent); border: 1px solid var(--border-default);
  border-radius: 4px; padding: 3px 6px;
}
.mem-new-scope { width: 110px; }
.mem-new-row input:not(.mem-new-scope) { flex: 1; min-width: 120px; }
.mem-new-imp { display: flex; align-items: center; gap: 5px; font-size: 11px; color: var(--text-secondary); }
.mem-new-imp input[type='range'] { width: 96px; accent-color: var(--accent-primary); }
.mem-new-hint { flex: 1; font-size: 10.5px; color: var(--text-tertiary); }
.mem-new .mem-edit { margin-bottom: 0; }
.mem-diff {
  max-height: 140px; overflow-y: auto; margin-bottom: 4px; padding: 4px 6px;
  border: 1px solid var(--border-subtle); border-radius: 4px;
  background: var(--bg-sunken, rgba(0,0,0,.12));
  font-family: ui-monospace, Consolas, monospace; font-size: 11px; line-height: 1.5;
}
.diff-line { display: flex; gap: 4px; white-space: pre-wrap; word-break: break-word; color: var(--text-secondary); }
.diff-line.add { color: #7fbc92; background: rgba(127,188,146,.08); }
.diff-line.del { color: #cf8a80; background: rgba(207,138,128,.08); }
.diff-mark { width: 8px; flex-shrink: 0; opacity: .8; }
.mem-history {
  margin-top: 5px; padding: 5px 8px; border-radius: 4px;
  border: 1px solid var(--border-subtle); background: var(--bg-sunken, rgba(0,0,0,.12));
}
.mem-history ul { list-style: none; margin: 0; padding: 0; }
.mem-history li { display: flex; align-items: center; gap: 8px; font-size: 11px; padding: 2px 0; }
.hist-meta { flex: 1; color: var(--text-tertiary); }
</style>
