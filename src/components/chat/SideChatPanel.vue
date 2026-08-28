<template>
  <div class="side-chat-panel" :style="{ width: panelWidth + 'px' }">
    <!-- 左侧拖拽手柄 -->
    <div class="resize-handle" @mousedown="startResize"></div>

    <!-- 顶部栏 -->
    <div class="panel-header">
      <slot name="header-prefix"></slot>
      <div class="header-spacer"></div>
      <div class="header-actions">
        <button class="icon-btn" title="历史记录" @click="showHistoryPanel = !showHistoryPanel">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
        </button>
        <button class="icon-btn" :class="{ loading: props.creating }" :title="props.creating ? '创建中...' : '新建对话'" :disabled="props.creating" @click="$emit('new-chat')">
          <svg v-if="!props.creating" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          <svg v-else class="spinner" viewBox="0 0 24 24" fill="none" width="14" height="14"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" stroke-opacity="0.25"/><path d="M22 12a10 10 0 0 0-10-10" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg>
        </button>
        <button class="icon-btn" title="关闭面板" @click="$emit('close')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
    </div>

    <!-- 主内容区 -->
    <div class="main-content" ref="messagesEl">
      <!-- 空状态 -->
      <div v-if="displayMessages.length === 0" class="empty-state">
        <div class="empty-icon-wrapper">
          <slot name="empty-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="40" height="40">
              <rect x="2" y="4" width="20" height="16" rx="2"/>
              <path d="M6 8h.01M10 8h.01M14 8h.01"/>
              <path d="M6 12h12M6 16h8"/>
            </svg>
          </slot>
        </div>
        <h3 class="empty-title">{{ emptyTitle }}</h3>
        <p class="empty-desc">{{ emptyDesc }}</p>
      </div>

      <!-- 消息列表 -->
      <div v-else class="messages-list">
        <template v-for="(msg, idx) in displayMessages" :key="msg.id">
          <!-- Agent 执行过程前置块：运行中且目标为最后一条 assistant 占位消息 -->
          <AgentTraceInline
            v-if="isLiveAgentRun(msg, idx)"
            :trace="agentStore.snapshot()"
            :is-live="true"
          />
          <MessageItem
            :message="msg"
            :is-streaming="streaming"
            :is-last="idx === displayMessages.length - 1"
          >
            <template v-if="showMessageActions" #actions="{ content }">
              <slot name="message-actions" :content="content" :msg="msg"></slot>
            </template>
          </MessageItem>
        </template>

        <!-- Agent 面板插槽（IDE 模式使用） -->
        <slot name="agent-panel"></slot>
      </div>

      <!-- 历史面板 -->
      <HistoryPanel
        v-if="showHistoryPanel"
        :groups="groupedConversations"
        :current-conv-id="currentConvId"
        :is-empty="conversations.length === 0"
        @close="showHistoryPanel = false"
        @switch="handleSwitch"
        @delete="handleDelete"
      />
    </div>

    <!-- 输入区域 -->
    <div class="input-section">
      <div class="input-container">
        <!-- 上下文标签插槽（IDE 文件标签等） -->
        <slot name="input-prefix"></slot>

        <!-- 已选技能标签（与 Work 模式 ChatInput 风格一致） -->
        <div v-if="selectedSkillTags.length > 0" class="skill-tags">
          <span v-for="tag in selectedSkillTags" :key="tag.id" class="tag-skill">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="9" height="9">
              <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>
            </svg>
            {{ tag.name }}
            <button class="tag-remove" @click="removeSkill(tag.id)" title="移除技能">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="8" height="8">
                <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </span>
        </div>

        <!-- 已选附件（上传文件，随消息注入 AI 上下文） -->
        <div v-if="pendingFiles.length > 0" class="pending-files">
          <span v-for="att in pendingFiles" :key="att.file_id" class="tag-file" :title="att.truncated ? '文件较大，已截断前 50K 字符注入' : ''">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="9" height="9"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
            <span class="file-name">{{ att.filename }}</span>
            <span v-if="att.size" class="file-size">{{ formatFileSize(att.size) }}</span>
            <button class="tag-remove" @click="removePendingFile(att.file_id)" title="移除文件">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="8" height="8">
                <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </span>
        </div>

        <textarea
          ref="inputRef"
          v-model="inputText"
          class="chat-input"
          rows="1"
          :placeholder="inputPlaceholder"
          :disabled="streaming || agentRunning"
          @keydown="handleKeydown"
          @input="onInput"
        ></textarea>

        <!-- / 触发技能菜单 -->
        <div v-if="showSkillMenu" class="skill-menu">
          <div class="skill-menu-header">
            <span class="menu-title">技能</span>
            <span class="menu-hint">↑↓ 选择 · Enter 确认 · Esc 关闭</span>
          </div>
          <div class="skill-menu-list">
            <div
              v-for="(item, i) in filteredSkills"
              :key="item.id"
              class="skill-menu-item"
              :class="{ active: i === skillActiveIdx }"
              @click="pickSkill(item)"
              @mouseenter="skillActiveIdx = i"
            >
              <div class="skill-item-info">
                <span class="skill-item-name">{{ item.name }}</span>
                <span v-if="item.description" class="skill-item-desc">{{ item.description }}</span>
              </div>
              <span v-if="item.category" class="skill-item-cat">{{ item.category }}</span>
            </div>
            <div v-if="filteredSkills.length === 0" class="skill-menu-empty">
              无匹配技能
            </div>
          </div>
        </div>
      </div>

      <!-- 工具栏 -->
      <div class="input-toolbar">
        <div class="toolbar-left">
          <!-- 上传文件附件 -->
          <button
            v-if="uploadHandler"
            class="toolbar-btn upload-btn"
            :disabled="uploading || streaming || agentRunning"
            :title="uploading ? '上传中...' : (streaming || agentRunning) ? '当前响应中，请稍后再上传' : '上传文件（文本类，单文件 ≤ 10MB）'"
            @click="handleUploadClick"
          >
            <svg v-if="!uploading" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="13" height="13"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
            <svg v-else class="spinner" viewBox="0 0 24 24" fill="none" width="13" height="13"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" stroke-opacity="0.25"/><path d="M22 12a10 10 0 0 0-10-10" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg>
          </button>
          <input
            ref="fileInputRef"
            type="file"
            multiple
            class="hidden-file-input"
            :disabled="streaming || agentRunning"
            @change="handleFileChange"
          />
          <button
            v-if="showAgentToggle"
            class="toolbar-btn agent-toggle"
            :class="{ active: agentMode }"
            :title="agentMode ? 'Agent 模式已开启 — AI 可自主调用工具' : '开启 Agent 模式 — AI 可自主调用工具'"
            @click="emit('update:agentMode', !agentMode)"
          >
            <span class="agent-label">Agent</span>
          </button>
          <slot name="toolbar-left"></slot>
        </div>
        <div class="toolbar-right">
          <!-- 模型选择器 -->
          <div class="model-selector" ref="modelSelectorRef">
            <button class="model-selector-btn" @click="showModelDropdown = !showModelDropdown">
              <span class="model-name">{{ currentModelName }}</span>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="10" height="10"><polyline points="6 9 12 15 18 9"/></svg>
            </button>
            <div v-if="showModelDropdown" class="model-dropdown">
              <div
                v-for="model in models"
                :key="model.id"
                class="model-dropdown-item"
                :class="{ active: model.id === selectedModelId }"
                @click="handleSelectModel(model.id)"
              >
                <div class="model-item-info">
                  <span class="model-label">{{ model.name || model.model_id || model.id }}</span>
                </div>
              </div>
            </div>
          </div>

          <!-- 发送/中止按钮 -->
          <button
            v-if="!streaming && !agentRunning"
            class="send-btn"
            :disabled="!canSend"
            @click="handleSend"
            :title="canSend ? '发送' : '请输入内容或添加附件后再发送'"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="12" y1="19" x2="12" y2="5"/>
              <polyline points="5 12 12 5 19 12"/>
            </svg>
          </button>
          <button v-else class="abort-btn" @click="$emit('abort')" title="中止">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue'
import type { ModelConfig } from '@/api/client'
import MessageItem from '@/components/chat/MessageItem.vue'
import HistoryPanel from '@/components/chat/HistoryPanel.vue'
import { useResizablePanel } from '@/composables/useResizablePanel'
import { useModelSelector } from '@/composables/useModelSelector'
import AgentTraceInline from '@/components/agent/AgentTraceInline.vue'
import { useAgentStore } from '@/stores/agent'
import { useSettingsStore } from '@/stores/settings'
import { useSkillStore } from '@/stores/skills'
import { useToastStore } from '@/stores/toast'
import type { ChatAttachment } from '@/api/client'

const settingsStore = useSettingsStore()
const skillStore = useSkillStore()
const toast = useToastStore()

// ── 消息接口（兼容 ModeMessage 和 MessageItem 的 ChatMessage） ──
interface DisplayMsg {
  id: string
  role: 'user' | 'assistant'
  content: string
  reasoning?: string
  streamingContent?: string
  streamingReasoning?: string
  duration?: number
  [key: string]: any
}

// ── 历史分组接口 ──
interface ConvGroup {
  label: string
  items: Array<{ id: string; title: string; updated_at: string }>
}

// ── Props ──
const props = defineProps<{
  mode: 'ssh'
  conversations: Array<{ id: string; title: string; updated_at: string }>
  currentConvId: string | null
  messages: DisplayMsg[]
  streaming: boolean
  selectedModelId: string
  models: ModelConfig[]
  groupedConversations: ConvGroup[]
  showAgentToggle?: boolean
  agentMode?: boolean
  showMessageActions?: boolean
  agentRunning?: boolean
  creating?: boolean
  placeholder?: string
  emptyTitle?: string
  emptyDesc?: string
  /** 上传处理函数：由父组件注入（返回上传成功的附件列表）；未注入则不显示上传入口 */
  uploadHandler?: (files: File[]) => Promise<ChatAttachment[]>
}>()

const emit = defineEmits<{
  close: []
  send: [content: string, skillIds: string[], attachments: ChatAttachment[]]
  'update:selectedModelId': [id: string]
  'update:agentMode': [val: boolean]
  'new-chat': []
  'switch-conversation': [id: string]
  'delete-conversation': [id: string]
  abort: []
}>()

// ── Refs ──
const messagesEl = ref<HTMLElement | null>(null)
const inputRef = ref<HTMLTextAreaElement | null>(null)
const inputText = ref('')
const showHistoryPanel = ref(false)

// ── 文件上传附件 ──
const fileInputRef = ref<HTMLInputElement | null>(null)
const pendingFiles = ref<ChatAttachment[]>([])
const uploading = ref(false)

function handleUploadClick() {
  fileInputRef.value?.click()
}

function formatFileSize(bytes?: number): string {
  if (!bytes || bytes <= 0) return ''
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`
}

async function handleFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  const files = input.files ? Array.from(input.files) : []
  input.value = '' // 允许重复选择同一文件
  if (files.length === 0 || !props.uploadHandler || uploading.value) return
  uploading.value = true
  try {
    const results = await props.uploadHandler(files)
    // 追加已上传成功的附件（失败的文件由 handler 内部 toast 提示）
    pendingFiles.value.push(...results)
  } catch (err: unknown) {
    // 防御：handler 出现非预期异常时给出提示，避免静默无反馈
    const msg = err instanceof Error ? err.message : String(err)
    toast.error(`上传失败：${msg}`)
    console.error('[SideChatPanel] upload failed:', err)
  } finally {
    uploading.value = false
  }
}

function removePendingFile(fileId: string) {
  pendingFiles.value = pendingFiles.value.filter(f => f.file_id !== fileId)
}

// ── / 斜杠技能菜单 ──
const selectedSkillIds = ref<string[]>([])
const showSkillMenu = ref(false)
const skillQuery = ref('')
const skillActiveIdx = ref(0)
let skillMenuStartPos = -1

/** 可用的技能列表 */
const modeSkills = computed(() => {
  return skillStore.enabledSkills
})

/** 根据查询串过滤技能 */
const filteredSkills = computed(() => {
  const q = skillQuery.value.toLowerCase().trim()
  const list = modeSkills.value
  if (!q) return list.slice(0, 8)
  return list
    .filter(s =>
      s.name.toLowerCase().includes(q) ||
      s.slug.toLowerCase().includes(q) ||
      (s.description || '').toLowerCase().includes(q),
    )
    .slice(0, 8)
})

/** 已选技能标签（用于 UI 展示） */
const selectedSkillTags = computed(() => {
  return selectedSkillIds.value
    .map(id => {
      const s = skillStore.enabledSkills.find(sk => sk.id === id)
      return s ? { id: s.id, name: s.name } : null
    })
    .filter(Boolean) as Array<{ id: string; name: string }>
})

/** 输入框 placeholder：根据是否已选技能显示提示 */
const inputPlaceholder = computed(() => {
  if (selectedSkillIds.value.length > 0) return '已附加技能，输入消息发送...'
  return props.placeholder || '输入消息，/ 调用技能...'
})

function onInput() {
  autoResize()
  detectSkillTrigger()
}

/** 检测 / 触发技能菜单 */
function detectSkillTrigger() {
  const el = inputRef.value
  if (!el) return
  const pos = el.selectionStart
  const text = inputText.value.slice(0, pos)
  const lastSlash = text.lastIndexOf('/')
  if (lastSlash === -1) { closeSkillMenu(); return }

  // 触发符前必须是空白或行首（避免路径、URL 误触发）
  if (lastSlash > 0) {
    const charBefore = text[lastSlash - 1]
    if (charBefore && !/\s/.test(charBefore)) { closeSkillMenu(); return }
  }

  // / 后到光标位置之间不能有空白
  const segment = text.slice(lastSlash + 1, pos)
  if (/\s/.test(segment)) { closeSkillMenu(); return }

  skillMenuStartPos = lastSlash
  skillQuery.value = segment
  skillActiveIdx.value = 0
  showSkillMenu.value = true
}

function closeSkillMenu() {
  showSkillMenu.value = false
  skillMenuStartPos = -1
  skillQuery.value = ''
  skillActiveIdx.value = 0
}

/** 选中技能：在输入框插入 @技能名 标记，并 push 到 selectedSkillIds */
function pickSkill(skill: { id: string; name: string }) {
  const el = inputRef.value
  const pos = el?.selectionStart ?? inputText.value.length
  const before = inputText.value.slice(0, skillMenuStartPos)
  const insert = `@${skill.name} `

  inputText.value = before + insert + inputText.value.slice(pos)
  if (!selectedSkillIds.value.includes(skill.id)) {
    selectedSkillIds.value.push(skill.id)
  }

  closeSkillMenu()
  requestAnimationFrame(() => {
    const newPos = (before + insert).length
    if (el) { el.focus(); el.setSelectionRange(newPos, newPos) }
    autoResize()
  })
}

function removeSkill(id: string) {
  selectedSkillIds.value = selectedSkillIds.value.filter(sid => sid !== id)
}

// ── Agent 执行轨迹 ──
const agentStore = useAgentStore()

function isLiveAgentRun(msg: DisplayMsg, idx: number): boolean {
  return !!props.agentRunning &&
    idx === props.messages.length - 1 &&
    msg.role === 'assistant' &&
    msg.id?.startsWith('temp-agent-')
}

// ── 面板拖拽 ──
const { panelWidth, startResize } = useResizablePanel()

// ── 模型选择器 ──
const { showModelDropdown, modelSelectorRef, currentModelName, selectModel } = useModelSelector({
  getSelectedId: () => props.selectedModelId,
  setSelectedId: (id) => emit('update:selectedModelId', id),
  models: () => props.models,
})

function handleSelectModel(id: string) {
  selectModel(id)
}

// ── 消息显示 ──
// 优化：仅对最后一条消息计算 live 状态，其余消息保持原始引用避免 O(n) 重渲染
const displayMessages = computed(() => {
  const lastIdx = props.messages.length - 1
  return props.messages.map((m, idx) => {
    if (idx === lastIdx && isLiveAgentRun(m, idx)) {
      return {
        ...m,
        // Agent 运行中：实时同步 agentStore 的 content/reasoning
        content: agentStore.content || m.content || m.streamingContent || '',
        streamingContent: undefined,
        reasoning: '',
        streamingReasoning: undefined,
      }
    }
    // 非活跃消息保持原始引用，避免不必要的重渲染
    return m
  })
})

// ── 滚动 ──
async function scrollToBottom() {
  if (!settingsStore.settings.chat.autoScroll) return
  await nextTick()
  if (messagesEl.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  }
}

watch(() => props.messages.length, () => {
  scrollToBottom()
})

// 监听流式内容变化：仅检查最后一条消息的 streamingContent 长度，避免 O(n) reduce 开销
watch(
  () => props.messages[props.messages.length - 1]?.streamingContent?.length,
  () => {
    if (props.streaming) scrollToBottom()
  }
)

// Agent 运行中 content/reasoning 通过 agentStore 实时更新，需单独监听以触发滚动
watch(() => agentStore.content, () => {
  if (agentStore.isRunning) scrollToBottom()
})

// reasoning 流式输出同样需要跟随滚动
watch(() => agentStore.reasoning, () => {
  if (agentStore.isRunning) scrollToBottom()
})

// ── 输入处理 ──
function handleKeydown(e: KeyboardEvent) {
  // 技能菜单激活时优先处理菜单导航
  if (showSkillMenu.value) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      skillActiveIdx.value = (skillActiveIdx.value + 1) % Math.max(filteredSkills.value.length, 1)
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      skillActiveIdx.value = (skillActiveIdx.value - 1 + Math.max(filteredSkills.value.length, 1)) % Math.max(filteredSkills.value.length, 1)
      return
    }
    if (e.key === 'Enter' && filteredSkills.value.length > 0) {
      e.preventDefault()
      pickSkill(filteredSkills.value[skillActiveIdx.value])
      return
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      closeSkillMenu()
      return
    }
    // Tab 也用于选择
    if (e.key === 'Tab' && filteredSkills.value.length > 0) {
      e.preventDefault()
      pickSkill(filteredSkills.value[skillActiveIdx.value])
      return
    }
  }
  // 发送快捷键：enter=Enter 发送(Shift+Enter 换行)，shiftEnter=Shift+Enter 发送(Enter 换行)
  if (e.key === 'Enter') {
    const wantShift = settingsStore.settings.chat.sendShortcut === 'shiftEnter'
    if (wantShift ? e.shiftKey : !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }
}

function autoResize() {
  const el = inputRef.value
  if (el) {
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 100) + 'px'
  }
}

/** 是否可发送：有输入文字或有待发送附件（与按钮 disabled 保持一致） */
const canSend = computed(() =>
  inputText.value.trim().length > 0 || pendingFiles.value.length > 0,
)

function handleSend() {
  const text = inputText.value.trim()
  if ((!text && pendingFiles.value.length === 0) || props.streaming || props.agentRunning) return
  // 关闭可能打开的菜单
  closeSkillMenu()
  // 复制 skillIds / attachments 后立即清空，避免下次发送残留
  const skillIds = [...selectedSkillIds.value]
  const attachments = [...pendingFiles.value]
  selectedSkillIds.value = []
  pendingFiles.value = []
  inputText.value = ''
  if (inputRef.value) inputRef.value.style.height = 'auto'
  emit('send', text, skillIds, attachments)
}

// ── 历史事件 ──
function handleSwitch(id: string) {
  emit('switch-conversation', id)
  showHistoryPanel.value = false
}

function handleDelete(id: string) {
  emit('delete-conversation', id)
}

// ── 暴露方法（供父组件调用） ──
function focusInput() {
  nextTick(() => inputRef.value?.focus())
}

defineExpose({ focusInput })
</script>

<style scoped>
.side-chat-panel {
  background: transparent;
  border-left: 1px solid var(--border-subtle, #2d2d30);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  overflow: hidden;
  font-family: var(--font-sans);
  position: relative;
}

/* Resize Handle */
.resize-handle {
  position: absolute;
  top: 0;
  left: 0;
  bottom: 0;
  width: 4px;
  cursor: col-resize;
  z-index: 5;
  transition: background 0.15s;
}

.resize-handle:hover,
.resize-handle:active {
  background: var(--accent-blue, #0078d4);
}

/* Header */
.panel-header {
  height: 34px;
  display: flex;
  align-items: center;
  padding: 0 4px;
  border-bottom: 1px solid var(--border-subtle, #2d2d30);
  flex-shrink: 0;
  gap: 8px;
}

.header-spacer {
  flex: 1;
}

.header-actions {
  display: flex;
  gap: 2px;
}

.icon-btn {
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  border-radius: 4px;
  transition: all 0.15s;
}

.icon-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.icon-btn:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.icon-btn.loading {
  pointer-events: none;
}

.icon-btn .spinner {
  animation: spin 0.8s linear infinite;
}

.icon-btn svg {
  width: 14px;
  height: 14px;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* Main Content */
.main-content {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  position: relative;
}

/* Empty State */
.empty-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 24px;
  gap: 12px;
}

.empty-icon-wrapper {
  width: 64px;
  height: 64px;
  background: var(--bg-hover);
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #818CF8;
  margin-bottom: 8px;
}

.empty-title {
  font-size: 18px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0;
}

.empty-desc {
  font-size: 13px;
  color: var(--text-secondary);
  margin: 0;
  text-align: center;
  line-height: 1.5;
}

/* Messages List */
.messages-list {
  padding: 12px 4px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

/* Input Section */
.input-section {
  border-top: 1px solid var(--border-subtle, #2d2d30);
  flex-shrink: 0;
}

.input-container {
  padding: 0 4px;
  border-bottom: 1px solid var(--border-subtle, #2d2d30);
  position: relative;
}

.chat-input {
  width: 100%;
  border: none;
  background: transparent;
  color: var(--text-primary);
  font-size: 13px;
  font-family: var(--font-sans);
  padding: 10px 0;
  resize: none;
  outline: none;
  min-height: 20px;
  max-height: 120px;
  line-height: 1.5;
  box-sizing: border-box;
}

.chat-input::placeholder {
  color: var(--text-tertiary);
  font-size: 12px;
  opacity: 0.7;
}

.chat-input:disabled {
  opacity: 0.5;
}

/* ── 已选技能标签 ── */
.skill-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding: 6px 0 2px;
}

.tag-skill {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  background: rgba(129, 140, 248, 0.12);
  color: #a5b4fc;
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
  max-width: 160px;
}

.tag-skill svg {
  flex-shrink: 0;
  color: #818cf8;
}

.tag-skill .tag-remove {
  display: flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: none;
  color: #a5b4fc;
  cursor: pointer;
  padding: 0;
  margin-left: 2px;
  opacity: 0.6;
}

.tag-skill .tag-remove:hover {
  opacity: 1;
}

/* ── 附件 chips（上传文件）── */
.pending-files {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 6px 4px 0;
}

.tag-file {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 220px;
  padding: 2px 6px;
  border-radius: 4px;
  background: var(--bg-hover);
  color: var(--text-secondary);
  font-size: 12px;
}

.tag-file svg {
  flex-shrink: 0;
  color: var(--text-tertiary);
}

.tag-file .file-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tag-file .file-size {
  flex-shrink: 0;
  color: var(--text-tertiary);
  font-size: 11px;
}

.hidden-file-input {
  display: none;
}

/* ── / 触发技能菜单 ── */
.skill-menu {
  position: absolute;
  bottom: 100%;
  left: 0;
  right: 0;
  margin-bottom: 4px;
  background: var(--bg-sidebar, #1a1a1a);
  border: 1px solid #3a3a3a;
  border-radius: 6px;
  box-shadow: 0 -4px 16px rgba(0, 0, 0, 0.5);
  z-index: 50;
  max-height: 280px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.skill-menu-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 6px 10px;
  border-bottom: 1px solid #2d2d30;
  font-size: 11px;
  color: #888;
  flex-shrink: 0;
}

.skill-menu-header .menu-title {
  color: #a5b4fc;
  font-weight: 500;
}

.skill-menu-header .menu-hint {
  font-size: 10px;
  color: #666;
}

.skill-menu-list {
  overflow-y: auto;
  padding: 4px;
}

.skill-menu-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 8px;
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.1s;
  gap: 8px;
}

.skill-menu-item:hover,
.skill-menu-item.active {
  background: rgba(129, 140, 248, 0.1);
}

.skill-item-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}

.skill-item-name {
  font-size: 12px;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.skill-item-desc {
  font-size: 10px;
  color: #888;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.skill-item-cat {
  font-size: 10px;
  color: #666;
  background: rgba(110, 118, 129, 0.15);
  padding: 1px 5px;
  border-radius: 3px;
  flex-shrink: 0;
}

.skill-menu-empty {
  padding: 12px;
  text-align: center;
  color: #666;
  font-size: 11px;
}

/* Input Toolbar */
.input-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 12px;
  gap: 8px;
}

.toolbar-left,
.toolbar-right {
  display: flex;
  align-items: center;
  gap: 6px;
}

/* Model Selector */
.model-selector {
  position: relative;
}

.model-selector-btn {
  display: flex;
  align-items: center;
  gap: 2px;
  height: 22px;
  background: transparent;
  border: none;
  color: var(--text-primary);
  font-size: 11px;
  padding: 0 4px;
  cursor: pointer;
  font-family: var(--font-sans);
  white-space: nowrap;
}

.model-selector-btn:hover {
  color: var(--text-primary);
}

.model-selector-btn .model-name {
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.model-selector-btn svg {
  width: 10px;
  height: 10px;
  flex-shrink: 0;
}

.model-dropdown {
  position: absolute;
  bottom: 100%;
  left: 50%;
  transform: translateX(-50%);
  margin-bottom: 4px;
  background: var(--bg-hover);
  border: 1px solid #3a3a3a;
  border-radius: 6px;
  padding: 4px;
  min-width: 180px;
  max-width: 260px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
  z-index: 30;
}

.model-dropdown-item {
  padding: 4px 8px;
  color: var(--text-primary);
  cursor: pointer;
  transition: background 0.1s;
  min-height: 32px;
  display: flex;
  align-items: center;
  border-radius: 4px;
}

.model-dropdown-item:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.model-dropdown-item.active {
  color: var(--text-primary);
  background: var(--accent-primary);
}

.model-item-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
  min-width: 0;
}

.model-dropdown-item .model-label {
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  letter-spacing: 0.2px;
}

.model-dropdown-item .model-id {
  font-size: 10px;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.model-dropdown-item:hover .model-id,
.model-dropdown-item.active .model-id {
  color: inherit;
  opacity: 0.7;
}

/* Send Button */
.send-btn {
  width: 26px;
  height: 26px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 4px;
  background: var(--bg-hover);
  color: var(--text-primary);
  cursor: pointer;
  transition: all 0.15s;
  flex-shrink: 0;
}

.send-btn:hover:not(:disabled) {
  background: var(--bg-active);
  color: var(--text-primary);
}

.send-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.send-btn svg {
  width: 14px;
  height: 14px;
}

.abort-btn {
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 4px;
  background: rgba(244, 135, 113, 0.15);
  color: #f48771;
  cursor: pointer;
  transition: all 0.15s;
  flex-shrink: 0;
}

.abort-btn:hover {
  background: rgba(244, 135, 113, 0.25);
}

.abort-btn svg {
  width: 12px;
  height: 12px;
}

/* ── Toolbar buttons (scoped inside SideChatPanel, not relying on parent) ── */
.toolbar-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 24px;
  padding: 0 8px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s;
}

.toolbar-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.toolbar-btn.active {
  background: rgba(99, 102, 241, 0.15);
  color: #818cf8;
}

.toolbar-btn.active:hover {
  background: rgba(99, 102, 241, 0.25);
}

.agent-label {
  font-size: 12px;
}

/* 启用自定义背景图时，让右侧面板透明 */
:global(.app.has-background) .side-chat-panel {
  background: transparent !important;
}
</style>
