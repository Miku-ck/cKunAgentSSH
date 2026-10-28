<template>
  <section class="qq-records" aria-label="QQ 会话记录">
    <div class="records-head">
      <h3>会话记录</h3>
      <span class="records-note">只读 · 每 5 秒刷新</span>
    </div>
    <p v-if="error" class="records-error">{{ error }}</p>
    <p v-if="!conversations.length" class="records-empty">还没有 QQ 消息；到 QQ 里 @ 一下机器人试试。</p>
    <div v-else class="records-grid">
      <nav class="records-list" aria-label="QQ 会话">
        <button
          v-for="c in conversations"
          :key="c.id"
          type="button"
          class="records-item"
          :class="{ active: c.id === activeId }"
          @click="select(c.id)"
        >
          <span class="records-channel">{{ channelLabel(c) }}</span>
          <span class="records-title">{{ c.title }}</span>
          <small>{{ formatLocalDateTime(c.updated_at) }}</small>
        </button>
      </nav>
      <div ref="listRef" class="records-messages">
        <p v-if="!messages.length" class="records-empty">这段会话还没有消息。</p>
        <div v-for="m in messages" :key="m.id" class="records-msg" :class="m.role">
          <div class="records-meta">{{ roleLabel(m) }} · {{ formatLocalDateTime(m.created_at) }}</div>
          <div class="records-body">{{ m.content }}</div>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { botApi, type BotConversation, type BotMessage, type BotPersona } from '@/api/bot'
import { toFriendlyMessage } from '@/api/base'
import { formatLocalDateTime } from '@/utils/datetime'

const props = defineProps<{ personas: BotPersona[] }>()

const conversations = ref<BotConversation[]>([])
const messages = ref<BotMessage[]>([])
const activeId = ref('')
const error = ref('')
const listRef = ref<HTMLElement | null>(null)
let timer: ReturnType<typeof setInterval> | undefined
let busy = false

const activeConversation = computed(() => conversations.value.find(c => c.id === activeId.value) || null)

function channelLabel(conversation: BotConversation): string {
  return (conversation.external_id || '').startsWith('group:') ? '群聊' : '私聊'
}

function roleLabel(message: BotMessage): string {
  if (message.role === 'user') return '对方'
  const persona = props.personas.find(p => p.id === activeConversation.value?.persona_id)
  return persona?.name || '机器人'
}

function atBottom(): boolean {
  const el = listRef.value
  return !el || el.scrollHeight - el.scrollTop - el.clientHeight < 48
}

async function scrollToBottom() {
  await nextTick()
  const el = listRef.value
  if (el) el.scrollTop = el.scrollHeight
}

async function select(id: string) {
  activeId.value = id
  try {
    messages.value = await botApi.getMessages(id)
    error.value = ''
  } catch (e) {
    error.value = toFriendlyMessage(e, 'QQ 记录读取失败')
  }
  await scrollToBottom()
}

async function refresh() {
  if (busy) return
  busy = true
  try {
    const rows = await botApi.listQqConversations()
    conversations.value = rows
    if (!rows.length) {
      activeId.value = ''
      messages.value = []
      return
    }
    if (!rows.some(c => c.id === activeId.value)) activeId.value = rows[0].id
    const stick = atBottom()
    messages.value = await botApi.getMessages(activeId.value)
    error.value = ''
    if (stick) await scrollToBottom()
  } catch (e) {
    error.value = toFriendlyMessage(e, 'QQ 记录读取失败')
  } finally {
    busy = false
  }
}

onMounted(() => {
  void refresh()
  timer = setInterval(() => { void refresh() }, 5000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
.qq-records {
  margin-top: var(--spacing-16);
  padding-top: var(--spacing-16);
  border-top: 1px solid var(--border-subtle);
  display: flex;
  flex-direction: column;
  gap: var(--spacing-10);
}
.records-head { display: flex; align-items: baseline; justify-content: space-between; gap: var(--spacing-10); }
.records-head h3 { margin: 0; font-size: 1rem; color: var(--text-primary); }
.records-note { color: var(--text-tertiary); font-size: 0.8462rem; }
.records-empty { margin: 0; color: var(--text-secondary); font-size: 0.9231rem; }
.records-error { margin: 0; color: var(--danger-text); font-size: 0.9231rem; }
.records-grid {
  display: grid;
  grid-template-columns: 14rem minmax(0, 1fr);
  gap: var(--spacing-12);
  height: 20rem;
  min-height: 0;
}
.records-list {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-4);
  overflow: auto;
  padding-right: var(--spacing-4);
}
.records-item {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: var(--spacing-2) var(--spacing-8);
  padding: var(--spacing-8) var(--spacing-10);
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.records-item:hover { background: var(--bg-elevated); color: var(--text-primary); }
.records-item.active { border-color: var(--border-subtle); background: var(--bg-elevated); color: var(--text-primary); }
.records-channel { color: var(--accent-text); font-size: 0.7692rem; }
.records-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.records-item small { grid-column: 2; color: var(--text-tertiary); font-size: 0.7692rem; }
.records-messages {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-10);
  overflow: auto;
  padding: var(--spacing-12);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--bg-base);
}
.records-msg { display: flex; flex-direction: column; gap: var(--spacing-2); }
.records-meta { color: var(--text-tertiary); font-size: 0.7692rem; }
.records-body { color: var(--text-primary); font-size: 0.9231rem; line-height: 1.6; white-space: pre-wrap; word-break: break-word; }
.records-msg.user .records-body { color: var(--text-secondary); }
</style>
