<template>
  <div class="history-panel-overlay">
    <div class="history-panel">
      <div class="history-panel-header">
        <span class="history-panel-title">历史任务</span>
        <button class="icon-btn" @click="$emit('close')" title="关闭">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="history-panel-list">
        <div v-for="group in groups" :key="group.label" class="history-group">
          <div class="history-group-label">{{ group.label }}</div>
          <div
            v-for="conv in group.items"
            :key="conv.id"
            class="history-panel-item"
            :class="{ active: conv.id === currentConvId }"
            @click="$emit('switch', conv.id)"
          >
            <span class="history-panel-item-dot" :class="{ active: conv.id === currentConvId }"></span>
            <span class="history-panel-item-title">{{ conv.title }}</span>
            <button class="history-panel-item-delete" @click.stop="$emit('delete', conv.id)" title="删除">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
            </button>
          </div>
        </div>
        <div v-if="isEmpty" class="history-panel-empty">
          暂无历史对话
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
interface ConvGroup {
  label: string
  items: Array<{ id: string; title: string; updated_at: string }>
}

defineProps<{
  groups: ConvGroup[]
  currentConvId: string | null
  isEmpty: boolean
}>()

defineEmits<{
  close: []
  switch: [id: string]
  delete: [id: string]
}>()
</script>

<style scoped>
.history-panel-overlay {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.5);
  z-index: 20;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding-top: 40px;
  backdrop-filter: blur(2px);
}

.history-panel {
  width: 100%;
  max-height: calc(100% - 48px);
  border: 1px solid var(--border-default);
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  margin: 0 8px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
  background: color-mix(in srgb, var(--bg-elevated) 90%, transparent);
  backdrop-filter: blur(12px);
}

.history-panel-header {
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 12px;
  border-bottom: 1px solid var(--border-subtle, #2d2d30);
  flex-shrink: 0;
}

.history-panel-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.icon-btn {
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  border-radius: 4px;
  transition: all 0.15s;
}

.icon-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.icon-btn svg {
  width: 14px;
  height: 14px;
}

.history-panel-list {
  flex: 1;
  overflow-y: auto;
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

.history-panel-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  cursor: pointer;
  transition: background 0.1s;
}

.history-panel-item:hover {
  background: var(--bg-hover);
}

.history-panel-item.active {
  background: var(--bg-active);
}

.history-panel-item-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-tertiary);
  flex-shrink: 0;
}

.history-panel-item-dot.active {
  background: #818CF8;
}

.history-panel-item-title {
  flex: 1;
  font-size: 12px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.history-panel-item.active .history-panel-item-title {
  color: var(--text-primary);
}

.history-panel-item-delete {
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

.history-panel-item:hover .history-panel-item-delete {
  opacity: 1;
}

.history-panel-item-delete:hover {
  background: rgba(244, 113, 113, 0.15);
  color: #f47171;
}

.history-panel-empty {
  padding: 24px 12px;
  text-align: center;
  font-size: 12px;
  color: var(--text-tertiary);
}
</style>
