<template>
  <button
    class="toggle-switch"
    :class="{ on: modelValue, disabled }"
    role="switch"
    :aria-checked="modelValue ? 'true' : 'false'"
    :disabled="disabled"
    @click="$emit('update:modelValue', !modelValue)"
  >
    <span class="toggle-thumb" />
  </button>
</template>

<script setup lang="ts">
defineProps<{
  modelValue: boolean
  disabled?: boolean
}>()

defineEmits<{
  'update:modelValue': [value: boolean]
}>()
</script>

<style scoped>
.toggle-switch {
  width: 36px;
  height: 20px;
  border-radius: var(--radius-xl);
  /* 关闭态的轨道 = --bg-slider（variables.css 里那条"滑块轨道"就是为这种控件底色设的），
     描边 = --border-default：#3d3d3d/#4d4d4d 是深色主题的一档灰，在白卡与米纸卡上
     都是一块不属于任何主题的深色补丁 */
  background: var(--bg-slider);
  border: 1px solid var(--border-default);
  cursor: pointer;
  position: relative;
  transition: background 0.15s ease, border-color 0.15s ease;
  padding: 0;
}
.toggle-switch.on {
  background: var(--accent-primary);
  border-color: var(--accent-primary);
}
.toggle-thumb {
  /* 旋钮恒为白：浅色轨道上要看得见，深色强调色轨道上也要看得见，两头的解都是白盘。
     写成组件局部色而不是 --accent-on —— 后者是按用户强调色算的，选"白色"时它会翻成深字 */
  --toggle-knob: #ffffff;
  position: absolute;
  top: 2px;
  left: 2px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--toggle-knob);
  transition: transform 0.15s ease;
  box-shadow: 0 1px 2px rgba(0,0,0,0.3);
}
.toggle-switch.on .toggle-thumb {
  transform: translateX(16px);
}
.toggle-switch:hover {
  border-color: var(--text-tertiary);
}
.toggle-switch.on:hover {
  border-color: var(--accent-primary);
  filter: brightness(1.1);
}
.toggle-switch.disabled {
  opacity: 0.45;
  cursor: default;
}
</style>
