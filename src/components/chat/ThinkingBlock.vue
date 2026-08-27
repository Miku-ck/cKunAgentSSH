<template>
  <div class="reasoning-block" :class="{ streaming }">
    <button class="reasoning-toggle" @click="open = !open">
      <span class="reasoning-label">{{ streaming ? '正在思考' : '深度思考' }}</span>
      <svg
        class="reasoning-arrow"
        :class="{ open }"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        width="14"
        height="14"
      >
        <polyline points="9 18 15 12 9 6"/>
      </svg>
    </button>
    <div v-show="open" class="reasoning-content">
      <MarkdownStream v-if="!streaming && markdownEnabled" :content="reasoning" />
      <pre v-else class="reasoning-raw">{{ reasoning }}</pre>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import MarkdownStream from './MarkdownStream.vue'

const props = defineProps<{
  reasoning: string
  streaming?: boolean
  defaultOpen?: boolean
}>()

const settingsStore = useSettingsStore()
const markdownEnabled = computed(() => settingsStore.settings.chat.markdown)
// 优先级：显式 defaultOpen > 用户设置 thinkingExpanded > false
// 流式中调用方一般会传 defaultOpen=true，非流式时回退到用户设置
const initialOpen = computed(() => props.defaultOpen ?? settingsStore.settings.chat.thinkingExpanded)
const open = ref(initialOpen.value)
</script>

<style scoped>
.reasoning-block {
  margin-bottom: 4px;
}

.reasoning-toggle {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 0;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
  text-align: left;
}

.reasoning-toggle:hover {
  color: var(--text-primary);
}

.reasoning-arrow {
  transition: transform 0.15s;
  flex-shrink: 0;
  opacity: 0.5;
}

.reasoning-arrow.open {
  transform: rotate(90deg);
}

.reasoning-label {
  font-weight: 400;
}

.reasoning-content {
  padding: 8px 0 8px 18px;
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.6;
}

.reasoning-raw {
  margin: 0;
  padding: 0;
  font-family: var(--font-sans);
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
  background: transparent;
  border: none;
}

/* Markdown 渲染内容样式适配 */
.reasoning-content :deep(.markdown-body) {
  font-size: 12px;
  color: var(--text-secondary);
}

.reasoning-content :deep(.markdown-body p) {
  margin: 0.4em 0;
}

.reasoning-content :deep(.markdown-body pre) {
  margin: 0.5em 0;
  padding: 8px 10px;
}

.reasoning-content :deep(.markdown-body code) {
  font-size: 11px;
}
</style>
