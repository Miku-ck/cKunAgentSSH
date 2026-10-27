<template>
  <div class="audit-view">
    <!-- 头部：分段 + 链完整性状态 + 操作 -->
    <div class="audit-header">
      <div class="audit-head-left">
        <div class="audit-title">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="16" height="16"><circle cx="4.5" cy="18.5" r="2"/><circle cx="19.5" cy="5.5" r="2"/><path d="M6.5 18.5h3a4 4 0 0 0 4-4v-5a4 4 0 0 1 4-4"/></svg>
          <span>运维轨迹</span>
        </div>
        <!-- 分段：事件流 / 运行列表。两者是同一条追溯链的两个视图 —— 前者按
             命令逐条记（含手动敲的），后者按 run 汇总（步数/耗时/成本）。
             原「运行记录」独立页并入此处，顶栏不再为它单占一栏。 -->
        <div class="audit-seg" role="tablist" aria-label="追溯视图">
          <button class="audit-seg-btn" :class="{ on: segment === 'events' }" role="tab"
            :aria-selected="segment === 'events'" @click="segment = 'events'">事件流</button>
          <button class="audit-seg-btn" :class="{ on: segment === 'runs' }" role="tab"
            :aria-selected="segment === 'runs'" @click="segment = 'runs'">运行列表</button>
        </div>
      </div>
      <!-- 链校验/导出/归档清理都作用于审计表本身，切到运行列表时它们没有对象 -->
      <div v-if="segment === 'events'" class="audit-actions">
        <span
          class="audit-badge"
          :class="verifyState ? (verifyState.intact ? 'ok' : 'broken') : 'idle'"
          :title="verifyState && !verifyState.intact ? `检测到篡改: ${verifyState.broken.join(', ')}` : ''"
        >
          {{ verifyState ? (verifyState.intact ? '链完整' : '链被篡改!') : '未校验' }}
        </span>
        <button class="btn btn-ghost" @click="refresh" :disabled="loading">刷新</button>
        <button class="btn" @click="onVerify" :disabled="loading">校验</button>
        <button class="btn" @click="onExport" :disabled="loading" title="导出审计表全部记录为 CSV（追加式全量导出，不受当前筛选影响）">导出 CSV</button>
        <button
          class="btn btn-danger"
          @click="onClearArchived"
          :disabled="loading || clearing || tableIsEmpty"
          :title="`归档清理：后端审计表的全部记录先导出 CSV 存档到数据库目录（与当前筛选无关），再清空并写入封缄记录开启新审计链（证据不丢、链不断）`"
        >{{ clearing ? '归档中…' : '归档清理' }}</button>
      </div>
    </div>

    <template v-if="segment === 'events'">
    <!-- 写入健康度：「链完整」只说明已写入的那段没被篡改，不说明没漏记录。
         审计写入失败历史上只留一行后端日志，页面照样渲染成一条干净的链 ——
         用户会当成「确实没发生什么」。 -->
    <div v-if="healthState && !healthState.ok" class="audit-health-warn">
      审计链最近有写入失败（连续 {{ healthState.consecutive_failures }} 次，
      最后失败动作：{{ healthState.last_failed_action || '—' }}）：
      这段时间内的操作可能没被记录，链完整不等于没漏，请先排查磁盘与数据库锁。
    </div>
    <!-- 过滤器 -->
    <div class="audit-filters">
      <select v-model="filters.environment" @change="loadFirst">
        <option value="">全部环境</option>
        <option value="test">测试 (test)</option>
        <option value="prod">生产 (prod)</option>
      </select>
      <select v-model="filters.conclusion" @change="loadFirst">
        <option value="">全部结论</option>
        <option value="executed">执行 (executed)</option>
        <option value="approved">已批准 (approved)</option>
        <option value="auto_allowed">自动放行 (auto_allowed)</option>
        <option value="blocked">已拦截 (blocked)</option>
        <option value="denied">已拒绝 (denied)</option>
        <!-- 后端 append_record 的合法结论共 10 种：failed 是「工具执行失败」，
             每次失败的命令都会写一条，之前既筛不出来的也没有样式；
             answered/skipped 是澄清向导的终态（用户的决策，不是执行结果） -->
        <option value="failed">执行失败 (failed)</option>
        <option value="released">授权已释放 (released)</option>
        <option value="revoked">授权已撤销 (revoked)</option>
        <option value="answered">已作答 (answered)</option>
        <option value="skipped">已跳过 (skipped)</option>
      </select>
      <input v-model="filters.target_id" placeholder="目标 ID 过滤" @keyup.enter="loadFirst" />
      <!-- 后端 run_id 是 `= ?` 精确匹配（audit.py:_build_filters），半个号筛不出东西 ——
           运行列表分段的 trace 列点一下就是全号。 -->
      <input
        v-model="filters.run_id"
        class="trace-input"
        placeholder="trace  "
        title="按 trace 号找回某一条 run 的全部记录。运行列表分段的 trace 列/待放行队列头部点 № 可复制整串号"
        @keyup.enter="loadFirst"
      />
      <button
        v-if="filters.run_id"
        class="btn btn-ghost"
        title="清空 trace 筛选"
        @click="filters.run_id = ''; loadFirst()"
      >清 trace</button>
    </div>

    <!-- 记录表 -->
    <div class="audit-table-wrap">
      <table class="audit-table">
        <thead>
          <tr>
            <th>时间</th>
            <th>环境</th>
            <th>操作人</th>
            <th>工具</th>
            <th>结论</th>
            <th title="Agent 轮次号（run_id）；手动敲的命令没有这个，为空">trace</th>
            <th>命令 / 目标</th>
            <th>结果</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in records" :key="r.id" :class="{ prod: r.environment === 'prod' }">
            <td class="mono">{{ formatTs(r.ts) }}</td>
            <td><span class="env" :class="r.environment">{{ r.environment === 'prod' ? 'prod' : 'test' }}</span></td>
            <td>{{ r.operator }}</td>
            <td class="mono">{{ r.action }}</td>
            <td><span class="concl" :class="r.conclusion">{{ r.conclusion }}</span></td>
            <td class="mono">{{ r.run_id || '—' }}</td>
            <td class="mono cmd" :title="r.command_raw">
              {{ r.command_raw || r.target_type + (r.target_id ? ' #' + shortId(r.target_id) : '') }}
            </td>
            <td class="result" :title="r.result">{{ truncate(r.result) }}</td>
          </tr>
          <tr v-if="!loading && records.length === 0">
            <td colspan="8" class="empty">{{ total === 0 ? '暂无审计记录' : '没有匹配的记录' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 分页：仅当有记录时显示（P3 修复，配合 offset 翻页） -->
    <div v-if="total > 0" class="audit-pager">
      <button class="btn" :disabled="offset === 0 || loading" @click="prevPage">上一页</button>
      <span class="audit-pager-info">
        第 {{ offset + 1 }}–{{ Math.min(offset + records.length, total) }} 条 / 共 {{ total }} 条
      </span>
      <button class="btn" :disabled="offset + records.length >= total || loading" @click="nextPage">下一页</button>
    </div>

    <div v-if="loading" class="audit-loading">加载中…</div>
    <div v-if="error" class="audit-error">{{ error }}</div>
    </template>

    <!-- 运行列表分段：agent_runs 按 run 汇总，点开一行读 DB 权威轨迹 -->
    <AgentRunsTable
      v-else
      :focus-run-id="focusRunId"
      :focus-seq="focusSeq"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, reactive, ref, watch } from 'vue'
import { auditApi, toFriendlyMessage, type AuditHealth, type AuditRecord, type AuditVerifyResult } from '@/api/client'
import { useConfirm } from '@/composables/useConfirm'
import { useToastStore } from '@/stores/toast'
import { formatBackendTs } from '@/utils/format'

// 运行列表只在切到那一段时才挂载（同时才发第一次请求），审计表这一段不背它
const AgentRunsTable = defineAsyncComponent(() => import('@/components/ssh/AgentRunsTable.vue'))

const props = defineProps<{
  /** 消息气泡「轨迹」直达：给定 run 号即切到运行列表并定位该行 */
  focusRunId?: string
  focusSeq?: number
}>()

const segment = ref<'events' | 'runs'>('events')
// 直达入口优先于默认分段：先切段再让 AgentRunsTable 挂载，
// 它拿到的 focusRunId 会预填筛选并展开该 run。
watch(() => props.focusSeq, () => {
  if (props.focusRunId) segment.value = 'runs'
}, { immediate: true })

const { confirm } = useConfirm()
const toast = useToastStore()

const records = ref<AuditRecord[]>([])
const total = ref(0)
const loading = ref(false)
const clearing = ref(false)
const error = ref('')
// load() 的请求序号（过期响应丢弃）
let loadSeq = 0
const verifyState = ref<AuditVerifyResult | null>(null)
// 审计写入健康度（后端 /audit/health）：与链校验是两件事 —— 校验查「写进去的有没有被改」，
// 健康度查「该写的有没有写进去」。后者失败时页面必须自己说出来。
const healthState = ref<AuditHealth | null>(null)
// P3 修复：offset 分页（后端 /audit/records 已支持 offset，limit/offset 均带边界）。
// 页大小保持 200，记录倒序（id DESC）——"下一页"取更旧的记录（offset 增大）。
const pageSize = 200
const offset = ref(0)

const filters = reactive<{ environment: string; conclusion: string; target_id: string; run_id: string }>({
  environment: '',
  conclusion: '',
  target_id: '',
  run_id: '',
})

// 「归档清理」作用于整表，可点与否就必须看整表条数：total 是**当前筛选命中数**，
// 用它做禁用条件会出现「筛到 0 条 → 按钮灰掉，而后端一堆记录等着归档」。
// verify() 与 load() 同在 onMounted/refresh 里跑，全库计数在按钮可用前就已就绪；
// 校验失败时（verifyState 为 null）退回筛选数，至少不会把空库判成有库。
const tableIsEmpty = computed(() =>
  verifyState.value ? verifyState.value.total === 0 : total.value === 0
)

async function load() {
  // 序号守卫：连续切筛选/翻页会并发多个请求，慢的那个后到 —— 没有守卫时
  // 它会把已匹配当前筛选的结果覆盖成旧筛选的记录（轨迹页误读风险）
  const seq = ++loadSeq
  loading.value = true
  error.value = ''
  try {
    const res = await auditApi.query({
      environment: filters.environment || undefined,
      conclusion: filters.conclusion || undefined,
      target_id: filters.target_id || undefined,
      run_id: filters.run_id.trim() || undefined,
      limit: pageSize,
      offset: offset.value,
    })
    if (seq !== loadSeq) return
    records.value = res.items
    total.value = res.total
  } catch (e) {
    if (seq !== loadSeq) return
    error.value = toFriendlyMessage(e, '加载审计记录失败')
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

// 回到第一页加载（筛选条件变化 / 手动刷新时调用，避免沿用旧 offset）
function loadFirst(): Promise<void> {
  offset.value = 0
  return load()
}

// 上一页：offset 减小（取更新的记录）
function prevPage(): void {
  if (loading.value || offset.value <= 0) return
  offset.value = Math.max(0, offset.value - pageSize)
  load()
}

// 下一页：offset 增大（取更旧的记录）
function nextPage(): void {
  if (loading.value) return
  if (offset.value + records.value.length >= total.value) return
  offset.value += pageSize
  load()
}

async function refresh() {
  await Promise.all([loadFirst(), verify(), refreshHealth()])
}

/** 健康度拉取失败不污染表格的 error 条：它只是一条附加提示，拿不到就不提。 */
async function refreshHealth() {
  try {
    healthState.value = await auditApi.health()
  } catch {
    healthState.value = null
  }
}

async function onVerify() {
  await verify()
}

async function verify() {
  // 先作废上一次结论：读不到审计表时若留着旧的 intact 结果，徽章会继续宣称
  // 「链完整」，而下面同时报「审计链校验失败」——两个说法互相打脸
  verifyState.value = null
  try {
    verifyState.value = await auditApi.verify()
  } catch (e) {
    error.value = toFriendlyMessage(e, '审计链校验失败')
  }
}

async function onExport() {
  try {
    await auditApi.exportCsv()
  } catch (e) {
    error.value = toFriendlyMessage(e, '导出失败')
  }
}

/** 归档清理：CSV 归档 → 清表 → 封缄开新链（后端单事务，失败链原样） */
async function onClearArchived() {
  const ok = await confirm({
    title: '归档清理审计记录',
    // total 是「当前筛选命中数」，而归档清理作用于整表 —— 只报筛选数会低估
    // 被归档的证据量（审计功能的文案不该在这一点上含糊）
    message: `将把后端审计表【全部】记录归档为 CSV 文件（保存在后端数据库目录，与当前筛选无关，`
      + `现在筛选命中 ${total.value} 条），然后清空列表并写入一条"清理封缄"记录开启新审计链。\n`
      + '历史证据保存在归档文件中，可离线校验，不会丢失。',
    confirmText: '归档并清理',
    danger: true,
  })
  if (!ok) return
  clearing.value = true
  try {
    const r = await auditApi.clearArchived()
    if (r.archived > 0) {
      toast.success(`已归档 ${r.archived} 条记录并清理，新链已开启（归档文件：${r.file}）`)
    } else {
      toast.info('审计记录已为空，无需清理')
    }
    await refresh()
  } catch (e) {
    toast.error(toFriendlyMessage(e, '归档清理失败，审计链未变动'))
  } finally {
    clearing.value = false
  }
}

function formatTs(ts: string): string {
  return formatBackendTs(ts, true)
}

function shortId(id: string): string {
  return id && id.length > 8 ? id.slice(0, 8) : (id || '')
}

function truncate(s: string, max = 80): string {
  return s && s.length > max ? s.slice(0, max) + '…' : (s || '')
}

onMounted(refresh)
</script>

<style scoped>
.audit-view {
  height: 100%;
  display: flex;
  flex-direction: column;
  /* 整页结构画布：走 --canvas-bg（默认=--bg-base；详见 DashboardView 同注）。 */
  background: var(--canvas-bg);
  color: var(--text-primary);
  overflow: hidden;
  box-sizing: border-box;
}
/* 头部统一以 RemoteManager .rm-header 为准：44px 高 + 16px 侧距 + 底部分隔线 */
.audit-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: var(--page-head-h);
  padding: 0 16px;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.audit-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--page-title-size);
  font-weight: 600;
  color: var(--text-primary);
  font-family: var(--font-sans);
}
.audit-head-left {
  display: flex;
  align-items: center;
  gap: 12px;
}
/* 分段切换：与远程管理页的「SSH 连接 / TLS 网关」同一形态（同一族约束，不另画一份） */
.audit-seg {
  display: flex;
  height: 26px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  overflow: hidden;
}
.audit-seg-btn {
  padding: 0 12px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  font-family: var(--font-sans);
  cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast);
}
.audit-seg-btn + .audit-seg-btn { border-left: 1px solid var(--border-subtle); }
.audit-seg-btn:hover { color: var(--text-primary); }
.audit-seg-btn.on {
  background: var(--bg-active);
  color: var(--text-primary);
  font-weight: 600;
}
.audit-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.audit-badge {
  font-size: 11px;
  padding: 2px 10px;
  border-radius: var(--radius-xl);
  border: 1px solid var(--border-default);
}
.audit-badge.ok { color: var(--accent-green); border-color: var(--accent-green); }
.audit-badge.broken { color: var(--accent-red); border-color: var(--accent-red); }
.audit-badge.idle { color: var(--text-tertiary); }
.audit-filters {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
  /* 筛选条同样卡片化，与下方表格卡同边距对齐 */
  margin: 12px 16px 0;
  padding: 10px 12px;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
}
.audit-filters select,
.audit-filters input {
  background: var(--bg-input);
  border: 1px solid var(--border-default);
  color: var(--text-primary);
  border-radius: var(--radius-sm);
  padding: 4px 8px;
  font-size: 12px;
}
.audit-filters input { flex: 1; max-width: 240px; }
.audit-table-wrap {
  flex: 1;
  overflow: auto;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  margin: 12px 16px 0;
  background: var(--bg-elevated);
}
.audit-table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed; /* 钉死列宽：翻页时浏览器不再按内容重排，防抖动 */
  font-size: 12px;
}
.audit-table th {
  position: sticky;
  top: 0;
  /* 表头浮在滚动行之上，须不透明：用 --bg-elevated-solid（实体主题与卡面同值，
     渐变模式下卡面半透明，滚动行也不会从表头底下透出来） */
  background: var(--bg-elevated-solid);
  text-align: left;
  padding: 8px 10px;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  color: var(--text-tertiary);
  border-bottom: 1px solid var(--border-subtle);
  z-index: 1;
}
.audit-table td {
  padding: 6px 10px;
  border-bottom: 1px solid var(--border-subtle);
  vertical-align: top;
  text-align: left;
}
.audit-table tr:hover td { background: var(--bg-hover); }
.audit-table tr.prod td { background: color-mix(in srgb, var(--accent-red) 5%, transparent); }
.mono { font-family: var(--font-mono); }
.env {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: var(--radius-lg);
  font-weight: 600;
}
.env.test { color: var(--accent-green); background: color-mix(in srgb, var(--accent-green) 12%, transparent); }
.env.prod { color: var(--accent-red); background: color-mix(in srgb, var(--accent-red) 12%, transparent); }
.concl {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: var(--radius-lg);
}
/* 底色由文字那枚令牌自己派生：原先压的是终端 ANSI 档（rgba(63,185,80/.229,165,75/…)），
   与 --accent-green/orange/red 不同 hue，墨绿字压 GitHub 绿、朱砂字压 GitHub 红 */
.concl.executed, .concl.auto_allowed, .concl.approved { color: var(--accent-green); background: color-mix(in srgb, var(--accent-green) 12%, transparent); }
.concl.blocked { color: var(--accent-orange); background: color-mix(in srgb, var(--accent-orange) 12%, transparent); }
.concl.denied { color: var(--accent-red); background: color-mix(in srgb, var(--accent-red) 12%, transparent); }
.concl.failed { color: var(--danger-text); background: color-mix(in srgb, var(--danger-text) 12%, transparent); }
.concl.released, .concl.revoked, .concl.answered, .concl.skipped { color: var(--text-tertiary); background: color-mix(in srgb, var(--text-tertiary) 12%, transparent); }
.cmd { max-width: 40ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.result { max-width: 30ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-tertiary); }
.empty { text-align: center; color: var(--text-tertiary); padding: 24px; }
.audit-pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  flex-shrink: 0;
  padding: 12px 16px;
}
.audit-pager-info { font-size: 12px; color: var(--text-tertiary); }
.audit-loading { color: var(--text-tertiary); font-size: 12px; padding: 0 16px 12px; }
.audit-error { color: var(--accent-red); font-size: 12px; padding: 0 16px 12px; }
/* 写入失败提示：用警示色描边而非满屏红底 —— 它是「证据可能不全」的告知，
   不是页面错误本身（表格数据仍然可读）。 */
.audit-health-warn {
  margin: 12px 16px 0;
  padding: 8px 12px;
  border: 1px solid var(--accent-yellow);
  border-left-width: 3px;
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--accent-yellow) 10%, transparent);
  color: var(--text-primary);
  font-size: 12px;
  line-height: 1.6;
  flex-shrink: 0;
}
</style>