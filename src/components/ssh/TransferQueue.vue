<template>
  <div class="transfer-bar" :class="{ active: store.transfers.length > 0 }">
    <div class="transfer-queue">
      <div v-if="store.transfers.length === 0" class="transfer-idle">
        <span>{{ statusText }}</span>
      </div>
      <div v-else class="transfer-list">
        <div
          v-for="t in visibleTransfers"
          :key="t.id"
          class="transfer-item"
          :class="t.status"
        >
          <div class="transfer-icon" :class="t.direction">
            <svg v-if="t.direction === 'up'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
            <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="7 16 12 21 17 16"/><line x1="12" y1="3" x2="12" y2="21"/></svg>
          </div>

          <div class="transfer-info" :title="`${t.from} → ${t.to}`">
            <div class="transfer-name">
              <span class="name-text">{{ t.name }}</span>
              <span class="name-direction">{{ t.direction === 'up' ? '上传' : '下载' }}</span>
            </div>
            <div class="transfer-meta">
              <span v-if="t.status === 'running'" class="meta-speed">{{ formatSpeed(t) }}</span>
              <span v-else-if="t.status === 'done'" class="meta-done">完成 · 耗时 {{ formatDuration(t) }}</span>
              <span v-else-if="t.status === 'error'" class="meta-error" :title="t.error">失败: {{ t.error }}</span>
              <span v-else-if="t.status === 'cancelled'" class="meta-cancelled">已取消</span>
              <span v-else class="meta-pending">等待中</span>
            </div>
          </div>

          <div class="transfer-progress">
            <div class="progress-bar">
              <div
                v-if="isIndeterminate(t)"
                class="progress-fill indeterminate"
              ></div>
              <div
                v-else
                class="progress-fill"
                :class="t.status"
                :style="{ width: progressPercent(t) + '%' }"
              ></div>
            </div>
          </div>

          <div class="transfer-percent">
            <span v-if="isIndeterminate(t)" class="indeterminate-text">复制中</span>
            <span v-else-if="t.status === 'running' || t.status === 'done'">{{ progressPercent(t) }}%</span>
            <span v-else-if="t.status === 'error'" class="err-text">失败</span>
            <span v-else-if="t.status === 'cancelled'">已取消</span>
            <span v-else>—</span>
          </div>

          <button
            v-if="t.status === 'running' || t.status === 'pending'"
            class="transfer-action cancel"
            title="取消"
            @click="store.cancelTransfer(t.id)"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
          <button
            v-else
            class="transfer-action close"
            title="关闭"
            @click="store.removeTransfer(t.id)"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/></svg>
          </button>
        </div>
      </div>
    </div>

    <div v-if="store.transfers.length > 0" class="transfer-summary">
      <span><b>{{ store.activeTransfers.length }}</b> 项进行中</span>
      <span v-if="store.transfers.length > visibleCount" class="more-text">还有 {{ store.transfers.length - visibleCount }} 项</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useSftpStore, type TransferItem } from '@/stores/sftp'

const store = useSftpStore()
const visibleCount = 3

const visibleTransfers = computed(() =>
  store.transfers.slice(-visibleCount)
)

const statusText = computed(() => {
  if (store.online) {
    return `本地: ${store.localCwd} · 远程: ${store.remoteCwd}`
  }
  return store.localCwd ? `本地: ${store.localCwd}` : '就绪'
})

function progressPercent(t: TransferItem): number {
  if (!t.total) return 0
  return Math.min(100, Math.round((t.transferred / t.total) * 100))
}

/** 远程 cp -r 等无法获取进度的操作：running 且 total=0 */
function isIndeterminate(t: TransferItem): boolean {
  return t.status === 'running' && !t.total
}

function formatSpeed(t: TransferItem): string {
  if (!t.startTime) return ''
  const elapsed = (Date.now() - t.startTime) / 1000
  if (elapsed <= 0) return ''
  const speed = t.transferred / elapsed
  const speedText = speed < 1024
    ? `${Math.round(speed)} B/s`
    : speed < 1024 * 1024
      ? `${(speed / 1024).toFixed(1)} KB/s`
      : `${(speed / 1024 / 1024).toFixed(1)} MB/s`

  const remaining = t.total > t.transferred && speed > 0
    ? Math.ceil((t.total - t.transferred) / speed)
    : 0
  if (remaining > 0) {
    return `${speedText} · 剩余 ${remaining}s`
  }
  return speedText
}

function formatDuration(t: TransferItem): string {
  if (!t.startTime || !t.endTime) return '—'
  const ms = t.endTime - t.startTime
  if (ms < 1000) return `${ms}ms`
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const rs = s % 60
  return `${m}m ${rs}s`
}
</script>

<style scoped>
.transfer-bar {
  height: 32px;
  display: flex;
  align-items: center;
  padding: 0 12px;
  background: var(--bg-elevated);
  border-top: 1px solid var(--border-subtle);
  font-size: 11px;
  color: var(--text-tertiary);
  gap: 12px;
  flex-shrink: 0;
  overflow: hidden;
}
.transfer-bar.active {
  height: auto;
  min-height: 64px;
  padding: 8px 12px;
  align-items: stretch;
  flex-direction: column;
  gap: 6px;
}

.transfer-idle {
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.transfer-list { display: flex; flex-direction: column; gap: 6px; }

.transfer-item {
  display: grid;
  grid-template-columns: 16px 1fr 100px 50px 18px;
  gap: 8px;
  align-items: center;
  font-size: 11px;
  min-width: 0;
  padding: 2px 0;
}

.transfer-icon { display: flex; justify-content: center; }
.transfer-icon.up { color: var(--accent-green); }
.transfer-icon.down { color: var(--accent-cyan); }
.transfer-icon svg { width: 12px; height: 12px; }

.transfer-info {
  overflow: hidden;
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.transfer-name {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.name-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
  font-weight: 500;
}
.name-direction {
  flex-shrink: 0;
  font-size: 10px;
  color: var(--text-tertiary);
  background: var(--bg-hover);
  padding: 0 5px;
  border-radius: 3px;
}

.transfer-meta {
  font-size: 10px;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.meta-speed { color: var(--accent-blue); }
.meta-done { color: var(--accent-green); }
.meta-error { color: var(--accent-red); }
.meta-cancelled { color: var(--text-tertiary); }
.meta-pending { color: var(--text-tertiary); }

.transfer-progress { width: 100px; }
.progress-bar {
  width: 100%; height: 4px;
  background: var(--bg-hover);
  border-radius: 2px;
  overflow: hidden;
}
.progress-fill {
  height: 100%;
  background: var(--accent-blue);
  border-radius: 2px;
  transition: width 0.15s;
}
.progress-fill.done { background: var(--accent-green); }
.progress-fill.error { background: var(--accent-red); }
.progress-fill.cancelled { background: var(--text-tertiary); }

/* 不定进度条（远程 cp -r 等无法获取进度的操作） */
.progress-fill.indeterminate {
  width: 40%;
  background: var(--accent-blue);
  animation: indeterminate-slide 1.2s ease-in-out infinite;
}
@keyframes indeterminate-slide {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(250%); }
}
.indeterminate-text { color: var(--text-secondary); }

.transfer-percent {
  font-family: var(--font-mono);
  color: var(--text-secondary);
  text-align: right;
  font-size: 10px;
}
.err-text { color: var(--accent-red); }

.transfer-action {
  width: 16px; height: 16px;
  display: flex; align-items: center; justify-content: center;
  background: transparent; border: none;
  color: var(--text-tertiary);
  cursor: pointer; border-radius: 3px;
  padding: 0;
}
.transfer-action:hover { background: var(--bg-hover); }
.transfer-action.cancel:hover { color: var(--accent-red); }
.transfer-action.close:hover { color: var(--text-secondary); }
.transfer-action svg { width: 10px; height: 10px; }

.transfer-summary {
  display: flex; align-items: center; gap: 12px;
  color: var(--text-tertiary);
  font-size: 11px;
  flex-shrink: 0;
}
.transfer-summary b { color: var(--text-secondary); font-weight: 600; }
.more-text { color: var(--text-tertiary); }
</style>
