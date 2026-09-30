<template>
  <div class="ssh-view">
    <!-- 主终端区域 -->
    <div class="ssh-main">
      <!-- 标签栏 -->
      <div class="term-tabs">
        <div class="tabs-scroll">
          <!-- 首页标签：恒在、不可关闭，是打开应用时的第一个屏 -->
          <div
            class="term-tab home-tab"
            :class="{ active: homeTabActive }"
            title="首页"
            @click="goHome"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12"><path d="M4 11l8-7 8 7v9a1 1 0 0 1-1 1h-4v-6h-6v6H5a1 1 0 0 1-1-1z"/></svg>
            <span class="tab-label">首页</span>
          </div>

          <!-- 终端标签 -->
          <div
            v-for="s in store.sessions"
            :key="s.id"
            class="term-tab"
            :class="{ active: s.id === store.activeSessionId && !showHome }"
            @click="activateTerminal(s.id)"
            @contextmenu.prevent="showTabContextMenu($event, s.id)"
          >
            <IconMonitor v-if="s.type === 'remote'" :width="12" :height="12" :stroke-width="2" />
            <span class="tab-label">{{ s.name }}</span>
            <button class="tab-close" @click.stop="store.closeSession(s.id)">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </div>

          <!-- 新建终端（本地 shell / 已配好的远程连接） -->
          <NewTerminalMenu @open-local="openLocalShell" @open-remote="connectRemote" />
        </div>
      </div>

      <!-- 内容区包装器: flex:1 + position:relative 为内部绝对定位提供参照 -->
      <div class="content-area">
        <!-- 终端内容区 (始终渲染以保持终端实例, 首页标签活动时被盖住) -->
        <div class="term-content" v-if="store.sessions.length > 0" v-show="!showHome">
          <template v-for="s in store.sessions" :key="s.id">
<LocalTerminal
v-if="s.type === 'local'"
:ref="(el) => { if (el) terminalRefs[s.id] = el; else delete terminalRefs[s.id] }"
:pty-id="s.id"
:shell="s.shell"
:visible="s.id === store.activeSessionId && !showHome"
v-show="s.id === store.activeSessionId"
@exit="handleLocalExit(s.id, $event)"
/>
<RemoteTerminal
v-if="s.type === 'remote'"
:ref="(el) => { if (el) terminalRefs[s.id] = el; else delete terminalRefs[s.id] }"
:ssh-id="s.id"
:visible="s.id === store.activeSessionId && !showHome"
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
          <DashboardView v-if="pageMounted('dashboard')" />
        </div>

        <!-- 首页：目标主机 + 下达 + 跨 run 待放行汇总 -->
        <div v-show="homeTabActive" class="home-pane">
          <HomeTargetTitle @bulk="showBulk = true" />
          <HomeCommandCard />
          <HomeApprovalQueue />
        </div>
      </div>

      <!-- 快捷命令条 -->
      <QuickCommandBar v-if="showCommandBar" />
    </div>

    <!-- AI 运维助手面板 -->
    <SshAiPanel v-if="sshAiStore.showAiPanel" ref="aiPanelRef" @close="sshAiStore.closeAiPanel()" />

    <!-- 多节点批量下发（首页目标主机的「全部 N 台」入口；远程管理页同一组件） -->
    <BulkCommandModal v-if="showBulk" @close="showBulk = false" />

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
import { onMounted, onUnmounted, ref, nextTick, watch, defineAsyncComponent } from 'vue'
import LocalTerminal from '@/components/ssh/LocalTerminal.vue'
import RemoteTerminal from '@/components/ssh/RemoteTerminal.vue'
import IconMonitor from '@/components/icons/IconMonitor.vue'
// 首页主角静态引入：下面那批 defineAsyncComponent 拆的是"首次访问才下载"的页面，
// 而目标标题、指令卡与待放行队列是打开应用第一眼要看到的东西，不该等一个 chunk。
import HomeTargetTitle from '@/components/home/HomeTargetTitle.vue'
import HomeCommandCard from '@/components/home/HomeCommandCard.vue'
import HomeApprovalQueue from '@/components/home/HomeApprovalQueue.vue'
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
import { useSSHStore } from '@/stores/ssh'
import { useSshAiStore } from '@/stores/ssh-ai'
import { useSettings } from '@/composables/useSettings'
import { useCommandDelivery } from '@/composables/useCommandDelivery'
import { useSshNavigation } from '@/composables/useSshNavigation'
import { useTabContextMenu } from '@/composables/useTabContextMenu'
import { qcHeightForAlign } from '@/utils/qc-align'

const store = useSSHStore()
const sshAiStore = useSshAiStore()
const { openSettings } = useSettings()

// 终端组件引用映射（key: sessionId, value: 组件实例）
const terminalRefs: Record<string, any> = {}

// 命令投递 + 终端聚焦（内部注册 ssh:focus-terminal / ssh:deliver-command 监听）
const { focusActiveTerminal } = useCommandDelivery(terminalRefs)

// 页面导航 + 终端生命周期 + 模态框 + 远程连接 + 首页/终端标签态
const {
  showHome,
  homeTabActive,
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
  background: var(--bg-base);
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
  background: var(--bg-base);
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

/* ─── Home Pane ─── */
.home-pane {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 48px 24px;
  overflow-y: auto;
  z-index: 10;
}

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

/* ─── Tab Context Menu ─── */
.tab-ctx-menu {
  position: fixed;
  z-index: 9999;
  min-width: 140px;
  padding: 4px 0;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: 6px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
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
  background: rgba(244, 135, 113, 0.15);
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
