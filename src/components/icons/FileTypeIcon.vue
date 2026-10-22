<script setup lang="ts">
/**
 * 文件类型图标 —— 按名字给出该类型的图形与颜色，判据全在 file-type-icons.ts。
 *
 * 两种画法是刻意的：通用图形走本仓既有约定（fill:none + stroke，颜色读 --accent-*
 * 令牌，所以随 6 套主题自动变）；品牌图形是填充色块，颜色取自公布标色、刻意不随
 * 主题变 —— 代价是 #f7df1e 这类浅黄在米纸底上会发虚，故给它一层主题化底衬。
 *
 * 底衬读 --text-primary 而不是新增一枚令牌：深色主题上它是浅的、浅底/米纸上它是
 * 深的，同一句 color-mix 在 6 套主题里各自成立，不必每主题补一份值。
 */
import { computed } from 'vue'
import { iconForFile, resolveKey, usesBrandColor } from '@/components/icons/file-type-icons'

const props = withDefaults(defineProps<{
  name: string
  isDirectory?: boolean
}>(), { isDirectory: false })

const key = computed(() => resolveKey(props.name, props.isDirectory))
const icon = computed(() => iconForFile(props.name, props.isDirectory))
/** 只有取品牌色的条目需要衬底；令牌色本就随主题走，衬了反而糊。 */
const brand = computed(() => usesBrandColor(key.value))
const cssVars = computed(() => ({ '--fti-color': icon.value.color }))
</script>

<template>
  <svg
    class="fti"
    :class="{ 'fti-brand': brand }"
    :style="cssVars"
    viewBox="0 0 24 24"
    aria-hidden="true"
    focusable="false"
  >
    <path v-for="(d, i) in icon.d" :key="i" :d="d" />
  </svg>
</template>

<style scoped>
.fti {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
}
.fti path {
  fill: none;
  stroke: var(--fti-color);
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.fti-brand path {
  fill: var(--fti-color);
  stroke: color-mix(in srgb, var(--text-primary) 55%, transparent);
  stroke-width: 1.4;
  /* stroke 先画、fill 后压：底衬只露在轮廓外一圈，不会啃掉图形细节 */
  paint-order: stroke fill;
}
</style>
