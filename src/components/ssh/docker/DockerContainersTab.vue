<template>
  <!-- 容器页签：表头 + 列表。异常优先排序由父级算好，这里只管画与抛事件。 -->
  <div class="thead">
    <span></span><span>容器 ID</span><span>镜像</span><span>命令</span><span>创建</span><span>状态</span><span>端口</span><span>名称</span><span>操作</span>
  </div>
  <div class="list">
    <div v-for="c in rows" :key="c.id" class="crow" role="button" tabindex="0"
         :class="{ selected: selected?.id === c.id, abnormal: isAbnormal(c) }" :aria-pressed="selected?.id === c.id"
         @click="emit('select', c)" @keydown.enter.self.prevent="emit('select', c)" @keydown.space.self.prevent="emit('select', c)">
      <span class="st-dot" :class="[c.state, { unhealthy: c.health === 'unhealthy' }]"></span>
      <span class="c-id mono" :title="c.id">{{ c.id }}</span>
      <span class="c-image"><span class="repo" :title="c.repo + ':' + c.tag">{{ c.repo }}</span><span class="tag mono">{{ c.tag }}</span></span>
      <span class="c-cmd mono" :title="c.cmd">{{ c.cmd || '—' }}</span>
      <span class="c-created">{{ c.created || '—' }}</span>
      <span class="c-state" :class="[c.state, { unhealthy: c.health === 'unhealthy' }]" :title="c.stateText">{{ c.stateText }}</span>
      <span class="c-ports mono" :class="{ none: !c.ports }" :title="c.ports || '未映射'">{{ c.ports || '未映射' }}</span>
      <span class="c-name mono" :title="c.name">{{ c.name }}</span>
      <!-- 行内统一操作列（单一操作面）：启停 toggle + exec（仅运行中）+ 重启 + 删除；均为图标，in-flight 禁用防连点 -->
      <span class="c-acts" @click.stop>
        <button v-if="c.state === 'running'" class="icon-btn stop" title="停止" :disabled="!!busy" @click="emit('stop', c)">
          <svg v-if="busy === c.id" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
          <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
        </button>
        <button v-else class="icon-btn start" title="启动" :disabled="!!busy" @click="emit('start', c)">
          <svg v-if="busy === c.id" class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9" stroke-opacity="0.25"/><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
          <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="6 4 20 12 6 20 6 4"/></svg>
        </button>
        <button v-if="c.state === 'running'" class="icon-btn act-exec" title="exec 进入终端（新标签）" :disabled="!!busy" @click="emit('exec', c)">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
        </button>
        <button class="icon-btn act-restart" title="重启" :disabled="!!busy" @click="emit('restart', c)">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/></svg>
        </button>
        <button class="icon-btn act-del" title="删除" :disabled="!!busy" @click="emit('remove', c)">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
        </button>
      </span>
    </div>
    <div v-if="!rows.length" class="list-empty">{{ emptyText }}</div>
  </div>
</template>

<script setup lang="ts">
// DockerContainersTab.vue — 容器页签的纯视图：排序、取数、动作编排都在 DockerView。
// 动作按钮只抛事件，父级拿事件里的行去跑 docker 命令（动作锁也从底座取，不在此自持）。
import { isAbnormal } from '@/stores/docker-core'
import type { ContainerRow } from '@/stores/docker-core'
import { useDockerHost } from './context'

defineProps<{
  rows: ContainerRow[]
  selected: ContainerRow | null
  emptyText: string
}>()
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
</style>
