<script setup lang="ts">
/** MRU 切换浮层：Ctrl+Tab 按住期间的会话列表。贴标签条下缘，不抢焦点不居中。 */
import { computed } from 'vue'
import { useSSHStore } from '@/stores/ssh'
import { mruSwitcher } from '@/composables/useGlobalShortcuts'
import { tabStatusKind } from '@/utils/tabStatus'
import IconMonitor from '@/components/icons/IconMonitor.vue'
import IconTerminal from '@/components/icons/IconTerminal.vue'

const store = useSSHStore()

const entries = computed(() => {
  const alive = new Map(store.sessions.map(s => [s.id, s]))
  return store.mruIds.filter(id => alive.has(id)).map(id => alive.get(id)!)
})
</script>

<template>
  <div class="mru-switcher" role="listbox" aria-label="切换终端会话">
    <div
      v-for="(s, i) in entries"
      :key="s.id"
      class="mru-row"
      :class="{ on: i === mruSwitcher.index }"
      role="option"
      :aria-selected="i === mruSwitcher.index"
    >
      <span
        v-if="tabStatusKind(s, store.activeSessionId)"
        class="mru-dot"
        :class="tabStatusKind(s, store.activeSessionId)"
        aria-hidden="true"
      />
      <IconMonitor v-if="s.type === 'remote'" :width="12" :height="12" :stroke-width="2" />
      <IconTerminal v-else :width="12" :height="12" :stroke-width="2" />
      <span class="mru-name">{{ s.name }}</span>
    </div>
  </div>
</template>

<style scoped>
.mru-switcher {
  position: fixed;
  top: calc(var(--titlebar-h) + var(--tabbar-h) + 4px);
  left: 8px;
  z-index: 900;
  min-width: 200px;
  max-width: 320px;
  padding: 4px;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
}

.mru-row {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 5px 8px;
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  font-size: 12px;
}

.mru-row.on {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.mru-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mru-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}

.mru-dot.activity { background: var(--accent-primary); }
.mru-dot.gave-up { background: var(--accent-red); }
.mru-dot.reconnecting { background: var(--status-warn-text); }
</style>
