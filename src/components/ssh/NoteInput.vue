<template>
  <div class="note-input" :class="{ focused }">
    <input
      ref="inputRef"
      :value="modelValue"
      class="note-field"
      :placeholder="placeholder"
      :maxlength="maxLength"
      @input="handleInput"
      @compositionstart="isComposing = true"
      @compositionend="handleCompositionEnd"
      @focus="focused = true"
      @blur="focused = false"
    />
    <span v-if="showCounter" class="note-counter" :class="{ warn: count >= maxLength }">
      {{ count }}/{{ maxLength }}
    </span>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'

const props = withDefaults(defineProps<{
  modelValue: string
  placeholder?: string
  maxLength?: number
  showCounter?: boolean
}>(), {
  placeholder: '请输入备注',
  maxLength: 10,
  showCounter: true,
})

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

const inputRef = ref<HTMLInputElement | null>(null)
const focused = ref(false)
// C38 修复：IME 组合态标志 — 组合期间强制截断会破坏中文转换
const isComposing = ref(false)

const count = computed(() => (props.modelValue || '').length)

function handleInput(e: Event) {
  const target = e.target as HTMLInputElement
  // C38 修复：IME 组合态期间跳过截断（拼音中间态被切掉会导致无法上屏），
  // 组合结束（compositionend）时再统一强制截断
  if (isComposing.value) {
    emit('update:modelValue', target.value)
    return
  }
  // 在最外层强制限制长度
  const v = target.value.slice(0, props.maxLength)
  emit('update:modelValue', v)
  if (target.value !== v) target.value = v
}

/** IME 组合结束：退出组合态并对上屏结果统一截断 */
function handleCompositionEnd(e: Event) {
  isComposing.value = false
  handleInput(e)
}

function focus() {
  inputRef.value?.focus()
}

defineExpose({ focus })
</script>

<style scoped>
.note-input {
  position: relative;
  display: flex;
  align-items: center;
  background: var(--bg-input);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  transition: border-color var(--transition-fast);
  height: 32px;
  padding: 0 var(--spacing-8);
}

.note-input.focused {
  border-color: var(--accent-primary);
}

.note-field {
  flex: 1;
  height: 28px;
  border: none;
  background: transparent;
  color: var(--text-primary);
  font-size: 0.9231rem;
  font-family: var(--font-sans);
  outline: none;
  padding: 0;
  min-width: 0;
}

.note-field::placeholder {
  color: var(--text-tertiary);
}

.note-counter {
  flex-shrink: 0;
  font-size: 0.7692rem;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  margin-left: var(--spacing-8);
  user-select: none;
}

.note-counter.warn {
  color: var(--accent-orange);
}
</style>
