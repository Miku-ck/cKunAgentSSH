<template>
  <div class="slider-row">
    <input
      class="setting-range"
      type="range"
      :value="modelValue"
      :min="min"
      :max="max"
      :step="step"
      :disabled="disabled"
      @input="onInput"
    />
    <span class="slider-value">{{ display }}</span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  modelValue: number
  min?: number
  max?: number
  step?: number
  /** 数值后缀：'%'（默认）/ 'px' / ' 条' */
  unit?: string
  disabled?: boolean
}>(), {
  min: 0,
  max: 100,
  step: 1,
  unit: '%',
  disabled: false,
})

const emit = defineEmits<{
  'update:modelValue': [value: number]
}>()

const display = computed(() => `${props.modelValue}${props.unit}`)

function onInput(e: Event) {
  const v = Number((e.target as HTMLInputElement).value)
  // range 的 value 正常总是数字串；非有限值只可能来自脏绑定，宁可不写也不把 NaN 落进设置
  if (Number.isFinite(v)) emit('update:modelValue', v)
}
</script>

<style scoped>
.slider-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-12);
}
.setting-range {
  width: 120px;
  height: 4px;
  -webkit-appearance: none;
  appearance: none;
  background: var(--bg-slider);
  border-radius: var(--radius-xs);
  cursor: pointer;
}
.setting-range::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--accent-primary);
  border: 2px solid var(--bg-input);
  cursor: pointer;
  box-shadow: 0 1px 3px rgba(0,0,0,0.4);
}
.setting-range:disabled {
  opacity: 0.45;
  cursor: default;
}
.setting-range:disabled::-webkit-slider-thumb {
  cursor: default;
}
.slider-value {
  font-size: 0.9231rem;
  color: var(--text-secondary);
  min-width: 40px;
  text-align: right;
}
</style>
