<script setup lang="ts">
/** 标签溢出菜单：tabs-scroll 横向装不下时，标签条右侧「▾」的全部会话列表。 */
import { onMounted, onUnmounted } from 'vue'
import { useSSHStore } from '@/stores/ssh'
import { tabStatusKind } from '@/utils/tabStatus'
import IconMonitor from '@/components/icons/IconMonitor.vue'
import IconTerminal from '@/components/icons/IconTerminal.vue'

const props = defineProps<{ anchor: HTMLElement | null }>()
const emit = defineEmits<{ activate: [id: string]; close: [] }>()

const store = useSSHStore()

// 定点坐标：菜单必须逃出 .term-tabs 的 overflow:hidden（与 NewTerminalMenu 同一约束）
const r = props.anchor?.getBoundingClientRect()
const pos = { top: (r?.bottom ?? 0) + 4, left: Math.max(8, (r?.right ?? 280) - 280) }

function onDocClick(e: MouseEvent) {
  if (!(e.target as HTMLElement).closest('.tof-menu, .tof-btn')) emit('close')
}
function onScroll() { emit('close') }

onMounted(() => {
  document.addEventListener('click', onDocClick)
  document.addEventListener('scroll', onScroll, true)
})
onUnmounted(() => {
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('scroll', onScroll, true)
})

function pick(id: string) {
  emit('activate', id)
  emit('close')
}
</script>

<template>
  <Teleport to="body">
    <div class="tof-menu" :style="{ top: pos.top + 'px', left: pos.left + 'px' }">
      <button
        v-for="s in store.sessions"
        :key="s.id"
        type="button"
        class="tof-item"
        :class="{ on: s.id === store.activeSessionId }"
        @click="pick(s.id)"
      >
        <span
          v-if="tabStatusKind(s, store.activeSessionId)"
          class="tof-dot"
          :class="tabStatusKind(s, store.activeSessionId)"
          aria-hidden="true"
        />
        <IconMonitor v-if="s.type === 'remote'" :width="12" :height="12" :stroke-width="2" />
        <IconTerminal v-else :width="12" :height="12" :stroke-width="2" />
        <span class="lbl">{{ s.name }}</span>
      </button>
    </div>
  </Teleport>
</template>

<style scoped>
.tof-menu {
  position: fixed;
  width: 280px;
  max-height: 340px;
  overflow-y: auto;
  padding: 4px;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  z-index: 900;
}

.tof-item {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  padding: 5px 8px;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  font-family: var(--font-sans);
  text-align: left;
  cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast);
}

.tof-item:hover { background: var(--bg-hover); color: var(--text-primary); }
.tof-item.on { color: var(--text-primary); }

.lbl {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tof-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}

.tof-dot.activity { background: var(--accent-primary); }
.tof-dot.gave-up { background: var(--accent-red); }
.tof-dot.reconnecting { background: var(--status-warn-text); }
</style>
