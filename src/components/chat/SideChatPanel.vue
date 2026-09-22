<template>
  <div class="side-chat-panel" :style="{ width: panelWidth + 'px' }">
    <!-- 左侧拖拽手柄 -->
    <div class="resize-handle" @mousedown="startResize"></div>

    <!-- 顶部栏 -->
    <div class="panel-header">
      <slot name="header-prefix"></slot>
      <span class="panel-title">{{ panelTitle }}</span>
      <div class="header-spacer"></div>
      <div class="header-actions">
        <slot name="header-actions-extra"></slot>
        <button class="icon-btn" title="历史记录" aria-label="历史记录" @click="showHistoryPanel = !showHistoryPanel">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
        </button>
        <button class="icon-btn" :class="{ loading: props.creating }" :title="props.creating ? '创建中...' : '新建对话'" :aria-label="props.creating ? '创建中...' : '新建对话'" :disabled="props.creating" @click="$emit('new-chat')">
          <svg v-if="!props.creating" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          <svg v-else class="spinner" viewBox="0 0 24 24" fill="none" width="14" height="14"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" stroke-opacity="0.25"/><path d="M22 12a10 10 0 0 0-10-10" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg>
        </button>
        <button class="icon-btn" title="关闭面板" aria-label="关闭面板" @click="$emit('close')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
    </div>

    <!-- 主内容区 -->
    <div class="main-content" ref="messagesEl">
      <!-- 空状态（历史面板打开时隐藏：遮罩半透明会透出当前对话，且 display:none
           使 main-content 无可滚内容，滚轮不再穿透滚动背后消息列表） -->
      <div v-if="displayMessages.length === 0" v-show="!showHistoryPanel" class="empty-state">
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

      <!-- 消息列表（历史面板打开时隐藏，见上方空状态注释） -->
      <div v-else class="messages-list" role="log" aria-live="polite" v-show="!showHistoryPanel">
        <template v-for="(msg, idx) in displayMessages" :key="msg.id">
          <MessageItem
            :message="msg"
            :is-last="idx === props.messages.length - 1"
            :is-streaming="isLiveAgentRun(msg, idx)"
          >
            <template v-if="showMessageActions" #actions="{ content }">
              <slot name="message-actions" :content="content" :msg="msg"></slot>
            </template>
          </MessageItem>
        </template>

        <!-- Agent 面板插槽（运行告警/审批卡等，由具体助手面板注入） -->
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
        <!-- 上下文标签插槽（会话附加信息，可选） -->
        <slot name="input-prefix"></slot>

        <!-- 已附加能力（+ 号菜单选中项）—— 目前仅前端态，后端接入前不参与发送 -->
        <div v-if="activeCaps.length > 0" class="cap-tags">
          <span v-for="cap in activeCaps" :key="cap" class="tag-cap">
            <svg v-if="cap === 'goal'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="9" height="9">
              <circle cx="12" cy="12" r="8"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>
            </svg>
            <svg v-else-if="cap === 'plan'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="9" height="9">
              <path d="m3 7 2 2 4-4"/><path d="M13 6h8"/><path d="M13 12h8"/><path d="M13 18h8"/><path d="M3 16h4"/><path d="M3 20h4"/>
            </svg>
            <svg v-else-if="cap === 'skill'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="9" height="9">
              <path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>
            </svg>
            <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="9" height="9">
              <rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/>
              <rect x="3" y="14" width="7" height="7" rx="1.5"/><path d="M14 17.5h7M17.5 14v7"/>
            </svg>
            {{ CAP_LABELS[cap] }}
            <button class="tag-remove" :title="`移除${CAP_LABELS[cap]}`" :aria-label="`移除${CAP_LABELS[cap]}`" @click="pickCap(cap)">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="8" height="8">
                <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </span>
        </div>

        <!-- 已选技能标签（与 Work 模式 ChatInput 风格一致） -->
        <div v-if="selectedSkillTags.length > 0" class="skill-tags">
          <span v-for="tag in selectedSkillTags" :key="tag.id" class="tag-skill">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="9" height="9">
              <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>
            </svg>
            {{ tag.name }}
            <button class="tag-remove" @click="removeSkill(tag.id)" title="移除技能" aria-label="移除技能">
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
            <button class="tag-remove" @click="removePendingFile(att.file_id)" title="移除文件" aria-label="移除文件">
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
          aria-label="消息输入框"
          role="combobox"
          :aria-expanded="showSkillMenu || showPlusMenu"
          :aria-controls="showPlusMenu ? 'plus-menu' : 'skill-menu-list'"
          :aria-activedescendant="showPlusMenu ? plusActiveOptionId : showSkillMenu ? activeSkillOptionId : undefined"
          :maxlength="50000"
          :disabled="agentRunning"
          @keydown="handleKeydown"
          @input="onInput"
        ></textarea>

        <!-- / 触发技能菜单 -->
        <div v-if="showSkillMenu" class="skill-menu">
          <div class="skill-menu-header">
            <span class="menu-title">技能</span>
            <span class="menu-hint">↑↓ 选择 · Enter 确认 · Esc 关闭</span>
          </div>
          <div id="skill-menu-list" class="skill-menu-list" role="listbox" aria-label="技能">
            <div
              v-for="(item, i) in filteredSkills"
              :key="item.id"
              class="skill-menu-item"
              :class="{ active: i === skillActiveIdx }"
              role="option"
              :id="`skill-opt-${item.id}`"
              :aria-selected="i === skillActiveIdx"
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
          <!-- 能力入口（目标 / 计划 / 技能 / 插件），位于上传按钮左侧。
               工具栏贴面板底部，故菜单向上展开（与 / 技能菜单同层、同定位策略）。 -->
          <div class="plus-wrap">
            <button
              class="plus-btn"
              :disabled="agentRunning"
              :title="agentRunning ? '当前响应中，请稍后再试' : '添加'"
              :aria-label="agentRunning ? '当前响应中' : '添加'"
              aria-haspopup="menu"
              aria-controls="plus-menu"
              :aria-expanded="showPlusMenu"
              @click="togglePlusMenu"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="13" height="13">
                <line x1="12" y1="5" x2="12" y2="19"/>
                <line x1="5" y1="12" x2="19" y2="12"/>
              </svg>
            </button>

            <div v-if="showPlusMenu" id="plus-menu" class="plus-menu" role="menu" aria-label="添加">
              <button
                class="menu-item"
                :class="{ 'is-active': plusActiveIdx === 0, 'is-on': activeCaps.includes('goal') }"
                role="menuitemcheckbox"
                id="plus-opt-goal"
                :aria-checked="activeCaps.includes('goal')"
                @click="pickCap('goal')"
                @mouseenter="plusActiveIdx = 0"
              >
                <span class="mi-ico">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="13" height="13">
                    <circle cx="12" cy="12" r="8"/>
                    <path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>
                  </svg>
                </span>
                <span class="mi-name">目标</span>
                <span class="mi-desc">明确要达成什么</span>
              </button>

              <button
                class="menu-item"
                :class="{ 'is-active': plusActiveIdx === 1, 'is-on': activeCaps.includes('plan') }"
                role="menuitemcheckbox"
                id="plus-opt-plan"
                :aria-checked="activeCaps.includes('plan')"
                @click="pickCap('plan')"
                @mouseenter="plusActiveIdx = 1"
              >
                <span class="mi-ico">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="13" height="13">
                    <path d="m3 7 2 2 4-4"/><path d="M13 6h8"/><path d="M13 12h8"/><path d="M13 18h8"/>
                    <path d="M3 16h4"/><path d="M3 20h4"/>
                  </svg>
                </span>
                <span class="mi-name">计划</span>
                <span class="mi-desc">先出方案再执行</span>
              </button>

              <button
                class="menu-item"
                :class="{ 'is-active': plusActiveIdx === 2, 'is-on': activeCaps.includes('skill') }"
                role="menuitemcheckbox"
                id="plus-opt-skill"
                :aria-checked="activeCaps.includes('skill')"
                @click="pickCap('skill')"
                @mouseenter="plusActiveIdx = 2"
              >
                <span class="mi-ico">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="13" height="13">
                    <path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>
                  </svg>
                </span>
                <span class="mi-name">技能</span>
                <span class="mi-desc">调用已启用技能</span>
              </button>

              <button
                class="menu-item"
                :class="{ 'is-active': plusActiveIdx === 3, 'is-on': activeCaps.includes('plugin') }"
                role="menuitemcheckbox"
                id="plus-opt-plugin"
                :aria-checked="activeCaps.includes('plugin')"
                @click="pickCap('plugin')"
                @mouseenter="plusActiveIdx = 3"
              >
                <span class="mi-ico">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="13" height="13">
                    <rect x="3" y="3" width="7" height="7" rx="1.5"/>
                    <rect x="14" y="3" width="7" height="7" rx="1.5"/>
                    <rect x="3" y="14" width="7" height="7" rx="1.5"/>
                    <path d="M14 17.5h7M17.5 14v7"/>
                  </svg>
                </span>
                <span class="mi-name">插件</span>
                <span class="mi-desc">连接 MCP 与扩展</span>
              </button>
            </div>
          </div>

          <!-- 上传文件附件 -->
          <button
            v-if="uploadHandler"
            class="toolbar-btn upload-btn"
            :disabled="uploading || agentRunning"
            :title="uploading ? '上传中...' : agentRunning ? '当前响应中，请稍后再上传' : '上传文件（文本类，单文件 ≤ 10MB）'"
            :aria-label="uploading ? '上传中' : '上传文件'"
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
            :disabled="agentRunning"
            @change="handleFileChange"
          />
          <slot name="toolbar-left"></slot>
        </div>
        <div class="toolbar-right">
          <!-- 模型选择器（未配置模型时显示引导空态） -->
          <div
            v-if="models.length === 0"
            class="model-empty-hint"
            role="button"
            tabindex="0"
            title="尚未配置模型，点击去设置"
            @click="openSettings('models')"
            @keydown.enter="openSettings('models')"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
            <span>尚未配置模型</span>
            <span class="model-empty-hint-action">去设置</span>
          </div>
          <div v-else class="model-selector" ref="modelSelectorRef">
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
            v-if="!agentRunning"
            class="send-btn"
            :disabled="!canSend"
            @click="handleSend"
            :title="canSend ? '发送' : '请输入内容或添加附件后再发送'"
            aria-label="发送"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="12" y1="19" x2="12" y2="5"/>
              <polyline points="5 12 12 5 19 12"/>
            </svg>
          </button>
          <button v-else class="abort-btn" @click="$emit('abort')" title="中止" aria-label="中止">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import type { ModelConfig } from '@/api/client'
import type { ConvGroup } from '@/types/chat'
import MessageItem from '@/components/chat/MessageItem.vue'
import HistoryPanel from '@/components/chat/HistoryPanel.vue'
import { useResizablePanel } from '@/composables/useResizablePanel'
import { useModelSelector } from '@/composables/useModelSelector'
import { useSettings } from '@/composables/useSettings'
import { useAgentStore } from '@/stores/agent'
import { useSettingsStore } from '@/stores/settings'
import { useSkillStore } from '@/stores/skills'
import { useToastStore } from '@/stores/toast'
import type { ChatAttachment } from '@/api/client'

const settingsStore = useSettingsStore()
const skillStore = useSkillStore()
// 审查修复：loadBuiltinSkills 此前全仓零调用方 —— 内置技能扫描、后端同步
// （syncBuiltinToBackend → SkillRein 匹配）与远程 manual/auto 装载整条链路
// 悬空，斜杠技能菜单永远为空。面板创建时装载一次（重复挂载重扫，语义同刷新）。
void skillStore.loadBuiltinSkills()
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

// ── Props ──
const props = defineProps<{
  conversations: Array<{ id: string; title: string; updated_at: string }>
  currentConvId: string | null
  messages: DisplayMsg[]
  selectedModelId: string
  models: ModelConfig[]
  groupedConversations: ConvGroup[]
  showMessageActions?: boolean
  agentRunning?: boolean
  creating?: boolean
  placeholder?: string
  emptyTitle?: string
  emptyDesc?: string
  /** 面板头部标题（默认 "AI 助手"） */
  panelTitle?: string
  /** 上传处理函数：由父组件注入（返回上传成功的附件列表）；未注入则不显示上传入口 */
  uploadHandler?: (files: File[]) => Promise<ChatAttachment[]>
}>()

const emit = defineEmits<{
  close: []
  send: [content: string, skillIds: string[], attachments: ChatAttachment[]]
  'update:selectedModelId': [id: string]
  'new-chat': []
  'switch-conversation': [id: string]
  'delete-conversation': [id: string]
  abort: []
}>()

// ── 面板头部标题（未传时使用通用默认） ──
const panelTitle = computed(() => props.panelTitle || 'AI 助手')

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

// ── 能力入口 + 号菜单（目标 / 计划 / 技能 / 插件）──
// 说明：当前为纯前端实现 —— 选中项只在 UI 上体现（按钮开启态 + 输入框上方 chip），
// 不进入 emit('send') 载荷，后端逻辑未接入。后续接后端时改动点集中在 pickCap()。
type CapKey = 'goal' | 'plan' | 'skill' | 'plugin'
const CAP_LABELS: Record<CapKey, string> = { goal: '目标', plan: '计划', skill: '技能', plugin: '插件' }
const CAP_KEYS: CapKey[] = ['goal', 'plan', 'skill', 'plugin']

const showPlusMenu = ref(false)
const plusActiveIdx = ref(0)
const activeCaps = ref<CapKey[]>([])

/** aria-activedescendant 目标 id（与 / 技能菜单同一无障碍模式） */
const plusActiveOptionId = computed(() => {
  const key = CAP_KEYS[plusActiveIdx.value]
  return key ? `plus-opt-${key}` : undefined
})

function togglePlusMenu() {
  if (showPlusMenu.value) {
    closePlusMenu()
    return
  }
  if (props.agentRunning) return
  showPlusMenu.value = true
  plusActiveIdx.value = 0
  // 焦点留在输入框：键盘导航由 handleKeydown 统一处理（同 / 技能菜单的手感）
  nextTick(() => inputRef.value?.focus())
}

function closePlusMenu(refocus = false) {
  if (!showPlusMenu.value) return
  showPlusMenu.value = false
  plusActiveIdx.value = 0
  if (refocus) nextTick(() => inputRef.value?.focus())
}

/** 切换一项能力的选中态（再次点击 = 取消） */
function pickCap(key: CapKey) {
  activeCaps.value = activeCaps.value.includes(key)
    ? activeCaps.value.filter(c => c !== key)
    : [...activeCaps.value, key]
  closePlusMenu(true)
}

// ── / 斜杠技能菜单 ──
const selectedSkillIds = ref<string[]>([])
const showSkillMenu = ref(false)
const skillQuery = ref('')
const skillActiveIdx = ref(0)

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

/** aria-activedescendant 目标 id（C-review NIT：技能菜单无障碍） */
const activeSkillOptionId = computed(() => {
  const item = filteredSkills.value[skillActiveIdx.value]
  return item ? `skill-opt-${item.id}` : undefined
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

/** 触发菜单的 '/' 在输入框中的下标（选中技能后要抹掉整段 "/query"） */
let skillTriggerPos = -1

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

  skillQuery.value = segment
  skillActiveIdx.value = 0
  skillTriggerPos = lastSlash
  showSkillMenu.value = true
}

function closeSkillMenu() {
  showSkillMenu.value = false
  skillQuery.value = ''
  skillActiveIdx.value = 0
  skillTriggerPos = -1
}

// + 号菜单与 / 技能菜单互斥：两者浮在同一输入区，需要保证同时只开一个
watch(showPlusMenu, (v) => { if (v) closeSkillMenu() })
watch(showSkillMenu, (v) => { if (v) closePlusMenu() })

// 审查修复（P2）：点击菜单外区域关闭技能菜单 — 原实现只有 Esc/选中/失焦
// 能关，鼠标点输入区外（如消息列表）菜单残留，视觉像死菜单
function onDocClickOutsideSkillMenu(e: MouseEvent) {
  if (!showSkillMenu.value) return
  const target = e.target as HTMLElement | null
  // 菜单自身或输入框内的点击不关闭（菜单项点击走 pickSkill 自行关闭）
  if (target && target.closest?.('.skill-menu')) return
  if (inputRef.value?.contains(target)) return
  closeSkillMenu()
}
// 同理（+ 号菜单）：点菜单/按钮以外区域关闭。.plus-wrap 内含触发按钮与菜单，
// 按钮自行处理开合，故容器内的 mousedown 不在此关闭。
function onDocClickOutsidePlusMenu(e: MouseEvent) {
  if (!showPlusMenu.value) return
  const target = e.target as HTMLElement | null
  if (target && target.closest?.('.plus-wrap')) return
  closePlusMenu()
}
onMounted(() => {
  document.addEventListener('mousedown', onDocClickOutsideSkillMenu)
  document.addEventListener('mousedown', onDocClickOutsidePlusMenu)
})
onUnmounted(() => {
  document.removeEventListener('mousedown', onDocClickOutsideSkillMenu)
  document.removeEventListener('mousedown', onDocClickOutsidePlusMenu)
})

/** 选中技能：加入 selectedSkillIds（输入框上方以标签展示）。
 *  C-review NIT：原实现同时在输入框插入 "@技能名" 文本 — 文本与 skillIds
 *  双重注入可漂移（手动删文本技能仍附加 / 删标签文本残留在消息里）。
 *  现以 skillIds 为单一数据源，标签提供视觉反馈。 */
function pickSkill(skill: { id: string; name: string }) {
  const el = inputRef.value
  // 抹掉唤起菜单的 "/query" 片段：斜杠只是触发符，留在输入框里会被当成正文发出去。
  // （早先的实现用 "@技能名" 文本替换它，改为 skillIds 单一数据源时把替换一起删了）
  let caretPos = -1
  if (skillTriggerPos >= 0) {
    const end = el?.selectionStart ?? inputText.value.length
    inputText.value = inputText.value.slice(0, skillTriggerPos) + inputText.value.slice(end)
    caretPos = skillTriggerPos
  }
  if (!selectedSkillIds.value.includes(skill.id)) {
    selectedSkillIds.value.push(skill.id)
  }
  closeSkillMenu()
  requestAnimationFrame(() => {
    if (el && caretPos >= 0) {
      el.focus()
      el.setSelectionRange(caretPos, caretPos)
    } else {
      inputRef.value?.focus()
    }
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
const { openSettings } = useSettings()

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
        // C-review NIT：原实现 reasoning 强制空串、注释声称"外层 ThinkingBlock
        // 展示"，实际不存在该展示（运行中 reasoning 完全丢失）。现透传
        // agentStore.reasoning，由 MessageItem 的 ThinkingBlock 渲染。
        content: agentStore.content || m.content || m.streamingContent || '',
        streamingContent: undefined,
        reasoning: agentStore.reasoning || '',
        streamingReasoning: undefined,
        // 运行中的实时轨迹：不注入这一份，气泡里的内联轨迹要等到整轮跑完才出现 ——
        // 期间用户看不到任何工具调用进度（AgentTraceInline 的 isLive 分支也因此永远
        // 走不到）。lastRunId 必须清空：它是「查看轨迹」按钮的显示条件，而按钮拉的是
        // 后端已固化的 run，运行中点开只会拿到半截结果并被组件缓存住不再刷新。
        agentTrace: { ...agentStore.snapshot(), lastRunId: '' },
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
  // C-review M16 修复：IME 组合输入期间不拦截按键，
  // 否则中文输入法按 Enter 确认候选词会同时触发发送/技能选择
  if (e.isComposing || e.keyCode === 229) return
  // + 号菜单激活时优先处理菜单导航（与下方技能菜单同手感）
  if (showPlusMenu.value) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      plusActiveIdx.value = (plusActiveIdx.value + 1) % CAP_KEYS.length
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      plusActiveIdx.value = (plusActiveIdx.value - 1 + CAP_KEYS.length) % CAP_KEYS.length
      return
    }
    if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault()
      const key = CAP_KEYS[plusActiveIdx.value]
      if (key) pickCap(key)
      return
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      closePlusMenu(true)
      return
    }
  }
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

// M18：草稿快照 — 发送后立即清空（保留"发送即清空"体验），父层 send 失败时
// 由 restoreDraft() 回填，避免长文/附件在"后端未就绪/无模型/建会话失败"早退路径丢失
const lastDraft = ref<{ text: string; skillIds: string[]; attachments: ChatAttachment[]; caps: CapKey[] } | null>(null)

function handleSend() {
  const text = inputText.value.trim()
  if ((!text && pendingFiles.value.length === 0)) return
  // AI 运行中时给出明确反馈，不再静默吞掉
  if (props.agentRunning) {
    toast.info('AI 正在运行中，请等待完成')
    return
  }
  // 关闭可能打开的菜单
  closeSkillMenu()
  closePlusMenu()
  // 复制 skillIds / attachments / caps 后立即清空，避免下次发送残留
  const skillIds = [...selectedSkillIds.value]
  const attachments = [...pendingFiles.value]
  const caps = [...activeCaps.value]
  lastDraft.value = { text, skillIds, attachments, caps }
  selectedSkillIds.value = []
  pendingFiles.value = []
  activeCaps.value = []
  inputText.value = ''
  if (inputRef.value) inputRef.value.style.height = 'auto'
  emit('send', text, skillIds, attachments)
}

/** M18：父层发送被拒绝（早退）时调用，把草稿回填输入框供用户重试 */
function restoreDraft() {
  if (!lastDraft.value) return
  const { text, skillIds, attachments, caps } = lastDraft.value
  lastDraft.value = null
  inputText.value = text
  selectedSkillIds.value = skillIds
  pendingFiles.value = attachments
  activeCaps.value = caps
  nextTick(() => {
    autoResize()
    inputRef.value?.focus()
  })
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

/** 预填输入框草稿（「问 AI」等外部入口注入问题草稿，用户可修改后发送） */
function prefill(text: string) {
  if (!text) return
  inputText.value = text
  nextTick(() => {
    autoResize()
    inputRef.value?.focus()
  })
}

defineExpose({ focusInput, restoreDraft, prefill })
</script>

<style scoped>
.side-chat-panel {
  background: transparent;
  border-left: 1px solid var(--border-subtle);
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
  background: var(--accent-primary);
}

/* Header */
.panel-header {
  height: 34px;
  display: flex;
  align-items: center;
  padding: 0 4px;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
  gap: 8px;
}

.header-spacer {
  flex: 1;
}

.panel-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
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
  color: var(--accent-indigo);
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
  padding: 12px 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

/* Input Section */
.input-section {
  border-top: 1px solid var(--border-subtle);
  flex-shrink: 0;
}

.input-container {
  padding: 0 4px;
  border-bottom: 1px solid var(--border-subtle);
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

/* focus 指示环已移除：输入框自带 outline:none，focus-visible 的彩色
   outline 会在聚焦时画出明显的颜色框，与无边框输入区风格不符 */

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
  background: var(--accent-indigo-bg);
  color: var(--accent-indigo-soft);
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
  max-width: 160px;
}

.tag-skill svg {
  flex-shrink: 0;
  color: var(--accent-indigo);
}

.tag-skill .tag-remove {
  display: flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: none;
  color: var(--accent-indigo-soft);
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

/* ── 能力入口 + 号（工具栏最左，上传按钮左侧）── */
.plus-wrap {
  position: relative;
  display: flex;
}

.plus-btn {
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}

.plus-btn:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.plus-btn:focus-visible {
  outline: 2px solid var(--accent-primary);
  outline-offset: 1px;
}

.plus-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 展开态：启用色底，+ 号旋转 45° 变 ×（同一按钮闭合，不另设关闭动作） */
.plus-btn[aria-expanded="true"] {
  background: var(--accent-indigo-bg);
  color: var(--accent-indigo);
}

.plus-btn svg {
  transition: transform 0.18s ease;
}

.plus-btn[aria-expanded="true"] svg {
  transform: rotate(45deg);
}

/* 向上展开：工具栏贴面板底部，向下会溢出面板；
   视觉与层级对齐既有 .skill-menu（--bg-elevated-solid + --border-default + z-index 50） */
.plus-menu {
  position: absolute;
  bottom: calc(100% + 6px);
  left: 0;
  width: 208px;
  padding: 4px;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: 6px;
  box-shadow: 0 -4px 16px rgba(0, 0, 0, 0.5);
  z-index: 50;
  transform-origin: bottom left;
  animation: plus-menu-in 0.14s ease-out;
}

@keyframes plus-menu-in {
  from { opacity: 0; transform: translateY(4px) scale(0.98); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}

.plus-menu .menu-item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  height: 30px;
  padding: 0 8px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--text-primary);
  font-family: var(--font-sans);
  font-size: 12px;
  text-align: left;
  cursor: pointer;
  transition: background 0.1s;
}

.plus-menu .menu-item:hover,
.plus-menu .menu-item.is-active {
  background: var(--bg-hover);
}

.plus-menu .menu-item:focus-visible {
  outline: 2px solid var(--accent-primary);
  outline-offset: -2px;
}

.plus-menu .mi-ico {
  flex-shrink: 0;
  width: 13px;
  height: 13px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-secondary);
}

.plus-menu .menu-item:hover .mi-ico,
.plus-menu .menu-item.is-active .mi-ico {
  color: var(--accent-indigo);
}

.plus-menu .mi-name {
  flex-shrink: 0;
  font-size: 12px;
  color: var(--text-primary);
  white-space: nowrap;
}

.plus-menu .mi-desc {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 已选中项：图标与说明转为启用色（底部另有 chip 作二次反馈） */
.plus-menu .menu-item.is-on .mi-ico,
.plus-menu .menu-item.is-on .mi-desc {
  color: var(--accent-indigo);
}

@media (prefers-reduced-motion: reduce) {
  .plus-menu { animation: none; }
  .plus-btn svg { transition: none; }
}

/* ── 已附加能力 chip（+ 号菜单选中项）── */
.cap-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding: 6px 0 2px;
}

.tag-cap {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  background: var(--accent-indigo-bg);
  color: var(--accent-indigo-soft);
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
}

.tag-cap svg {
  flex-shrink: 0;
  color: var(--accent-indigo);
}

.tag-cap .tag-remove {
  display: flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: none;
  color: var(--accent-indigo-soft);
  cursor: pointer;
  padding: 0;
  margin-left: 2px;
  opacity: 0.6;
}

.tag-cap .tag-remove:hover {
  opacity: 1;
}

/* ── / 触发技能菜单 ── */
.skill-menu {
  position: absolute;
  bottom: 100%;
  left: 0;
  right: 0;
  margin-bottom: 4px;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
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
  border-bottom: 1px solid var(--border-subtle);
  font-size: 11px;
  color: var(--text-tertiary);
  flex-shrink: 0;
}

.skill-menu-header .menu-title {
  color: var(--accent-indigo-soft);
  font-weight: 500;
}

.skill-menu-header .menu-hint {
  font-size: 11px;
  color: var(--text-tertiary);
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
  background: var(--accent-indigo-hover);
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
  font-size: 11px;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.skill-item-cat {
  font-size: 11px;
  color: var(--text-tertiary);
  background: var(--bg-hover);
  padding: 1px 5px;
  border-radius: 3px;
  flex-shrink: 0;
}

.skill-menu-empty {
  padding: 12px;
  text-align: center;
  color: var(--text-tertiary);
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
.model-empty-hint {
  display: flex;
  align-items: center;
  gap: 4px;
  height: 22px;
  padding: 0 8px;
  border: 1px dashed #4d4d50;
  border-radius: 6px;
  color: var(--text-secondary);
  font-size: 11px;
  cursor: pointer;
  white-space: nowrap;
  transition: color 0.15s, border-color 0.15s;
}
.model-empty-hint:hover,
.model-empty-hint:focus-visible {
  color: var(--accent-primary);
  border-color: var(--accent-primary);
  outline: none;
}
.model-empty-hint-action {
  color: var(--accent-primary);
  font-weight: 500;
}

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
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
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
  background: color-mix(in srgb, var(--accent-red) 15%, transparent);
  color: var(--accent-red);
  cursor: pointer;
  transition: all 0.15s;
  flex-shrink: 0;
}

.abort-btn:hover {
  background: color-mix(in srgb, var(--accent-red) 25%, transparent);
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
  background: var(--accent-indigo-bg);
  color: var(--accent-indigo);
}

.toolbar-btn.active:hover {
  background: var(--accent-indigo-hover);
}

/* 启用自定义背景图时，让右侧面板透明 */
:global(.app.has-background) .side-chat-panel {
  background: transparent !important;
}
</style>
