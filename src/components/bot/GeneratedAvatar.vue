<script setup lang="ts">
import { computed } from 'vue'
import { avatarPlaceholderStyle, placeholderInitial } from '@/utils/bot-chat-decor'

// QQ 对端匿名占位头像（Phase 4 §4.1，取代 identicon 马赛克）：淡色底 + 首字/代号。
// 只用 openid 等不透明标识做种子 —— 同一会话恒同色，不同会话不同色；
// 不采集任何真实昵称/头像，真实资料官方机器人拿不到，也不该拿。
const props = defineProps<{ seed?: string | null; label?: string | null }>()
const style = computed(() => avatarPlaceholderStyle(props.seed || ''))
const initial = computed(() => placeholderInitial(props.label, props.seed))
</script>

<template>
  <span class="gen-avatar" :style="style" aria-hidden="true">{{ initial }}</span>
</template>

<style scoped>
.gen-avatar {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  border-radius: inherit;
  font-size: 0.42em;
  font-weight: 600;
  line-height: 1;
  user-select: none;
}
</style>
