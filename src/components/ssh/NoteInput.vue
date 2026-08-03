<template>
  <div class="note-input" :class="{ focused }">
    <input
      ref="inputRef"
      :value="modelValue"
      class="note-field"
      :placeholder="placeholder"
      :maxlength="maxLength"
      @input="handleInput"
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

const count = computed(() => (props.modelValue || '').length)

function handleInput(e: Event) {
  const target = e.target as HTMLInputElement
  // 在最外层强制限制长度,避免中文输入法的 IME 组合态超出限制
  const v = target.value.slice(0, props.maxLength)
  emit('update:modelValue', v)
  // 如果 IME 过程中超长,同步到 input.value
  if (target.value !== v) target.value = v
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
  background: var(--bg-base);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  transition: border-color var(--transition-fast);
  height: 32px;
  padding: 0 8px;
}

.note-input.focused {
  border-color: var(--accent-blue);
}

.note-field {
  flex: 1;
  height: 28px;
  border: none;
  background: transparent;
  color: var(--text-primary);
  font-size: 12px;
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
  font-size: 10px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  margin-left: 8px;
  user-select: none;
}

.note-counter.warn {
  color: var(--accent-orange, #f59e0b);
}
</style>
