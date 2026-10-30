<template>
  <div class="git-view">
    <!-- 页头：只有标题与子页说明。连接类 UI 一律收在仓库子页内 ——
         加速子页不需要连接，壳不能替它做主（设计稿 2026-10-24） -->

    <div class="page-head">
      <div class="page-title">
        <span>Git</span>
      </div>
      <span class="page-sub">GitHub 加速 · 源码管理（仓库 · 远端一致性）</span>
    </div>

    <!-- 子页分段：整页只有这一排全局控件，仓库子页自带自己的工具条 -->
    <div class="toolbar">
      <div class="seg" role="tablist" aria-label="Git 子页">
        <button
          v-for="t in SUB_TABS"
          :key="t.key"
          type="button"
          class="seg-btn"
          role="tab"
          :aria-selected="sub === t.key"
          :class="{ on: sub === t.key }"
          @click="sub = t.key"
        >{{ t.label }}</button>
      </div>
    </div>

    <div class="git-body">
      <!-- sticky-visited：子页首次访问才挂载、之后保持挂载（同 SshView 页面容器语义） -->
      <GitAccelPanel v-if="visited.accel" v-show="sub === 'accel'" />
      <GitReposPanel
        v-if="visited.repos"
        v-show="sub === 'repos'"
        :active="active && sub === 'repos'"
        @exec-terminal="(id: string) => emit('exec-terminal', id)"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
// GitView.vue — Git 页壳（C 方案：GitHub 加速 + 源码管理双子页）。
// 壳只做三件事：页头 / 子页分段 / 子页惰性挂载；子页选择存 subTab.ts 的模块级
// ref（命令面板深链在页面挂载前就能指定目标子页，会话内记忆上次停留）。
// 2026-10-30：原「同步」子页并入仓库工作台，子页只剩两项。
import { computed, ref, watch } from 'vue'
import GitAccelPanel from './GitAccelPanel.vue'
import GitReposPanel from './GitReposPanel.vue'
import { gitSubTab, type GitSubTab } from './subTab'

defineProps<{ active?: boolean }>()
const emit = defineEmits<{ 'exec-terminal': [sessionId: string] }>()

const SUB_TABS = [
  { key: 'accel', label: '加速' },
  { key: 'repos', label: '源码管理' },
] as const

/** v-model 到模块级单例：切走再回来 / 深链预置，都从这一份状态出发 */
const sub = computed<GitSubTab>({
  get: () => gitSubTab.value,
  set: (v) => { gitSubTab.value = v },
})

// 首访惰性挂载：默认子页 repos 首帧即挂；accel 只有被选中过才挂载
// （避免只为看仓库的用户一进 Git 页就触发加速配置的额外取数）
const visited = ref<Record<GitSubTab, boolean>>({ accel: sub.value === 'accel', repos: sub.value === 'repos' })
watch(sub, (s) => { visited.value[s] = true }, { immediate: true })
</script>

<style scoped>
.git-view {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--canvas-bg);
  font-family: var(--font-sans);
}

.page-head {
  display: flex;
  align-items: center;
  gap: var(--spacing-10);
  padding: 0 var(--spacing-16);
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
  height: var(--page-head-h);
}

.page-title {
  display: flex;
  align-items: center;
  gap: var(--spacing-6);
  font-size: var(--page-title-size);
  font-weight: 600;
  color: var(--text-primary);
}

.page-sub {
  font-size: 0.9231rem;
  color: var(--text-tertiary);
}

.toolbar {
  display: flex;
  align-items: center;
  gap: var(--spacing-10);
  padding: var(--spacing-10) var(--spacing-16);
  border-bottom: 1px solid var(--divider-primary);
  flex-shrink: 0;
}

/* 分段控件与 DockerView 同款 */
.seg { display: inline-flex; background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); padding: var(--spacing-2); gap: var(--spacing-2); }
.seg-btn {
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font-size: 0.9231rem;
  font-family: var(--font-sans);
  padding: var(--spacing-4) var(--spacing-14);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast);
}
.seg-btn:hover { color: var(--text-primary); background: var(--bg-hover); }
.seg-btn.on { background: var(--bg-elevated); color: var(--accent-text); }

.git-body {
  flex: 1;
  min-height: 0;
  display: flex;
}
</style>
