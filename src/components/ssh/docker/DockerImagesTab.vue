<template>
  <!-- 镜像页签：引用列的三元态由 usageKnown（docker ps 是否被截断）+ 容器集合算出。
       ps 被 1MB 截断时列表不全，一律显示「—」，宁可少报也不要把在用的镜像说成可删。 -->
  <div class="list">
    <div v-if="error" class="list-empty err-hint">镜像取数失败：{{ error }}</div>
    <table v-else-if="rows.length" class="ptable">
      <colgroup><col style="width:38%"><col style="width:18%"><col style="width:14%"><col style="width:16%"><col style="width:14%"></colgroup>
      <thead><tr><th>镜像</th><th>镜像 ID</th><th>内容大小</th><th>引用</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="im in rows" :key="imageLabel(im)">
          <td class="mono" :title="imageLabel(im)">{{ imageLabel(im) }}</td>
          <td class="mono muted" :title="im.id">{{ im.id }}</td>
          <td class="mono">{{ im.size }}</td>
          <td class="use-hint" :class="imageUseHint(im).tone">{{ imageUseHint(im).text }}</td>
          <td>
            <span class="c-acts">
              <button class="icon-btn act-save" title="导出到远端目录（docker save）" :disabled="!!busy" @click="emit('export', im)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 8v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8"/><path d="M2 3h20v5H2z"/><path d="M10 12h4"/></svg>
              </button>
              <button class="icon-btn act-del" title="删除镜像" :disabled="!!busy" @click="emit('remove', im)">
                <svg v-if="busy === imageLabel(im)" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
                <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
              </button>
            </span>
          </td>
        </tr>
      </tbody>
    </table>
    <div v-else class="list-empty">{{ emptyText }}</div>
  </div>
</template>

<script setup lang="ts">
// DockerImagesTab.vue — 镜像页签的纯视图。取数、拉取、导出/加载、prune 都在 DockerView；
// 这里只把「引用列怎么读」这件事收敛到一个函数，避免表和状态栏各算一套口径。
import { imageLabel, imageUsageCount, type ContainerRow, type ImageRow } from '@/stores/docker-core'
import { useDockerHost } from './context'

const props = defineProps<{
  rows: ImageRow[]
  error: string | undefined
  emptyText: string
  /** docker ps 是否完整：截断时引用列不可信 */
  usageKnown: boolean
  containers: ContainerRow[]
}>()
const emit = defineEmits<{
  export: [row: ImageRow]
  remove: [row: ImageRow]
}>()
const { busy } = useDockerHost()

function imageUseHint(im: ImageRow): { text: string; tone: 'ok' | 'warn' | 'muted' } {
  if (!props.usageKnown) return { text: '—', tone: 'muted' }
  const n = imageUsageCount(im, props.containers)
  return n > 0 ? { text: `${n} 个容器`, tone: 'ok' } : { text: '未使用', tone: 'warn' }
}
</script>
