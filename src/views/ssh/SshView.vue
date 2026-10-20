<template>
  <div class="ssh-view">
    <!-- 主终端区域 -->
    <div class="ssh-main">
      <!-- 标签栏 -->
      <div class="term-tabs">
        <div class="tabs-scroll" @wheel.prevent="onTabsWheel">
          <!-- 首页标签：恒在、不可关闭，是打开应用时的第一个屏。
               AI 星芒取代房子：这里的主人是助手，不是一个"启动器"。 -->
          <div
            class="term-tab home-tab"
            :class="{ active: homeTabActive }"
            role="tab"
            :aria-selected="homeTabActive"
            :title="homeTabLabel"
            :aria-label="homeTabLabel"
            tabindex="0"
            @click="goHome"
            @keydown.enter.prevent="goHome"
            @keydown.space.prevent="goHome"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="12" height="12"><path d="M12 3l1.8 4.9L18.7 9.7l-4.9 1.8L12 16.4l-1.8-4.9L5.3 9.7l4.9-1.8z"/><path d="M18.6 15.4l.7 1.9 1.9.7-1.9.7-.7 1.9-.7-1.9-1.9-.7 1.9-.7z"/></svg>
            <span class="tab-label">首页</span>
            <span v-if="presence !== 'idle'" class="home-tab-dot" :class="presence" aria-hidden="true"></span>
          </div>

          <!-- 终端标签：中键关闭 / 拖拽排序 / title 全名（截断后可看全） -->
          <div
            v-for="s in store.sessions"
            :key="s.id"
            class="term-tab"
            :class="{ active: s.id === store.activeSessionId && terminalVisible }"
            role="tab"
            :aria-selected="s.id === store.activeSessionId && terminalVisible"
            :title="s.name"
            draggable="true"
            @click="activateTerminal(s.id)"
            @auxclick.middle.prevent="store.closeSession(s.id)"
            @contextmenu.prevent="showTabContextMenu($event, s.id)"
            @dragstart="onTabDragStart(s.id, $event)"
            @dragover.prevent
            @drop="onTabDrop(s.id, $event)"
            @dragend="dragTabId = ''"
          >
            <IconMonitor v-if="s.type === 'remote'" :width="12" :height="12" :stroke-width="2" />
            <span class="tab-label">{{ s.name }}</span>
            <button class="tab-close" aria-label="关闭终端" @click.stop="store.closeSession(s.id)">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </div>

          <!-- 运行过程不再有标签：它直接长在首页下面（指令卡与待放行队列之后）。
               标签那一版把首页整屏换掉，于是"看过程"和"下一条指令"变成互斥的两屏
               —— 而下达之后人两样都要。 -->

          <!-- 新建终端（本地 shell / 已配好的远程连接） -->
          <NewTerminalMenu @open-local="openLocalShell" @open-remote="connectRemote" />
        </div>

        <!-- 会话入口固定在滚动区之外：终端标签再多也不会把这两枚挤走。
             历史抽屉在 S2 挂载，这里先落按钮与状态。 -->
        <div class="tab-actions">
          <button
            ref="homeHistoryBtnRef"
            type="button"
            class="tab-act"
            title="历史记录"
            aria-label="历史记录"
            aria-controls="home-history-drawer"
            :aria-expanded="homeHistoryOpen"
            @click="toggleHomeHistory"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="14" height="14"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><polyline points="3 3 3 8 8 8"/><polyline points="12 7 12 12 15 14"/></svg>
          </button>
          <button
            type="button"
            class="tab-act"
            :disabled="agentStore.isRunning"
            :aria-disabled="agentStore.isRunning"
            :title="agentStore.isRunning ? '运行中不可新建会话' : '新建会话'"
            aria-label="新建会话"
            @click="startNewTask"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="14" height="14"><path d="M11 4H5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h13a2 2 0 0 0 2-2v-6"/><path d="M18.5 2.5a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4z"/></svg>
          </button>
        </div>
      </div>

      <!-- 内容区包装器: flex:1 + position:relative 为内部绝对定位提供参照 -->
      <div class="content-area">
        <!-- 终端内容区 (始终渲染以保持终端实例, 首页活动时被盖住) -->
        <div class="term-content" v-if="store.sessions.length > 0" v-show="terminalVisible">
          <template v-for="s in store.sessions" :key="s.id">
<LocalTerminal
v-if="s.type === 'local'"
:ref="getTerminalRefHandler(s.id)"
:pty-id="s.id"
:shell="s.shell"
:visible="s.id === store.activeSessionId && terminalVisible"
v-show="s.id === store.activeSessionId"
@exit="handleLocalExit(s.id, $event)"
/>
<RemoteTerminal
v-if="s.type === 'remote'"
:ref="getTerminalRefHandler(s.id)"
:ssh-id="s.id"
:visible="s.id === store.activeSessionId && terminalVisible"
v-show="s.id === store.activeSessionId"
@close="handleRemoteClose(s.id)"
/>
          </template>
        </div>

        <!-- RemoteManager 页面 -->
        <div v-show="activeNavPage === 'remote-mgr'" class="rm-page">
          <RemoteManager v-if="pageMounted('remote-mgr')"
            @connect="handleRmConnect"
            @edit="editConnection"
            @new-connection="editConnection(null)"
            :connecting="connecting"
            :pending-conn-id="pendingConnId"
          />
        </div>

        <!-- SFTP 文件传输页面 -->
        <div v-show="activeNavPage === 'sftp'" class="sftp-page">
          <SftpView v-if="pageMounted('sftp')" />
        </div>

        <!-- 运维审计页面 -->
        <div v-show="activeNavPage === 'audit'" class="audit-page">
          <AuditView v-if="pageMounted('audit')" />
        </div>

        <!-- 定时任务页面 -->
        <div v-show="activeNavPage === 'schedules'" class="schedules-page">
          <SchedulesView v-if="pageMounted('schedules')" :active="activeNavPage === 'schedules'" />
        </div>

        <!-- 监控仪表盘页面 -->
        <div v-show="activeNavPage === 'dashboard'" class="dashboard-page">
          <DashboardView
            v-if="pageMounted('dashboard')"
          />
        </div>

        <!-- SSH 隧道页面 -->
        <div v-show="activeNavPage === 'tunnels'" class="tunnel-page">
          <TunnelView v-if="pageMounted('tunnels')" />
        </div>

        <!-- Docker 管理页面（依附当前远程 SSH 会话） -->
        <div v-show="activeNavPage === 'docker'" class="docker-page">
          <DockerView v-if="pageMounted('docker')" :active="activeNavPage === 'docker'" @exec-terminal="activateTerminal($event)" />
        </div>

        <!-- GitHub 加速页面（镜像套件，纯本地功能） -->
        <div v-show="activeNavPage === 'github'" class="gh-page">
          <GithubAccelView v-if="pageMounted('github')" />
        </div>

        <!-- 首页：智能体任务台（消息区 + 常驻输入区，历史走右侧抽屉） -->
        <HomeWorkspace
          v-show="homeTabActive"
          :history-open="homeHistoryOpen"
          @close-history="closeHomeHistory"
          @bulk="showBulk = true"
        />
      </div>

      <!-- 快捷命令条 -->
      <QuickCommandBar v-if="showCommandBar" />
    </div>

    <!-- AI 运维助手面板 -->
    <SshAiPanel
      v-if="sshAiStore.showAiPanel"
      ref="aiPanelRef"
      @close="sshAiStore.closeAiPanel()"
      @open-home="onOpenHome"
    />

    <!-- 多节点批量下发（首页目标主机的「全部 N 台」入口；远程管理页同一组件） -->
    <BulkCommandModal v-if="showBulk" @close="showBulk = false" />

    <!-- 命令面板（Ctrl+Shift+P）：页面 / 会话 / 连接 / 快捷命令统一入口 -->
    <CommandPalette
      v-if="paletteOpen"
      @close="paletteOpen = false"
      @navigate="onPaletteNavigate"
      @go-home="onPaletteGoHome"
      @activate-session="onPaletteActivateSession"
      @connect="onPaletteConnect"
    />

    <!-- 连接弹窗 -->
    <ConnectionModal
      v-if="showModal"
      :edit-data="editingConnection"
      @close="closeModal"
      @saved="handleSaved"
    />

    <!-- 标签右键菜单 -->
    <Teleport to="body">
      <div
        v-if="tabCtxMenu.visible"
        class="tab-ctx-menu"
        :style="{ left: tabCtxMenu.x + 'px', top: tabCtxMenu.y + 'px' }"
        @click="tabCtxMenu.visible = false"
        @contextmenu.prevent
      >
        <div class="tab-ctx-item" @click="handleTabCtxAction('duplicate')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
          复制终端
        </div>
        <div class="tab-ctx-sep"></div>
        <div class="tab-ctx-item" @click="handleTabCtxAction('closeCurrent')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          关闭当前
        </div>
        <div class="tab-ctx-item" @click="handleTabCtxAction('closeLeft')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="15 18 9 12 15 6"/></svg>
          关闭左侧
        </div>
        <div class="tab-ctx-item" @click="handleTabCtxAction('closeRight')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="9 18 15 12 9 6"/></svg>
          关闭右侧
        </div>
        <div class="tab-ctx-sep"></div>
        <div class="tab-ctx-item danger" @click="handleTabCtxAction('closeAll')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="9" x2="15" y2="15"/><line x1="15" y1="9" x2="9" y2="15"/></svg>
          关闭全部
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref, computed, nextTick, watch, defineAsyncComponent } from 'vue'
import LocalTerminal from '@/components/ssh/LocalTerminal.vue'
import RemoteTerminal from '@/components/ssh/RemoteTerminal.vue'
import IconMonitor from '@/components/icons/IconMonitor.vue'
// 首页是首屏要看到的东西，静态引入不走异步 chunk
import HomeWorkspace from '@/components/home/HomeWorkspace.vue'
import NewTerminalMenu from '@/components/ssh/NewTerminalMenu.vue'
// 启动性能优化：非首屏功能整体移出启动关键路径 —
// 原实现全部静态 import + v-show（组件启动即全部挂载），每加一条功能线主包
// 就长大一截（index.js 一度 10MB，Monaco 文件编辑器占大头）。现在：
//   1. defineAsyncComponent 拆包，chunk 只在首次访问该页面时下载；
//   2. v-show 改「sticky-visited」v-if — 访问过才挂载、之后保持挂载，
//      保留 v-show 的状态不丢语义（切换标签不重置 SFTP 树/审计列表等）。
const ConnectionModal = defineAsyncComponent(() => import('@/components/ssh/ConnectionModal.vue'))
const BulkCommandModal = defineAsyncComponent(() => import('@/components/ssh/BulkCommandModal.vue'))
const SftpView = defineAsyncComponent(() => import('@/components/ssh/SftpView.vue'))
const RemoteManager = defineAsyncComponent(() => import('@/components/ssh/RemoteManager.vue'))
const QuickCommandBar = defineAsyncComponent(() => import('@/components/ssh/QuickCommandBar.vue'))
const SshAiPanel = defineAsyncComponent(() => import('@/components/ssh/SshAiPanel.vue'))
const AuditView = defineAsyncComponent(() => import('@/views/ssh/AuditView.vue'))
const SchedulesView = defineAsyncComponent(() => import('@/views/ssh/SchedulesView.vue'))
const DashboardView = defineAsyncComponent(() => import('@/views/ssh/DashboardView.vue'))
const TunnelView = defineAsyncComponent(() => import('@/components/ssh/TunnelView.vue'))
const DockerView = defineAsyncComponent(() => import('@/components/ssh/DockerView.vue'))
const GithubAccelView = defineAsyncComponent(() => import('@/components/ssh/GithubAccelView.vue'))
const CommandPalette = defineAsyncComponent(() => import('@/components/common/CommandPalette.vue'))
import { useSSHStore } from '@/stores/ssh'
import { useSshAiStore } from '@/stores/ssh-ai'
import { useAgentStore } from '@/stores/agent'
import { useTunnelStore } from '@/stores/tunnel'
import { useAccessStore } from '@/stores/access'
import { useSettings } from '@/composables/useSettings'
import { useCommandDelivery } from '@/composables/useCommandDelivery'
import { useSshNavigation } from '@/composables/useSshNavigation'
import { useGlobalShortcuts } from '@/composables/useGlobalShortcuts'
import { useAgentPresence } from '@/composables/useAgentPresence'
import { useToastStore } from '@/stores/toast'
import type { NavPage } from '@/composables/useSshNavigation'
import { useTabContextMenu } from '@/composables/useTabContextMenu'
import { qcHeightForAlign } from '@/utils/qc-align'

const store = useSSHStore()
const sshAiStore = useSshAiStore()
const agentStore = useAgentStore()
const { openSettings } = useSettings()
const toast = useToastStore()
const { presence, pendingCount } = useAgentPresence()

// 终端组件引用映射（key: sessionId, value: 组件实例）
const terminalRefs: Record<string, any> = {}
// P2 修复：为每个会话 id 缓存一个【稳定身份】的 ref 回调，避免模板里内联箭头函数
// 每次渲染都换身份 → Vue 对全部终端重复触发 detach/attach（终端实例虽不被重建，但 ref 回调被无谓调用）。
const terminalRefHandlers = new Map<string, (el: any) => void>()
function getTerminalRefHandler(id: string): (el: any) => void {
  let h = terminalRefHandlers.get(id)
  if (!h) {
    h = (el: any) => {
      if (el) terminalRefs[id] = el
      else delete terminalRefs[id]
    }
    terminalRefHandlers.set(id, h)
  }
  return h
}

// useSshNavigation 清理的是 terminalRefs，这张按 sessionId 缓存的回调表它管不到。
// 「复制终端」/开关标签每次都是新 id，不摘就是每个关掉的会话留一个闭包到应用退出。
watch(() => store.sessions.map(s => s.id), (ids) => {
  const active = new Set(ids)
  for (const id of [...terminalRefHandlers.keys()]) {
    if (!active.has(id)) terminalRefHandlers.delete(id)
  }
})

// 终端聚焦（useCommandDelivery 内部只注册 ssh:focus-terminal 监听；命令投递见其文件头的退役说明）
const { focusActiveTerminal } = useCommandDelivery(terminalRefs)

// 页面导航 + 终端生命周期 + 模态框 + 远程连接 + 首页/终端两态（运行过程已并入首页）
const {
  homeTabActive,
  terminalVisible,
  activeNavPage,
  showModal,
  editingConnection,
  showCommandBar,
  goHome,
  activateTerminal,
  toggleRemoteManager,
  toggleSftp,
  toggleAiPanel,
  toggleAudit,
  toggleSchedules,
  toggleDashboard,
  toggleTunnels,
  toggleDocker,
  toggleGithub,
  showAiPanel,
  openLocalShell,
  handleLocalExit,
  connectRemote,
  connecting,
  pendingConnId,
  handleRmConnect,
  handleRemoteClose,
  editConnection,
  closeModal,
  handleSaved,
} = useSshNavigation(focusActiveTerminal, terminalRefs)

/** 首页目标菜单里「全部 N 台」拉起的批量下发弹窗 */
const showBulk = ref(false)

/** 历史抽屉的开合。放在宿主而不是 useSshNavigation：那里每次调用都会新建一份状态。 */
const homeHistoryOpen = ref(false)
const homeHistoryBtnRef = ref<HTMLButtonElement | null>(null)

/** 抽屉关闭（Esc / 遮罩 / 关闭钮 / 选中会话）后焦点回到历史按钮：
 *  否则焦点落在被卸载的抽屉里，键盘用户会瞬移回文档开头。 */
function closeHomeHistory() {
  if (!homeHistoryOpen.value) return
  homeHistoryOpen.value = false
  void nextTick(() => homeHistoryBtnRef.value?.focus())
}

/** 状态点的文案与颜色同源（useAgentPresence）：颜色不是唯一信道，title/aria 说清是什么。 */
const homeTabLabel = computed(() => {
  if (presence.value === 'pending') return `首页 · ${pendingCount.value} 条待审批`
  if (presence.value === 'running') return '首页 · 运行中'
  if (presence.value === 'unread') return '首页 · 有新回复'
  return '首页'
})

/** 任务台可见即「看见」：清未读，离开时关掉历史抽屉（它属于首页）。 */
watch(homeTabActive, (active) => {
  sshAiStore.setHomeVisible(active)
  if (!active) homeHistoryOpen.value = false
}, { immediate: true })

function toggleHomeHistory() {
  goHome()
  homeHistoryOpen.value = !homeHistoryOpen.value
}

/** 「新建会话」：进入空白任务，不提前产生历史条目（首条消息发出才入历史）。 */
function startNewTask() {
  if (agentStore.isRunning) {
    toast.info('AI 正在运行中，请等待完成')
    return
  }
  if (!sshAiStore.beginNewTask()) {
    toast.info('AI 正在运行中，请等待完成')
    return
  }
  goHome()
  homeHistoryOpen.value = false
}

/** 侧栏的「去任务台」：关掉侧栏再回首页，审批卡只在内联过程里结算 */
function onOpenHome() {
  sshAiStore.closeAiPanel()
  goHome()
}

// ── 全局快捷键 + 命令面板（M1-P0）──
// 独立页统一开关表：快捷键与命令面板共用，避免八个 toggle 各写一遍分发
const PAGE_TOGGLES: Record<Exclude<NavPage, 'home'>, () => void> = {
  'remote-mgr': toggleRemoteManager,
  sftp: () => void toggleSftp(),
  audit: toggleAudit,
  schedules: toggleSchedules,
  dashboard: toggleDashboard,
  tunnels: toggleTunnels,
  docker: toggleDocker,
  github: toggleGithub,
}

const paletteOpen = ref(false)

function closeActiveSession(): void {
  if (store.activeSessionId) void store.closeSession(store.activeSessionId)
}

useGlobalShortcuts({
  activateTerminal,
  closeActiveSession,
  toggleAiPanel,
  toggleNavPage: (page) => PAGE_TOGGLES[page](),
  togglePalette: () => { paletteOpen.value = !paletteOpen.value },
  isPaletteOpen: () => paletteOpen.value,
  openSettings,
})

// 命令面板动作：先收面板再执行，避免面板遮挡后续页面的转场反馈
function onPaletteNavigate(page: Exclude<NavPage, 'home'>) {
  paletteOpen.value = false
  PAGE_TOGGLES[page]()
}
function onPaletteGoHome() {
  paletteOpen.value = false
  goHome()
}
function onPaletteActivateSession(id: string) {
  paletteOpen.value = false
  activateTerminal(id)
}
function onPaletteConnect(connId: string) {
  paletteOpen.value = false
  handleRmConnect(connId)
}

// ── 标签条增强：滚轮横滚 + 拖拽排序 ──
// tabs-scroll 的滚动条是 display:none 的，滚轮 delta 默认不产生横向位移，这里手工转接
function onTabsWheel(e: WheelEvent): void {
  const el = e.currentTarget as HTMLElement
  el.scrollLeft += e.deltaY + e.deltaX
}

const dragTabId = ref('')
function onTabDragStart(id: string, e: DragEvent): void {
  dragTabId.value = id
  if (e.dataTransfer) {
    e.dataTransfer.setData('text/plain', id)
    e.dataTransfer.effectAllowed = 'move'
  }
}
function onTabDrop(id: string, e: DragEvent): void {
  e.preventDefault()
  if (dragTabId.value && dragTabId.value !== id) {
    store.reorderSessions(dragTabId.value, id)
  }
  dragTabId.value = ''
}

// ── sticky-visited 页面挂载闸门（配合上方 defineAsyncComponent）──
type PageKey = typeof activeNavPage.value
const visitedPages = ref<Partial<Record<PageKey, boolean>>>({})
// immediate：恢复场景下 activeNavPage 可能已不是 home，首帧即放行该页挂载
watch(activeNavPage, (p) => { if (p !== 'home') visitedPages.value[p] = true }, { immediate: true })
const pageMounted = (p: PageKey): boolean => !!visitedPages.value[p]

// 标签右键菜单（内部注册全局 click 关闭监听）
const { tabCtxMenu, showTabContextMenu } = useTabContextMenu()

// 标签菜单动作桥接：连接菜单状态 → store 方法
function handleTabCtxAction(action: string) {
  const targetId = tabCtxMenu.value.targetId
  tabCtxMenu.value.visible = false
  store.handleTabContextAction(action, targetId)
}

onMounted(() => {
  store.init()
  // 隧道状态订阅全局存活（不挂在 TunnelView 上）：规则列表 + 运行状态推送
  // 在应用会话内持续更新，切到隧道页时数据已就绪
  void useTunnelStore().init()
  // 网关会话的订阅同样挂在这里（组件常驻）：网关卡片已并入远程管理页的 TLS 网关分段，
  // 「在线」徽标不能只在那个分段挂载后才活着
  void useAccessStore().init()
})

// ── QuickCommandBar 顶线对齐 ──
// QC 在 .ssh-main（左栏）底部，它在 y 上要对齐的不是右栏的分区顶线，而是
// .input-container 的 border-bottom —— 也就是输入框底下那条 1px 分隔线。
// 两栏同为 .ssh-view 的 stretch 子项、底边同 y，所以面板底边可直接当基线用。
// 算式抽到 @/utils/qc-align（含"两条线必须同像素行"的不变量单测）。
const aiPanelRef = ref<InstanceType<typeof SshAiPanel> | null>(null)
let syncObs: ResizeObserver | null = null

function setupQcSync() {
  syncObs?.disconnect()
  syncObs = null

  const panelEl = aiPanelRef.value?.$el as HTMLElement | undefined
  if (!panelEl) return

  const container = panelEl.querySelector('.input-container') as HTMLElement | null
  if (!container) return

  const apply = () => {
    const baseBottom = panelEl.getBoundingClientRect().bottom
    const h = qcHeightForAlign(baseBottom, container.getBoundingClientRect().bottom)
    if (h <= 0) return
    const root = panelEl.closest('.ssh-view') as HTMLElement | null
    if (!root) return
    if (root.style.getPropertyValue('--qc-h') === `${h}px`) return
    root.style.setProperty('--qc-h', `${h}px`)
  }

  apply()
  syncObs = new ResizeObserver(apply)
  // 面板被拖窄时工具条换行 → 它自身变高、分隔线上移，必须重算
  const toolbar = panelEl.querySelector('.input-toolbar') as HTMLElement | null
  if (toolbar) syncObs.observe(toolbar)
  // textarea 输入多行会把工具条顶上去 → 容器高度变、分隔线跟着移动
  syncObs.observe(container)
}

onMounted(() => nextTick(setupQcSync))
// QC bar 从隐藏切到显示时重新测量（此前若在窄面板下量过，值已经不准）
watch(showCommandBar, (v) => { if (v) nextTick(setupQcSync) })
// 监听面板实例而非 showAiPanel：SshAiPanel 是 defineAsyncComponent，首次打开
// 时 chunk 尚未 resolve，showAiPanel 翻转后的 nextTick 里 ref 还是 null，
// 观察器永远装不上、QC 停在兜底 38px。ref 在内部实例真正挂载后才置位，
// 关闭→重开（含缓存秒挂）也都会再次触发重测。
watch(aiPanelRef, (el) => { if (el) nextTick(setupQcSync) })
onUnmounted(() => syncObs?.disconnect())

// 暴露方法给父组件（TitleBar 事件）
defineExpose({
  toggleAiPanel,
  toggleRemoteManager,
  toggleSftp,
  toggleAudit,
  toggleSchedules,
  toggleDashboard,
  toggleTunnels,
  toggleDocker,
  toggleGithub,
  openSettings,
  activeNavPage,
  showAiPanel,
})
</script>

<style scoped>
.ssh-view {
  display: flex;
  flex: 1;
  overflow: hidden;
  position: relative;
  min-height: 0;
  /* --canvas-bg 默认就是 --bg-base；手记在里面加了纸纹 + 光斑。
     attachment:fixed 让标题条/标签条/画布取样同一个视口坐标，一条纸面不被切成三张 */
  background: var(--canvas-bg);
  background-attachment: fixed;
}

/* ─── Terminal Area ─── */
.ssh-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  position: relative;
  min-width: 0;
  min-height: 0;
}

/* ─── Content Area (终端与首页/各独立页面的容器) ─── */
.content-area {
  flex: 1;
  position: relative;
  overflow: hidden;
  min-height: 0;
}

/* ─── Tab Bar ─── */
.term-tabs {
  height: var(--tabbar-h);
  display: flex;
  align-items: center;
  background: var(--canvas-bg);
  background-attachment: fixed;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
  overflow: hidden;
  position: relative;
  z-index: 20;
}

.tabs-scroll {
  flex: 1;
  display: flex;
  align-items: flex-end;
  gap: 1px;
  overflow-x: auto;
  height: 100%;
}

.tabs-scroll::-webkit-scrollbar {
  display: none;
}

/* 会话入口：固定在滚动区右侧，标签再多也不被挤走 */
.tab-actions {
  flex: 0 0 auto;
  height: 100%;
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 0 6px;
  border-left: 1px solid var(--border-subtle);
}

.tab-act {
  width: 28px;
  height: 26px;
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

.tab-act:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.tab-act:disabled {
  opacity: 0.38;
  cursor: not-allowed;
}

/* 首页状态点：待审批（警示黄）> 运行中（呼吸蓝）> 未读（强调色）。
   颜色只是辅助信道 —— 标签的 title/aria-label 同时说明状态。 */
.home-tab-dot {
  width: 5px;
  height: 5px;
  margin-left: -1px;
  border-radius: 50%;
  flex-shrink: 0;
}

.home-tab-dot.pending { background: var(--status-warn-text); }
.home-tab-dot.running {
  background: var(--status-info-text);
  animation: home-dot-pulse 1.6s ease-in-out infinite;
}
.home-tab-dot.unread { background: var(--accent-primary); }

@keyframes home-dot-pulse {
  0%, 100% { opacity: 0.3; transform: scale(0.85); }
  50% { opacity: 1; transform: scale(1); }
}

@media (prefers-reduced-motion: reduce) {
  .home-tab-dot.running { animation: none; }
}

.term-tab {
  height: calc(var(--tabbar-h) - 4px);
  padding: 0 10px;
  display: flex;
  align-items: center;
  gap: 6px;
  border: 1px solid transparent;
  border-bottom: none;
  border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
  font-size: 12px;
  font-family: var(--font-sans);
  white-space: nowrap;
  flex-shrink: 0;
  transition: all var(--transition-fast);
}

.term-tab:hover {
  color: var(--text-secondary);
  background: var(--bg-hover);
}

.term-tab:active {
  background: var(--bg-active);
}

.term-tab.active {
  color: var(--text-primary);
  border-color: var(--border-default);
  margin-bottom: -1px;
  padding-bottom: 1px;
}

.tab-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 120px;
}

.tab-close {
  width: 16px;
  height: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  border-radius: 3px;
  color: var(--text-tertiary);
  cursor: pointer;
  opacity: 0;
  transition: opacity var(--transition-fast);
}

.term-tab:hover .tab-close,
.term-tab.active .tab-close {
  opacity: 1;
}

.tab-close:hover {
  background: var(--bg-active);
  color: var(--accent-red);
}

.tab-close svg {
  width: 10px;
  height: 10px;
}

.term-content {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  overflow: hidden;
  /* 终端 ANSI 主题固定暗色，必须给独立深底：浅色主题下若透出浅色底层会不可读。
     背景图模式下由文件末尾的 has-background 规则置为透明（用户主动选择）。 */
  background: var(--bg-terminal);
}

/* ─── Directory Panel ─── */

/* ─── RemoteManager Page ─── */
.rm-page {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 10;
  overflow: hidden;
}

/* ─── SFTP Page ─── */
.sftp-page {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 10;
  overflow: hidden;
}

/* ─── Audit Page ─── */
.audit-page {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 10;
  overflow: hidden;
}

/* ─── Schedules Page ─── */
.schedules-page {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 10;
  overflow: hidden;
  display: flex;
}

/* ─── Dashboard Page ─── */
.dashboard-page {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 10;
  overflow: hidden;
}

/* ─── Tunnel Page ─── */
.tunnel-page {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 10;
  overflow: hidden;
}

/* ─── Docker Page ─── */
.docker-page {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 10;
  overflow: hidden;
}

/* ─── GitHub 加速 Page ─── */
.gh-page {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 10;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

/* ─── Tab Context Menu ─── */
.tab-ctx-menu {
  position: fixed;
  z-index: 9999;
  min-width: 140px;
  padding: 4px 0;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: 6px;
  box-shadow: var(--shadow-lg);
}

.tab-ctx-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
  transition: background 0.1s;
}

.tab-ctx-item:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.tab-ctx-item.danger:hover {
  background: color-mix(in srgb, var(--accent-red) 15%, transparent);
  color: var(--accent-red);
}

.tab-ctx-sep {
  height: 1px;
  margin: 4px 0;
  background: var(--border-subtle);
}

/* 启用自定义背景图时，让主内容区透明 */
:global(.app.has-background) .ssh-view,
:global(.app.has-background) .term-tabs,
:global(.app.has-background) .term-content {
  background: transparent !important;
}
</style>
