<script setup lang="ts">
import { computed } from 'vue'
import { isImageAvatar } from '@/utils/persona-avatar'
import { avatarPlaceholderStyle, placeholderInitial } from '@/utils/bot-chat-decor'

// 头像两态：上传图 > 淡色首字占位（同 seed 恒同色，每个人不一样 —— spec §4.1 去马赛克）。
// 非 data URL（旧数据里的表情/文本）不当图片，走占位而不是空圈。
const props = defineProps<{ avatar?: string | null; seed?: string | null; label?: string | null }>()
const image = computed(() => {
  const avatar = props.avatar
  return avatar && isImageAvatar(avatar) ? avatar : ''
})
const style = computed(() => avatarPlaceholderStyle(props.seed || props.label || ''))
const initial = computed(() => placeholderInitial(props.label, props.seed))
</script>

<template>
  <img v-if="image" class="avatar-img" :src="image" alt="" />
  <span v-else class="avatar-placeholder" :style="style" aria-hidden="true">{{ initial }}</span>
</template>

<style scoped>
.avatar-img { width: 100%; height: 100%; display: block; border-radius: inherit; object-fit: cover; }
.avatar-placeholder { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; border-radius: inherit; font-size: 0.42em; font-weight: 600; line-height: 1; user-select: none; }
</style>
