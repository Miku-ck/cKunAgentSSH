<template>
  <section class="bot-view">
    <header v-if="!personaEditor" class="bot-header">
      <div class="brand">
        <button
          v-if="tab === 'chat'"
          type="button"
          class="btn sidebar-toggle"
          :aria-expanded="sidebarOpen"
          aria-label="会话列表"
          @click="sidebarOpen = !sidebarOpen"
        >会话</button>
        <h1>cKunBot</h1>
      </div>
      <nav class="bot-tabs" aria-label="cKunBot 功能">
        <button v-for="item in tabs" :key="item.id" :class="{ active: tab === item.id }" @click="tab = item.id">{{ item.label }}</button>
      </nav>
    </header>

    <!-- 编辑人格是整页而不是弹窗：排在分支链首位，编辑时接管 header 以下全部区域。
         品牌标记由这里下发到编辑器顶栏，取消/保存就与 cKunBot 同一行。 -->
    <PersonaEditor
      v-if="personaEditor"
      :persona="editingPersona"
      :models="models.models"
      :options="personaOptions"
      @close="personaEditor = false"
      @saved="onPersonaSaved"
    >
      <template #brand><div class="brand"><h1>cKunBot</h1></div></template>
    </PersonaEditor>

    <div v-else-if="tab === 'chat'" class="chat-layout" :class="{ 'sidebar-open': sidebarOpen }">
      <div v-if="sidebarOpen" class="sidebar-backdrop" aria-hidden="true" @click="sidebarOpen = false"></div>
      <aside class="chat-sidebar">
        <div class="channel-switch" role="tablist" aria-label="会话渠道">
          <button role="tab" :aria-selected="store.channel === 'local'" :class="{ active: store.channel === 'local' }" @click="store.selectChannel('local')">本地</button>
          <button role="tab" :aria-selected="store.channel === 'qq'" :class="{ active: store.channel === 'qq' }" @click="store.selectChannel('qq')">QQ</button>
        </div>
        <template v-if="store.channel === 'local'">
          <button class="btn new-chat" :disabled="!store.activePersonaId || store.streaming" @click="createChat">新建会话</button>
          <div class="history-label">陪聊记录</div>
          <div class="conversation-list">
            <div v-for="c in store.filteredConversations" :key="c.id" class="conversation" :class="{ active: c.id === store.activeConversationId }">
              <button class="conversation-main" @click="selectLocal(c.id)">
                <span>{{ c.title }}</span><small>{{ formatLocalDateTime(c.updated_at) }}</small>
              </button>
              <button class="icon-button" title="删除会话" aria-label="删除会话" @click="removeConversation(c.id)">×</button>
            </div>
          </div>
        </template>
        <template v-else>
          <div class="history-label qq-label">来自 QQ</div>
          <div class="conversation-list">
            <div v-for="c in store.qqConversations" :key="c.id" class="conversation" :class="{ active: c.id === store.qqActiveId }">
              <button class="conversation-main qq-single" :title="qqRowHint(c)" @click="selectQq(c.id)">
                <i class="conv-peer-avatar"><PersonaAvatar :avatar="c.display_avatar" /></i>
                <span class="qq-title-text">{{ c.display_name || c.title }}</span>
                <i v-if="peerTag(c.external_id)" class="conv-tag">{{ peerTag(c.external_id) }}</i>
              </button>
              <button
                type="button"
                class="icon-button conv-menu"
                title="自定义显示"
                aria-label="自定义该会话的显示名与头像"
                @click="openQqDisplay(c)"
              >⋯</button>
            </div>
            <p v-if="store.qqError" class="sidebar-error">{{ store.qqError }}</p>
            <p v-else-if="!store.qqConversations.length" class="sidebar-empty">还没有 QQ 消息；在 QQ 里 @ 一下机器人试试。</p>
          </div>
        </template>
      </aside>

      <div class="chat-main">
        <div v-if="!store.isQq && store.activeConversation" class="chat-titlebar">
          <span class="persona-avatar"><PersonaAvatar :avatar="store.activePersona?.avatar" /></span>
          <div><strong>{{ store.activePersona?.name || 'cKunBot' }}</strong><small>只聊此刻 </small></div>
          <span v-if="store.activeRelation" class="relation-chip" :title="relationHint(store.activeRelation)">
            {{ store.activeRelation.stage }}
            <i class="relation-bar" aria-hidden="true"><b :style="{ width: relationPercent(store.activeRelation) }"></b></i>
          </span>
        </div>
        <div v-else-if="store.isQq && store.qqActiveConversation" class="chat-titlebar">
          <span class="persona-avatar"><PersonaAvatar :avatar="qqActivePersona?.avatar" /></span>
          <div><strong>{{ qqPersonaName }}</strong><small>来自 QQ · {{ store.qqActiveConversation.display_name || peerLabel(store.qqActiveConversation.external_id) }} · 只读</small></div>
          <span v-if="qqActiveRelation" class="relation-chip" :title="relationHint(qqActiveRelation)">
            {{ qqActiveRelation.stage }}
            <i class="relation-bar" aria-hidden="true"><b :style="{ width: relationPercent(qqActiveRelation) }"></b></i>
          </span>
        </div>
        <div ref="messageListRef" class="bot-messages">
          <div v-if="!store.isQq && !store.activeConversationId" class="chat-empty">
            <h2>今天想聊点什么？</h2>
            <p>到「人格」页挑一位，新建一段只属于你们的会话。</p>
          </div>
          <div v-else-if="store.isQq && !store.qqActiveId" class="chat-empty">
            <h2>{{ store.qqError ? 'QQ 记录读取失败' : '还没有 QQ 消息' }}</h2>
            <p>{{ store.qqError || '在 QQ 里 @ 一下机器人，这里会同步出现。' }}</p>
          </div>
          <!-- 本地与 QQ 记录共用这一份气泡：方向、配色、meta、渲染口径都只按角色决定，
               渠道不再各留一套覆盖，切 tab 就是同一副观感。 -->
          <template v-else>
            <div v-if="!store.isQq && !store.messages.length && !store.streaming" class="greeting">
              {{ store.activePersona?.greeting || `你好，我是${store.activePersona?.name || 'cKunBot'}。` }}
            </div>
            <p v-else-if="store.isQq && !store.qqMessages.length" class="qq-empty">这段会话还没有消息。</p>
            <template v-for="d in messageList" :key="`${d.item.id}-${d.item.bubbleIndex}`">
              <div v-if="d.divider" class="day-divider" role="separator">{{ d.divider }}</div>
              <!-- P4 在场感：还没到投递点的气泡先不渲染（isHiddenBubble），位置由 typing 行占住 -->
              <div v-if="!isHiddenBubble(d.item)" class="bot-message" :class="[d.item.role, { cont: d.cont, 'turn-start': d.item.bubbleIndex === 0 }]">
                <span class="msg-avatar" aria-hidden="true"><PersonaAvatar :avatar="bubbleAvatar(d.item)" /></span>
                <div class="msg-body">
                  <!-- 说话人按「轮」标一次：一轮连发多条只在第一条标，同一人另起一轮也要标 -->
                  <span v-if="d.item.bubbleIndex === 0" class="msg-meta">{{ speakerLabel(d.item) }} · {{ formatLocalDateTime(d.item.created_at) }}</span>
                  <div class="message-content">
                    <span class="sr-only">{{ speakerLabel(d.item) }}：</span>
                    <div v-if="d.item.images?.length && !d.item.bubbleIndex" class="msg-images">
                      <img v-for="(u, i) in d.item.images" :key="i" :src="u" class="msg-image" alt="图片" @click="openLightbox(u)" />
                    </div>
                    <details v-if="!d.item.bubbleIndex && d.item.role !== 'user' && thinkingOn && d.item.reasoning" class="reasoning"><summary>思考过程</summary><div class="reasoning-body">{{ d.item.reasoning }}</div></details>
                    <MarkdownStream v-if="d.item.role !== 'user'" :content="d.item.bubble" />
                    <template v-else>{{ d.item.bubble }}</template>
                  </div>
                </div>
              </div>
            </template>
            <template v-if="store.streaming">
              <div v-for="(b, i) in streamBubbles" :key="`stream-${i}`" class="bot-message assistant" :class="{ cont: i > 0, 'turn-start': i === 0 }">
                <span class="msg-avatar" aria-hidden="true"><PersonaAvatar :avatar="bubbleAvatar({ role: 'assistant' })" /></span>
                <div class="msg-body">
                  <div class="message-content">
                    <details v-if="!i && store.streamReasoning && thinkingOn" class="reasoning" open><summary>思考中…</summary><div class="reasoning-body">{{ store.streamReasoning }}</div></details>
                    <MarkdownStream v-if="b" :content="b" />
                    <span v-else class="typing-dots" :class="{ eager: store.pacing === 'eager' }" role="img" aria-label="正在输入"><i></i><i></i><i></i></span>
                  </div>
                </div>
              </div>
            </template>
            <!-- P4 在场感：排程期间的下一条气泡以「正在输入」占位（欲言又止 = 它消失一下再回来） -->
            <template v-else-if="stagingTyping">
              <div class="bot-message assistant">
                <span class="msg-avatar" aria-hidden="true"><PersonaAvatar :avatar="bubbleAvatar({ role: 'assistant' })" /></span>
                <div class="msg-body">
                  <div class="message-content">
                    <span class="typing-dots" role="img" aria-label="正在输入"><i></i><i></i><i></i></span>
                  </div>
                </div>
              </div>
            </template>
          </template>
        </div>
        <div v-if="!store.isQq && store.error" class="chat-error">
          <span>{{ store.error }}</span>
          <button v-if="store.lastFailedText" class="btn btn-sm" @click="retry">重试</button>
        </div>
        <p v-if="store.isQq && store.qqActiveId" class="readonly-bar">来自 QQ · 仅供查看，回复请在 QQ 里进行</p>
        <form v-if="!store.isQq && store.activeConversationId" class="chat-composer" @submit.prevent="send">
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
      <div class="section-heading"><h2>人格</h2><button class="btn btn-primary" @click="openPersona()">新增人格</button></div>
      <div class="persona-grid">
        <article v-for="p in store.personas" :key="p.id" class="persona-card">
          <div class="persona-card-head">
            <span class="persona-large-avatar"><PersonaAvatar :avatar="p.avatar" /></span>
            <div><h3>{{ p.name }}</h3><span>{{ modelName(p.model_id) }}</span></div>
            <span v-if="store.relations[p.id]" class="relation-chip small" :title="relationHint(store.relations[p.id])">{{ store.relations[p.id].stage }}</span>
          </div>
          <p class="personality">{{ p.personality || '还没有写下性格描述。' }}</p>
          <p class="persona-greeting">“{{ p.greeting }}”</p>
          <div class="persona-actions">
            <button class="btn btn-sm btn-primary" @click="chatWith(p.id)">用此人格聊天</button>
            <button class="btn btn-sm" @click="openPersona(p)">编辑</button>
            <button class="btn btn-sm" @click="copyPersona(p.id)">复制</button>
            <button class="btn btn-sm btn-danger" @click="removePersona(p)">删除</button>
          </div>
        </article>
      </div>
    </div>

    <ConnectionPage v-else :personas="store.personas" />

    <!-- QQ 会话显示覆盖（Phase 4 §4.4）：只改本地看到的名字/头像，真实群资料不采集不改写。
         弹窗绑点进来的那一行，不是当前活动会话。 -->
    <BaseModal v-if="qqDisplayTarget" title="自定义显示" @close="qqDisplayTarget = null">
      <AvatarPicker v-model="qqDisplayAvatar" :clearable="false">
        <template #fields>
          <label class="qq-field">
            <span>会话显示名</span>
            <input v-model.trim="qqDisplayName" maxlength="50" :placeholder="qqDisplayTarget.title" />
          </label>
        </template>
      </AvatarPicker>
      <p v-if="qqDisplayError" class="form-error">{{ qqDisplayError }}</p>
      <template #actions>
        <button type="button" class="btn btn-ghost qq-display-reset" @click="resetQqDisplay">恢复默认</button>
        <button type="button" class="btn" @click="qqDisplayTarget = null">取消</button>
        <button type="button" class="btn btn-primary" :disabled="qqDisplaySaving" @click="saveQqDisplay">{{ qqDisplaySaving ? '保存中…' : '保存' }}</button>
      </template>
    </BaseModal>

    <ChatImageLightbox :visible="!!lightboxUrl" :src="lightboxUrl" @close="lightboxUrl = ''" />
  </section>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import PersonaAvatar from './PersonaAvatar.vue'
import PersonaEditor from './PersonaEditor.vue'
import ConnectionPage from './ConnectionPage.vue'
import BaseModal from '@/components/common/BaseModal.vue'
import { formatLocalDateTime } from '@/utils/datetime'
import { bubbleDeliveryPlan, decorateMessages, expandBubbles, peerLabel, peerTag, personaName, splitBubbles } from '@/utils/bot-chat-decor'
import { shouldFollowScroll } from '@/utils/scroll-follow'
import AvatarPicker from './AvatarPicker.vue'
import ChatImageLightbox from './ChatImageLightbox.vue'
import { botApi, type BotConversation, type BotPersona, type BotPersonaOptions } from '@/api/bot'
import { toFriendlyMessage } from '@/api/base'
import { useBotStore } from '@/stores/bot'
import { useModelsStore } from '@/stores/models'
import { useToastStore } from '@/stores/toast'
import { useConfirm } from '@/composables/useConfirm'

const tabs = [{ id: 'chat', label: '陪聊' }, { id: 'personas', label: '人格' }, { id: 'connection', label: '连接' }] as const
// Markdown 渲染栈体积大：异步引入，不进首屏包（与主聊天页同口径）。
const MarkdownStream = defineAsyncComponent(() => import('@/components/chat/MarkdownStream.vue'))
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

// 窄屏抽屉：选会话/新建后收起，正文区回到聊天
const sidebarOpen = ref(false)
function selectLocal(id: string) {
  sidebarOpen.value = false
  void store.selectConversation(id)
}
async function createChat() {
  sidebarOpen.value = false
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
  try { await store.deleteConversation(id) } catch (e) { toast.error(toFriendlyMessage(e, '删除失败')) }
}
/** 消息流跟到底：四处触发（新消息/流式、逐条揭开、切会话、QQ 轮询）共用一份。 */
function scrollToBottom() {
  const el = messageListRef.value
  if (el) el.scrollTop = el.scrollHeight
}

watch(() => [store.messages.length, store.streamText], async () => {
  // 贴底才跟：streamText 每个 token 变一次，无条件抢滚动条会把页面一段一段往上蹿，
  // 用户上翻看上一条时还会被一次次拽回底部。判定与 QQ 轮询同一份。
  const el = messageListRef.value
  const stick = !el || shouldFollowScroll(el.scrollTop, el.clientHeight, el.scrollHeight, 48)
  await nextTick()
  if (stick) scrollToBottom()
})

const personaEditor = ref(false)
const editingPersona = ref<BotPersona | null>(null)
// 档位字典由后端给出：控件的 label 与可选项不在这里手抄，避免和提示词文案漂移。
const personaOptions = ref<BotPersonaOptions | null>(null)
function openPersona(p?: BotPersona) {
  editingPersona.value = p || null
  personaEditor.value = true
}
async function onPersonaSaved() {
  personaEditor.value = false
  await store.init()
  toast.success('人格已保存')
}
async function loadPersonaOptions() {
  try { personaOptions.value = await botApi.getPersonaOptions() }
  catch { personaOptions.value = null }
}
async function copyPersona(id: string) {
  try { await botApi.copyPersona(id); await store.init(); toast.success('已复制人格') } catch (e) { toast.error(toFriendlyMessage(e, '复制失败')) }
}
async function removePersona(p: BotPersona) {
  if (!await confirm({ message: `删除人格「${p.name}」？已有会话会保留人格快照和聊天记录。`, danger: true })) return
  try { await botApi.deletePersona(p.id); await store.init() } catch (e) { toast.error(toFriendlyMessage(e, '删除失败')) }
}
async function chatWith(id: string) {
  store.selectChannel('local')
  await store.selectPersona(id)
  if (!store.activeConversationId) await store.createConversation(id)
  tab.value = 'chat'
}
function modelName(id: string | null) { return id ? models.models.find(m => m.id === id)?.name || '指定模型' : '默认模型' }

/** 关系徽标：显示阶段与「离下一阶段还有多远」，让「越聊越熟」这件事可见。 */
function relationPercent(state: { progress: number }) {
  return `${Math.round(Math.min(Math.max(state.progress, 0), 1) * 100)}%`
}
function relationHint(state: { stage: string; turns: number; days_since: number | null }) {
  const base = `关系：${state.stage} · 已聊 ${state.turns} 轮`
  if (state.days_since !== null && state.days_since >= 3) {
    return `${base}（${Math.round(state.days_since)} 天没聊，熟悉度会回落一点）`
  }
  return base
}

const qqActivePersona = computed(() => store.qqActiveConversation?.persona || null)
const qqPersonaName = computed(() => personaName(qqActivePersona.value) || '机器人')

// ── QQ 会话自定义显示（Phase 4 §4.4）──
// 弹窗绑「点进来的那一行」而不是当前活动会话：列表每 5 秒轮询刷新，
// 绑活动会话就可能把 A 会话的草稿写进 B。
const qqDisplayTarget = ref<BotConversation | null>(null)
const qqDisplayName = ref('')
const qqDisplayAvatar = ref('')
const qqDisplaySaving = ref(false)
const qqDisplayError = ref('')

// 打开时以该行当前值打底；空串 = 显式清除回落默认。
function openQqDisplay(conversation: BotConversation) {
  qqDisplayTarget.value = conversation
  qqDisplayName.value = conversation.display_name || ''
  qqDisplayAvatar.value = conversation.display_avatar || ''
  qqDisplayError.value = ''
}

async function saveQqDisplay() {
  const conversation = qqDisplayTarget.value
  if (!conversation) return
  qqDisplaySaving.value = true
  qqDisplayError.value = ''
  try {
    await store.updateQqDisplay(conversation.id, { display_name: qqDisplayName.value, display_avatar: qqDisplayAvatar.value })
    qqDisplayTarget.value = null
  } catch (e) { qqDisplayError.value = toFriendlyMessage(e, '保存失败') }
  finally { qqDisplaySaving.value = false }
}

function resetQqDisplay() {
  qqDisplayName.value = ''
  qqDisplayAvatar.value = ''
}
// 关系是「人格 × 用户」维度：QQ 会话也归属某人格，取其关系快照补上徽标。
const qqActiveRelation = computed(() => {
  const pid = qqActivePersona.value?.id
  return pid ? store.relations[pid] || null : null
})
// 装饰元数据（日期分隔 + 连续合并）交给纯函数算，模板只做映射。
// 先按空行展开成气泡再装饰：同一轮的后续气泡天然算「续接」，隐头像、收间距都复用现成规则。
const localDecorated = computed(() => decorateMessages(expandBubbles(store.messages)))
const qqDecorated = computed(() => decorateMessages(expandBubbles(store.qqMessages)))
// 流式阶段就分条：打字时一条条冒出来，收尾换成落库消息时不会看到气泡重排。
// 一个字符都还没有时留一条空气泡，「正在输入」的三个点就落在那里面。
const streamBubbles = computed(() => {
  const parts = splitBubbles(store.streamText)
  return parts.length ? parts : ['']
})

// ── 在场感（P4）：多气泡投递节奏 ──
// done 落库的一轮若是多气泡，第 2~N 条按拟打字延迟逐条揭开：每条揭开前
// 显示「正在输入」。首气泡零延迟（流式已在渲染），所以不影响首字延迟。
const staged = ref<{ id: string; shown: number } | null>(null)
const stagingPaused = ref(false)
let stagingTimers: ReturnType<typeof setTimeout>[] = []

function clearStaging() {
  stagingTimers.forEach(t => { clearTimeout(t) })
  stagingTimers = []
  staged.value = null
  stagingPaused.value = false
}

function isHiddenBubble(item: { id: string; role: string; bubbleIndex: number }) {
  return !!staged.value
    && item.id === staged.value.id
    && item.role === 'assistant'
    && item.bubbleIndex >= staged.value.shown
}

watch(() => `${store.messages.length}:${store.messages[store.messages.length - 1]?.id || ''}`, () => {
  const last = store.messages[store.messages.length - 1]
  if (!last || store.streaming) { if (store.streaming) clearStaging(); return }
  if (last.role !== 'assistant') { clearStaging(); return } // 用户插话 = 打断，余下气泡立即放行
  // 这一轮刚被流式逐字演过：排程若从 shown=1 重来，等于把演完的气泡收回去重放
  if (last.id === store.lastStreamedId) { clearStaging(); return }
  if (staged.value?.id === last.id) return
  const bubbles = splitBubbles(last.content)
  if (bubbles.length < 2) { clearStaging(); return }
  clearStaging()
  staged.value = { id: last.id, shown: 1 }
  const delays = bubbleDeliveryPlan(bubbles)
  let acc = 0
  let quick = false
  if (store.pacing === 'hesitate') {
    // 欲言又止：打字出现 → 停一下 → 再出现，然后才开讲
    stagingTimers.push(setTimeout(() => { stagingPaused.value = true }, 600))
    stagingTimers.push(setTimeout(() => { stagingPaused.value = false }, 1150))
    acc = 1400
  } else if (store.pacing === 'eager') {
    // 兴冲冲：不演停顿，气泡间的等待也收短——想快点说出来
    quick = true
  }
  // relief（有点别扭但在缓和）与默认共用正常节奏：不演停顿即可
  for (let i = 1; i < bubbles.length; i++) {
    acc += quick ? Math.round(delays[i] * 0.6) : delays[i]
    const target = i + 1
    stagingTimers.push(setTimeout(() => {
      if (staged.value?.id === last.id) staged.value = { id: last.id, shown: target }
    }, acc))
  }
})

const stagingTyping = computed(() => {
  if (!staged.value || stagingPaused.value) return false
  const last = store.messages[store.messages.length - 1]
  if (!last || last.id !== staged.value.id) return false
  return splitBubbles(last.content).length > staged.value.shown
})

// 逐条揭开时也要跟到底（既有的滚动 watcher 只盯 messages 长度，盯不到揭开）
watch(() => staged.value?.shown, async () => {
  const el = messageListRef.value
  const stick = !el || shouldFollowScroll(el.scrollTop, el.clientHeight, el.scrollHeight, 48)
  await nextTick()
  if (stick) scrollToBottom()
})

// 图片灯箱：本地/历史消息里的图片点开放大，不跳转、不外发。
const lightboxUrl = ref('')
function openLightbox(url: string) { lightboxUrl.value = url }

// 两个渠道一份列表：QQ 读的是只读记录，本地读的是自己的会话，气泡规则只有一套。
const messageList = computed(() => (store.isQq ? qqDecorated.value : localDecorated.value))
// 思考开关只裁决本地会话：后端只在 local 通道落 reasoning，QQ 记录没有思考块可显示，
// 拿本地选中人格的开关去判 QQ 那一侧只会张冠李戴（两边可能不是同一个人格）。
const thinkingOn = computed(() => !store.isQq && Boolean(store.activePersona?.enable_thinking))

/** 头像按「哪一侧」取：bot = 当前应答人格，人 = 本地是你、QQ 是对方。 */
function bubbleAvatar(message: { role: string }) {
  if (message.role !== 'user') return store.isQq ? qqActivePersona.value?.avatar : store.activePersona?.avatar
  return store.isQq ? store.qqActiveConversation?.display_avatar : store.userAvatar
}

/** 说话人显示名，同一口径：真人用他自己的显示名（QQ 拿不到真名时用会话标题兜底），bot 用人格名。 */
function speakerLabel(message: { role: string }) {
  if (message.role !== 'user') return store.isQq ? qqPersonaName.value : personaName(store.activePersona) || 'Bot'
  if (!store.isQq) return '你'
  const conversation = store.qqActiveConversation
  return conversation?.display_name || conversation?.title || '对方'
}

function qqRowHint(conversation: BotConversation) {
  return `${peerLabel(conversation.external_id)} · 最后消息 ${formatLocalDateTime(conversation.updated_at)}`
}

async function selectQq(id: string) {
  sidebarOpen.value = false
  await store.selectQqConversation(id)
  await nextTick()
  scrollToBottom()
}

let qqTimer: ReturnType<typeof setInterval> | undefined

function stopQqPolling() {
  if (qqTimer) {
    clearInterval(qqTimer)
    qqTimer = undefined
  }
}

async function refreshQqChat() {
  // 贴底判定复用任务台的共享件：用户上翻看记录时，轮询刷新不许抢滚动条
  const el = messageListRef.value
  const stick = !el || shouldFollowScroll(el.scrollTop, el.clientHeight, el.scrollHeight, 48)
  await store.refreshQq()
  if (stick) {
    await nextTick()
    scrollToBottom()
  }
}

watch([tab, () => store.channel], () => {
  sidebarOpen.value = false
  stopQqPolling()
  if (tab.value !== 'chat' || store.channel !== 'qq') return
  void refreshQqChat()
  qqTimer = setInterval(() => { void refreshQqChat() }, 5000)
})

onMounted(async () => {
  await Promise.all([store.init(), models.fetchModels(), loadPersonaOptions()])
})
onUnmounted(() => { stopQqPolling(); store.stopGenerating(); clearStaging() })
</script>

<style scoped>
.bot-view { --bubble-radius: calc(var(--radius-lg) * 2); height: 100%; display: flex; flex-direction: column; background: var(--canvas-bg); color: var(--text-primary); overflow: hidden; }
.bot-header { flex: 0 0 auto; display: flex; align-items: center; justify-content: space-between; gap: var(--spacing-16); padding: 20px 28px var(--spacing-12); border-bottom: 1px solid var(--border-subtle); }
.brand { min-width: 0; display: flex; align-items: center; gap: var(--spacing-10); }
/* 窄屏抽屉开关：默认不渲染（宽屏侧栏常驻），≤700px 才由媒体查询放出 */
.sidebar-toggle { display: none; }
h1, h2, h3, p { margin: 0; } h1 { font-size: 1.6923rem; letter-spacing: -.5px; } .bot-tabs { display: flex; gap: var(--spacing-2); padding: 3px; border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); background: var(--bg-input); }
.bot-tabs button { border: 0; border-radius: var(--radius-md); background: transparent; color: var(--text-secondary); padding: 6px 16px; cursor: pointer; transition: color var(--transition-fast), background var(--transition-fast); }
.bot-tabs button:hover { color: var(--text-primary); }
.bot-tabs button.active { background: var(--bg-elevated); color: var(--text-primary); box-shadow: var(--shadow-sm); }
.chat-layout { flex: 1; min-height: 0; display: grid; grid-template-columns: 232px minmax(0, 1fr); }
.chat-sidebar { position: relative; min-height: 0; display: flex; flex-direction: column; padding: var(--spacing-16) var(--spacing-12); border-right: 1px solid var(--border-subtle); overflow: hidden; }
.chat-sidebar::before { content: ''; position: absolute; inset: 0 0 auto 0; height: 200px; background: radial-gradient(72% 100% at 50% 0%, color-mix(in srgb, var(--accent-primary) 10%, transparent), transparent 72%); pointer-events: none; }
.chat-sidebar > * { position: relative; }
.channel-switch { display: flex; gap: var(--spacing-2); margin: 0 0 var(--spacing-12); padding: 3px; border: 1px solid var(--border-subtle); border-radius: var(--radius-pill); background: var(--bg-input); }
.channel-switch button { flex: 1; border: 0; border-radius: var(--radius-pill); background: transparent; color: var(--text-secondary); padding: 5px 0; font: inherit; font-size: 0.9231rem; cursor: pointer; transition: color var(--transition-fast), background var(--transition-fast); }
.channel-switch button:hover { color: var(--text-primary); }
.channel-switch button.active { background: var(--bg-elevated); color: var(--text-primary); box-shadow: var(--shadow-sm); }
.new-chat { width: 100%; height: 30px; margin: 0 0 var(--spacing-16); justify-content: center; border-color: transparent; border-radius: var(--radius-pill); background: color-mix(in srgb, var(--accent-primary) 12%, transparent); color: var(--accent-text); font-size: 0.9231rem; }
.new-chat:hover:not(:disabled) { border-color: transparent; background: color-mix(in srgb, var(--accent-primary) 20%, transparent); color: var(--accent-text); }
.history-label { padding: 0 var(--spacing-6) var(--spacing-8); color: var(--text-tertiary); font-size: 0.8462rem; }
.conversation-list { overflow-y: auto; min-height: 0; display: flex; flex-direction: column; gap: var(--spacing-2); }
.conversation { display: flex; align-items: center; border-radius: var(--radius-xl); transition: background var(--transition-fast), box-shadow var(--transition-fast); }
.conversation:hover { background: var(--bg-hover); }
.conversation.active { background: var(--bg-elevated); box-shadow: var(--shadow-sm); }
.conversation-main { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: flex-start; gap: 3px; border: 0; background: transparent; color: var(--text-primary); padding: 9px var(--spacing-12); cursor: pointer; }
.conversation-main span { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; } .conversation-main small { color: var(--text-tertiary); font-size: 0.7692rem; }
.qq-title-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* QQ 行收成一行：名字吃剩余宽度，群聊才贴一枚「群」标（私聊是默认，标了是噪声）。
   代号与最后消息时间不进正文 —— 250px 窄栏里第二段必然折行，行高忽高忽低，挪进 title。 */
.conversation-main.qq-single { flex-direction: row; align-items: center; gap: var(--spacing-6); }
.conv-tag { flex: 0 0 auto; padding: 0 var(--spacing-4); border-radius: var(--radius-sm); background: color-mix(in srgb, var(--accent-primary) 12%, transparent); color: var(--accent-text); font-size: 0.6923rem; line-height: 1.6; }
/* 侧栏会话的对方头像：只显示自定义上传图，没设就留空 —— 圆裁、不暴露真实资料、不自造图案。 */
.conv-peer-avatar { flex: 0 0 auto; width: 26px; height: 26px; border-radius: 50%; overflow: hidden; }
.sidebar-empty, .sidebar-error { margin: var(--spacing-4) 0 0; padding: 0 var(--spacing-6); color: var(--text-tertiary); font-size: 0.8462rem; line-height: 1.6; }
.sidebar-error { color: var(--danger-text); }
.icon-button { flex: 0 0 auto; border: 0; background: transparent; color: var(--text-tertiary); cursor: pointer; padding: 5px var(--spacing-8); font-size: 1.3077rem; opacity: 0; transition: opacity var(--transition-fast), color var(--transition-fast); } .conversation:hover .icon-button, .conversation:focus-within .icon-button { opacity: 1; } .icon-button:hover { color: var(--danger-text); }
@media (hover: none) { .icon-button { opacity: 1; } }
.chat-main { position: relative; min-width: 0; min-height: 0; display: flex; flex-direction: column; }
.chat-main::before { content: ''; position: absolute; inset: 0 0 auto 0; height: 240px; background: radial-gradient(78% 100% at 50% 0%, color-mix(in srgb, var(--accent-primary) 14%, transparent), transparent 70%); pointer-events: none; }
.chat-main > * { position: relative; }
.chat-titlebar { display: flex; align-items: center; gap: var(--spacing-10); padding: var(--spacing-12) var(--spacing-24) var(--spacing-8); }
.chat-titlebar .persona-avatar { width: 32px; height: 32px; flex: 0 0 32px; display: grid; place-items: center; border-radius: 50%; font-size: 1.1538rem; }
.chat-titlebar div { display: flex; flex-direction: column; gap: var(--spacing-2); } .chat-titlebar strong { font-size: 1rem; } .chat-titlebar small { color: var(--text-tertiary); font-size: 0.7692rem; }
/* 关系徽标：阶段 + 到下一阶段的进度条。它存在的意义是让「越聊越熟」可见，
   所以进度条必须窄而低对比，不能抢走对话本身的注意力。 */
.relation-chip { margin-left: auto; display: inline-flex; align-items: center; gap: 6px; padding: 3px var(--spacing-10); border: 1px solid var(--border-subtle); border-radius: var(--radius-pill); background: color-mix(in srgb, var(--accent-primary) 8%, transparent); color: var(--accent-text); font-size: 0.8462rem; white-space: nowrap; }
.relation-chip.small { padding: 2px var(--spacing-8); font-size: 0.7692rem; }
.relation-bar { width: 34px; height: 3px; flex: 0 0 34px; border-radius: var(--radius-pill); background: color-mix(in srgb, var(--accent-primary) 20%, transparent); overflow: hidden; }
.relation-bar b { display: block; height: 100%; background: var(--accent-primary); transition: width .3s ease; }
.bot-messages { flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; padding: var(--spacing-10) max(var(--spacing-24), calc((100% - 760px) / 2)) var(--spacing-8); }
.chat-empty { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; color: var(--text-secondary); } .chat-empty h2 { color: var(--text-primary); font-size: 1.5385rem; } .chat-empty p { margin-top: var(--spacing-8); font-size: 1rem; }
/* 基准取自 QQ 记录那套气泡：人格说的话用强调色（那才是你在读的内容），人说的话走中性卡片。
   尾巴朝自己这侧：bot 在左收左下、人在右收右下。 */
.greeting { align-self: flex-start; max-width: 82%; margin-bottom: var(--spacing-16); padding: var(--spacing-12) var(--spacing-14); border: 1px solid color-mix(in srgb, var(--accent-primary) 24%, transparent); border-radius: var(--bubble-radius) var(--bubble-radius) var(--bubble-radius) var(--radius-sm); background: color-mix(in srgb, var(--accent-primary) 12%, transparent); box-shadow: none; color: var(--text-secondary); line-height: 1.7; white-space: pre-wrap; }
.bot-message { display: flex; align-items: flex-start; gap: 9px; max-width: 82%; margin-bottom: var(--spacing-10); }
.bot-message.assistant { align-self: flex-start; } .bot-message.user { align-self: flex-end; }
/* 用户/对方气泡靠右，头像跟着排在气泡右侧；bot 在左，头像在气泡左侧。 */
.bot-message.user .msg-avatar { order: 2; }
.msg-avatar { width: 26px; height: 26px; flex: 0 0 26px; display: grid; place-items: center; border-radius: 50%; font-size: 0.9231rem; }
.message-content { padding: var(--spacing-10) var(--spacing-14); border: 1px solid color-mix(in srgb, var(--accent-primary) 24%, transparent); border-radius: var(--bubble-radius) var(--bubble-radius) var(--bubble-radius) var(--radius-sm); background: color-mix(in srgb, var(--accent-primary) 12%, transparent); box-shadow: none; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
.bot-message.user .message-content { border-color: var(--border-subtle); border-radius: var(--bubble-radius) var(--bubble-radius) var(--radius-sm) var(--bubble-radius); background: var(--bg-elevated); box-shadow: var(--shadow-sm); }
.reasoning { margin: 0 0 var(--spacing-6); border-radius: var(--radius-sm); background: var(--bg-input); font-size: 0.8462rem; }
.reasoning summary { cursor: pointer; padding: 4px var(--spacing-8); color: var(--text-tertiary); list-style: none; }
.reasoning summary::-webkit-details-marker { display: none; }
.reasoning-body { padding: 0 var(--spacing-8) var(--spacing-6); color: var(--text-secondary); line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; }
/* 气泡只有一套规则：方向、配色与 meta 对齐都按角色决定（bot 在左、人在右），
   本地与 QQ 记录同形。头像侧由 .msg-avatar 的 order 决定（user 通用规则给 order:2 → 头像在气泡右）。 */
/* 弹窗内容：BaseModal 的 body 已给 padding 与纵向间距，这里只管控件自身。
   显示名走 AvatarPicker 的 #fields 插槽，与头像同一栏，不再另起一行。 */
.qq-field { display: flex; flex-direction: column; gap: 7px; color: var(--text-secondary); font-size: 0.9231rem; }
.qq-field input { border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-base); color: var(--text-primary); padding: 9px var(--spacing-10); font: inherit; font-size: 0.9231rem; }
.qq-field input:focus { border-color: var(--accent-primary); outline: none; box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent-primary) 16%, transparent); }
.form-error { color: var(--danger-text); font-size: 0.8462rem; }
/* 重置是整表单级的动作，和取消/保存同一层；margin-right:auto 把它推到左端 */
.qq-display-reset { margin-right: auto; }
/* 行右侧的 ⋯ 默认藏起来：hover 淡入 / 触屏常显的行为由 .icon-button 统一提供
  （该元素同时挂 icon-button conv-menu 两个类），这里不再重复一份。 */
.msg-body { min-width: 0; display: flex; flex-direction: column; align-items: flex-start; gap: var(--spacing-2); }
.bot-message.user .msg-body { align-items: flex-end; }
.msg-meta { color: var(--text-tertiary); font-size: 0.7692rem; }
/* 日期分隔线：居中淡色小字，两侧渐隐细线，不打断阅读节奏。 */
.day-divider { align-self: center; display: flex; align-items: center; gap: var(--spacing-10); margin: var(--spacing-12) 0 var(--spacing-10); color: var(--text-tertiary); font-size: 0.7692rem; }
.day-divider::before, .day-divider::after { content: ''; width: 44px; height: 1px; background: linear-gradient(90deg, transparent, var(--border-subtle), transparent); }
/* 三档间距：同轮连发 4px（一眼是连着发的）、同一人另起一轮 8px、换人 10px。
   头像在连发时隐去重复，说话人那行按「轮」标（见模板的 bubbleIndex === 0）。 */
.bot-message.cont { margin-bottom: var(--spacing-4); }
.bot-message.cont.turn-start { margin-bottom: var(--spacing-8); }
.bot-message.cont .msg-avatar { visibility: hidden; }
.bot-message.cont .message-content { border-radius: var(--radius-sm); }
/* 段落首尾外边距在气泡里归零：marked 的 <p> 自带 0.6em，落在气泡 padding 里
   会把一行字撑成 60px 高的框；拆成多条气泡后每条都吃一遍，必须清掉。 */
.message-content :deep(.markdown-body) > :first-child { margin-top: 0; }
.message-content :deep(.markdown-body) > :last-child { margin-bottom: 0; }
.qq-empty { margin: auto; color: var(--text-secondary); font-size: 0.9231rem; }
.typing-dots { display: inline-flex; align-items: center; gap: var(--spacing-4); padding: 3px 0; min-height: 1.7em; }
.typing-dots i { width: 5px; height: 5px; border-radius: 50%; background: var(--text-tertiary); animation: bot-bob 1.2s ease-in-out infinite; }
.typing-dots i:nth-child(2) { animation-delay: .15s; } .typing-dots i:nth-child(3) { animation-delay: .3s; }
/* 兴冲冲（eager）：三个点跳得更快，演出「有话憋不住」的节奏 */
.typing-dots.eager i { animation-duration: .7s; }
.typing-dots.eager i:nth-child(2) { animation-delay: .1s; } .typing-dots.eager i:nth-child(3) { animation-delay: .2s; }
@keyframes bot-bob { 0%, 60%, 100% { transform: translateY(0); opacity: .55; } 30% { transform: translateY(-3px); opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .typing-dots i { animation: none; } }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
.chat-error { display: flex; align-items: center; justify-content: space-between; gap: var(--spacing-12); margin: 0 max(var(--spacing-24), calc((100% - 760px) / 2)) var(--spacing-8); padding: var(--spacing-10) var(--spacing-12); border: 1px solid var(--danger-border); border-radius: var(--radius-md); background: var(--danger-bg); color: var(--danger-text); font-size: 0.9231rem; line-height: 1.6; } .chat-error span { min-width: 0; overflow-wrap: anywhere; }
.readonly-bar { margin: 0; padding: var(--spacing-8) var(--spacing-24) var(--spacing-12); color: var(--text-tertiary); font-size: 0.7692rem; text-align: center; }
.chat-composer { position: relative; display: flex; align-items: center; gap: var(--spacing-8); margin: 0 max(var(--spacing-24), calc((100% - 760px) / 2)) var(--spacing-20); padding: var(--spacing-6) var(--spacing-6) var(--spacing-6) var(--spacing-16); border: 1px solid var(--border-subtle); border-radius: var(--radius-pill); background: var(--bg-elevated); box-shadow: var(--shadow-lg); transition: border-color var(--transition-fast), box-shadow var(--transition-fast); }
.chat-composer:focus-within { border-color: var(--accent-primary); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent-primary) 16%, transparent), var(--shadow-lg); }
.composer-input { flex: 1; min-width: 0; display: block; max-height: 32vh; overflow-y: auto; border: 0; outline: 0; resize: none; background: transparent; color: var(--text-primary); padding: var(--spacing-8) 0; font-family: var(--font-sans); font-size: 1.1538rem; line-height: 1.6; caret-color: var(--accent-primary); }
.composer-input.with-count { padding-bottom: var(--spacing-20); }
.composer-input::placeholder { color: var(--text-tertiary); }
.composer-count { position: absolute; left: var(--spacing-16); bottom: var(--spacing-4); color: var(--text-tertiary); font-size: 0.7692rem; font-variant-numeric: tabular-nums; pointer-events: none; }
.composer-count.full { color: var(--danger-text); }
.composer-send { width: 32px; height: 32px; flex: 0 0 32px; display: inline-flex; align-items: center; justify-content: center; padding: 0; border: 0; border-radius: 50%; background: linear-gradient(150deg, var(--accent-primary), color-mix(in srgb, var(--accent-primary) 60%, #ffffff)); color: var(--accent-on); cursor: pointer; box-shadow: 0 2px 8px color-mix(in srgb, var(--accent-primary) 35%, transparent); transition: filter var(--transition-fast), background var(--transition-fast), box-shadow var(--transition-fast); }
.composer-send:hover:not(:disabled) { filter: brightness(1.06); } .composer-send:disabled { background: var(--bg-active); color: var(--text-tertiary); cursor: default; box-shadow: none; } .composer-send svg { width: 15px; height: 15px; }
.composer-send.stop { background: var(--danger-bg); color: var(--danger-text); box-shadow: none; } .composer-send.stop:hover:not(:disabled) { filter: none; background: var(--danger-border); }
.persona-page { flex: 1; min-height: 0; overflow-y: auto; padding: var(--spacing-24) 28px; } .section-heading { display: flex; justify-content: space-between; align-items: center; margin-bottom: 22px; } .section-heading h2 { font-size: 1.3846rem; }
.persona-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: var(--spacing-16); } .persona-card { display: flex; flex-direction: column; padding: 18px; border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); background: var(--bg-elevated); }
.persona-card-head { display: flex; align-items: center; gap: var(--spacing-12); } .persona-large-avatar { width: 44px; height: 44px; flex: 0 0 44px; display: grid; place-items: center; border-radius: 50%; font-size: 2.4615rem; } .persona-card-head h3 { margin-bottom: var(--spacing-4); font-size: 1.2308rem; } .persona-card-head div span { color: var(--text-tertiary); font-size: 0.8462rem; }
/* 卡片定高：性格截两行、开场白一行占位 —— 长文案不撑爆网格，卡片高度不随内容漂 */
.personality { flex: 1; margin: 18px 0 var(--spacing-12); color: var(--text-secondary); font-size: 1rem; line-height: 1.7; white-space: pre-wrap; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; min-height: 3.4em; } .persona-greeting { margin-bottom: var(--spacing-16); color: var(--text-tertiary); font-size: 0.9231rem; line-height: 1.6; min-height: 1.6em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.persona-actions { display: flex; flex-wrap: wrap; gap: var(--spacing-6); }
.msg-images { display: flex; flex-wrap: wrap; gap: var(--spacing-6); margin-bottom: var(--spacing-6); }
.msg-image { max-width: 200px; max-height: 200px; border-radius: var(--radius-md); border: 1px solid var(--border-subtle); object-fit: contain; cursor: zoom-in; transition: filter var(--transition-fast), transform var(--transition-fast); }
.msg-image:hover { filter: brightness(1.06); transform: translateY(-1px); }
@media (max-width: 850px) { .chat-layout { grid-template-columns: 190px minmax(0, 1fr); } .bot-header { padding: var(--spacing-16); } }
/* 窄屏：侧栏收成抽屉，点「会话」拉出，选完/点遮罩收起，聊天区拿满整行 */
@media (max-width: 700px) {
  .chat-layout { position: relative; grid-template-columns: minmax(0, 1fr); }
  .chat-sidebar {
    position: absolute; top: 0; bottom: 0; left: 0; z-index: 6;
    width: min(78vw, 280px);
    background: var(--canvas-bg);
    box-shadow: var(--shadow-lg);
    transform: translateX(-102%);
    transition: transform var(--transition-fast);
  }
  .chat-layout.sidebar-open .chat-sidebar { transform: none; }
  .sidebar-backdrop { position: absolute; inset: 0; z-index: 5; background: color-mix(in srgb, #000 32%, transparent); }
  .sidebar-toggle { display: inline-flex; }
}
@media (prefers-reduced-motion: reduce) { .chat-sidebar { transition: none; } }
</style>
