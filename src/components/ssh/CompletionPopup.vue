<template>
  <div
    v-if="visible"
    ref="rootRef"
    class="completion-popup"
    :style="{ left: x + 'px', top: y + 'px', maxHeight: maxItemHeight * maxVisible + 8 + 'px' }"
    @click.stop
  >
    <div
      v-for="(cand, idx) in candidates"
      :key="`${cand.type}:${cand.text}:${idx}`"
      class="completion-item"
      :class="{ active: idx === selectedIndex, dir: cand.type === 'dir' }"
      @mouseenter="selectedIndex = idx"
      @click="emit('select', cand)"
    >
      <svg
        v-if="cand.type === 'dir'"
        class="item-icon"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        width="13"
        height="13"
      >
        <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
      </svg>
      <svg
        v-else-if="cand.type === 'command'"
        class="item-icon"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        width="13"
        height="13"
      >
        <polyline points="4 17 10 11 4 5"/>
        <line x1="12" y1="19" x2="20" y2="19"/>
      </svg>
      <svg
        v-else-if="cand.type === 'history'"
        class="item-icon"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        width="13"
        height="13"
      >
        <circle cx="12" cy="12" r="10"/>
        <polyline points="12 6 12 12 16 14"/>
      </svg>
      <svg
        v-else
        class="item-icon"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        width="13"
        height="13"
      >
        <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/>
        <polyline points="13 2 13 9 20 9"/>
      </svg>
      <span class="item-text">{{ cand.text }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, nextTick } from 'vue'
import type { CompletionCandidate } from '@/composables/useCompletion'

const props = defineProps<{
  visible: boolean
  candidates: CompletionCandidate[]
  x: number
  y: number
}>()

const emit = defineEmits<{
  select: [candidate: CompletionCandidate]
  cancel: []
}>()

const selectedIndex = ref(0)
const maxVisible = 8
const maxItemHeight = 22
// C39 修复：popup 根元素 ref — scrollIntoView 查询限定在组件内部
const rootRef = ref<HTMLElement | null>(null)

// 候选变化时重置选中项
watch(
  () => props.candidates,
  () => {
    selectedIndex.value = 0
  },
)

// 键盘导航
function moveUp(): void {
  if (props.candidates.length === 0) return
  selectedIndex.value = (selectedIndex.value - 1 + props.candidates.length) % props.candidates.length
  scrollActiveIntoView()
}

function moveDown(): void {
  if (props.candidates.length === 0) return
  selectedIndex.value = (selectedIndex.value + 1) % props.candidates.length
  scrollActiveIntoView()
}

function scrollActiveIntoView(): void {
  nextTick(() => {
    // C39 修复：原 document 全局 querySelector 会命中其他终端的 popup 同名元素
    // （多个 CompletionPopup 共存）— 限定在本组件根元素内查询
    const activeEl = rootRef.value?.querySelector('.completion-item.active') as HTMLElement | null
    if (activeEl) {
      activeEl.scrollIntoView({ block: 'nearest' })
    }
  })
}

function selectCurrent(): CompletionCandidate | null {
  if (props.candidates.length === 0) return null
  return props.candidates[selectedIndex.value] || null
}

defineExpose({
  moveUp,
  moveDown,
  selectCurrent,
})
</script>

<style scoped>
.completion-popup {
  position: fixed;
  z-index: 2000;
  min-width: 180px;
  max-width: 420px;
  background: var(--bg-input);
  border: 1px solid var(--border-default, #3e3e42);
  border-radius: 4px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
  overflow-y: auto;
  overflow-x: hidden;
  padding: 4px 0;
}

.completion-popup::-webkit-scrollbar {
  width: 6px;
}

.completion-popup::-webkit-scrollbar-thumb {
  background: #3e3e42;
  border-radius: 3px;
}

.completion-item {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 3px 10px;
  font-size: 12px;
  font-family: var(--font-mono, 'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'Consolas', monospace);
  color: var(--text-primary);
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  line-height: 1.5;
}

.completion-item.active {
  background: var(--accent-primary);
  color: #ffffff;
}

.item-icon {
  width: 13px;
  height: 13px;
  flex-shrink: 0;
  color: #79c0ff;
}

.completion-item.active .item-icon {
  color: #ffffff;
}

.item-text {
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
