<template>
  <!-- 容器页签：表头 + 列表。异常优先排序由父级算好，这里只管画与抛事件。 -->
  <div class="thead">
    <span></span><span>容器 ID</span><span>镜像</span><span>命令</span><span>创建</span><span>状态</span><span>端口</span><span>名称</span><span>操作</span>
  </div>
  <div class="list">
    <template v-for="item in items" :key="item.key">
      <div v-if="item.kind === 'group'" class="cgroup">
        <span class="cgroup-name mono">{{ item.label }}</span>
        <span class="cgroup-count mono">{{ item.count }}</span>
      </div>
      <div v-else class="crow" role="button" tabindex="0"
           :class="{ selected: selected?.id === item.row.id, abnormal: isAbnormal(item.row) }" :aria-pressed="selected?.id === item.row.id"
           @click="emit('select', item.row)" @keydown.enter.self.prevent="emit('select', item.row)" @keydown.space.self.prevent="emit('select', item.row)">
        <span class="st-dot" :class="[item.row.state, { unhealthy: item.row.health === 'unhealthy' }]"></span>
        <span class="c-id mono" :title="item.row.id">{{ item.row.id }}</span>
        <span class="c-image"><span class="repo" :title="item.row.repo + ':' + item.row.tag">{{ item.row.repo }}</span><span class="tag mono">{{ item.row.tag }}</span></span>
        <span class="c-cmd mono" :title="item.row.cmd">{{ item.row.cmd || '—' }}</span>
        <span class="c-created">{{ item.row.created || '—' }}</span>
        <span class="c-state" :class="[item.row.state, { unhealthy: item.row.health === 'unhealthy' }]" :title="item.row.stateText">{{ item.row.stateText }}</span>
        <span class="c-ports mono" :class="{ none: !item.row.ports }" :title="item.row.ports || '未映射'">{{ item.row.ports || '未映射' }}</span>
        <span class="c-name mono" :title="item.row.name">{{ item.row.name }}</span>
        <!-- 行内统一操作列（单一操作面）：启停 toggle + exec（仅运行中）+ 重启 + 删除；均为图标，in-flight 禁用防连点 -->
        <span class="c-acts" @click.stop>
          <button v-if="item.row.state === 'running'" class="icon-btn stop" title="停止" :disabled="!!busy" @click="emit('stop', item.row)">
            <svg v-if="busy === item.row.id" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
            <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
          </button>
          <button v-else class="icon-btn start" title="启动" :disabled="!!busy" @click="emit('start', item.row)">
            <svg v-if="busy === item.row.id" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
            <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="6 4 20 12 6 20 6 4"/></svg>
          </button>
          <button v-if="item.row.state === 'running'" class="icon-btn act-exec" title="exec 进入终端（新标签）" :disabled="!!busy" @click="emit('exec', item.row)">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
          </button>
          <button class="icon-btn act-restart" title="重启" :disabled="!!busy" @click="emit('restart', item.row)">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/></svg>
          </button>
          <button class="icon-btn act-del" title="删除" :disabled="!!busy" @click="emit('remove', item.row)">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
          </button>
        </span>
      </div>
    </template>
    <div v-if="!rows.length" class="list-empty">{{ emptyText }}</div>
  </div>
</template>

<script setup lang="ts">
// DockerContainersTab.vue — 容器页签的纯视图：排序、取数、动作编排都在 DockerView。
// 动作按钮只抛事件，父级拿事件里的行去跑 docker 命令（动作锁也从底座取，不在此自持）。
import { computed } from 'vue'
import { isAbnormal } from '@/stores/docker-core'
import type { ContainerRow } from '@/stores/docker-core'
import { useDockerHost } from './context'

const props = defineProps<{
  rows: ContainerRow[]
  selected: ContainerRow | null
  emptyText: string
  /** 按 compose 项目归组；分组头只影响渲染顺序，不改变行本身 */
  groupByCompose?: boolean
}>()

type ListItem =
  | { kind: 'group'; key: string; label: string; count: number }
  | { kind: 'row'; key: string; row: ContainerRow }

/**
 * 分组只重排、不重算：父级已按异常优先排好序，组内保持原序。
 * 未归属项目（没有 compose 标签）固定排在最后，不抢在具名项目前面。
 */
const items = computed<ListItem[]>(() => {
  if (!props.groupByCompose) {
    return props.rows.map(c => ({ kind: 'row', key: c.id, row: c }))
  }
  const groups = new Map<string, ContainerRow[]>()
  for (const c of props.rows) {
    const k = c.compose || ''
    const list = groups.get(k)
    if (list) list.push(c)
    else groups.set(k, [c])
  }
  const keys = [...groups.keys()].sort((a, b) => (a === '' ? 1 : b === '' ? -1 : a.localeCompare(b)))
  const out: ListItem[] = []
  for (const k of keys) {
    const list = groups.get(k) ?? []
    out.push({ kind: 'group', key: `g:${k}`, label: k || '未归属项目', count: list.length })
    for (const c of list) out.push({ kind: 'row', key: c.id, row: c })
  }
  return out
})
const emit = defineEmits<{
  select: [row: ContainerRow]
  start: [row: ContainerRow]
  stop: [row: ContainerRow]
  restart: [row: ContainerRow]
  remove: [row: ContainerRow]
  exec: [row: ContainerRow]
}>()
const { busy } = useDockerHost()
</script>

<style scoped>
/* 容器表是网格行（镜像/卷/网络是 .ptable），列宽与单元格只此一处使用 */
.thead, .crow {
  display: grid;
  grid-template-columns: 12px 84px minmax(0, 1.3fr) minmax(0, 1.1fr) 76px minmax(0, 1fr) minmax(0, 1.1fr) minmax(0, 1fr) 120px;
  gap: var(--spacing-12);
  align-items: center;
  padding: 0 var(--spacing-16);
}
/* 表头与数据单元格同轴左对齐（操作列 .c-acts 保持居中） */
.thead > span,
.crow > span:not(.st-dot):not(.c-acts) {
  text-align: left;
}
.thead {
  height: 30px; flex-shrink: 0; border-bottom: 1px solid var(--border-subtle);
  font-size: 0.7692rem; color: var(--text-tertiary); letter-spacing: .8px; text-transform: uppercase; user-select: none;
}
.crow {
  height: 46px; border-bottom: 1px solid var(--divider-primary); cursor: pointer;
  transition: background var(--transition-fast);
}
.crow:hover { background: var(--bg-hover); }
.crow.selected { background: var(--bg-elevated); box-shadow: inset 2px 0 0 var(--accent-primary); }
/* 键盘可达：行可 Tab 聚焦，Enter/Space 选中；焦点环内缩避免溢出相邻网格单元 */
.crow:focus-visible { outline: 2px solid var(--accent-primary); outline-offset: -2px; }
.st-dot { width: 8px; height: 8px; border-radius: 50%; justify-self: center; background: var(--text-tertiary); }
.st-dot.running { background: var(--status-success-text); }
.st-dot.exited { background: var(--status-error-text); }
.st-dot.stopped { background: var(--text-tertiary); }
/* healthcheck 失败优先于 running 绿：容器还在跑，但已经在报错 */
.st-dot.unhealthy { background: var(--status-error-text); }
.c-id { font-size: 0.8462rem; color: var(--text-tertiary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.c-image { min-width: 0; display: flex; align-items: center; gap: var(--spacing-6); }
.c-image .repo { font-size: 0.9231rem; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tag {
  font-size: 0.7692rem; color: var(--accent-indigo); background: color-mix(in srgb, var(--accent-indigo) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--accent-indigo) 20%, transparent); border-radius: var(--radius-sm); padding: 0 5px; flex: none;
}
.c-state { font-size: 0.8462rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.c-state.running { color: var(--status-success-text); }
.c-state.exited { color: var(--status-error-text); }
.c-state.stopped { color: var(--text-tertiary); }
.c-state.unhealthy { color: var(--status-error-text); }
.c-ports { font-size: 0.7692rem; color: var(--accent-cyan); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.c-ports.none { color: var(--text-tertiary); }
.c-cmd { font-size: 0.8462rem; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.c-created { font-size: 0.8462rem; color: var(--text-tertiary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.c-name { font-size: 0.9231rem; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
/* 异常优先：非0退出行左侧警示色带（选中时让位于 accent 选中条，不叠加） */
.crow.abnormal:not(.selected) { box-shadow: inset 3px 0 0 var(--status-error-text); }
/* compose 分组头：只做分隔与计数，不提供按钮（避免把整组启停混进单行操作面） */
.cgroup {
  display: flex; align-items: center; gap: var(--spacing-8); height: 28px; flex: none;
  padding: 0 var(--spacing-16); background: var(--bg-input);
  border-bottom: 1px solid var(--divider-primary); font-size: 0.8462rem; color: var(--text-secondary);
}
.cgroup-name { font-weight: 500; }
.cgroup-count { color: var(--text-tertiary); }
</style>
