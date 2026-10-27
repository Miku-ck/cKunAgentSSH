<template>
  <Teleport to="body">
    <div v-if="visible" class="transfer-overlay" @click.self="emit('close')" @contextmenu.prevent>
      <div class="tc" role="dialog" aria-modal="true" aria-label="传输任务">
        <!-- 头 -->
        <div class="tc-head">
          <span class="tc-title">传输任务</span>
          <div class="tc-head-spacer" />
          <button v-if="pausableCount > 0" class="btn" @click="pauseAll">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
            全部暂停
          </button>
          <button v-if="resumableCount > 0" class="btn" @click="resumeAll">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
            全部继续
          </button>
          <button v-if="stats.error > 0" class="btn" @click="retryAllErrors">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
            重试失败 {{ stats.error }}
          </button>
          <button v-if="clearableCount > 0" class="btn" @click="clearCompleted">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>
            清除已结束
          </button>
          <button class="tc-close" aria-label="关闭" @click="emit('close')">×</button>
        </div>

        <!-- 筛选 -->
        <div class="tc-tabs">
          <button
            v-for="tab in TABS"
            :key="tab.key"
            class="tc-tab"
            :class="{ on: activeTab === tab.key }"
            @click="activeTab = tab.key"
          >
            {{ tab.label }}<span class="n" :class="{ bad: tab.key === 'error' && stats.error > 0 }">{{ tabCount(tab.key) }}</span>
          </button>
          <div class="tc-tabs-spacer" />
          <input v-model.trim="filter" class="tc-search" placeholder="筛选文件名 / 路径" />
        </div>

        <!-- 表头 -->
        <div class="tc-thead">
          <div />
          <div
            v-for="col in COLS"
            :key="col.key"
            class="th"
            :class="[col.align, { sorted: sortKey === col.key }]"
            @click="toggleSort(col.key)"
          >
            {{ col.label }}
            <svg v-if="sortKey === col.key" class="th-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline v-if="sortDir > 0" points="6 9 12 3 18 9" />
              <polyline v-else points="6 15 12 21 18 15" />
            </svg>
          </div>
        </div>

        <!-- 行 -->
        <div class="tc-body">
          <div v-if="store.transfers.length === 0" class="tc-empty">
            <p>还没有传输记录</p>
          </div>
          <template v-else>
            <template v-for="unit in renderUnits" :key="unit.key">
              <TransferRow
                :view="unit"
                :now="now"
                @pause="eachId(unit, id => store.pauseTransfer(id))"
                @resume="eachId(unit, id => store.resumeTransfer(id))"
                @cancel="eachId(unit, id => store.cancelTransfer(id))"
                @retry="eachId(unit, id => store.retryTransfer(id))"
                @remove="eachId(unit, id => store.removeTransfer(id))"
                @delete="eachId(unit, id => store.deleteTransfer(id))"
                @toggle="toggleGroup(unit)"
                @context="onRowContext($event, unit)"
              />
              <div v-if="unit.truncated" class="grp-more">
                组内还有 {{ unit.truncated }} 项未展开，切到「排队」页可逐条查看
              </div>
            </template>
            <div v-if="renderUnits.length === 0" class="tc-empty">
              <p>{{ emptyText }}</p>
            </div>
          </template>
        </div>

        <!-- 底栏 -->
        <div class="tc-foot">
          <span class="conc">
            最大并发
            <button class="conc-btn" :disabled="store.maxConcurrent <= 1" @click="store.setMaxConcurrent(store.maxConcurrent - 1)">−</button>
            <span class="conc-val">{{ store.maxConcurrent }}</span>
            <button class="conc-btn" :disabled="store.maxConcurrent >= MAX_CONCURRENT_CEIL" @click="store.setMaxConcurrent(store.maxConcurrent + 1)">+</button>
          </span>
        </div>
      </div>
    </div>

    <!-- 传输行右键菜单 -->
    <div
      v-if="ctx.visible"
      class="ctx-menu"
      :style="{ left: ctx.x + 'px', top: ctx.y + 'px' }"
      @click.stop
      @contextmenu.prevent
    >
      <div v-for="a in ctxActions" :key="a.label" class="ctx-item" @click="a.run()">
        <!-- 图标按 key 就地内联渲染：菜单项是固定枚举，不做字符串拼接式注入 -->
        <svg v-if="a.icon === 'pause'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
        <svg v-else-if="a.icon === 'resume'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polygon points="5 3 19 12 5 21 5 3"/></svg>
        <svg v-else-if="a.icon === 'cancel'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        <svg v-else-if="a.icon === 'remove'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="3 6 5 6 21 6"/></svg>
        <svg v-else-if="a.icon === 'del'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="3 6 5 6 21 6"/><path d="M5 6v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V6"/><line x1="9" y1="6" x2="9" y2="20"/><line x1="15" y1="6" x2="15" y2="20"/></svg>
        <svg v-else-if="a.icon === 'retry'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
        <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
        {{ a.label }}
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
/**
 * 传输中心 —— 从 SftpView.vue 整体迁出。
 *
 * 迁出前它挤在 SftpView 的模板里（原 :51-239），和连接选择、文件右键、重命名、
 * ZIP 四个浮层同住一个 1458 行的文件；「进行中」与「已完成」还各抄一份行模板。
 *
 * 三处刻意的设计：
 *  1. `now` 由父级下发而不是本组件读 Date.now() —— 每行的剩余时间要与会话栏徽标踩
 *     同一个秒级 tick，否则同一屏上两个数字不同步。
 *  2. 这里不摆队列级读数：逐项统计与总进度条都已按裁决撤掉，本组件只有每行自己的
 *     数字（queueStats 现在只剩 tab 计数与会话栏徽标两个消费方）。
 *  3. 目录任务折叠成组头，动作对组内 id 逐个调 store 的既有单条方法 ——
 *     不新增组级 IPC，调度与续传语义完全不变。
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import TransferRow from './TransferRow.vue'
import { MAX_CONCURRENT_CEIL, useSftpStore } from '@/stores/sftp'
import { useToastStore } from '@/stores/toast'
import {
  buildRenderUnits,
  type TransferRowView,
  type TransferSortKey,
  type TransferTabKey,
} from '@/utils/transfer-groups'
import { menuTop } from '@/utils/menu'

const props = defineProps<{ visible: boolean; now: number }>()
const emit = defineEmits<{ close: [] }>()

const store = useSftpStore()
const toast = useToastStore()

/** tab / 排序键的取值域由归并模块定义，这里只起本地名 */
type TabKey = TransferTabKey
type SortKey = TransferSortKey

const TABS: Array<{ key: TabKey; label: string }> = [
  { key: 'active', label: '进行中' },
  { key: 'queued', label: '排队' },
  { key: 'done', label: '已完成' },
  { key: 'error', label: '失败' },
  { key: 'all', label: '全部' },
]

const COLS: Array<{ key: SortKey; label: string; align?: string }> = [
  { key: 'name', label: '名称' },
  { key: 'path', label: '路径' },
  { key: 'percent', label: '进度' },
  { key: 'speed', label: '速度', align: 'r' },
  { key: 'size', label: '已传 / 总量', align: 'r' },
  { key: 'eta', label: '剩余', align: 'r' },
  { key: 'status', label: '状态' },
  { key: 'queue', label: '操作', align: 'c' },
]

const activeTab = ref<TabKey>('active')
const filter = ref('')
const sortKey = ref<SortKey>('queue')
const sortDir = ref<1 | -1>(1)
const expanded = ref<Set<string>>(new Set())

const stats = computed(() => store.queueStats)

function tabCount(key: TabKey): number {
  if (key === 'all') return store.transfers.length
  if (key === 'active') return stats.value.running + stats.value.paused
  if (key === 'queued') return stats.value.pending
  if (key === 'done') return stats.value.done + stats.value.cancelled
  return stats.value.error
}

const emptyText = computed(() =>
  filter.value ? `没有匹配「${filter.value}」的任务` : '该分类下暂无任务')

// 归并/过滤/排序全在 utils/transfer-groups（那边有单测）。组件只负责把状态喂进去。
const renderUnits = computed(() => buildRenderUnits({
  transfers: store.transfers,
  tab: activeTab.value,
  needle: filter.value,
  expanded: expanded.value,
  sortKey: sortKey.value,
  sortDir: sortDir.value,
  now: props.now,
}))

/** 可暂停 = 在跑的 upload/download。copy 类是单条 cp -r / fsCopy 循环，没有中途暂停语义。 */
const pausableCount = computed(() =>
  store.transfers.filter(t => t.status === 'running' && (t.kind === 'upload' || t.kind === 'download')).length)
const resumableCount = computed(() => stats.value.paused)
const clearableCount = computed(() => stats.value.done + stats.value.error + stats.value.cancelled)

function eachId(unit: TransferRowView, fn: (id: string) => void): void {
  for (const id of unit.ids) fn(id)
}
/** 批量动作就是逐条调 store 的单条方法 —— 不新增组级/全局 IPC，
 *  于是取消竞态、续传 .part 定位、暂停不被 CANCELLED 覆盖这些既有语义原样生效。 */
function pauseAll(): void {
  for (const t of store.transfers) {
    if (t.status === 'running' && (t.kind === 'upload' || t.kind === 'download')) store.pauseTransfer(t.id)
  }
}
function resumeAll(): void {
  for (const t of store.transfers) if (t.status === 'paused') store.resumeTransfer(t.id)
}
function retryAllErrors(): void {
  for (const t of store.transfers) if (t.status === 'error') store.retryTransfer(t.id)
}
function clearCompleted(): void {
  store.clearCompleted()
}
function toggleGroup(unit: TransferRowView): void {
  const gid = unit.key.slice(2)
  const next = new Set(expanded.value)
  if (next.has(gid)) next.delete(gid)
  else next.add(gid)
  expanded.value = next
}
function toggleSort(key: SortKey): void {
  if (sortKey.value === key) {
    sortDir.value = sortDir.value === 1 ? -1 : 1
  } else {
    sortKey.value = key
    sortDir.value = 1
  }
}

// ─── 右键菜单 ─────────────────────────
const ctx = ref<{ visible: boolean; x: number; y: number; unit: TransferRowView | null }>({
  visible: false, x: 0, y: 0, unit: null,
})

type CtxIcon = 'pause' | 'resume' | 'cancel' | 'remove' | 'del' | 'copy' | 'retry'

function writeClip(text: string, label: string): void {
  navigator.clipboard.writeText(text)
    .then(() => toast.success(`已复制${label}`))
    .catch(() => toast.error('复制失败'))
}

const ctxActions = computed<Array<{ label: string; icon: CtxIcon; run: () => void }>>(() => {
  const u = ctx.value.unit
  if (!u) return []
  const act = (fn: (id: string) => void) => () => { eachId(u, fn); ctx.value.visible = false }
  const out: Array<{ label: string; icon: CtxIcon; run: () => void }> = []
  const pausable = u.kind === 'upload' || u.kind === 'download'
  if (u.status === 'running' && pausable) out.push({ label: '暂停传输', icon: 'pause', run: act(id => store.pauseTransfer(id)) })
  if (u.status === 'paused') out.push({ label: '继续传输', icon: 'resume', run: act(id => store.resumeTransfer(id)) })
  if (u.status === 'running') out.push({ label: '取消传输', icon: 'cancel', run: act(id => store.cancelTransfer(id)) })
  if (u.status === 'pending') out.push({ label: '取消排队', icon: 'cancel', run: act(id => store.cancelTransfer(id)) })
  if (u.status === 'done' || u.status === 'error' || u.status === 'cancelled') {
    out.push({ label: '移除记录', icon: 'remove', run: act(id => store.removeTransfer(id)) })
  } else {
    out.push({ label: '删除任务', icon: 'del', run: act(id => store.deleteTransfer(id)) })
  }
  out.push({ label: '复制源路径', icon: 'copy', run: () => { writeClip(u.from, '源路径'); ctx.value.visible = false } })
  out.push({ label: '复制目标路径', icon: 'copy', run: () => { writeClip(u.to, '目标路径'); ctx.value.visible = false } })
  if (u.status === 'error') out.push({ label: '重试', icon: 'retry', run: act(id => store.retryTransfer(id)) })
  return out
})

function onRowContext(event: MouseEvent, unit: TransferRowView): void {
  ctx.value = { visible: true, x: event.clientX, y: menuTop(event.clientY, 150), unit }
}
function dismissCtx(): void { ctx.value.visible = false }
document.addEventListener('click', dismissCtx)
onUnmounted(() => document.removeEventListener('click', dismissCtx))

// Esc 关闭 + 关闭时收起右键菜单（浮层卸载后监听必须清理，见 DESIGN.md 交互节）
function onKey(e: KeyboardEvent): void {
  if (e.key === 'Escape') {
    if (ctx.value.visible) { ctx.value.visible = false; return }
    emit('close')
  }
}
watch(() => props.visible, v => {
  if (v) document.addEventListener('keydown', onKey)
  else { document.removeEventListener('keydown', onKey); ctx.value.visible = false }
}, { immediate: true })
onUnmounted(() => document.removeEventListener('keydown', onKey))
</script>

<style scoped>
.transfer-overlay {
  position: fixed;
  inset: 0;
  background: var(--scrim);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.tc {
  /* 1180 不是随手加的：路径独立成列后，固定列合计 812px + 8 条 8px 间隙 + 内缩与滚动条，
     再窄下去名称列就只剩百来像素，等于把路径的账算到名称头上。 */
  width: min(1180px, 95vw);
  height: min(748px, 92vh);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
}

.tc-head {
  display: flex; align-items: center; gap: 8px;
  padding: 0 12px 0 16px; height: 46px; flex-shrink: 0;
  border-bottom: 1px solid var(--border-subtle);
}
.tc-title { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.tc-head-spacer { flex: 1; }
.tc-head .btn svg { width: 12px; height: 12px; }
.tc-close {
  width: 24px; height: 24px; padding: 0; line-height: 1;
  background: transparent; border: none; color: var(--text-tertiary);
  font-size: 18px; cursor: pointer; border-radius: var(--radius-sm);
}
.tc-close:hover { background: var(--bg-hover); color: var(--text-primary); }

/* 筛选 */
.tc-tabs {
  display: flex; align-items: center; gap: 2px; padding: 0 12px;
  height: 34px; flex-shrink: 0; border-bottom: 1px solid var(--border-subtle);
}
.tc-tab {
  display: flex; align-items: center; gap: 5px; height: 24px; padding: 0 9px;
  border: none; background: transparent; color: var(--text-tertiary);
  font-size: 11px; cursor: pointer; border-radius: var(--radius-sm);
  border-bottom: 2px solid transparent; transition: all var(--transition-fast);
  font-family: inherit;
}
.tc-tab:hover { background: var(--bg-hover); color: var(--text-secondary); }
.tc-tab.on { color: var(--text-primary); background: var(--bg-hover); border-bottom-color: var(--accent-primary); }
.tc-tab .n { font-family: var(--font-mono); font-size: 10px; color: var(--text-tertiary); }
.tc-tab.on .n { color: var(--accent-text); }
.tc-tab .n.bad { color: var(--accent-red); }
.tc-tabs-spacer { flex: 1; }
.tc-search {
  height: 22px; width: 160px; padding: 0 8px;
  background: var(--bg-input); border: 1px solid var(--border-input);
  border-radius: var(--radius-sm); color: var(--text-primary);
  font-size: 11px; outline: none; font-family: inherit;
}
.tc-search:focus { border-color: var(--accent-primary); }
.tc-search::placeholder { color: var(--text-tertiary); }

/* 表头 —— 列宽必须与 TransferRow 的 .tr 逐字一致 */
.tc-thead {
  display: grid;
  grid-template-columns: 30px minmax(0, 1fr) 260px 120px 80px 124px 56px 76px 66px;
  gap: 8px; align-items: center; height: 26px; padding: 0 12px 0 10px;
  /* 画卡片自己那一层，不是 --bg-base：这枚是 sticky 表头，行会从它底下滚过，
     透明（继承弹窗底）就等于让行文字压在表头文字上。先前取 --bg-base 是在
     --bg-elevated 的对话框上挖洞。与页面里的 .file-list-head 同一读法 ——
     表头永远与它所在的那一层同色，靠 border-bottom 认边界。 */
  flex-shrink: 0; background: var(--bg-elevated);
  border-bottom: 1px solid var(--border-subtle);
  font-size: 11px; color: var(--text-tertiary);
  position: sticky; top: 0; z-index: 2;
}
.th { cursor: pointer; display: flex; align-items: center; gap: 3px; user-select: none; }
.th:hover { color: var(--text-secondary); }
.th.sorted { color: var(--accent-text); }
.th.r { justify-content: flex-end; }
.th.c { justify-content: center; }
.th-arrow { width: 10px; height: 10px; }

.tc-body { flex: 1; overflow-y: auto; padding: 4px 0 6px; }
.tc-body::-webkit-scrollbar { width: 6px; }
.tc-body::-webkit-scrollbar-thumb { background: var(--border-default); border-radius: 3px; }
.grp-more {
  padding: 4px 0 6px 44px;
  font-family: var(--font-mono); font-size: 10px; line-height: 1.4; color: var(--text-tertiary);
}

/* tc-body 是滚动块不是 flex 容器，flex:1 撑不起来 —— 用固定高度做双向居中 */
.tc-empty {
  min-height: 240px;
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  padding: 24px; text-align: center;
  font-size: 12px; color: var(--text-tertiary);
}

.tc-foot {
  flex-shrink: 0; display: flex; align-items: center; gap: 14px;
  padding: 0 14px; height: 38px;
  border-top: 1px solid var(--border-subtle); background: var(--bg-elevated);
  font-family: var(--font-mono); font-size: 11px; line-height: 1; color: var(--text-tertiary);
}
.tc-foot b { color: var(--text-secondary); font-weight: 600; }
.conc { display: flex; align-items: center; gap: 6px; }
.conc-btn {
  width: 18px; height: 18px; padding: 0; line-height: 1;
  border: 1px solid var(--border-default); background: transparent;
  color: var(--text-secondary); border-radius: var(--radius-sm);
  cursor: pointer; font-size: 11px; font-family: inherit;
}
.conc-btn:hover:not(:disabled) { background: var(--bg-hover); color: var(--accent-text); border-color: var(--accent-primary); }
.conc-btn:disabled { opacity: .45; cursor: not-allowed; }
.conc-val { color: var(--text-primary); min-width: 10px; text-align: center; }

/* 右键菜单 */
.ctx-menu {
  position: fixed; z-index: 1100; min-width: 160px; padding: 4px;
  background: var(--bg-elevated); border: 1px solid var(--border-default);
  border-radius: var(--radius-md); box-shadow: var(--shadow-lg);
}
.ctx-item {
  display: flex; align-items: center; gap: 8px; padding: 6px 12px;
  font-size: 12px; color: var(--text-secondary); cursor: pointer;
  border-radius: var(--radius-sm);
}
.ctx-item:hover { background: var(--bg-hover); color: var(--text-primary); }
.ctx-item :deep(svg) { flex-shrink: 0; }
</style>
