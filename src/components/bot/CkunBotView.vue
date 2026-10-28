<template>
  <section class="bot-view">
    <header class="bot-header">
      <div class="brand"><h1>cKunBot</h1></div>
      <nav class="bot-tabs" aria-label="cKunBot 功能">
        <button v-for="item in tabs" :key="item.id" :class="{ active: tab === item.id }" @click="tab = item.id">{{ item.label }}</button>
      </nav>
    </header>

    <div v-if="tab === 'chat'" class="chat-layout">
      <aside class="chat-sidebar">
        <button class="btn new-chat" :disabled="!store.activePersonaId || store.streaming" @click="createChat">新建会话</button>
        <div class="history-label">陪聊记录</div>
        <div class="conversation-list">
          <div v-for="c in store.filteredConversations" :key="c.id" class="conversation" :class="{ active: c.id === store.activeConversationId }">
            <button class="conversation-main" @click="store.selectConversation(c.id)">
              <span>{{ c.title }}</span><small>{{ formatLocalDateTime(c.updated_at) }}</small>
            </button>
            <button class="icon-button" title="删除会话" aria-label="删除会话" @click="removeConversation(c.id)">×</button>
          </div>
        </div>
      </aside>

      <div class="chat-main">
        <div v-if="store.activeConversation" class="chat-titlebar">
          <span class="persona-avatar"><PersonaAvatar :avatar="store.activePersona?.avatar" /></span>
          <div><strong>{{ store.activePersona?.name || 'cKunBot' }}</strong><small>只聊此刻 · 不调用工具</small></div>
        </div>
        <div ref="messageListRef" class="bot-messages">
          <div v-if="!store.activeConversationId" class="chat-empty">
            <h2>今天想聊点什么？</h2>
            <p>到「人格」页挑一位，新建一段只属于你们的会话。</p>
          </div>
          <template v-else>
            <div v-if="!store.messages.length && !store.streaming" class="greeting">
              {{ store.activePersona?.greeting || `你好，我是${store.activePersona?.name || 'cKunBot'}。` }}
            </div>
            <div v-for="m in store.messages" :key="m.id" class="bot-message" :class="m.role">
              <span v-if="m.role !== 'user'" class="msg-avatar" aria-hidden="true"><PersonaAvatar :avatar="store.activePersona?.avatar" /></span>
              <div class="message-content"><span class="sr-only">{{ m.role === 'user' ? '你' : (store.activePersona?.name || 'Bot') }}：</span>{{ m.content }}</div>
            </div>
            <div v-if="store.streaming" class="bot-message assistant">
              <span class="msg-avatar" aria-hidden="true"><PersonaAvatar :avatar="store.activePersona?.avatar" /></span>
              <div class="message-content">
                <template v-if="store.streamText">{{ store.streamText }}</template>
                <span v-else class="typing-dots" role="img" aria-label="正在输入"><i></i><i></i><i></i></span>
              </div>
            </div>
          </template>
        </div>
        <div v-if="store.error" class="chat-error">
          <span>{{ store.error }}</span>
          <button v-if="store.lastFailedText" class="btn btn-sm" @click="retry">重试</button>
        </div>
        <form v-if="store.activeConversationId" class="chat-composer" @submit.prevent="send">
          <textarea
            ref="composerRef"
            v-model="draft"
            class="composer-input"
            :class="{ 'with-count': nearLimit }"
            rows="1"
            maxlength="20000"
            placeholder="说点什么…"
            aria-label="聊天内容"
            @input="growComposer"
            @keydown="onComposerKeydown"
          />
          <span v-if="nearLimit" class="composer-count" :class="{ full: draft.length >= MAX_DRAFT }">{{ draft.length }} / {{ MAX_DRAFT }}</span>
          <button
            v-if="store.streaming"
            type="button"
            class="composer-send stop"
            title="停止生成"
            aria-label="停止生成"
            @click="store.stopGenerating()"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="1" fill="currentColor" stroke="none"/></svg>
          </button>
          <button v-else type="submit" class="composer-send" title="发送" aria-label="发送" :disabled="!draft.trim()">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>
          </button>
        </form>
      </div>
    </div>

    <div v-else-if="tab === 'personas'" class="persona-page">
      <div class="section-heading"><div><h2>人格</h2><p>每个会话保留创建时的人格快照，之后修改不会改写旧回忆。</p></div><button class="btn btn-primary" @click="openPersona()">新增人格</button></div>
      <div class="persona-grid">
        <article v-for="p in store.personas" :key="p.id" class="persona-card">
          <div class="persona-card-head"><span class="persona-large-avatar"><PersonaAvatar :avatar="p.avatar" /></span><div><h3>{{ p.name }}</h3><span>{{ modelName(p.model_id) }}</span></div></div>
          <p class="personality">{{ p.personality || '还没有写下性格描述。' }}</p>
          <p v-if="p.greeting" class="persona-greeting">“{{ p.greeting }}”</p>
          <div class="persona-actions">
            <button class="btn btn-sm btn-primary" @click="chatWith(p.id)">用此人格聊天</button>
            <button class="btn btn-sm" @click="openPersona(p)">编辑</button>
            <button class="btn btn-sm" @click="copyPersona(p.id)">复制</button>
            <button class="btn btn-sm btn-danger" @click="removePersona(p)">删除</button>
          </div>
        </article>
      </div>
    </div>

    <div v-else class="qq-page">
      <div class="section-heading"><div><h2>QQ 接入</h2><p>使用 QQ 官方机器人 WebSocket，应用退出后机器人随之下线。</p></div><span class="qq-status" :class="qqStatus.state">{{ qqStateText }}</span></div>
      <div class="qq-grid">
        <form class="qq-form" @submit.prevent="saveAndConnect">
          <label class="field"><span>AppID</span><input v-model.trim="qqForm.app_id" maxlength="100" autocomplete="off" placeholder="QQ 开放平台的机器人 AppID" /></label>
          <label class="field"><span>AppSecret</span><input v-model="qqForm.app_secret" type="password" maxlength="500" autocomplete="new-password" :placeholder="qqConfig.has_secret ? '已保存；留空表示不修改' : '首次接入必填'" /></label>
          <label class="field"><span>当前人格</span><select v-model="qqForm.persona_id" :disabled="qqStatus.state !== 'offline'"><option value="" disabled>请选择人格</option><option v-for="p in store.personas" :key="p.id" :value="p.id">{{ p.name }}</option></select></label>
          <p class="form-note">群聊只响应 @机器人，私聊直接回复。切换人格后，QQ 会话会开启新的上下文。</p>
          <div v-if="qqError" class="form-error">{{ qqError }}</div>
          <div class="qq-actions"><button type="submit" class="btn btn-primary" :disabled="qqBusy || !qqForm.app_id || !qqForm.persona_id">{{ qqBusy ? '处理中…' : '保存并连接' }}</button><button type="button" class="btn" :disabled="qqBusy || qqStatus.state === 'offline'" @click="disconnectQq">断开</button></div>
        </form>
        <aside class="qq-info">
          <h3>连接状态</h3>
          <dl><div><dt>运行状态</dt><dd>{{ qqStatus.detail || qqStateText }}</dd></div><div><dt>凭据</dt><dd>{{ qqConfig.has_secret ? 'Secret 已加密保存' : '尚未配置 Secret' }}</dd></div><div><dt>会话上下文</dt><dd>{{ qqConfig.context_id ? qqConfig.context_id.slice(0, 8) : '保存后生成' }}</dd></div></dl>
          <div v-if="qqStatus.detail" class="qq-detail"><strong>最近错误</strong><p>{{ qqStatus.detail }}</p></div>
          <p class="privacy-note">AppSecret 只写入本地加密存储，配置接口不会回显。</p>
        </aside>
      </div>
      <QqRecords :personas="store.personas" />
    </div>

    <BaseModal v-if="personaEditor" :title="editingPersonaId ? '编辑人格' : '新增人格'" @close="personaEditor = false">
      <div class="persona-form">
        <label class="field"><span>名称</span><input v-model.trim="personaForm.name" maxlength="50" placeholder="例如：晚风" /></label>
        <div class="field">
          <span>头像</span>
          <div class="avatar-picker">
            <span class="avatar-preview"><PersonaAvatar :avatar="personaForm.avatar" /></span>
            <div class="avatar-picker-body">
              <div class="avatar-picker-actions">
                <button type="button" class="btn" :disabled="avatarBusy" @click="avatarInput?.click()">{{ avatarBusy ? '处理中…' : '上传图片' }}</button>
              </div>
              <small>PNG / JPG / WebP，自动裁成方形；上传后才能保存</small>
            </div>
            <input ref="avatarInput" class="avatar-file" type="file" accept="image/png,image/jpeg,image/webp,image/gif" @change="pickAvatar" />
          </div>
          <div v-if="avatarError" class="form-error">{{ avatarError }}</div>
        </div>
        <label class="field"><span>性格与说话方式</span><textarea v-model="personaForm.personality" rows="4" maxlength="5000" placeholder="真诚、温和，回答简短，偶尔有一点幽默…" /></label>
        <label class="field"><span>背景设定</span><textarea v-model="personaForm.background" rows="4" maxlength="10000" placeholder="它是谁，你们是什么关系，有哪些共同经历…" /></label>
        <label class="field"><span>开场白</span><textarea v-model="personaForm.greeting" rows="2" maxlength="2000" placeholder="新会话里它对你说的第一句话" /></label>
        <label class="field"><span>聊天模型</span><select v-model="personaForm.model_id"><option :value="null">跟随全局默认模型</option><option v-for="m in models.models" :key="m.id" :value="m.id">{{ m.name }}{{ m.model_id ? `（${m.model_id}）` : '' }}</option></select></label>
        <div v-if="personaError" class="form-error">{{ personaError }}</div>
      </div>
      <template #actions><button class="btn" @click="personaEditor = false">取消</button><button class="btn btn-primary" :disabled="personaSaving || !personaForm.name || !isImageAvatar(personaForm.avatar)" @click="savePersona">{{ personaSaving ? '保存中…' : '保存' }}</button></template>
    </BaseModal>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import BaseModal from '@/components/common/BaseModal.vue'
import PersonaAvatar from './PersonaAvatar.vue'
import QqRecords from './QqRecords.vue'
import { isImageAvatar, readAvatarFile } from '@/utils/persona-avatar'
import { formatLocalDateTime } from '@/utils/datetime'
import { botApi, type BotPersona, type BotQqConfig, type BotQqStatus } from '@/api/bot'
import { toFriendlyMessage } from '@/api/base'
import { useBotStore } from '@/stores/bot'
import { useModelsStore } from '@/stores/models'
import { useToastStore } from '@/stores/toast'
import { useConfirm } from '@/composables/useConfirm'

const tabs = [{ id: 'chat', label: '陪聊' }, { id: 'personas', label: '人格' }, { id: 'qq', label: 'QQ 接入' }] as const
const tab = ref<(typeof tabs)[number]['id']>('chat')
const store = useBotStore()
const models = useModelsStore()
const toast = useToastStore()
const { confirm } = useConfirm()
const draft = ref('')
const messageListRef = ref<HTMLElement | null>(null)
const composerRef = ref<HTMLTextAreaElement | null>(null)
const MAX_DRAFT = 20000
const nearLimit = computed(() => draft.value.length > MAX_DRAFT - 1000)

function growComposer() {
  const el = composerRef.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${el.scrollHeight}px`
}
/** 中文输入法确认候选词时也会派发 Enter；不拦 isComposing 会把半句消息发出去。 */
function onComposerKeydown(e: KeyboardEvent) {
  if (e.key !== 'Enter' || e.shiftKey || e.isComposing) return
  e.preventDefault()
  void send()
}
watch(draft, () => { void nextTick(growComposer) })

async function createChat() {
  try { await store.createConversation() } catch (e) { toast.error(toFriendlyMessage(e, '新建会话失败')) }
}
async function send() {
  const text = draft.value
  if (!text.trim() || store.streaming) return
  draft.value = ''
  await store.sendMessage(text)
}
async function retry() {
  const text = store.lastFailedText
  if (text) await store.sendMessage(text)
}
async function removeConversation(id: string) {
  if (!await confirm({ message: '删除这段陪聊记录？删除后无法恢复。', danger: true })) return
  try { await store.deleteConversation(id) } catch (e) { toast.error(toFriendlyMessage(e, '删除失败')) }
}
watch(() => [store.messages.length, store.streamText], async () => {
  await nextTick()
  if (messageListRef.value) messageListRef.value.scrollTop = messageListRef.value.scrollHeight
})

const personaEditor = ref(false)
const editingPersonaId = ref('')
const personaSaving = ref(false)
const personaError = ref('')
const avatarInput = ref<HTMLInputElement | null>(null)
const avatarBusy = ref(false)
const avatarError = ref('')
const personaForm = reactive({ name: '', avatar: '', personality: '', background: '', greeting: '', model_id: null as string | null })
function openPersona(p?: BotPersona) {
  editingPersonaId.value = p?.id || ''
  Object.assign(personaForm, p ? { name: p.name, avatar: p.avatar, personality: p.personality, background: p.background, greeting: p.greeting, model_id: p.model_id } : { name: '', avatar: '', personality: '', background: '', greeting: '', model_id: null })
  personaError.value = ''
  avatarError.value = ''
  personaEditor.value = true
}
async function pickAvatar(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  avatarBusy.value = true
  avatarError.value = ''
  try { personaForm.avatar = await readAvatarFile(file) }
  catch (e) { avatarError.value = toFriendlyMessage(e, '头像处理失败') }
  finally { avatarBusy.value = false }
}
async function savePersona() {
  // 头像只收上传图片：旧数据里的表情/文本在编辑保存时被清理，不再回写
  if (!isImageAvatar(personaForm.avatar)) personaForm.avatar = ''
  personaSaving.value = true
  personaError.value = ''
  try {
    if (editingPersonaId.value) await botApi.updatePersona(editingPersonaId.value, { ...personaForm })
    else await botApi.createPersona({ ...personaForm })
    personaEditor.value = false
    await store.init()
  } catch (e) { personaError.value = toFriendlyMessage(e, '人格保存失败') } finally { personaSaving.value = false }
}
async function copyPersona(id: string) {
  try { await botApi.copyPersona(id); await store.init(); toast.success('已复制人格') } catch (e) { toast.error(toFriendlyMessage(e, '复制失败')) }
}
async function removePersona(p: BotPersona) {
  if (!await confirm({ message: `删除人格「${p.name}」？已有会话会保留人格快照和聊天记录。`, danger: true })) return
  try { await botApi.deletePersona(p.id); await store.init() } catch (e) { toast.error(toFriendlyMessage(e, '删除失败')) }
}
async function chatWith(id: string) {
  await store.selectPersona(id)
  if (!store.activeConversationId) await store.createConversation(id)
  tab.value = 'chat'
}
function modelName(id: string | null) { return id ? models.models.find(m => m.id === id)?.name || '指定模型' : '默认模型' }

const emptyQqConfig: BotQqConfig = { app_id: '', has_secret: false, persona_id: '', context_id: '' }
const qqConfig = ref<BotQqConfig>({ ...emptyQqConfig })
const qqStatus = ref<BotQqStatus>({ state: 'offline', detail: '' })
const qqForm = reactive({ app_id: '', app_secret: '', persona_id: '' })
const qqBusy = ref(false)
const qqError = ref('')
const qqStateText = computed(() => ({ offline: '离线', connecting: '连接中', online: '在线', reconnecting: '重连中', error: '连接异常' })[qqStatus.value.state])
async function refreshQq(populateForm = false) {
  try {
    const [config, status] = await Promise.all([botApi.getQqConfig(), botApi.getQqStatus()])
    qqConfig.value = config
    qqStatus.value = status
    if (populateForm) {
      qqForm.app_id = config.app_id
      qqForm.persona_id = config.persona_id || store.activePersonaId || store.personas[0]?.id || ''
    }
  } catch (e) { qqError.value = toFriendlyMessage(e, 'QQ 状态读取失败') }
}
async function saveAndConnect() {
  qqBusy.value = true
  qqError.value = ''
  try {
    qqConfig.value = await botApi.saveQqConfig({ app_id: qqForm.app_id, ...(qqForm.app_secret ? { app_secret: qqForm.app_secret } : {}), persona_id: qqForm.persona_id })
    qqForm.app_secret = ''
    qqStatus.value = await botApi.connectQq()
  } catch (e) { qqError.value = toFriendlyMessage(e, 'QQ 连接失败'); await refreshQq() } finally { qqBusy.value = false }
}
async function disconnectQq() {
  qqBusy.value = true
  qqError.value = ''
  try { qqStatus.value = await botApi.disconnectQq() } catch (e) { qqError.value = toFriendlyMessage(e, '断开失败') } finally { qqBusy.value = false }
}
let qqTimer: ReturnType<typeof setInterval> | undefined
onMounted(async () => {
  await Promise.all([store.init(), models.fetchModels()])
  await refreshQq(true)
  qqTimer = setInterval(() => { if (tab.value === 'qq' && !qqBusy.value) void refreshQq() }, 5000)
})
onUnmounted(() => { if (qqTimer) clearInterval(qqTimer); store.stopGenerating() })
</script>

<style scoped>
.bot-view { --bubble-radius: calc(var(--radius-lg) * 2); height: 100%; display: flex; flex-direction: column; background: var(--canvas-bg); color: var(--text-primary); overflow: hidden; }
.bot-header { flex: 0 0 auto; display: flex; align-items: center; justify-content: space-between; gap: var(--spacing-16); padding: 20px 28px var(--spacing-12); border-bottom: 1px solid var(--border-subtle); }
.brand { min-width: 0; }
h1, h2, h3, p { margin: 0; } h1 { font-size: 1.6923rem; letter-spacing: -.5px; } .section-heading p { margin-top: 5px; color: var(--text-secondary); font-size: 0.9231rem; }
.bot-tabs { display: flex; gap: var(--spacing-2); padding: 3px; border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); background: var(--bg-input); }
.bot-tabs button { border: 0; border-radius: var(--radius-md); background: transparent; color: var(--text-secondary); padding: 6px 16px; cursor: pointer; transition: color var(--transition-fast), background var(--transition-fast); }
.bot-tabs button:hover { color: var(--text-primary); }
.bot-tabs button.active { background: var(--bg-elevated); color: var(--text-primary); box-shadow: var(--shadow-sm); }
.chat-layout { flex: 1; min-height: 0; display: grid; grid-template-columns: 232px minmax(0, 1fr); }
.chat-sidebar { position: relative; min-height: 0; display: flex; flex-direction: column; padding: var(--spacing-16) var(--spacing-12); border-right: 1px solid var(--border-subtle); overflow: hidden; }
.chat-sidebar::before { content: ''; position: absolute; inset: 0 0 auto 0; height: 200px; background: radial-gradient(72% 100% at 50% 0%, color-mix(in srgb, var(--accent-primary) 10%, transparent), transparent 72%); pointer-events: none; }
.chat-sidebar > * { position: relative; }
.field { display: flex; flex-direction: column; gap: 7px; color: var(--text-secondary); font-size: 0.9231rem; }
.field input, .field select, .field textarea { width: 100%; box-sizing: border-box; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-base); color: var(--text-primary); padding: 9px var(--spacing-10); font: inherit; resize: vertical; }
.field input:focus, .field select:focus, .field textarea:focus { border-color: var(--accent-primary); outline: none; }
.new-chat { width: 100%; height: 30px; margin: var(--spacing-10) 0 var(--spacing-16); justify-content: center; border-color: transparent; border-radius: var(--radius-pill); background: color-mix(in srgb, var(--accent-primary) 12%, transparent); color: var(--accent-text); font-size: 0.9231rem; }
.new-chat:hover:not(:disabled) { border-color: transparent; background: color-mix(in srgb, var(--accent-primary) 20%, transparent); color: var(--accent-text); }
.history-label { padding: 0 var(--spacing-6) var(--spacing-8); color: var(--text-tertiary); font-size: 0.8462rem; }
.conversation-list { overflow-y: auto; min-height: 0; display: flex; flex-direction: column; gap: var(--spacing-2); }
.conversation { display: flex; align-items: center; border-radius: var(--radius-xl); transition: background var(--transition-fast), box-shadow var(--transition-fast); }
.conversation:hover { background: var(--bg-hover); }
.conversation.active { background: var(--bg-elevated); box-shadow: var(--shadow-sm); }
.conversation-main { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: flex-start; gap: 3px; border: 0; background: transparent; color: var(--text-primary); padding: 9px var(--spacing-12); cursor: pointer; }
.conversation-main span { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; } .conversation-main small { color: var(--text-tertiary); font-size: 0.7692rem; }
.icon-button { flex: 0 0 auto; border: 0; background: transparent; color: var(--text-tertiary); cursor: pointer; padding: 5px var(--spacing-8); font-size: 1.3077rem; opacity: 0; transition: opacity var(--transition-fast), color var(--transition-fast); } .conversation:hover .icon-button, .conversation:focus-within .icon-button { opacity: 1; } .icon-button:hover { color: var(--danger-text); }
@media (hover: none) { .icon-button { opacity: 1; } }
.chat-main { position: relative; min-width: 0; min-height: 0; display: flex; flex-direction: column; }
.chat-main::before { content: ''; position: absolute; inset: 0 0 auto 0; height: 240px; background: radial-gradient(78% 100% at 50% 0%, color-mix(in srgb, var(--accent-primary) 14%, transparent), transparent 70%); pointer-events: none; }
.chat-main > * { position: relative; }
.chat-titlebar { display: flex; align-items: center; gap: var(--spacing-10); padding: var(--spacing-12) var(--spacing-24) var(--spacing-8); }
.chat-titlebar .persona-avatar { width: 32px; height: 32px; flex: 0 0 32px; display: grid; place-items: center; border-radius: 50%; background: color-mix(in srgb, var(--accent-primary) 14%, transparent); font-size: 1.1538rem; box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent-primary) 8%, transparent); }
.chat-titlebar div { display: flex; flex-direction: column; gap: var(--spacing-2); } .chat-titlebar strong { font-size: 1rem; } .chat-titlebar small { color: var(--text-tertiary); font-size: 0.7692rem; }
.bot-messages { flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; padding: var(--spacing-10) max(var(--spacing-24), calc((100% - 760px) / 2)) var(--spacing-8); }
.chat-empty { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; color: var(--text-secondary); } .chat-empty h2 { color: var(--text-primary); font-size: 1.5385rem; } .chat-empty p { margin-top: var(--spacing-8); font-size: 1rem; }
.greeting { align-self: flex-start; max-width: 82%; margin-bottom: var(--spacing-16); padding: var(--spacing-12) var(--spacing-14); border: 1px solid var(--border-subtle); border-radius: var(--bubble-radius) var(--bubble-radius) var(--bubble-radius) var(--radius-sm); background: var(--bg-elevated); box-shadow: var(--shadow-sm); color: var(--text-secondary); line-height: 1.7; white-space: pre-wrap; }
.bot-message { display: flex; align-items: flex-start; gap: 9px; max-width: 82%; margin-bottom: var(--spacing-10); }
.bot-message.assistant { align-self: flex-start; } .bot-message.user { align-self: flex-end; }
.msg-avatar { width: 26px; height: 26px; flex: 0 0 26px; display: grid; place-items: center; border-radius: 50%; background: color-mix(in srgb, var(--accent-primary) 14%, transparent); font-size: 0.9231rem; }
.message-content { padding: var(--spacing-10) var(--spacing-14); border: 1px solid var(--border-subtle); border-radius: var(--bubble-radius) var(--bubble-radius) var(--bubble-radius) var(--radius-sm); background: var(--bg-elevated); box-shadow: var(--shadow-sm); line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
.bot-message.user .message-content { border-color: color-mix(in srgb, var(--accent-primary) 24%, transparent); border-radius: var(--bubble-radius) var(--bubble-radius) var(--radius-sm) var(--bubble-radius); background: color-mix(in srgb, var(--accent-primary) 12%, transparent); box-shadow: none; }
.typing-dots { display: inline-flex; align-items: center; gap: var(--spacing-4); padding: 3px 0; }
.typing-dots i { width: 5px; height: 5px; border-radius: 50%; background: var(--text-tertiary); animation: bot-bob 1.2s ease-in-out infinite; }
.typing-dots i:nth-child(2) { animation-delay: .15s; } .typing-dots i:nth-child(3) { animation-delay: .3s; }
@keyframes bot-bob { 0%, 60%, 100% { transform: translateY(0); opacity: .55; } 30% { transform: translateY(-3px); opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .typing-dots i { animation: none; } }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
.chat-error { display: flex; align-items: center; justify-content: space-between; gap: var(--spacing-12); margin: 0 max(var(--spacing-24), calc((100% - 760px) / 2)) var(--spacing-8); padding: var(--spacing-10) var(--spacing-12); border: 1px solid var(--danger-border); border-radius: var(--radius-md); background: var(--danger-bg); color: var(--danger-text); font-size: 0.9231rem; line-height: 1.6; } .chat-error span { min-width: 0; overflow-wrap: anywhere; }
.chat-composer { position: relative; display: flex; align-items: flex-end; gap: var(--spacing-8); margin: 0 max(var(--spacing-24), calc((100% - 760px) / 2)) var(--spacing-20); padding: var(--spacing-6) var(--spacing-6) var(--spacing-6) var(--spacing-16); border: 1px solid var(--border-subtle); border-radius: var(--radius-pill); background: var(--bg-elevated); box-shadow: var(--shadow-lg); transition: border-color var(--transition-fast), box-shadow var(--transition-fast); }
.chat-composer:focus-within { border-color: var(--accent-primary); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent-primary) 16%, transparent), var(--shadow-lg); }
.composer-input { flex: 1; min-width: 0; display: block; max-height: 32vh; overflow-y: auto; border: 0; outline: 0; resize: none; background: transparent; color: var(--text-primary); padding: var(--spacing-8) 0; font-family: var(--font-sans); font-size: 1.1538rem; line-height: 1.6; caret-color: var(--accent-primary); }
.composer-input.with-count { padding-bottom: var(--spacing-20); }
.composer-input::placeholder { color: var(--text-tertiary); }
.composer-count { position: absolute; left: var(--spacing-16); bottom: var(--spacing-4); color: var(--text-tertiary); font-size: 0.7692rem; font-variant-numeric: tabular-nums; pointer-events: none; }
.composer-count.full { color: var(--danger-text); }
.composer-send { width: 32px; height: 32px; flex: 0 0 32px; display: inline-flex; align-items: center; justify-content: center; padding: 0; border: 0; border-radius: 50%; background: linear-gradient(150deg, var(--accent-primary), color-mix(in srgb, var(--accent-primary) 60%, #ffffff)); color: var(--accent-on); cursor: pointer; box-shadow: 0 2px 8px color-mix(in srgb, var(--accent-primary) 35%, transparent); transition: filter var(--transition-fast), background var(--transition-fast), box-shadow var(--transition-fast); }
.composer-send:hover:not(:disabled) { filter: brightness(1.06); } .composer-send:disabled { background: var(--bg-active); color: var(--text-tertiary); cursor: default; box-shadow: none; } .composer-send svg { width: 15px; height: 15px; }
.composer-send.stop { background: var(--danger-bg); color: var(--danger-text); box-shadow: none; } .composer-send.stop:hover:not(:disabled) { filter: none; background: var(--danger-border); }
.persona-page, .qq-page { flex: 1; min-height: 0; overflow-y: auto; padding: var(--spacing-24) 28px; } .section-heading { display: flex; justify-content: space-between; align-items: center; margin-bottom: 22px; } .section-heading h2 { font-size: 1.3846rem; }
.persona-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: var(--spacing-16); } .persona-card { display: flex; flex-direction: column; padding: 18px; border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); background: var(--bg-elevated); }
.persona-card-head { display: flex; align-items: center; gap: var(--spacing-12); } .persona-large-avatar { width: 44px; height: 44px; flex: 0 0 44px; display: grid; place-items: center; border-radius: 50%; background: color-mix(in srgb, var(--accent-primary) 14%, transparent); font-size: 2.4615rem; } .persona-card-head h3 { margin-bottom: var(--spacing-4); font-size: 1.2308rem; } .persona-card-head div span { color: var(--text-tertiary); font-size: 0.8462rem; }
.personality { flex: 1; margin: 18px 0 var(--spacing-12); color: var(--text-secondary); font-size: 1rem; line-height: 1.7; white-space: pre-wrap; } .persona-greeting { margin-bottom: var(--spacing-16); color: var(--text-tertiary); font-size: 0.9231rem; }
.persona-actions { display: flex; flex-wrap: wrap; gap: var(--spacing-6); } .persona-form { display: flex; flex-direction: column; gap: var(--spacing-14); }
.avatar-picker { display: flex; align-items: center; gap: var(--spacing-12); }
.avatar-preview { width: 44px; height: 44px; flex: 0 0 44px; display: grid; place-items: center; border-radius: 50%; background: color-mix(in srgb, var(--accent-primary) 14%, transparent); box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent-primary) 8%, transparent); font-size: 1.4615rem; }
.avatar-picker-body { min-width: 0; display: flex; flex-direction: column; gap: 5px; }
.avatar-picker-actions { display: flex; align-items: center; gap: var(--spacing-8); }
.avatar-picker-body small { color: var(--text-tertiary); font-size: 0.8462rem; }
.avatar-file { display: none; }
.qq-grid { display: grid; grid-template-columns: minmax(300px, 560px) minmax(240px, 340px); gap: var(--spacing-24); align-items: start; } .qq-form, .qq-info { display: flex; flex-direction: column; gap: 18px; padding: 22px; border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); background: var(--bg-elevated); }
.form-note, .privacy-note { color: var(--text-tertiary); font-size: 0.9231rem; line-height: 1.7; } .form-error { color: var(--danger-text); font-size: 0.9231rem; } .qq-actions { display: flex; gap: var(--spacing-8); }
.qq-status { padding: 5px var(--spacing-10); border-radius: var(--radius-pill); background: var(--bg-hover); color: var(--text-secondary); font-size: 0.9231rem; } .qq-status.online { color: var(--status-success-text); } .qq-status.reconnecting { color: var(--status-warn-text); } .qq-status.error { color: var(--danger-text); }
.qq-info dl { display: flex; flex-direction: column; gap: var(--spacing-12); margin: 0; } .qq-info dl div { display: flex; justify-content: space-between; gap: var(--spacing-12); font-size: 0.9231rem; } .qq-info dt { color: var(--text-tertiary); } .qq-info dd { margin: 0; text-align: right; }
.qq-detail { padding: var(--spacing-10); border-radius: var(--radius-sm); background: var(--bg-hover); color: var(--danger-text); font-size: 0.9231rem; } .qq-detail p { margin-top: var(--spacing-6); overflow-wrap: anywhere; }
@media (max-width: 850px) { .qq-grid { grid-template-columns: 1fr; } .chat-layout { grid-template-columns: 190px minmax(0, 1fr); } .bot-header { padding: var(--spacing-16); } }
</style>
