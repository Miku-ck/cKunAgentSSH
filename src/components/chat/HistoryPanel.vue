<template>
  <div class="history-panel-overlay">
    <div class="history-panel">
      <div class="history-panel-header">
        <span class="history-panel-title">历史任务</span>
        <button class="icon-btn" @click="$emit('close')" title="关闭" aria-label="关闭">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="history-panel-list">
        <HistoryConversationList
          :groups="groups"
          :current-conv-id="currentConvId"
          :is-empty="isEmpty"
          :busy="busy"
          @switch="$emit('switch', $event)"
          @delete="$emit('delete', $event)"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { ConvGroup } from '@/types/chat'
import HistoryConversationList from './HistoryConversationList.vue'

withDefaults(defineProps<{
  groups: ConvGroup[]
  currentConvId: string | null
  isEmpty: boolean
  busy?: boolean
}>(), { busy: false })

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
  /* 不透明背景：历史面板打开时遮住当前对话（配合 SideChatPanel 的 v-show
     隐藏消息列表），消除半透明遮罩透出的视觉干扰 */
  background: var(--bg-base);
  z-index: 20;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding-top: 40px;
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
  box-shadow: var(--shadow-lg);
  background: var(--bg-elevated-solid);
}

.history-panel-header {
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 12px;
  border-bottom: 1px solid var(--border-subtle);
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
}
</style>
