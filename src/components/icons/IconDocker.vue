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
    <!-- 🐋 大鲸鱼：胖圆身、双叉尾、挖空眼（evenodd 打孔），头朝左实心填充 -->
    <path
      fill-rule="evenodd"
      clip-rule="evenodd"
      fill="currentColor"
      stroke="none"
      d="M2.4 13.4C2.4 9.9 5.5 7.4 9.4 7.4C13.1 7.4 15.9 9 17.2 11.5C18 10.1 19.3 8.6 21 7.7C21.8 7.3 22.7 7.1 23.4 7.2C23 8.3 22.2 9.3 21.2 10C22.1 10.7 22.9 11.6 23.4 12.7C22.4 13.2 21.2 13.9 19.9 14.4C17.8 16.9 14.3 18.3 10.2 18.3C6.3 18.3 3.4 16.5 2.4 13.4Z M6.7 10.85a1.05 1.05 0 1 0 0 2.1a1.05 1.05 0 1 0 0 -2.1Z"
    />
    <!-- 喷气孔水花：三撮紧贴头顶的矮水柱 -->
    <path fill="none" d="M8.6 6.4C8.6 5.8 8.6 5.3 8.6 4.8" />
    <path fill="none" d="M7.5 6.2C7.0 5.8 6.6 5.4 6.4 4.9" />
    <path fill="none" d="M9.7 6.2C10.2 5.8 10.6 5.4 10.8 4.9" />
    <!-- 微笑嘴线 -->
    <path fill="none" d="M2.8 15.0C3.8 16.0 5.1 16.5 6.6 16.4" />
  </svg>
</template>
