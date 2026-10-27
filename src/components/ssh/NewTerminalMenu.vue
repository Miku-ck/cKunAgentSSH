<script setup lang="ts">
/**
 * 标签条 ＋ 菜单 —— 新建终端的唯一入口（spec 外壳标签态）
 *
 * 取代原 Quick Start 的搜索 + 两组卡片：启动器不该占一整屏，但它提供的
 * 能力（按名字找一个本地 shell / 一台已配好的连接）必须原样还在，所以
 * 整块搬进这枚下拉。目标列表只读 ssh store，动作以事件交回 SshView →
 * useSshNavigation：防抖、转圈、聚焦、离开首页这些副作用属于导航状态机，
 * 抄一份进菜单就是第二条通路。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { connNote, useSSHStore, type SSHConnection } from '@/stores/ssh'
import { LOCAL_SHELLS } from '@/composables/useSshNavigation'

const emit = defineEmits<{
  'open-local': [shell: string]
  'open-remote': [conn: SSHConnection]
}>()

const ssh = useSSHStore()
const open = ref(false)
const btn = ref<HTMLElement | null>(null)
const pos = ref({ top: 0, left: 0 })
const search = ref('')

/** 与下方 .ntm-menu 的 width 同值 —— 只用于把菜单裁回窗口内，不参与渲染 */
const MENU_W = 288

const q = computed(() => search.value.toLowerCase().trim())
const shells = computed(() =>
  LOCAL_SHELLS.filter(s => !q.value || s.toLowerCase().includes(q.value)),
)
const connections = computed(() =>
  ssh.connections.filter(c =>
    !q.value ||
    c.name.toLowerCase().includes(q.value) ||
    c.host.toLowerCase().includes(q.value) ||
    c.username.toLowerCase().includes(q.value) ||
    // 备注参与搜索：与远程管理页搜索同口径（connNote 单一来源）
    connNote(c).toLowerCase().includes(q.value),
  ),
)
const empty = computed(() => shells.value.length === 0 && connections.value.length === 0)

function onDocClick(e: MouseEvent) {
  if (open.value && !e.composedPath().includes(btn.value as Node)) open.value = false
}
function close() {
  open.value = false
  search.value = ''
}
onMounted(() => {
  document.addEventListener('click', onDocClick)
  // 菜单一经定位就写死坐标：标签条横向滚起来时 ＋ 会移走，跟着飘的菜单做不到
  // （它必须逃出 .term-tabs 的 overflow:hidden），所以滚动即收起。
  window.addEventListener('resize', close)
  document.addEventListener('scroll', close, true)
})
onUnmounted(() => {
  document.removeEventListener('click', onDocClick)
  window.removeEventListener('resize', close)
  document.removeEventListener('scroll', close, true)
})

function toggle() {
  if (open.value) return close()
  const r = btn.value?.getBoundingClientRect()
  if (r) {
    pos.value = {
      top: r.bottom + 4,
      // 标签多到 ＋ 被推到窗口右边时，右边缘不能被裁掉
      left: Math.max(8, Math.min(r.left, window.innerWidth - MENU_W - 8)),
    }
  }
  open.value = true
}

function pickLocal(shell: string) {
  emit('open-local', shell)
  close()
}

function pickRemote(conn: SSHConnection) {
  emit('open-remote', conn)
  close()
}
</script>

<template>
  <div class="ntm">
    <button
      ref="btn"
      type="button"
      class="ntm-btn"
      title="新建终端"
      aria-label="新建终端"
      :aria-expanded="open"
      @click.stop="toggle"
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
        <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
      </svg>
    </button>

    <!-- 标签条与 .term-tabs 都是 overflow:hidden，菜单留在原地会被裁成一像素。
         只能逃到 body 下用固定坐标摆。 -->
    <Teleport to="body">
      <div
        v-if="open"
        class="ntm-menu"
        :style="{ top: pos.top + 'px', left: pos.left + 'px' }"
        @click.stop
      >
      <div class="ntm-search">
        <input v-model="search" placeholder="搜索终端或连接..." class="ntm-input" />
      </div>

      <template v-if="!empty">
        <div v-if="shells.length" class="ntm-group">
          <div class="ntm-title">本地终端</div>
          <button
            v-for="shell in shells"
            :key="shell"
            type="button"
            class="ntm-item"
            @click="pickLocal(shell)"
          >
            <span class="lbl">{{ shell }}</span>
          </button>
        </div>

        <div v-if="connections.length" class="ntm-group">
          <div class="ntm-title">远程连接</div>
          <button
            v-for="conn in connections"
            :key="conn.id"
            type="button"
            class="ntm-item"
            @click="pickRemote(conn)"
          >
            <span class="dot" :class="{ live: ssh.isOnline(conn.id) }" />
            <span class="lbl">{{ conn.name }}</span>
            <span v-if="connNote(conn)" class="note" :title="connNote(conn)">{{ connNote(conn) }}</span>
            <span class="meta">{{ conn.username }}@{{ conn.host }}</span>
          </button>
        </div>
      </template>

      <div v-else class="ntm-empty">没有匹配的终端或连接</div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.ntm {
  position: relative;
  display: flex;
  align-items: flex-end;
  height: 100%;
  flex-shrink: 0;
}
.ntm-btn {
  height: calc(var(--tabbar-h) - 4px);
  width: calc(var(--tabbar-h) - 4px);
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid transparent;
  border-bottom: none;
  border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
  transition: all var(--transition-fast);
}
.ntm-btn:hover { color: var(--text-secondary); background: var(--bg-hover) }
.ntm-menu {
  position: fixed;
  width: 288px;
  max-height: 340px;
  overflow-y: auto;
  padding: var(--spacing-8);
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  z-index: 900;
}
.ntm-search {
  display: flex;
  align-items: center;
  height: 26px;
  padding: 0 var(--spacing-8);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  margin-bottom: var(--spacing-8);
}
.ntm-search:focus-within { border-color: var(--accent-primary) }
.ntm-input {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  color: var(--text-primary);
  font-size: 0.9231rem;
  font-family: var(--font-sans);
  outline: none;
}
.ntm-group + .ntm-group { margin-top: var(--spacing-8) }
.ntm-title {
  font-size: 0.7692rem;
  letter-spacing: .5px;
  text-transform: uppercase;
  color: var(--text-tertiary);
  padding: var(--spacing-4) var(--spacing-6);
}
.ntm-item {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  padding: 5px var(--spacing-6);
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  font-size: 0.9231rem;
  text-align: left;
  transition: background var(--transition-fast), color var(--transition-fast);
}
.ntm-item:hover { background: var(--bg-hover); color: var(--text-primary) }
.ntm-item .lbl {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ntm-item .meta {
  margin-left: auto;
  font-family: var(--font-mono);
  font-size: 0.7692rem;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 130px;
}
.ntm-item .note {
  font-size: 0.7692rem;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 64px;
  flex-shrink: 1;
  min-width: 0;
}
.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
  background: var(--text-tertiary);
  opacity: .5;
}
.dot.live { background: var(--accent-green); opacity: 1 }
.ntm-empty {
  padding: var(--spacing-10) var(--spacing-6);
  font-size: 0.9231rem;
  color: var(--text-tertiary);
  text-align: center;
}
</style>
