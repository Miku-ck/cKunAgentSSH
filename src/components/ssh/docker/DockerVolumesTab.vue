<template>
  <!-- 卷页签：整行可点开详情（选中条落在首单元格左边框 —— 表格布局下 td 的 inset shadow 会沿列区断裂） -->
  <div class="list">
    <div v-if="error" class="list-empty err-hint">卷取数失败：{{ error }}</div>
    <table v-else-if="rows.length" class="ptable clickable">
      <colgroup><col style="width:9%"><col style="width:30%"><col style="width:11%"><col style="width:12%"><col style="width:24%"><col style="width:14%"></colgroup>
      <thead><tr><th>驱动</th><th>卷名</th><th>大小</th><th>引用</th><th>标签</th><th>操作</th></tr></thead>
      <tbody>
        <tr
          v-for="v in rows" :key="v.name"
          :class="{ selected: selectedName === v.name }"
          @click="emit('open', v)"
        >
          <td class="mono muted">{{ v.driver }}</td>
          <td class="mono" :title="v.name">{{ v.name }}</td>
          <td class="mono">{{ v.size || '—' }}</td>
          <td class="use-hint" :class="volumeUseHint(v).tone">{{ volumeUseHint(v).text }}</td>
          <td class="mono label-hint" :title="v.labels ? Object.entries(v.labels).map(([k, val]) => k + '=' + val).join('\n') : ''">{{ labelHint(v.labels) }}</td>
          <td>
            <span class="c-acts" @click.stop>
              <button class="icon-btn act-del" title="删除卷" :disabled="!!busy" @click="emit('remove', v)">
                <svg v-if="busy === 'v:' + v.name" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
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
// DockerVolumesTab.vue — 卷页签的纯视图。取数、用量富集、rm/prune 都在 DockerView。
import { labelHint, type VolumeRow } from '@/stores/docker-core'
import { useDockerHost } from './context'

defineProps<{
  rows: VolumeRow[]
  error: string | undefined
  /** 详情弹窗开着的那一行：名字跨宿主可能撞名，故按名字高亮即可 */
  selectedName: string | null
  emptyText: string
}>()
const emit = defineEmits<{
  open: [row: VolumeRow]
  remove: [row: VolumeRow]
}>()
const { busy } = useDockerHost()

// linkCount 三元态：undefined（老 Docker 不支持）/ 0（未使用，可安全回收）/ >0（在用，rm 会失败）
function volumeUseHint(v: VolumeRow): { text: string; tone: 'ok' | 'warn' | 'muted' } {
  if (typeof v.linkCount !== 'number') return { text: '—', tone: 'muted' }
  if (v.linkCount === 0) return { text: '未使用', tone: 'warn' }
  return { text: `${v.linkCount} 个容器`, tone: 'ok' }
}
</script>
