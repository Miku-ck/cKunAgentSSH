<template>
  <div
    class="tr"
    :class="[`st-${view.status}`, { 'is-group': view.isGroup, child: view.isChild }]"
    @contextmenu.prevent="emit('context', $event)"
  >
    <!-- 方向 / 折叠 -->
    <div class="td-dir">
      <button
        v-if="view.isGroup"
        class="caret"
        :class="{ open: view.expanded }"
        :aria-expanded="view.expanded"
        :title="view.expanded ? '收起组内文件' : '展开组内文件'"
        @click.stop="emit('toggle')"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>
      </button>
      <span class="tl-icon" :class="iconClass">
        <svg v-if="view.kind === 'copy-local' || view.kind === 'copy-remote'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
        <svg v-else-if="view.direction === 'up'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="7 16 12 21 17 16"/><line x1="12" y1="3" x2="12" y2="21"/></svg>
      </span>
    </div>

    <!-- 名称 + 路径 / 错误 -->
    <!-- 名称 -->
    <div class="td-name">
      <div class="nm">
        <span class="nm-text">{{ view.label }}</span>
        <span v-if="view.meta" class="tag" :class="{ warn: view.status === 'paused' }">{{ view.meta }}</span>
      </div>
      <div v-if="view.error" class="err-line" :title="view.error">{{ view.error }}</div>
    </div>

    <!-- 路径：源与目标各占一行，独立成列 -->
    <div class="td-path">
      <div class="pth-line">
        <span class="pth-k">源</span>
        <span class="pth-v" :title="view.from">{{ truncatePath(view.from) }}</span>
      </div>
      <div class="pth-line">
        <span class="pth-k">目标</span>
        <span class="pth-v" :title="view.to">{{ truncatePath(view.to) }}</span>
      </div>
    </div>

    <!-- 进度 -->
    <div class="td-prog">
      <div class="pbar">
        <div v-if="indeterminate" class="pind"><i /></div>
        <div v-else class="pfill" :class="fillClass" :style="{ width: percent + '%' }" />
      </div>
      <span class="pnum" :class="{ dim: view.status !== 'running', ind: indeterminate }">
        {{ indeterminate ? UNKNOWN : percent + '%' }}
      </span>
    </div>

    <!-- 速度 / 已传 · 总量 / 剩余 -->
    <div class="cell-num" :class="{ live: speed > 0 }">{{ speedText }}</div>
    <div class="cell-num">{{ sizeText }}</div>
    <div class="cell-num eta" :class="{ zero: eta === UNKNOWN }">{{ eta }}</div>

    <!-- 状态 -->
    <div class="st">
      <i /><span>{{ statusText }}</span>
    </div>

    <!-- 动作 -->
    <div class="td-act">
      <button
        v-if="view.status === 'running' && pausable"
        class="act pause"
        title="暂停"
        @click.stop="emit('pause')"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
      </button>
      <button
        v-else-if="view.status === 'paused'"
        class="act resume"
        title="继续"
        @click.stop="emit('resume')"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
      </button>
      <button
        v-else-if="view.status === 'error' || view.status === 'cancelled'"
        class="act resume"
        title="重新传输"
        @click.stop="emit('retry')"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="1 4 1 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>
      </button>
      <button
        v-if="cancellable"
        class="act cancel"
        :title="view.status === 'pending' ? '取消排队' : '取消'"
        @click.stop="emit('cancel')"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
      <button
        class="act"
        :title="terminable ? '移除记录' : '删除任务'"
        @click.stop="onTrailingAction"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M5 6v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V6"/></svg>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 传输中心的一行 —— 单文件任务或一个目录组头共用同一份模板。
 *
 * 抽出来的直接原因：SftpView.vue 里「进行中」和「已完成」各抄了一份几乎逐字
 * 相同的 ~60 行行模板（原 :75-183 与 :191-235），改一处必漏另一处。
 *
 * 它是哑的：所有数字由父组件算好后经 `view` 传入，`now` 也由父级的同一个秒级
 * tick 下发（组件自己读 Date.now() 就是原实现里数字僵住的成因）。
 * 动作一律 emit 出去，由父级对 `view.ids` 逐个调 store —— 组头一次作用于组内
 * 全部成员，不新增任何组级 IPC。
 */
import { computed } from 'vue'
import { formatBytes } from '@/utils/format'
import {
  averageSpeed,
  formatDuration,
  formatEta,
  formatRate,
  isIndeterminate,
  progressPercent,
  truncatePath,
  UNKNOWN,
  type TransferStatus,
} from '@/utils/transfer-format'

/** 行视图的形状与归并逻辑同住在 utils/transfer-groups（本组件只消费，不定义） */
import type { TransferRowView } from '@/utils/transfer-groups'

const props = defineProps<{ view: TransferRowView; now: number }>()

const emit = defineEmits<{
  pause: []
  resume: []
  cancel: []
  retry: []
  remove: []
  delete: []
  toggle: []
  context: [MouseEvent]
}>()

const v = computed(() => props.view)
const progress = computed(() => ({
  status: v.value.status,
  transferred: v.value.transferred,
  total: v.value.total,
  startTime: v.value.startTime,
  endTime: v.value.endTime,
}))

const percent = computed(() => progressPercent(progress.value))
const indeterminate = computed(() => isIndeterminate(progress.value))
const speed = computed(() => averageSpeed(progress.value, props.now))

/** 只有真正在跑的行才谈"暂停"：copy 类是单条 cp -r / fsCopy 循环，没有中途暂停语义 */
const pausable = computed(() => v.value.kind === 'upload' || v.value.kind === 'download')
const cancellable = computed(() => v.value.status === 'running' || v.value.status === 'pending')
/** 已结束 = 只剩「移除记录」；未结束 = 删行会连带停掉主进程传输，措辞是「删除任务」 */
const terminable = computed(() =>
  v.value.status === 'done' || v.value.status === 'error' || v.value.status === 'cancelled')

/** 同一枚按钮承担两种终态动作。emit 的每个事件是一条独立重载，
 *  `emit(cond ? 'remove' : 'delete')` 传联合字面量过不了类型检查 —— 在这里分流。 */
function onTrailingAction(): void {
  if (terminable.value) emit('remove')
  else emit('delete')
}

const iconClass = computed(() => {
  if (v.value.kind === 'copy-local' || v.value.kind === 'copy-remote') return 'copy'
  return v.value.direction === 'up' ? 'up' : 'down'
})

const fillClass = computed(() => ({
  up: v.value.direction === 'up' && v.value.kind !== 'copy-local' && v.value.kind !== 'copy-remote',
  down: v.value.direction === 'down' && v.value.kind !== 'copy-local' && v.value.kind !== 'copy-remote',
  paused: v.value.status === 'paused',
  error: v.value.status === 'error',
  done: v.value.status === 'done',
  sheen: v.value.status === 'running',
}))

const speedText = computed(() => {
  if (v.value.status === 'running') return indeterminate.value ? UNKNOWN : formatRate(speed.value)
  if (v.value.status === 'paused') return '0 B/s'
  return UNKNOWN
})

const sizeText = computed(() => {
  if (!v.value.total) {
    // 总量未知：只报已传，不写「/ —」，那会被读成一个空缺的字段
    return v.value.transferred > 0 ? `已传 ${formatBytes(v.value.transferred)}` : UNKNOWN
  }
  return `${formatBytes(v.value.transferred)} / ${formatBytes(v.value.total)}`
})

const eta = computed(() => {
  if (v.value.status === 'running') return formatEta(progress.value, props.now)
  // 已结束的行「剩余」位没有剩余可言，但用时是回看队列时真正想知道的量
  if (v.value.status === 'done' || v.value.status === 'error' || v.value.status === 'cancelled') {
    return formatDuration(progress.value, props.now)
  }
  return UNKNOWN
})

const statusText = computed(() => {
  const s: Record<TransferStatus, string> = {
    pending: '排队',
    running: indeterminate.value ? '复制中' : '传输中',
    paused: '已暂停',
    done: '完成',
    error: '失败',
    cancelled: '已取消',
  }
  return s[v.value.status]
})
</script>

<style scoped>
.tr {
  display: grid;
  grid-template-columns: 30px minmax(0, 1fr) 260px 120px 80px 124px 56px 76px 66px;
  gap: 8px;
  align-items: center;
  min-height: 38px;
  padding: 0 12px 0 10px;
  border-radius: var(--radius-sm);
  position: relative;
  transition: background var(--transition-fast);
}
.tr + .tr { margin-top: 1px; }
.tr:hover { background: var(--bg-hover); }
.tr.child { min-height: 32px; }
.tr.child .td-name { padding-left: 18px; }
.tr.is-group {
  min-height: 44px;
  background: color-mix(in srgb, var(--accent-primary) 6%, transparent);
}
.tr.is-group:hover { background: color-mix(in srgb, var(--accent-primary) 11%, transparent); }

.td-dir { display: flex; align-items: center; gap: 2px; }
.caret {
  width: 16px; height: 16px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  border: none; background: transparent; color: var(--text-tertiary);
  cursor: pointer; border-radius: var(--radius-sm);
}
.caret:hover { background: var(--bg-active); color: var(--text-primary); }
.caret svg { width: 10px; height: 10px; transition: transform var(--transition-fast); }
.caret.open svg { transform: rotate(90deg); }

.tl-icon {
  width: 22px; height: 22px; border-radius: 50%; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
}
.tl-icon svg { width: 12px; height: 12px; }
.tl-icon.up { background: color-mix(in srgb, var(--accent-green) 12%, transparent); color: var(--accent-green); }
.tl-icon.down { background: color-mix(in srgb, var(--accent-cyan) 12%, transparent); color: var(--accent-cyan); }
.tl-icon.copy { background: color-mix(in srgb, var(--accent-primary) 14%, transparent); color: var(--accent-text); }

.td-name { min-width: 0; display: flex; flex-direction: column; gap: 1px; }
.nm { display: flex; align-items: baseline; gap: 6px; min-width: 0; }
.nm-text {
  font-size: 12px; font-weight: 500; color: var(--text-primary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.tag { font-family: var(--font-mono); font-size: 10px; line-height: 1.3; color: var(--text-tertiary); white-space: nowrap; }
.tag.warn { color: var(--accent-orange); }
/* 路径列：源 / 目标各一行，左侧短标签标身份 —— 没有标签就得靠猜谁是谁。
   truncatePath 保留末两段：CSS 省略号砍的是尾巴，那正好是文件名。 */
.td-path { min-width: 0; display: flex; flex-direction: column; gap: 1px; }
.pth-line { display: flex; align-items: baseline; gap: 5px; min-width: 0; }
.pth-k {
  flex-shrink: 0; width: 24px;
  font-family: var(--font-mono); font-size: 10px; line-height: 1.4;
  color: var(--text-tertiary); opacity: .75;
}
.pth-v {
  min-width: 0;
  font-family: var(--font-mono); font-size: 10px; line-height: 1.4;
  color: var(--text-tertiary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.pth-line:last-child .pth-v { color: var(--text-secondary); }
.err-line {
  font-family: var(--font-mono); font-size: 10px; line-height: 1.3; color: var(--accent-red);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

.td-prog { display: flex; align-items: center; gap: 8px; }
.pbar {
  flex: 1; height: 6px; border-radius: var(--radius-sm); overflow: hidden;
  background: var(--bg-input); border: 1px solid var(--border-subtle); position: relative;
}
.pfill { height: 100%; border-radius: var(--radius-xs); background: var(--accent-primary); transition: width var(--transition-fast); }
.pfill.up { background: var(--accent-green); }
.pfill.down { background: var(--accent-cyan); }
.pfill.paused { background: var(--accent-orange); }
.pfill.error { background: var(--accent-red); }
.pfill.done { background: color-mix(in srgb, var(--accent-green) 60%, transparent); }
/* 跑动中的行加一道扫光：6px 的静止条看不出"还在动" */
.pfill.sheen { position: relative; overflow: hidden; }
.pfill.sheen::after {
  content: ''; position: absolute; inset: 0;
  background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--bg-elevated) 55%, transparent), transparent);
  animation: tr-sheen 1.6s linear infinite;
}
@keyframes tr-sheen { from { transform: translateX(-100%); } to { transform: translateX(100%); } }
.pind { position: absolute; inset: 0; overflow: hidden; }
.pind i {
  position: absolute; top: 0; bottom: 0; width: 38%; border-radius: var(--radius-xs);
  background: var(--accent-primary); animation: tr-slide 1.2s ease-in-out infinite;
}
@keyframes tr-slide { 0% { transform: translateX(-105%); } 100% { transform: translateX(270%); } }
.pnum { font-family: var(--font-mono); font-size: 11px; font-weight: 600; line-height: 1; color: var(--text-primary); min-width: 34px; text-align: right; }
.pnum.dim { color: var(--text-tertiary); font-weight: 400; }
.pnum.ind { color: var(--text-secondary); font-weight: 400; }

.cell-num { font-family: var(--font-mono); font-size: 11px; line-height: 1; color: var(--text-secondary); text-align: right; white-space: nowrap; }
.cell-num.live { color: var(--accent-text); }
.cell-num.zero { color: var(--text-tertiary); }
.cell-num.eta { color: var(--text-secondary); }

.st { display: flex; align-items: center; gap: 5px; font-size: 11px; white-space: nowrap; color: var(--text-tertiary); }
.st i { width: 5px; height: 5px; border-radius: 50%; flex-shrink: 0; background: var(--text-tertiary); }
.st-pending .st { color: var(--text-tertiary); }
.st-running .st { color: var(--accent-text); }
.st-running .st i { background: var(--accent-primary); animation: tr-blink 1.2s ease-in-out infinite; }
.st-paused .st { color: var(--accent-orange); } .st-paused .st i { background: var(--accent-orange); }
.st-done .st { color: var(--accent-green); } .st-done .st i { background: var(--accent-green); }
.st-error .st { color: var(--accent-red); } .st-error .st i { background: var(--accent-red); }
.st-cancelled .st { color: var(--text-tertiary); }
@keyframes tr-blink { 0%, 100% { opacity: 1; } 50% { opacity: .25; } }

.td-act { display: flex; gap: 2px; justify-content: flex-end; }
.act {
  width: 22px; height: 22px; padding: 0; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  background: transparent; border: none; color: var(--text-tertiary);
  cursor: pointer; border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
}
.act svg { width: 12px; height: 12px; }
.act:hover { background: var(--bg-active); color: var(--text-primary); }
.act.pause:hover { color: var(--accent-orange); }
.act.resume:hover { color: var(--accent-green); }
.act.cancel:hover { color: var(--accent-red); }
</style>
