<template>
  <div class="ssh-view">
    <!-- 主终端区域 -->
    <div class="ssh-main">
      <!-- 标签栏 -->
      <div class="term-tabs">
        <div class="tabs-scroll">
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

          <!-- 新建按钮（加号，跟随终端标签右侧） -->
          <button
            class="new-tab-btn"
            :class="{ active: showHome && activeNavPage === 'home' }"
            title="新建终端"
            @click="goHome"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
              <line x1="12" y1="5" x2="12" y2="19"/>
              <line x1="5" y1="12" x2="19" y2="12"/>
            </svg>
          </button>
        </div>
      </div>

      <!-- 内容区包装器: flex:1 + position:relative 为内部绝对定位提供参照 -->
      <div class="content-area">
        <!-- 终端内容区 (始终渲染以保持终端实例, 被quick-start覆盖或隐藏) -->
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

        <!-- Quick Start 页面 -->
        <div v-show="(showHome || store.sessions.length === 0) && activeNavPage === 'home'" class="quick-start">
          <!-- 首页主角：下达 + 跨 run 待放行汇总（Quick Start 启动器暂留在下方，
               标签态改造在下一片一并处理） -->
          <HomeCommandCard />
          <HomeApprovalQueue />

          <div class="qs-header">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
            <span>Quick Start</span>
          </div>

          <div class="qs-search">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
            <input v-model="qsSearch" placeholder="搜索终端或连接..." class="qs-input" />
          </div>

          <!-- 本地终端 -->
          <div class="qs-section">
            <div class="qs-section-title">本地终端</div>
            <div class="qs-grid">
              <div
                v-for="shell in filteredShells"
                :key="shell"
                class="qs-card"
                @click="openLocalShell(shell)"
              >
                <div class="qs-card-icon local">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
                </div>
                <span class="qs-card-label">{{ shell }}</span>
              </div>
            </div>
          </div>

          <!-- 远程连接 -->
          <div class="qs-section">
            <div class="qs-section-title">远程连接</div>
            <div class="qs-grid">
              <div
                v-for="conn in filteredConnections"
                :key="conn.id"
                class="qs-card"
                @click="connectRemote(conn)"
                @contextmenu.prevent="editConnection(conn)"
              >
                <div class="qs-card-icon remote">
                  <IconMonitor :width="20" :height="20" :stroke-width="2" />
                </div>
                <span class="qs-card-label">{{ conn.name }}</span>
                <span class="qs-card-meta">{{ conn.host }}</span>
              </div>
            </div>
          </div>

          <div v-if="filteredShells.length === 0 && filteredConnections.length === 0" class="qs-empty">
            没有匹配的结果
          </div>
        </div>
      </div>

      <!-- 快捷命令条 -->
      <QuickCommandBar v-if="showCommandBar" />
    </div>

    <!-- AI 运维助手面板 -->
    <SshAiPanel v-if="sshAiStore.showAiPanel" ref="aiPanelRef" @close="sshAiStore.closeAiPanel()" />

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
// 而指令卡与待放行队列是打开应用第一眼要看到的东西，不该等一个 chunk。
import HomeCommandCard from '@/components/home/HomeCommandCard.vue'
import HomeApprovalQueue from '@/components/home/HomeApprovalQueue.vue'
// 启动性能优化：非首屏功能整体移出启动关键路径 —
// 原实现全部静态 import + v-show（组件启动即全部挂载），每加一条功能线主包
// 就长大一截（index.js 一度 10MB，Monaco 文件编辑器占大头）。现在：
//   1. defineAsyncComponent 拆包，chunk 只在首次访问该页面时下载；
//   2. v-show 改「sticky-visited」v-if — 访问过才挂载、之后保持挂载，
//      保留 v-show 的状态不丢语义（切换标签不重置 SFTP 树/审计列表等）。
const ConnectionModal = defineAsyncComponent(() => import('@/components/ssh/ConnectionModal.vue'))
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

// 页面导航 + 终端生命周期 + 模态框 + 远程连接 + Quick Start + 自动回首页
const {
  showHome,
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
  qsSearch,
  filteredShells,
  filteredConnections,
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

/* ─── Content Area (终端和Quick Start的容器) ─── */
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

/* Home按钮(加号) - 跟随终端标签右侧,样式与 term-tab 一致但无内容 */
.new-tab-btn {
  height: calc(var(--tabbar-h) - 4px);
  width: calc(var(--tabbar-h) - 4px);
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid transparent;
  border-bottom: none;
  border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
  flex-shrink: 0;
  transition: all var(--transition-fast);
}

.new-tab-btn:hover {
  color: var(--text-secondary);
  background: var(--bg-hover);
}

.new-tab-btn:active {
  background: var(--bg-active);
}

.new-tab-btn.active {
  color: var(--accent-primary);
  border-color: var(--border-default);
  margin-bottom: -1px;
  padding-bottom: 1px;
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

/* ─── Quick Start ─── */
.quick-start {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 40px 24px;
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

.qs-header {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--accent-primary);
  font-size: 18px;
  font-weight: 600;
  margin-bottom: 24px;
}

.qs-search {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  max-width: 520px;
  height: 36px;
  padding: 0 12px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  color: var(--text-tertiary);
  margin-bottom: 32px;
  transition: border-color var(--transition-fast);
}

.qs-search:focus-within {
  border-color: var(--accent-primary);
}

.qs-input {
  flex: 1;
  border: none;
  background: transparent;
  color: var(--text-primary);
  font-size: 13px;
  font-family: var(--font-sans);
  outline: none;
}

.qs-input::placeholder {
  color: var(--text-tertiary);
}

.qs-section {
  width: 100%;
  max-width: 520px;
  margin-bottom: 28px;
}

.qs-section-title {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--text-tertiary);
  margin-bottom: 12px;
}

.qs-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(90px, 1fr));
  gap: 12px;
}

.qs-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 16px 8px;
  border-radius: var(--radius-md);
  border: 1px solid transparent;
  cursor: pointer;
  transition: all var(--transition-fast);
}

.qs-card:hover {
  background: var(--bg-hover);
  border-color: var(--border-default);
}

.qs-card-icon {
  width: 48px;
  height: 48px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-lg);
  border: 1px solid var(--border-subtle);
}

.qs-card-icon.local {
  background: rgba(88, 166, 255, 0.08);
  color: var(--accent-primary);
}

.qs-card-icon.remote {
  background: rgba(63, 185, 80, 0.08);
  color: var(--accent-green);
}

.qs-card-label {
  font-size: 12px;
  color: var(--text-primary);
  font-weight: 500;
  text-align: center;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 80px;
}

.qs-card-meta {
  font-size: 10px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  text-align: center;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 80px;
}

.qs-empty {
  color: var(--text-tertiary);
  font-size: 13px;
  padding: 20px;
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
