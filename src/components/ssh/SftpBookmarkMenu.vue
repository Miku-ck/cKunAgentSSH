<template>
  <div class="bm-wrap" @click.stop>
    <button
      class="bm-trigger"
      :class="{ on: isCurrentBookmarked, off: disabled }"
      type="button"
      :disabled="disabled"
      :title="isCurrentBookmarked ? '路径收藏（当前路径已收藏）' : '路径收藏'"
      aria-haspopup="menu"
      :aria-expanded="open"
      @click="togglePanel"
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>
      </svg>
    </button>

    <div v-if="open" class="bm-dropdown" role="menu" aria-label="路径收藏">
      <div
        class="bm-action"
        :class="{ on: isCurrentBookmarked, disabled: !cwd || disabled }"
        role="menuitem"
        :aria-disabled="!cwd || disabled"
        @click="toggleCurrent"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>
        </svg>
        <span class="bm-label">{{ isCurrentBookmarked ? '取消收藏当前路径' : '收藏当前路径' }}</span>
        <span v-if="items.length" class="bm-count">{{ items.length }}</span>
      </div>

      <div v-if="items.length" class="bm-sep"></div>

      <div v-if="items.length" class="bm-list">
        <div
          v-for="item in items"
          :key="item.id"
          class="bm-item"
          role="menuitem"
          :title="item.path"
          @click="goto(item)"
        >
          <svg class="bm-folder" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
          </svg>
          <div class="bm-text">
            <div class="bm-label">{{ item.label }}</div>
            <div class="bm-path">{{ item.path }}</div>
          </div>
          <button
            class="bm-del"
            type="button"
            :title="`删除收藏: ${item.label}`"
            :aria-label="`删除收藏 ${item.label}`"
            @click.stop="removeItem(item)"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
      </div>

      <div v-else class="bm-empty">还没有收藏路径</div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * SFTP 路径收藏面板
 *
 * 触发按钮放在 SftpPane 的按钮组里（上一级 与 刷新 之间）。面板自身只管
 * 展示与交互，跳转仍走 sftpStore.loadLocal / loadRemote，错误提示沿用
 * SftpPane 路径编辑栏的口径（toFriendlyMessage + 保持面板打开）。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useSftpStore } from '@/stores/sftp'
import {
  MAX_BOOKMARKS_PER_SCOPE,
  labelFromPath,
  useSftpBookmarksStore,
  type PathBookmark,
} from '@/stores/sftpBookmarks'
import { useToastStore } from '@/stores/toast'
import { toFriendlyMessage } from '@/api/client'

const props = defineProps<{
  side: 'local' | 'remote'
  cwd: string
  online: boolean
  hostKey: string
}>()

const sftp = useSftpStore()
const bookmarks = useSftpBookmarksStore()
const toast = useToastStore()

const open = ref(false)

const disabled = computed(() => props.side === 'remote' && !props.online)
const items = computed(() => bookmarks.listFor(props.side, props.hostKey))
const isCurrentBookmarked = computed(() =>
  !!props.cwd && bookmarks.isBookmarked(props.side, props.hostKey, props.cwd)
)

// 首屏就 load 一次：按钮的"已收藏"填充态要在打开面板之前就是对的
bookmarks.load()

function togglePanel(): void {
  if (disabled.value) return
  open.value = !open.value
}

function toggleCurrent(): void {
  if (!props.cwd || disabled.value) return
  const result = bookmarks.toggle(props.side, props.hostKey, props.cwd)
  if (result === 'noop') return
  if (result === 'limit') {
    toast.info(`收藏上限 ${MAX_BOOKMARKS_PER_SCOPE} 条，请先删除不用的收藏`)
    return
  }
  toast.success(result === 'added' ? `已收藏 ${labelFromPath(props.cwd)}` : '已取消收藏')
}

async function goto(item: PathBookmark): Promise<void> {
  if (disabled.value) return
  if (props.side === 'local') {
    await sftp.loadLocal(item.path)
    if (sftp.localError) {
      toast.error(toFriendlyMessage(sftp.localError, `路径不存在或无法访问: ${item.path}`))
      return
    }
  } else {
    await sftp.loadRemote(item.path)
    if (sftp.remoteError) {
      toast.error(toFriendlyMessage(sftp.remoteError, `路径不存在或无法访问: ${item.path}`))
      return
    }
  }
  open.value = false
}

function removeItem(item: PathBookmark): void {
  if (bookmarks.remove(props.side, props.hostKey, item.id)) {
    toast.success(`已删除收藏 ${item.label}`)
  }
}

function onDocumentClick(): void {
  open.value = false
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape' && open.value) {
    e.stopPropagation()
    open.value = false
  }
}

// 断开连接时面板内容已无意义，直接收起，避免残留一个可点但无效的列表
watch(disabled, (off) => {
  if (off) open.value = false
})

onMounted(() => {
  document.addEventListener('click', onDocumentClick)
  document.addEventListener('keydown', onKeydown)
})
onUnmounted(() => {
  document.removeEventListener('click', onDocumentClick)
  document.removeEventListener('keydown', onKeydown)
})
</script>

<style scoped>
.bm-wrap { position: relative; display: inline-flex; }

/* 规格与 SftpPane 的 .pane-btn 一致 —— scoped 样式无法跨组件继承，
   这里按同一份尺寸/色值复写，改动时两边需同步 */
.bm-trigger {
  width: 22px; height: 22px;
  display: flex; align-items: center; justify-content: center;
  background: transparent; border: none; color: var(--text-tertiary);
  cursor: pointer; border-radius: var(--radius-sm);
}
.bm-trigger svg { width: 12px; height: 12px; }
.bm-trigger:hover:not(:disabled) { background: var(--bg-hover); color: var(--text-secondary); }
.bm-trigger.off { opacity: 0.4; cursor: not-allowed; }
.bm-trigger.on { color: var(--accent-cyan); }
.bm-trigger.on svg { fill: color-mix(in srgb, var(--accent-cyan) 22%, transparent); }

.bm-dropdown {
  position: absolute;
  top: 26px;
  right: -6px;
  width: 272px;
  padding: 4px 0;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  z-index: 100;
  animation: bm-fade-in 0.1s ease-out;
}

.bm-count {
  margin-left: auto;
  font-size: 10px; font-family: var(--font-mono);
  color: var(--text-tertiary);
}

.bm-action {
  display: flex; align-items: center; gap: 8px;
  margin: 0 6px; padding: 6px 8px;
  border-radius: var(--radius-sm);
  color: var(--text-secondary); cursor: pointer;
}
.bm-action:hover:not(.disabled) { background: var(--bg-hover); color: var(--text-primary); }
.bm-action.disabled { opacity: 0.5; cursor: not-allowed; }
.bm-action.on { color: var(--accent-cyan); }
.bm-action.on svg { fill: color-mix(in srgb, var(--accent-cyan) 22%, transparent); }
.bm-action svg { width: 13px; height: 13px; flex: none; }

.bm-text { min-width: 0; flex: 1; line-height: 1.3; }
.bm-label {
  font-size: 12px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.bm-path {
  margin-top: 1px;
  font-family: var(--font-mono); font-size: 10px; color: var(--text-tertiary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

.bm-sep { height: 1px; margin: 4px 0; background: var(--border-subtle); }

.bm-list { max-height: 216px; overflow-y: auto; padding: 0 6px; }
.bm-item {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 8px; border-radius: var(--radius-sm);
  cursor: pointer;
}
.bm-item:hover { background: var(--bg-hover); }
.bm-folder { width: 13px; height: 13px; flex: none; color: var(--accent-orange); }

.bm-del {
  width: 18px; height: 18px; flex: none;
  display: none; align-items: center; justify-content: center;
  border: none; background: transparent; color: var(--text-tertiary);
  border-radius: var(--radius-sm); cursor: pointer;
}
.bm-del svg { width: 11px; height: 11px; }
.bm-del:hover { background: color-mix(in srgb, var(--danger-text) 16%, transparent); color: var(--danger-text); }
.bm-item:hover .bm-del { display: flex; }

.bm-empty { padding: 14px 12px 16px; text-align: center; font-size: 12px; color: var(--text-tertiary); }

@keyframes bm-fade-in {
  from { opacity: 0; transform: scale(0.96); }
  to { opacity: 1; transform: scale(1); }
}
</style>
