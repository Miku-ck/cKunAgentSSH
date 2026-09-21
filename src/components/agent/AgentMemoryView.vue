<template>
  <div class="mem-view" @click.stop>
    <header class="mem-head">
      <span class="mem-title">Agent 记忆</span>
      <span class="mem-count">{{ memories.length }} 条</span>
      <button class="mem-close" aria-label="关闭" @click="$emit('close')">
        <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </header>

    <div class="mem-body">
      <p v-if="loading" class="mem-hint">加载中…</p>
      <p v-else-if="error" class="mem-hint err">{{ error }}</p>
      <p v-else-if="!memories.length" class="mem-hint">
        还没有记忆。Agent 完成任务后会自动沉淀关键事实（如服务器端口、路径偏好），注入后续对话。
      </p>
      <ul v-else class="mem-list">
        <li v-for="m in memories" :key="m.id" class="mem-item">
          <div class="mem-line">
            <span class="mem-type" :class="m.type">{{ typeText[m.type] || m.type }}</span>
            <span class="mem-importance" :title="`重要度 ${fmtScore(m.importance)} · 命中 ${m.use_count} 次`">
              {{ fmtScore(m.importance) }}
            </span>
          </div>
          <p class="mem-content">{{ m.content }}</p>
          <div class="mem-meta">
            <span>{{ fmtDate(m.last_used_at) }} 写入</span>
            <span class="spacer" />
            <button class="mem-del" :disabled="deleting === m.id" @click="onDelete(m)">
              {{ deleting === m.id ? '删除中…' : '删除' }}
            </button>
          </div>
        </li>
      </ul>
    </div>

    <footer class="mem-foot">
      <template v-if="pruneArmed">
        <span class="prune-q">清理最旧最少使用的记忆（保留最近 100 条）？</span>
        <button class="mem-btn danger" @click="onPrune">确认</button>
        <button class="mem-btn" @click="pruneArmed = false">取消</button>
      </template>
      <template v-else>
        <button class="mem-btn" @click="load">刷新</button>
        <button class="mem-btn" :disabled="!memories.length" @click="pruneArmed = true">清理旧记忆</button>
      </template>
    </footer>
  </div>
</template>

<script setup lang="ts">
/**
 * AgentMemoryView — AI 面板内的记忆浮层
 *
 * 对接 GET/DELETE /agent/memories、POST /agent/memories/prune。
 * 记忆每次运行时自动注入 system prompt（前 5 条），用户在此可见可清。
 */
import { onMounted, ref } from 'vue'
import { agentAdminApi, toFriendlyMessage, type AgentMemoryItem } from '@/api/client'
import { useToastStore } from '@/stores/toast'
import { parseBackendTs } from '@/utils/format'

defineEmits<{ close: [] }>()

const toast = useToastStore()
const memories = ref<AgentMemoryItem[]>([])
const loading = ref(false)
const error = ref('')
const deleting = ref('')
const pruneArmed = ref(false)
const typeText: Record<string, string> = { episodic: '经历', semantic: '事实' }

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await agentAdminApi.listMemories({ mode: 'ssh', limit: 100 })
    memories.value = data.memories
  } catch (e) {
    error.value = toFriendlyMessage(e, '记忆加载失败')
  } finally {
    loading.value = false
  }
}

async function onDelete(m: AgentMemoryItem) {
  deleting.value = m.id
  try {
    await agentAdminApi.deleteMemory(m.id)
    memories.value = memories.value.filter((x) => x.id !== m.id)
  } catch (e) {
    toast.error(`删除失败：${toFriendlyMessage(e)}`)
  } finally {
    deleting.value = ''
  }
}

async function onPrune() {
  pruneArmed.value = false
  try {
    const r = await agentAdminApi.pruneMemories({ mode: 'ssh', maxCount: 100 })
    toast.success(`已清理 ${r.deleted} 条旧记忆`)
    await load()
  } catch (e) {
    toast.error(`清理失败：${toFriendlyMessage(e)}`)
  }
}

// 旧行/异常写入可能给出 null importance：直接 .toFixed 会在渲染中抛错，
// 一条脏数据就让整个记忆列表空白。
function fmtScore(v: number | null): string {
  return typeof v === 'number' ? v.toFixed(2) : '-'
}

function fmtDate(iso: string): string {
  try {
    const d = parseBackendTs(iso)
    return `${d.getMonth() + 1}/${d.getDate()}`
  } catch {
    return iso
  }
}

onMounted(load)
</script>

<style scoped>
.mem-view {
  position: absolute;
  top: 42px;
  right: 10px;
  left: 10px;
  max-height: calc(100% - 90px);
  display: flex;
  flex-direction: column;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: 8px;
  z-index: 30;
  overflow: hidden;
}
.mem-head {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 12px; border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.mem-title { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.mem-count { font-size: 11px; color: var(--text-tertiary); margin-left: auto; }
.mem-close {
  background: none; border: none; cursor: pointer; color: var(--text-secondary);
  display: flex; align-items: center; padding: 2px;
}
.mem-close:hover { color: var(--text-primary); }
.mem-body { flex: 1; overflow-y: auto; padding: 4px 12px; }
.mem-hint { font-size: 12px; color: var(--text-secondary); line-height: 1.6; }
.mem-hint.err { color: #d98f86; }
.mem-list { list-style: none; margin: 0; padding: 0; }
.mem-item { padding: 8px 0; border-bottom: 1px solid var(--border-subtle); }
.mem-item:last-child { border-bottom: none; }
.mem-line { display: flex; align-items: center; gap: 6px; margin-bottom: 3px; }
.mem-type {
  font-size: 10px; padding: 0 6px; border-radius: 7px;
  border: 1px solid var(--border-default); color: var(--text-tertiary);
}
.mem-type.semantic { color: #7fbc92; border-color: rgba(127,188,146,.35); }
.mem-type.episodic { color: #7aa2d0; border-color: rgba(122,162,208,.35); }
.mem-importance { margin-left: auto; font-size: 10px; color: var(--text-tertiary); font-family: ui-monospace, Consolas, monospace; }
.mem-content {
  margin: 0 0 4px; font-size: 12px; color: var(--text-primary);
  line-height: 1.5; word-break: break-word;
  display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden;
}
.mem-meta { display: flex; align-items: center; gap: 8px; font-size: 10.5px; color: var(--text-tertiary); }
.mem-meta .spacer { flex: 1; }
.mem-del {
  background: none; border: none; cursor: pointer; padding: 0;
  color: var(--text-secondary); font-size: 11px; font-family: inherit;
}
.mem-del:hover { color: #cf8a80; }
.mem-del:disabled { opacity: .5; cursor: default; }
.mem-foot {
  display: flex; align-items: center; gap: 8px; padding: 8px 12px;
  border-top: 1px solid var(--border-subtle);
  font-size: 11.5px; color: var(--text-secondary); flex-shrink: 0;
}
.prune-q { flex: 1; }
.mem-btn {
  height: 24px; padding: 0 10px; border-radius: 4px; font-size: 11.5px; font-family: inherit;
  background: none; border: 1px solid var(--border-default); color: var(--text-primary); cursor: pointer;
}
.mem-btn:hover:not(:disabled) { border-color: var(--accent-primary); }
.mem-btn:disabled { opacity: .45; cursor: default; }
.mem-btn.danger { color: #cf8a80; border-color: rgba(207,138,128,.4); }
</style>
