<template>
  <div class="reasoning-block" :class="{ streaming }">
    <button class="reasoning-toggle" @click="open = !open">
      <svg
        class="caret"
        :class="{ open }"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        width="12"
        height="12"
      >
        <polyline points="9 18 15 12 9 6"/>
      </svg>
      <span class="reasoning-label">{{ streaming ? '思考过程' : '已思考' }}</span>
    </button>
    <div v-show="open" class="reasoning-content">{{ reasoning }}</div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useSettingsStore } from '@/stores/settings'

const props = defineProps<{
  reasoning: string
  streaming?: boolean
  defaultOpen?: boolean
}>()

const settingsStore = useSettingsStore()
// 优先级：显式 defaultOpen > 用户设置 thinkingExpanded > false
// 流式中调用方一般会传 defaultOpen=true，非流式时回退到用户设置
const initialOpen = computed(() => props.defaultOpen ?? settingsStore.settings.chat.thinkingExpanded)
const open = ref(initialOpen.value)
</script>

<style scoped>
.reasoning-block {
  margin-bottom: 8px;
  border-radius: 6px;
  background: rgba(129, 140, 248, 0.08);
  border: 1px solid rgba(129, 140, 248, 0.2);
  overflow: hidden;
}

.reasoning-block.streaming {
  background: rgba(129, 140, 248, 0.12);
  border-color: rgba(129, 140, 248, 0.35);
}

.reasoning-toggle {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border: none;
  background: transparent;
  color: var(--accent-purple);
  font-size: 12px;
  cursor: pointer;
  text-align: left;
}

.reasoning-toggle:hover {
  background: rgba(129, 140, 248, 0.1);
}

.caret {
  transition: transform 0.15s;
  flex-shrink: 0;
}

.caret.open {
  transform: rotate(90deg);
}

.reasoning-label {
  font-weight: 500;
}

.reasoning-content {
  padding: 8px 12px 10px;
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  border-top: 1px solid rgba(129, 140, 248, 0.15);
}
</style>
