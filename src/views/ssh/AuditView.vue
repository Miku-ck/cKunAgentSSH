<template>
  <div class="audit-view">
    <!-- 头部：标题 + 链完整性状态 + 操作 -->
    <div class="audit-header">
      <div class="audit-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><path d="M9 12l2 2 4-4"/><path d="M20 12a8 8 0 1 1-2.34-5.66"/></svg>
        <span>运维审计</span>
      </div>
      <div class="audit-actions">
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
           运行视图头部的 № 点一下就是全号。 -->
      <input
        v-model="filters.run_id"
        class="trace-input"
        placeholder="trace 号（整串）"
        title="按 trace 号找回某一条 run 的全部记录。运行视图/待放行队列头部点 № 可复制整串号"
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
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { auditApi, toFriendlyMessage, type AuditRecord, type AuditVerifyResult } from '@/api/client'
import { useConfirm } from '@/composables/useConfirm'
import { useToastStore } from '@/stores/toast'
import { formatBackendTs } from '@/utils/format'

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
  // 它会把已匹配当前筛选的结果覆盖成旧筛选的记录（审计页误读风险）
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
  await Promise.all([loadFirst(), verify()])
}

async function onVerify() {
  await verify()
}

async function verify() {
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
  background: var(--bg-base);
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
.audit-sub {
  font-size: 12px;
  font-weight: 400;
  color: var(--text-tertiary);
}
.audit-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.audit-badge {
  font-size: 11px;
  padding: 2px 10px;
  border-radius: 10px;
  border: 1px solid var(--border-default);
}
.audit-badge.ok { color: var(--accent-green); border-color: var(--accent-green); }
.audit-badge.broken { color: var(--accent-red); border-color: var(--accent-red); }
.audit-badge.idle { color: var(--text-tertiary); }
.audit-filters {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
  padding: 12px 16px;
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
  margin: 0 16px;
}
.audit-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.audit-table th {
  position: sticky;
  top: 0;
  background: var(--bg-base);
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
  border-radius: 8px;
  font-weight: 600;
}
.env.test { color: var(--accent-green); background: color-mix(in srgb, var(--accent-green) 12%, transparent); }
.env.prod { color: var(--accent-red); background: color-mix(in srgb, var(--accent-red) 12%, transparent); }
.concl {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 8px;
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
</style>