<template>
  <section class="bot-view">
    <header class="bot-header">
      <div>
        <h1>cKunBot</h1>
        <p>给自己留一个合拍的名字，也给它一种说话的方式。</p>
      </div>
      <nav class="bot-tabs" aria-label="cKunBot 功能">
        <button v-for="item in tabs" :key="item.id" :class="{ active: tab === item.id }" @click="tab = item.id">{{ item.label }}</button>
      </nav>
    </header>

    <div v-if="tab === 'chat'" class="chat-layout">
      <aside class="chat-sidebar">
        <label class="field compact">
          <span>当前人格</span>
          <select :value="store.activePersonaId" :disabled="store.streaming" @change="store.selectPersona(($event.target as HTMLSelectElement).value)">
            <option v-for="p in store.personas" :key="p.id" :value="p.id">{{ p.avatar || '🤖' }} {{ p.name }}</option>
          </select>
        </label>
        <button class="btn btn-primary new-chat" :disabled="!store.activePersonaId || store.streaming" @click="createChat">新建会话</button>
        <div class="history-label">陪聊记录</div>
        <div class="conversation-list">
          <div v-for="c in store.filteredConversations" :key="c.id" class="conversation" :class="{ active: c.id === store.activeConversationId }">
            <button class="conversation-main" @click="store.selectConversation(c.id)">
              <span>{{ c.title }}</span><small>{{ shortDate(c.updated_at) }}</small>
            </button>
            <button class="icon-button" title="删除会话" aria-label="删除会话" @click="removeConversation(c.id)">×</button>
          </div>
          <div v-if="!store.filteredConversations.length" class="sidebar-empty">还没有这段故事<br>点上方「新建会话」开始</div>
        </div>
      </aside>

      <div class="chat-main">
        <div v-if="store.activeConversation" class="chat-titlebar">
          <span class="persona-avatar">{{ store.activePersona?.avatar || '🤖' }}</span>
          <div><strong>{{ store.activePersona?.name || 'cKunBot' }}</strong><small>只聊此刻，不调用运维工具</small></div>
        </div>
        <div ref="messageListRef" class="bot-messages">
          <div v-if="!store.activeConversationId" class="chat-empty">
            <span class="empty-bot">🤖</span>
            <h2>今天想聊点什么？</h2>
            <p>选择人格，新建一段只属于你们的会话。</p>
          </div>
          <template v-else>
            <div v-if="!store.messages.length && !store.streaming" class="greeting">
              {{ store.activePersona?.greeting || `你好，我是${store.activePersona?.name || 'cKunBot'}。` }}
            </div>
            <div v-for="m in store.messages" :key="m.id" class="bot-message" :class="m.role">
              <div class="message-role">{{ m.role === 'user' ? '你' : (store.activePersona?.name || 'Bot') }}</div>
              <div class="message-content">{{ m.content }}</div>
            </div>
            <div v-if="store.streaming" class="bot-message assistant">
              <div class="message-role">{{ store.activePersona?.name || 'Bot' }}</div>
              <div class="message-content">{{ store.streamText || '正在想…' }}<span class="typing-dot"></span></div>
            </div>
          </template>
        </div>
        <div v-if="store.error" class="chat-error">
          <span>{{ store.error }}</span>
          <button v-if="store.lastFailedText" class="btn btn-sm" @click="retry">重试</button>
        </div>
        <form class="chat-composer" @submit.prevent="send">
          <textarea v-model="draft" rows="3" maxlength="20000" :disabled="!store.activeConversationId" placeholder="说点什么… Enter 发送，Shift+Enter 换行" @keydown.enter.exact.prevent="send" />
          <div class="composer-actions">
            <span>{{ draft.length }} / 20000</span>
            <button v-if="store.streaming" type="button" class="btn" @click="store.stopGenerating()">停止生成</button>
            <button v-else type="submit" class="btn btn-primary" :disabled="!draft.trim() || !store.activeConversationId">发送</button>
          </div>
        </form>
      </div>
    </div>

    <div v-else-if="tab === 'personas'" class="persona-page">
      <div class="section-heading"><div><h2>人格</h2><p>每个会话保留创建时的人格快照，之后修改不会改写旧回忆。</p></div><button class="btn btn-primary" @click="openPersona()">新增人格</button></div>
      <div class="persona-grid">
        <article v-for="p in store.personas" :key="p.id" class="persona-card">
          <div class="persona-card-head"><span class="persona-large-avatar">{{ p.avatar || '🤖' }}</span><div><h3>{{ p.name }}</h3><span>{{ modelName(p.model_id) }}</span></div></div>
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
          <label class="field"><span>当前人格</span><select v-model="qqForm.persona_id" :disabled="qqStatus.state !== 'offline'"><option value="" disabled>请选择人格</option><option v-for="p in store.personas" :key="p.id" :value="p.id">{{ p.avatar || '🤖' }} {{ p.name }}</option></select></label>
          <p class="form-note">群聊只响应 @机器人，私聊直接回复。切换人格后，QQ 会话会开启新的上下文。</p>
          <div v-if="qqError" class="form-error">{{ qqError }}</div>
          <div class="qq-actions"><button type="submit" class="btn btn-primary" :disabled="qqBusy || !qqForm.app_id || !qqForm.persona_id">{{ qqBusy ? '处理中…' : '保存并连接' }}</button><button type="button" class="btn" :disabled="qqBusy || qqStatus.state === 'offline'" @click="disconnectQq">断开</button></div>
        </form>
        <aside class="qq-info">
          <h3>连接状态</h3>
          <dl><div><dt>运行状态</dt><dd>{{ qqStateText }}</dd></div><div><dt>凭据</dt><dd>{{ qqConfig.has_secret ? 'Secret 已加密保存' : '尚未配置 Secret' }}</dd></div><div><dt>会话上下文</dt><dd>{{ qqConfig.context_id ? qqConfig.context_id.slice(0, 8) : '保存后生成' }}</dd></div></dl>
          <div v-if="qqStatus.detail" class="qq-detail"><strong>最近错误</strong><p>{{ qqStatus.detail }}</p></div>
          <p class="privacy-note">AppSecret 只写入本地加密存储，配置接口不会回显。</p>
        </aside>
      </div>
    </div>

    <BaseModal v-if="personaEditor" :title="editingPersonaId ? '编辑人格' : '新增人格'" @close="personaEditor = false">
      <div class="persona-form">
        <div class="form-row"><label class="field"><span>名称</span><input v-model.trim="personaForm.name" maxlength="50" placeholder="例如：晚风" /></label><label class="field avatar-field"><span>头像</span><input v-model="personaForm.avatar" maxlength="500" placeholder="🤖" /></label></div>
        <label class="field"><span>性格与说话方式</span><textarea v-model="personaForm.personality" rows="4" maxlength="5000" placeholder="真诚、温和，回答简短，偶尔有一点幽默…" /></label>
        <label class="field"><span>背景设定</span><textarea v-model="personaForm.background" rows="4" maxlength="10000" placeholder="它是谁，你们是什么关系，有哪些共同经历…" /></label>
        <label class="field"><span>开场白</span><textarea v-model="personaForm.greeting" rows="2" maxlength="2000" placeholder="新会话里它对你说的第一句话" /></label>
        <label class="field"><span>聊天模型</span><select v-model="personaForm.model_id"><option :value="null">跟随全局默认模型</option><option v-for="m in models.models" :key="m.id" :value="m.id">{{ m.name }}{{ m.model_id ? `（${m.model_id}）` : '' }}</option></select></label>
        <div v-if="personaError" class="form-error">{{ personaError }}</div>
      </div>
      <template #actions><button class="btn" @click="personaEditor = false">取消</button><button class="btn btn-primary" :disabled="personaSaving || !personaForm.name" @click="savePersona">{{ personaSaving ? '保存中…' : '保存' }}</button></template>
    </BaseModal>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import BaseModal from '@/components/common/BaseModal.vue'
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
function shortDate(value: string) { return value ? value.slice(5, 16).replace('T', ' ') : '' }
watch(() => [store.messages.length, store.streamText], async () => {
  await nextTick()
  if (messageListRef.value) messageListRef.value.scrollTop = messageListRef.value.scrollHeight
})

const personaEditor = ref(false)
const editingPersonaId = ref('')
const personaSaving = ref(false)
const personaError = ref('')
const personaForm = reactive({ name: '', avatar: '🤖', personality: '', background: '', greeting: '', model_id: null as string | null })
function openPersona(p?: BotPersona) {
  editingPersonaId.value = p?.id || ''
  Object.assign(personaForm, p ? { name: p.name, avatar: p.avatar, personality: p.personality, background: p.background, greeting: p.greeting, model_id: p.model_id } : { name: '', avatar: '🤖', personality: '', background: '', greeting: '', model_id: null })
  personaError.value = ''
  personaEditor.value = true
}
async function savePersona() {
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
const qqStateText = computed(() => ({ offline: '离线', connecting: '连接中', online: '在线', error: '连接异常' })[qqStatus.value.state])
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
.bot-view { height: 100%; display: flex; flex-direction: column; background: var(--canvas-bg); color: var(--text-primary); overflow: hidden; }
.bot-header { flex: 0 0 auto; display: flex; align-items: center; justify-content: space-between; padding: 22px 28px var(--spacing-14); border-bottom: 1px solid var(--border-subtle); }
h1, h2, h3, p { margin: 0; } h1 { font-size: 1.6923rem; letter-spacing: -.5px; } .bot-header p, .section-heading p { margin-top: 5px; color: var(--text-secondary); font-size: 0.9231rem; }
.bot-tabs { display: flex; gap: var(--spacing-4); padding: 3px; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); background: var(--bg-base); }
.bot-tabs button { border: 0; border-radius: var(--radius-sm); background: transparent; color: var(--text-secondary); padding: 7px 18px; cursor: pointer; }
.bot-tabs button.active { background: var(--bg-hover); color: var(--text-primary); box-shadow: var(--shadow-sm); }
.chat-layout { flex: 1; min-height: 0; display: grid; grid-template-columns: 232px minmax(0, 1fr); }
.chat-sidebar { min-height: 0; display: flex; flex-direction: column; padding: var(--spacing-16) var(--spacing-12); border-right: 1px solid var(--border-subtle); }
.field { display: flex; flex-direction: column; gap: 7px; color: var(--text-secondary); font-size: 0.9231rem; }
.field input, .field select, .field textarea { width: 100%; box-sizing: border-box; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-base); color: var(--text-primary); padding: 9px var(--spacing-10); font: inherit; resize: vertical; }
.field input:focus, .field select:focus, .field textarea:focus { border-color: var(--accent-primary); outline: none; }
.new-chat { margin: var(--spacing-12) 0 18px; } .history-label { padding: 0 var(--spacing-6) var(--spacing-8); color: var(--text-tertiary); font-size: 0.8462rem; }
.conversation-list { overflow-y: auto; min-height: 0; } .conversation { display: flex; align-items: center; border-radius: var(--radius-sm); }
.conversation.active { background: var(--bg-hover); } .conversation-main { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: flex-start; gap: 3px; border: 0; background: transparent; color: var(--text-primary); padding: 9px var(--spacing-6); cursor: pointer; }
.conversation-main span { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; } .conversation-main small { color: var(--text-tertiary); font-size: 0.7692rem; }
.icon-button { border: 0; background: transparent; color: var(--text-tertiary); cursor: pointer; padding: 5px var(--spacing-8); font-size: 1.3077rem; } .icon-button:hover { color: var(--danger-text); }
.sidebar-empty { padding: 28px var(--spacing-8); color: var(--text-tertiary); font-size: 0.9231rem; line-height: 1.8; text-align: center; }
.chat-main { min-width: 0; min-height: 0; display: flex; flex-direction: column; } .chat-titlebar { display: flex; align-items: center; gap: var(--spacing-10); padding: var(--spacing-12) var(--spacing-24); border-bottom: 1px solid var(--border-subtle); }
.persona-avatar { font-size: 1.8462rem; } .chat-titlebar div { display: flex; flex-direction: column; gap: var(--spacing-2); } .chat-titlebar small { color: var(--text-tertiary); font-size: 0.7692rem; }
.bot-messages { flex: 1; min-height: 0; overflow-y: auto; padding: 22px max(var(--spacing-24), calc((100% - 760px) / 2)); }
.chat-empty { height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; color: var(--text-secondary); } .empty-bot { font-size: 3.0769rem; margin-bottom: 15px; } .chat-empty h2 { color: var(--text-primary); font-size: 1.5385rem; } .chat-empty p { margin-top: var(--spacing-8); font-size: 1rem; }
.greeting { margin-bottom: var(--spacing-24); padding: var(--spacing-14) var(--spacing-16); border-left: 2px solid var(--accent-primary); background: var(--bg-hover); color: var(--text-secondary); line-height: 1.7; white-space: pre-wrap; }
.bot-message { margin-bottom: var(--spacing-20); } .message-role { margin-bottom: 5px; color: var(--text-tertiary); font-size: 0.8462rem; } .message-content { max-width: 82%; padding: var(--spacing-10) 13px; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
.bot-message.user { display: flex; flex-direction: column; align-items: flex-end; } .bot-message.user .message-content { background: var(--bg-hover); } .bot-message.assistant .message-content { background: transparent; }
.typing-dot { display: inline-block; width: 5px; height: 5px; margin-left: 5px; border-radius: 50%; background: var(--accent-primary); animation: pulse 1s infinite alternate; } @keyframes pulse { to { opacity: .25; } }
.chat-error { display: flex; align-items: center; justify-content: space-between; gap: var(--spacing-10); margin: 0 var(--spacing-24) var(--spacing-8); padding: var(--spacing-8) var(--spacing-10); color: var(--danger-text); font-size: 0.9231rem; }
.chat-composer { margin: 0 max(var(--spacing-24), calc((100% - 760px) / 2)) var(--spacing-20); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); background: var(--bg-elevated); padding: var(--spacing-10); }
.chat-composer textarea { width: 100%; box-sizing: border-box; border: 0; outline: none; resize: none; background: transparent; color: var(--text-primary); font: inherit; line-height: 1.6; }
.composer-actions { display: flex; align-items: center; justify-content: flex-end; gap: var(--spacing-12); } .composer-actions span { color: var(--text-tertiary); font-size: 0.7692rem; }
.persona-page, .qq-page { flex: 1; min-height: 0; overflow-y: auto; padding: var(--spacing-24) 28px; } .section-heading { display: flex; justify-content: space-between; align-items: center; margin-bottom: 22px; } .section-heading h2 { font-size: 1.3846rem; }
.persona-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: var(--spacing-16); } .persona-card { display: flex; flex-direction: column; padding: 18px; border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); background: var(--bg-elevated); }
.persona-card-head { display: flex; align-items: center; gap: var(--spacing-12); } .persona-large-avatar { font-size: 2.4615rem; } .persona-card-head h3 { margin-bottom: var(--spacing-4); font-size: 1.2308rem; } .persona-card-head div span { color: var(--text-tertiary); font-size: 0.8462rem; }
.personality { flex: 1; margin: 18px 0 var(--spacing-12); color: var(--text-secondary); font-size: 1rem; line-height: 1.7; white-space: pre-wrap; } .persona-greeting { margin-bottom: var(--spacing-16); color: var(--text-tertiary); font-size: 0.9231rem; }
.persona-actions { display: flex; flex-wrap: wrap; gap: var(--spacing-6); } .persona-form { display: flex; flex-direction: column; gap: var(--spacing-14); } .form-row { display: grid; grid-template-columns: 1fr 90px; gap: var(--spacing-12); }
.qq-grid { display: grid; grid-template-columns: minmax(300px, 560px) minmax(240px, 340px); gap: var(--spacing-24); align-items: start; } .qq-form, .qq-info { display: flex; flex-direction: column; gap: 18px; padding: 22px; border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); background: var(--bg-elevated); }
.form-note, .privacy-note { color: var(--text-tertiary); font-size: 0.9231rem; line-height: 1.7; } .form-error { color: var(--danger-text); font-size: 0.9231rem; } .qq-actions { display: flex; gap: var(--spacing-8); }
.qq-status { padding: 5px var(--spacing-10); border-radius: var(--radius-pill); background: var(--bg-hover); color: var(--text-secondary); font-size: 0.9231rem; } .qq-status.online { color: var(--status-success-text); } .qq-status.error { color: var(--danger-text); }
.qq-info dl { display: flex; flex-direction: column; gap: var(--spacing-12); margin: 0; } .qq-info dl div { display: flex; justify-content: space-between; gap: var(--spacing-12); font-size: 0.9231rem; } .qq-info dt { color: var(--text-tertiary); } .qq-info dd { margin: 0; text-align: right; }
.qq-detail { padding: var(--spacing-10); border-radius: var(--radius-sm); background: var(--bg-hover); color: var(--danger-text); font-size: 0.9231rem; } .qq-detail p { margin-top: var(--spacing-6); overflow-wrap: anywhere; }
@media (max-width: 850px) { .qq-grid { grid-template-columns: 1fr; } .chat-layout { grid-template-columns: 190px minmax(0, 1fr); } .bot-header { padding: var(--spacing-16); } }
</style>
