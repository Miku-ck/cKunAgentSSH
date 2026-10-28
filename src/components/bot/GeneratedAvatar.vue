<script setup lang="ts">
import { computed } from 'vue'
import { identiconSvg } from '@/utils/bot-chat-decor'

// 匿名生成头像：只用 openid 等不透明标识做种子，确定性渲染一枚 identicon。
// SVG 由本地纯函数产出、不含任何外部资料与真实用户数据，v-html 可信。
const props = defineProps<{ seed?: string | null }>()
const markup = computed(() => identiconSvg(props.seed || ''))
</script>

<template>
  <span class="gen-avatar" v-html="markup" />
</template>

<style scoped>
.gen-avatar { display: block; width: 100%; height: 100%; }
.gen-avatar :deep(svg) { width: 100%; height: 100%; display: block; border-radius: inherit; }
</style>
