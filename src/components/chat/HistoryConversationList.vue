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
          :title="busy ? '运行中不可切换会话' : undefined"
          @click="$emit('switch', conv.id)"
        >
          <span class="history-item-dot" :class="{ active: conv.id === currentConvId }"></span>
          <span class="history-item-title">{{ conv.title }}</span>
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
    <div v-if="isEmpty" class="history-empty">还没有会话</div>
  </div>
</template>

<script setup lang="ts">
import type { ConvGroup } from '@/types/chat'

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
</script>

<style scoped>
.history-list {
  padding: 8px 0;
}

.history-group {
  margin-bottom: 4px;
}

.history-group-label {
  padding: 6px 12px 4px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.3px;
}

.history-item {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 0 12px;
}

/* 对话主体：真实按钮，可键盘聚焦 */
.history-item-main {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 4px;
  border: none;
  background: transparent;
  color: inherit;
  font-family: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
  border-radius: 4px;
  transition: background 0.1s;
}

.history-item-main:hover:not(:disabled) {
  background: var(--bg-hover);
}

.history-item-main.active {
  background: var(--bg-active);
}

.history-item-main:disabled {
  cursor: default;
  opacity: 0.5;
}

.history-item-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-tertiary);
  flex-shrink: 0;
}

.history-item-dot.active {
  background: var(--accent-indigo);
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

.history-item-delete {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border: none;
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
  border-radius: 3px;
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
  padding: 24px 12px;
  text-align: center;
  font-size: 12px;
  color: var(--text-tertiary);
}
</style>
