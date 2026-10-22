<script setup lang="ts">
withDefaults(
  defineProps<{
    width?: number | string
    height?: number | string
    strokeWidth?: number | string
  }>(),
  {
    width: 20,
    height: 20,
    strokeWidth: 1.8,
  }
)
</script>

<template>
  <svg
    :width="width"
    :height="height"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    :stroke-width="strokeWidth"
    stroke-linecap="round"
    stroke-linejoin="round"
  >
    <!-- 鲸鱼剪影（DeepSeek 风）：头朝左、背线滑向右、尾鳍上扬分叉，实心填充 -->
    <path
      fill="currentColor"
      stroke="none"
      d="M2.6 12.9C2.6 9.4 5.5 7 9.1 7C12.2 7 14.5 8.3 15.8 10.6C16.3 8.7 17.6 6.7 19.5 5.5C19.2 6.8 19 7.8 19.1 8.9C20.2 7.9 21.3 7.6 22.3 7.8C21.4 9.5 20.1 10.9 18.6 12.3C17.2 15.2 14 17.3 10.3 17.3C7.2 17.3 4.4 15.9 3 13.7C2.7 13.4 2.6 13.2 2.6 12.9Z"
    />
  </svg>
</template>
