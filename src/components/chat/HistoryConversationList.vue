<template>
  <div class="history-list">
    <div v-for="group in groups" :key="group.label" class="history-group">
      <div class="history-group-label">{{ group.label }}</div>
      <div
        v-for="conv in group.items"
        :key="conv.id"
        class="history-item"
      >
        <button
          class="history-item-main"
          :class="{ active: conv.id === currentConvId }"
          :disabled="busy"
          :aria-label="'切换到对话：' + conv.title"
          :aria-current="conv.id === currentConvId ? 'true' : undefined"
          :title="busy ? '运行中不可切换会话' : undefined"
          @click="$emit('switch', conv.id)"
        >
          <span class="history-item-title">{{ conv.title }}</span>
          <span class="history-item-time">{{ timeOf(conv.updated_at) }}</span>
        </button>
        <button
          class="history-item-delete"
          :disabled="busy"
          :title="busy ? '运行中不可删除会话' : '删除'"
          :aria-label="'删除对话：' + conv.title"
          @click="$emit('delete', conv.id)"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
        </button>
      </div>
    </div>
    <div v-if="isEmpty" class="history-empty">
      <svg class="history-empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><polyline points="3 3 3 8 8 8"/><polyline points="12 7 12 12 15 14"/>
      </svg>
      <div class="history-empty-title">还没有会话</div>
      <div class="history-empty-hint">发出第一条消息后，这里会留下记录</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { ConvGroup } from '@/types/chat'
import { parseBackendTs } from '@/utils/format'

withDefaults(defineProps<{
  groups: ConvGroup[]
  currentConvId: string | null
  isEmpty: boolean
  busy?: boolean
}>(), { busy: false })

defineEmits<{
  switch: [id: string]
  delete: [id: string]
}>()

/** 行尾时分：同名会话（多条「你好」）靠它区分先后。
 *  时间戳必须走 parseBackendTs —— 后端条目是 UTC-naive 串，裸 new Date() 会早 8 小时。 */
const timeFmt = new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
function timeOf(ts: string): string {
  const d = parseBackendTs(ts)
  return Number.isNaN(d.getTime()) ? '' : timeFmt.format(d)
}
</script>

<style scoped>
.history-list {
  padding: 2px 0 8px;
}

.history-group {
  margin-bottom: 8px;
}

/* 分组标签吸顶：列表变长后，「今天/昨天」要一直说明眼前这段属于哪天 */
.history-group-label {
  position: sticky;
  top: 0;
  z-index: 1;
  padding: 8px 10px 5px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.4px;
  background: var(--bg-elevated);
}

.history-item {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 0;
}

/* 对话主体：真实按钮，可键盘聚焦。
   选中态靠左缘一道 accent 竖条 + 淡底，不再用灰色小圆点 —— 那枚点
   在列表里会被读成原生 <ul> 的项目符号，像没做完样式。 */
.history-item-main {
  position: relative;
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  padding: 7px 10px;
  border: none;
  background: transparent;
  color: inherit;
  font-family: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
  border-radius: var(--radius-sm);
  transition: background 0.1s;
}

.history-item-main:hover:not(:disabled) {
  background: var(--bg-hover);
}

.history-item-main.active {
  background: var(--accent-indigo-bg);
}

.history-item-main.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  width: 2px;
  height: 14px;
  border-radius: var(--radius-xs);
  background: var(--accent-indigo);
  transform: translateY(-50%);
}

.history-item-main:disabled {
  cursor: default;
  opacity: 0.5;
}

.history-item-title {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.history-item-time {
  flex-shrink: 0;
  margin-left: 8px;
  font-size: 11px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}

.history-item-delete {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border: none;
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
  border-radius: var(--radius-sm);
  flex-shrink: 0;
  opacity: 0;
  transition: all 0.1s;
}

.history-item:hover .history-item-delete,
.history-item:focus-within .history-item-delete {
  opacity: 1;
}

.history-item-delete:disabled {
  opacity: 0.35;
  cursor: default;
}

.history-item-delete:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent-red) 15%, transparent);
  color: var(--accent-red);
}

.history-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 48px 16px;
  text-align: center;
}

.history-empty-icon {
  width: 22px;
  height: 22px;
  margin-bottom: 2px;
  color: var(--text-tertiary);
  opacity: 0.7;
}

.history-empty-title {
  font-size: 12px;
  color: var(--text-secondary);
}

.history-empty-hint {
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-tertiary);
}
</style>
