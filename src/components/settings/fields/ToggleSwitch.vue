<template>
  <button
    class="toggle-switch"
    :class="{ on: modelValue }"
    role="switch"
    :aria-checked="modelValue ? 'true' : 'false'"
    @click="$emit('update:modelValue', !modelValue)"
  >
    <span class="toggle-thumb" />
  </button>
</template>

<script setup lang="ts">
defineProps<{
  modelValue: boolean
}>()

defineEmits<{
  'update:modelValue': [value: boolean]
}>()
</script>

<style scoped>
.toggle-switch {
  width: 36px;
  height: 20px;
  border-radius: 10px;
  background: #3d3d3d;
  border: 1px solid #4d4d4d;
  cursor: pointer;
  position: relative;
  transition: background 0.15s ease, border-color 0.15s ease;
  padding: 0;
  outline: none;
}
.toggle-switch.on {
  background: var(--accent-primary);
  border-color: var(--accent-primary);
}
.toggle-thumb {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: #ffffff;
  transition: transform 0.15s ease;
  box-shadow: 0 1px 2px rgba(0,0,0,0.3);
}
.toggle-switch.on .toggle-thumb {
  transform: translateX(16px);
}
.toggle-switch:hover {
  border-color: #5d5d5d;
}
.toggle-switch.on:hover {
  border-color: var(--accent-primary);
  filter: brightness(1.1);
}
</style>
