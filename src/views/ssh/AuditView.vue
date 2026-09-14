<template>
  <div class="audit-view">
    <!-- 头部：标题 + 链完整性状态 + 操作 -->
    <div class="audit-header">
      <div class="audit-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><path d="M9 12l2 2 4-4"/><path d="M20 12a8 8 0 1 1-2.34-5.66"/></svg>
        <span>运维审计</span>
        <span class="audit-sub" v-if="verifyState">共 {{ verifyState.total }} 条记录</span>
      </div>
      <div class="audit-actions">
        <span
          class="audit-badge"
          :class="verifyState ? (verifyState.intact ? 'ok' : 'broken') : 'idle'"
          :title="verifyState && !verifyState.intact ? `检测到篡改: ${verifyState.broken.join(', ')}` : ''"
        >
          {{ verifyState ? (verifyState.intact ? '链完整' : '链被篡改!') : '未校验' }}
        </span>
        <button class="audit-btn" @click="refresh" :disabled="loading">刷新</button>
        <button class="audit-btn" @click="onVerify" :disabled="loading">校验</button>
        <button class="audit-btn" @click="onExport" :disabled="loading">导出 CSV</button>
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
      </select>
      <input v-model="filters.target_id" placeholder="目标 ID 过滤" @keyup.enter="loadFirst" />
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
            <td class="mono cmd" :title="r.command_raw">
              {{ r.command_raw || r.target_type + (r.target_id ? ' #' + shortId(r.target_id) : '') }}
            </td>
            <td class="result" :title="r.result">{{ truncate(r.result) }}</td>
          </tr>
          <tr v-if="!loading && records.length === 0">
            <td colspan="7" class="empty">{{ total === 0 ? '暂无审计记录' : '没有匹配的记录' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 分页：仅当有记录时显示（P3 修复，配合 offset 翻页） -->
    <div v-if="total > 0" class="audit-pager">
      <button class="audit-btn" :disabled="offset === 0 || loading" @click="prevPage">上一页</button>
      <span class="audit-pager-info">
        第 {{ offset + 1 }}–{{ Math.min(offset + records.length, total) }} 条 / 共 {{ total }} 条
      </span>
      <button class="audit-btn" :disabled="offset + records.length >= total || loading" @click="nextPage">下一页</button>
    </div>

    <div v-if="loading" class="audit-loading">加载中…</div>
    <div v-if="error" class="audit-error">{{ error }}</div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { auditApi, toFriendlyMessage, type AuditRecord, type AuditVerifyResult } from '@/api/client'

const records = ref<AuditRecord[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')
const verifyState = ref<AuditVerifyResult | null>(null)
// P3 修复：offset 分页（后端 /audit/records 已支持 offset，limit/offset 均带边界）。
// 页大小保持 200，记录倒序（id DESC）——"下一页"取更旧的记录（offset 增大）。
const pageSize = 200
const offset = ref(0)

const filters = reactive<{ environment: string; conclusion: string; target_id: string }>({
  environment: '',
  conclusion: '',
  target_id: '',
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await auditApi.query({
      environment: filters.environment || undefined,
      conclusion: filters.conclusion || undefined,
      target_id: filters.target_id || undefined,
      limit: pageSize,
      offset: offset.value,
    })
    records.value = res.items
    total.value = res.total
  } catch (e) {
    error.value = toFriendlyMessage(e, '加载审计记录失败')
  } finally {
    loading.value = false
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

function formatTs(ts: string): string {
  if (!ts) return ''
  try {
    const d = new Date(ts)
    if (isNaN(d.getTime())) return ts
    const p = (n: number) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  } catch {
    return ts
  }
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
  padding: 16px 20px;
  gap: 12px;
  background: var(--bg-base);
  color: var(--text-primary);
  overflow: hidden;
  box-sizing: border-box;
}
.audit-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
}
.audit-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 16px;
  font-weight: 600;
  color: var(--accent-blue);
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
.audit-btn {
  background: transparent;
  border: 1px solid var(--border-default);
  color: var(--text-secondary);
  border-radius: var(--radius-sm);
  padding: 4px 12px;
  font-size: 12px;
  cursor: pointer;
  transition: all var(--transition-fast);
}
.audit-btn:hover:not(:disabled) {
  color: var(--text-primary);
  border-color: var(--accent-blue);
}
.audit-btn:disabled { opacity: 0.5; cursor: default; }
.audit-filters {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}
.audit-filters select,
.audit-filters input {
  background: var(--bg-elevated);
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
  border: 1px solid var(--border-subtle, #2d2d30);
  border-radius: var(--radius-md);
}
.audit-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.audit-table th {
  position: sticky;
  top: 0;
  background: var(--bg-elevated);
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
  border-bottom: 1px solid var(--border-subtle, #26262a);
  vertical-align: top;
}
.audit-table tr:hover td { background: var(--bg-hover); }
.audit-table tr.prod td { background: rgba(244, 135, 113, 0.05); }
.mono { font-family: var(--font-mono); }
.env {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 8px;
  font-weight: 600;
}
.env.test { color: var(--accent-green); background: rgba(63, 185, 80, 0.12); }
.env.prod { color: var(--accent-red); background: rgba(244, 135, 113, 0.12); }
.concl {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 8px;
}
.concl.executed, .concl.auto_allowed, .concl.approved { color: var(--accent-green); background: rgba(63, 185, 80, 0.12); }
.concl.blocked { color: var(--accent-orange, #e5a54b); background: rgba(229, 165, 75, 0.12); }
.concl.denied { color: var(--accent-red); background: rgba(244, 135, 113, 0.12); }
.cmd { max-width: 40ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.result { max-width: 30ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-tertiary); }
.empty { text-align: center; color: var(--text-tertiary); padding: 24px; }
.audit-pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  flex-shrink: 0;
}
.audit-pager-info { font-size: 12px; color: var(--text-tertiary); }
.audit-loading { color: var(--text-tertiary); font-size: 12px; }
.audit-error { color: var(--accent-red); font-size: 12px; }
</style>