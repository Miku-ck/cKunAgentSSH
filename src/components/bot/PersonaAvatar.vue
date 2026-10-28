<script setup lang="ts">
import { computed } from 'vue'
import { isImageAvatar } from '@/utils/persona-avatar'

// 头像只有上传图片一种来源：非 data URL（旧数据里的表情/文本）不渲染，
// 展示位由父容器的占位圈兜底。
const props = defineProps<{ avatar?: string | null }>()
const image = computed(() => {
  const avatar = props.avatar
  return avatar && isImageAvatar(avatar) ? avatar : ''
})
</script>

<template>
  <img v-if="image" class="avatar-img" :src="image" alt="" />
</template>

<style scoped>
.avatar-img { width: 100%; height: 100%; display: block; border-radius: inherit; object-fit: cover; }
</style>
