<template>
  <!-- K8s 舵轮：与 IconDocker 同一口径 —— stroke=currentColor，尺寸由 props 决定，
       随主题变色，不写死颜色字面量 -->
  <svg
    :width="width" :height="height" viewBox="0 0 24 24" fill="none"
    :stroke-width="strokeWidth" stroke="currentColor" role="img" aria-label="Kubernetes"
  >
    <circle cx="12" cy="12" :r="outerR" />
    <circle cx="12" cy="12" :r="2.6" />
    <path d="M12 4.2v5.2M12 14.6v5.2" />
    <path d="M5.3 8.1l4.5 2.6M14.2 13.3l4.5 2.6" />
    <path d="M5.3 15.9l4.5-2.6M14.2 10.7l4.5-2.6" />
  </svg>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{ width?: number; height?: number; strokeWidth?: number }>(), {
  width: 18,
  height: 18,
  strokeWidth: 1.8,
})
// 描边越粗，外圈要往回收一点，否则 7 根辐条会顶穿圆环
const outerR = computed(() => (props.strokeWidth >= 2 ? 8.4 : 8.8))
</script>
