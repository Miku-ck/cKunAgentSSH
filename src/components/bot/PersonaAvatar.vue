<script setup lang="ts">
import { computed } from 'vue'
import { isImageAvatar } from '@/utils/persona-avatar'

// 头像只有两态：有上传图就显示图，没有就留空（完全透明）。
// 不用首字、不用派生色底 —— bot 的默认头像就是「没有」，不引入任何字母与色块。
// 非 data URL（旧数据里的表情/文本）不当图片。
const props = defineProps<{ avatar?: string | null }>()
const image = computed(() => (props.avatar && isImageAvatar(props.avatar) ? props.avatar : ''))
</script>

<template>
  <img v-if="image" class="avatar-img" :src="image" alt="" />
  <span v-else class="avatar-empty" aria-hidden="true" />
</template>

<style scoped>
.avatar-img { width: 100%; height: 100%; display: block; border-radius: inherit; object-fit: cover; }
/* 空位只占尺寸，不画底、不写字：透明。 */
.avatar-empty { width: 100%; height: 100%; display: block; border-radius: inherit; background: transparent; }
</style>
