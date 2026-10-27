<template>
  <div class="home-workspace">
    <!-- 左上角会话入口：钉在视口角落的页面级操作（不随消息流滚动）。
         历史开居中弹窗，新建会话就地开始空白任务 —— 按钮只在首页可见，无需跳转。 -->
    <div class="home-corner">
      <button
        ref="historyBtn"
        type="button"
        class="corner-btn"
        title="历史记录"
        aria-label="历史记录"
        aria-controls="home-history-dialog"
        :aria-expanded="historyOpen"
        @click="historyOpen = !historyOpen"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><polyline points="3 3 3 8 8 8"/><polyline points="12 7 12 12 15 14"/></svg>
      </button>
      <button
        type="button"
        class="corner-btn"
        :disabled="agentStore.isRunning"
        :title="agentStore.isRunning ? '运行中不可新建会话' : '新建会话'"
        aria-label="新建会话"
        @click="startNewTask"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="M11 4H5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h13a2 2 0 0 0 2-2v-6"/><path d="M18.5 2.5a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4z"/></svg>
      </button>
    </div>
    <div class="home-main">
      <div ref="scrollRef" class="home-scroll">
        <div class="home-column" :class="{ 'is-empty': isEmpty }">
          <h1 v-if="isEmpty" class="home-ask">今天想让助手替你做什么？</h1>
          <template v-else>
            <!-- 标题与首条气泡同文时不画：标题行随消息流滚动、就贴在首条消息上方，
                 同文时读起来是「两条一模一样的用户消息」（首页"两个用户气泡"）。 -->
            <div v-if="showTitle" class="home-title" :title="conversationTitle">{{ conversationTitle }}</div>
            <!-- 流顶折叠按钮（纯展示层裁剪）：超长会话只渲染最新窗口，更早的消息仍
                 完整留在 store 里，点一次多放行一批。挂在 role=log 之外 —— 它是
                 操作不是日志内容；放行到全量后 hiddenMessageCount 归零自动消失。 -->
            <button
              v-if="hiddenMessageCount > 0"
              type="button"
              class="older-messages-btn"
              @click="showOlderMessages"
            >
              显示更早的消息（还有 {{ hiddenMessageCount }} 条）
            </button>
            <div class="home-messages" role="log" aria-live="polite">
              <MessageItem
                v-for="(msg, idx) in windowedMessages"
                :key="msg.id"
                :message="msg"
                :is-last="hiddenMessageCount + idx === displayMessages.length - 1"
                :is-streaming="isLiveAt(hiddenMessageCount + idx)"
                :awaiting-user="agentStore.awaitingClarification"
                @open-trace="emit('open-trace', $event)"
              />
            </div>
          </template>
          <!-- 运行通知（任务规划 / 警告 / 人工介入 / 错误）长在消息流末尾，
               紧跟这一轮回答。两个决定别拆开看：
               ① 宿主不写挂载条件 —— hasRunNotice 是数据侧的唯一判定，面板加区块而这里
                  忘记同步，就是「需要决策时面板根本没挂载、用户只看到一条 toast」；
               ② 它不在 isEmpty 的 v-else 分支里 —— 消息为空时整块曾经跟着一起不挂载，
                  那是这份面板最早踩过的挂载洞，洞的形状由位置决定，不由「今天有没有
                  空态下的 run」决定。注：意图澄清已移入输入区上方的 ApprovalDock。 -->
          <AgentRunPanel />
        </div>
      </div>
      <HomeHistoryDialog
        v-if="historyOpen"
        :groups="sshAi.groupedConversations"
        :current-conv-id="sshAi.currentConvId"
        :is-empty="sshAi.conversations.length === 0"
        :busy="agentStore.isRunning"
        @close="closeHistory"
        @switch="switchConversation"
        @delete="deleteConversation"
      />
    </div>
    <div class="composer-wrap">
      <!-- 审批面钉在输入区上方：待决请求不属于消息流，不能跟着历史滚走 -->
      <ApprovalDock />
      <HomeComposer @bulk="emit('bulk')" @remote-mgr="emit('remote-mgr')" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useSshAiStore } from '@/stores/ssh-ai'
import { useAgentStore } from '@/stores/agent'
import { useSettingsStore } from '@/stores/settings'
import { useToastStore } from '@/stores/toast'
import { buildDisplayMessages, isLiveAgentRun } from '@/utils/display-messages'
import { isTitleEcho } from '@/utils/conversation-title'
import { shouldFollowScroll } from '@/utils/scroll-follow'
import type { BaseMessage } from '@/types/chat'
// MessageItem 的依赖链带着 marked/highlight.js/DOMPurify。它是首页里的常驻组件，
// 静态引入会把整条 Markdown 栈钉进入口包；异步化后空态首屏不下载，第一条消息
// 出现时才按需加载（与 SshView 拆页同一手法）。
const MessageItem = defineAsyncComponent(() => import('@/components/chat/MessageItem.vue'))
import HomeComposer from '@/components/home/HomeComposer.vue'
import HomeHistoryDialog from '@/components/home/HomeHistoryDialog.vue'
import AgentRunPanel from '@/components/agent/AgentRunPanel.vue'
import ApprovalDock from '@/components/agent/ApprovalDock.vue'

const emit = defineEmits<{
  bulk: []
  'remote-mgr': []
  'open-trace': [runId: string]
}>()

const sshAi = useSshAiStore()
const agentStore = useAgentStore()
const settingsStore = useSettingsStore()
const toast = useToastStore()

const scrollRef = ref<HTMLElement | null>(null)

// ── 历史弹窗与新建会话（左上角入口的状态自留地）──
const historyOpen = ref(false)
const historyBtn = ref<HTMLButtonElement | null>(null)

/** 关闭后把焦点还给触发按钮：Teleport 弹窗卸载时焦点会掉到 body，
 *  键盘用户再按 Tab 就得从头摸一遍。 */
function closeHistory() {
  historyOpen.value = false
  void nextTick(() => historyBtn.value?.focus())
}

/** 「新建会话」：就地开始空白任务，不提前产生历史条目（首条消息发出才入历史）。
 *  beginNewTask 在运行中返回 false —— 按钮禁用之外再挡一道。 */
function startNewTask() {
  if (!sshAi.beginNewTask()) {
    toast.info('AI 正在运行中，请等待完成')
    return
  }
  historyOpen.value = false
}

const isEmpty = computed(() => sshAi.messages.length === 0)
const conversationTitle = computed(() =>
  sshAi.conversations.find(c => c.id === sshAi.currentConvId)?.title || '新会话',
)

/** 标题行是否值得画：本地标题就是首条消息的 30 字摘要（S9-05），同文时不画，
 *  否则用户看到的是两个「你好」—— 一个是标题、一个是气泡。 */
const showTitle = computed(() => {
  if (isEmpty.value) return false
  const firstUser = sshAi.messages.find(m => m.role === 'user')?.content ?? ''
  return !isTitleEcho(conversationTitle.value, firstUser)
})

/** live 合并走 utils/display-messages 的纯函数；非运行中直接返回 store 的原数组，保持消息引用稳定 */
const displayMessages = computed<readonly BaseMessage[]>(() => {
  if (!agentStore.isRunning) return sshAi.messages
  return buildDisplayMessages(sshAi.messages, {
    running: true,
    content: agentStore.content,
    reasoning: agentStore.reasoning,
    trace: agentStore.snapshot(),
  })
})

function isLiveAt(idx: number): boolean {
  // 第四个参数：run 停在澄清闸口等用户作答时不算"正在输出"—— 后端已挂住，
  // 没有 token 在产出，界面不能再画呼吸点/「正在思考」（用户报的问题）。
  return isLiveAgentRun(
    displayMessages.value, idx, agentStore.isRunning, agentStore.awaitingClarification)
}

// ── 消息流裁剪（纯展示层）：超长会话把整段历史全量渲染，首屏挂载、滚动与内存
//    一起受损。这里只改「画多少」，不改「存多少」—— 数据仍完整在 sshAi.messages
//    （唯一对话面），窗口永远端锚在最新消息上，live 合并出的那条流式消息天然在窗内。
const INITIAL_VISIBLE_MESSAGES = 80
const OLDER_MESSAGES_STEP = 200
const visibleCount = ref(INITIAL_VISIBLE_MESSAGES)

/** 折叠在流顶的旧消息条数：放行到全量后归 0，按钮随之消失 */
const hiddenMessageCount = computed(() =>
  Math.max(0, displayMessages.value.length - visibleCount.value))

/** 窗口内的消息：slice 按整条消息切 —— 执行过程（reasoning/agentTrace）长在
 *  助手消息对象上、随整条进退，折叠边界永远落在消息之间，不会把它与所属回答拆开。
 *  无折叠时原样返回 store 数组，保持消息引用稳定（MessageItem 不无故重挂）。 */
const windowedMessages = computed<readonly BaseMessage[]>(() => {
  const total = displayMessages.value.length
  if (hiddenMessageCount.value === 0) return displayMessages.value
  return displayMessages.value.slice(total - visibleCount.value)
})

/** 「显示更早的消息」：多放行一批旧消息，并把视口锚回新展开内容与原首条消息的
 *  衔接处 —— 顶部凭空长高后 scrollTop 若不补，浏览器保底归位看起来就像跳到底部。
 *  Chromium 原生滚动锚定已在 .home-scroll 上关掉（overflow-anchor:none），
 *  否则两套补偿叠加会把视口推过头。 */
function showOlderMessages() {
  const el = scrollRef.value
  const heightBefore = el?.scrollHeight ?? 0
  visibleCount.value += OLDER_MESSAGES_STEP
  void nextTick(() => {
    if (!el) return
    el.scrollTop += el.scrollHeight - heightBefore
  })
}

// ── 自动滚动：一次性事件（新消息/切会话/运行通知）无条件跟到底；流式正文/思考
//    走「贴底跟随」—— 用户上翻查看历史时不抢滚动条 ──
// rAF 合帧：流式期间 content/reasoning 每秒触发数十次请求，若每次都 nextTick 后
// 读 scrollHeight，等于每秒几十次强制 layout。同一帧内的触发只排一次队，
// scrollTop 写入（连带那次必然的布局读取）每帧最多一次。
let scrollRafId = 0
function scheduleScrollToBottom() {
  if (!settingsStore.settings.chat.autoScroll) return
  if (scrollRafId) return
  scrollRafId = requestAnimationFrame(() => {
    scrollRafId = 0
    const el = scrollRef.value
    if (!el) return
    el.scrollTop = el.scrollHeight
    // 写入后我们就在底部，标志直接回真、不等下一次 scroll 事件：切到内容不足
    // 一屏的会话时 scrollTop 写入可能是 no-op、不派发 scroll 事件，不回真的话
    // 下一段流式会错判「用户上翻了」而从此不再跟随。
    stickyFollow = true
  })
}

// 贴不贴底只在 scroll 事件里读数：浏览器派发该事件时布局已就绪，读取不触发
// 强制 reflow。流式热路径（token 到达）只查这个标志，不做任何布局读取；
// 内容增长本身不派发 scroll 事件 —— 贴底时标志保持 true，跟随不断线。
let stickyFollow = true
function onScroll() {
  const el = scrollRef.value
  if (el) stickyFollow = shouldFollowScroll(el.scrollTop, el.clientHeight, el.scrollHeight)
}
onMounted(() => scrollRef.value?.addEventListener('scroll', onScroll, { passive: true }))
onUnmounted(() => {
  scrollRef.value?.removeEventListener('scroll', onScroll)
  if (scrollRafId) cancelAnimationFrame(scrollRafId)
})

/** 流式跟随（content/reasoning 的高频触发）：只在本就贴底时才跟着滚；
 *  一次性事件（新消息/切会话/通知）不问贴底，直接走 scheduleScrollToBottom。 */
function requestFollow() {
  if (!stickyFollow) return
  scheduleScrollToBottom()
}

watch(() => displayMessages.value.length, () => { scheduleScrollToBottom() }, { immediate: true })
// 切会话时消息条数可能恰好相同，length watch 不会动 —— 单独跟一次，避免停在上一段的位置
watch(() => sshAi.currentConvId, () => {
  // 裁剪窗口一并回到初始 80 条：放大过的窗口是上一段历史的临时状态，不该带进新会话
  visibleCount.value = INITIAL_VISIBLE_MESSAGES
  scheduleScrollToBottom()
})
watch(() => agentStore.content, () => { if (agentStore.isRunning) requestFollow() })
watch(() => agentStore.reasoning, () => { if (agentStore.isRunning) requestFollow() })
// 运行通知区块（任务规划/警告/人工介入/错误）是消息流之外的另一段高度变化，
// 出现时同样要跟到底 —— 否则提示停在视野下方。
watch(() => agentStore.hasRunNotice, () => { scheduleScrollToBottom() })

function switchConversation(id: string) {
  if (agentStore.isRunning) {
    toast.info('AI 正在运行中，请等待完成')
    return
  }
  void sshAi.switchConversation(id)
  closeHistory()
}

/** 历史条目点垃圾桶即删，不再弹二次确认：入口按钮本身已带 aria-label 与
 *  运行中禁用，删除后再多问一遍只是把「已经做出的决定」重问一次。 */
function deleteConversation(id: string) {
  if (agentStore.isRunning) {
    toast.info('AI 正在运行中，请等待完成')
    return
  }
  void sshAi.deleteConversation(id)
}
</script>

<style scoped>
.home-workspace {
  position: absolute;
  inset: 0;
  z-index: 10;
  display: flex;
  flex-direction: column;
}

/* 左上角会话入口：ghost 图标钮，页面级操作钉在视口角落 */
.home-corner {
  position: absolute;
  top: 10px;
  left: 12px;
  z-index: 5;
  display: flex;
  gap: 2px;
}

.corner-btn {
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast);
}
.corner-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}
.corner-btn:disabled {
  opacity: 0.38;
  cursor: not-allowed;
}

.home-main {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
}

.home-scroll {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  /* 展开折叠旧消息的补滚由 showOlderMessages 自己算：关掉 Chromium 的原生滚动
     锚定，两套补偿叠加会把视口推过头 */
  overflow-anchor: none;
}

.home-column {
  width: 100%;
  max-width: 820px;
  min-height: 100%;
  margin: 0 auto;
  padding: 32px 24px 16px;
  box-sizing: border-box;
}

/* 空白任务：问句落在视口中间略偏上，输入区仍钉在底部（两态位置一致） */
.home-column.is-empty {
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  padding-bottom: 12vh;
}

.home-ask {
  margin: 0;
  text-align: center;
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 600;
  line-height: 1.5;
  color: var(--text-primary);
  letter-spacing: 0.2px;
}

/* 会话标题随消息流滚动，不做固定头部 */
.home-title {
  margin: 0 0 8px;
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 600;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 流顶折叠按钮：居中细长 ghost 条，次级文字色、hover 提亮（与 corner-btn 同族语言） */
.older-messages-btn {
  display: block;
  margin: 0 auto 12px;
  padding: 5px 14px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-pill);
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast);
}
.older-messages-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

/* 运行通知紧跟消息流：与最后一条回答之间留一道呼吸，不粘连 */
.agent-run-panel {
  margin-top: 12px;
}

.composer-wrap {
  flex: 0 0 auto;
  padding: 8px 24px 18px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}
</style>
