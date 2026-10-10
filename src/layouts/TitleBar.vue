<template>
  <div class="titlebar">
    <div class="titlebar-brand">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14">
        <rect x="2" y="4" width="20" height="16" rx="2"/>
        <path d="m6 8 4 4-4 4"/>
        <line x1="12" y1="16" x2="18" y2="16"/>
      </svg>
      <span class="brand-text">cKunAgentSSH</span>
    </div>
    <div class="titlebar-drag" @dblclick="toggleMaximize"></div>
    <!-- 功能按钮 -->
    <div class="titlebar-actions" @dblclick.stop>
      <button class="titlebar-action-btn" :class="{ active: activePage === 'ai' }" :title="agent.isRunning ? 'AI 运维助手（运行中）' : 'AI 运维助手'" @click="emit('action-ai')">
        <span class="ai-text">AI</span>
        <!-- 两个入口在视觉上是同一个助手的两扇门：跑起来呼吸点、跑完且面板
             关着时未读点（ssh-ai store 的 unreadReply，开面板即清）。
             徽章只作辅助信道：title 文案同步说明状态，不单靠颜色传义。 -->
        <span v-if="agent.isRunning" class="ai-badge run" aria-hidden="true"></span>
        <span v-else-if="sshAi.unreadReply" class="ai-badge unread" aria-hidden="true"></span>
      </button>
      <button class="titlebar-action-btn" :class="{ active: activePage === 'schedules' }" title="定时任务" @click="emit('action-schedules')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 16 14"/></svg>
      </button>
      <button class="titlebar-action-btn" :class="{ active: activePage === 'dashboard' }" title="监控仪表盘" @click="emit('action-dashboard')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
      </button>
      <button class="titlebar-action-btn" :class="{ active: activePage === 'remote-mgr' }" title="远程终端" @click="emit('action-remote')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m6 8 4 4-4 4"/><line x1="12" y1="16" x2="18" y2="16"/></svg>
      </button>
      <button class="titlebar-action-btn" :class="{ active: activePage === 'tunnels' }" title="隧道" @click="emit('action-tunnels')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14"><path d="M7 17 17 7"/><path d="M8 7h9v9"/></svg>
      </button>
      <button class="titlebar-action-btn" :class="{ active: activePage === 'docker' }" title="Docker 管理" @click="emit('action-docker')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="14" height="14"><path d="M6 9.5h3v3H6zM11 9.5h3v3h-3zM11 6h3v3h-3z"/><path d="M3.5 12.5h13c1.6 0 3-.7 3.8-2 .7 2.3-.4 5-3.2 5H7a3.5 3.5 0 0 1-3.5-3z"/><path d="M3.5 12.5 2 10"/></svg>
      </button>
      <button class="titlebar-action-btn" :class="{ active: activePage === 'audit' }" title="运维审计" @click="emit('action-audit')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M9 12l2 2 4-4"/><path d="M20 12a8 8 0 1 1-2.34-5.66"/></svg>
      </button>
      <button class="titlebar-action-btn" :class="{ active: activePage === 'sftp' }" title="SFTP" @click="emit('action-sftp')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
      </button>
      <button class="titlebar-action-btn" title="设置" @click="emit('action-settings')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
      </button>
    </div>
    <!-- 窗口控制 -->
    <div class="titlebar-right" @dblclick.stop>
      <button class="win-btn" @click="minimize" title="最小化">
        <svg width="12" height="12" viewBox="0 0 12 12"><line x1="1" y1="6" x2="11" y2="6" stroke="currentColor" stroke-width="1.2"/></svg>
      </button>
      <button class="win-btn" @click="toggleMaximize" title="最大化">
        <svg width="12" height="12" viewBox="0 0 12 12"><rect x="1.5" y="1.5" width="9" height="9" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>
      </button>
      <button class="win-btn close" @click="close" title="关闭">
        <svg width="12" height="12" viewBox="0 0 12 12"><line x1="2" y1="2" x2="10" y2="10" stroke="currentColor" stroke-width="1.2"/><line x1="10" y1="2" x2="2" y2="10" stroke="currentColor" stroke-width="1.2"/></svg>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useAgentStore } from '@/stores/agent'
import { useSshAiStore } from '@/stores/ssh-ai'

const api = window.electronAPI

const agent = useAgentStore()
const sshAi = useSshAiStore()

defineProps<{
  /** 当前激活的功能页，对应图标高亮 */
  activePage: 'home' | 'remote-mgr' | 'sftp' | 'audit' | 'ai' | 'schedules' | 'dashboard' | 'tunnels' | 'docker'
}>()

const emit = defineEmits<{
  'action-ai': []
  'action-schedules': []
  'action-remote': []
  'action-audit': []
  'action-sftp': []
  'action-dashboard': []
  'action-tunnels': []
  'action-docker': []
  'action-settings': []
}>()

async function minimize() { await api?.windowMinimize() }
async function toggleMaximize() { await api?.windowMaximize() }
async function close() { await api?.windowClose() }
</script>

<style scoped>
.titlebar {
  position: relative;
  z-index: 1;
  height: var(--titlebar-h);
  border-bottom: 1px solid var(--border-subtle);
  background: var(--canvas-bg);
  background-attachment: fixed;
  display: flex;
  align-items: center;
  flex-shrink: 0;
  -webkit-app-region: drag;
  user-select: none;
  font-family: var(--font-sans);
}

.titlebar-brand {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 12px;
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 500;
  -webkit-app-region: no-drag;
}

.brand-text {
  letter-spacing: -0.3px;
}

.titlebar-drag {
  flex: 1;
  height: 100%;
}

.titlebar-right {
  display: flex;
  align-items: center;
  gap: 0;
  height: 100%;
  -webkit-app-region: no-drag;
}

.titlebar-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  height: 100%;
  -webkit-app-region: no-drag;
}

.titlebar-action-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 100%;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast);
}

.titlebar-action-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

/* 当前功能页图标高亮。取 --status-success-text 而不是写死一档绿：
   #4ade80 在白底只有 1.9:1、在米纸上更淡，浅/手记主题下"当前页"反而看不见 */
.titlebar-action-btn.active {
  color: var(--status-success-text);
  text-shadow: 0 0 8px color-mix(in srgb, var(--status-success-text) 45%, transparent);
}

.titlebar-action-btn.active:hover {
  color: var(--status-success-text);
}

.titlebar-action-btn .ai-text {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.5px;
}

/* AI 按钮状态徽章：绝对定位钉在按钮右上角，不挤动 "AI" 字本身。
   运行中 = info 蓝呼吸；未读 = 强调色实心点。都用主题令牌，八套主题各自取值。 */
.titlebar-action-btn {
  position: relative;
}
.ai-badge {
  position: absolute;
  top: 9px;
  right: 9px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  pointer-events: none;
}
.ai-badge.run {
  background: var(--status-info-text);
  animation: ai-badge-pulse 1.6s ease-in-out infinite;
}
.ai-badge.unread {
  background: var(--accent-primary);
}
@keyframes ai-badge-pulse {
  0%, 100% { opacity: 0.3; transform: scale(0.85); }
  50% { opacity: 1; transform: scale(1); }
}
@media (prefers-reduced-motion: reduce) {
  .ai-badge.run { animation: none; }
}

.win-btn {
  width: 46px;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  /* 与左邻的 .titlebar-action-btn 同一档文字：#cccccc 是深色主题的 --text-primary，
     写在浅/手记的画布上只有 1.6:1，三枚窗口按钮基本隐形 */
  color: var(--text-secondary);
  cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast);
}

.win-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.win-btn.close:hover {
  /* 关窗按钮的红是 Windows 平台约定（用户对手上这三枚按钮的既有预期），
     不属于主题色板，所以写成组件局部色、不跟 --accent-* 走 */
  --win-close: #e81123;
  --win-close-on: #ffffff;
  background: var(--win-close);
  color: var(--win-close-on);
}

:global(.app.has-background) .titlebar {
  background: transparent !important;
}

/* ── 窄窗口：图标优先。
   6 个功能按钮 + 3 个窗口按钮约占 350px，窗口收窄时品牌区会被压没，
   故隐藏品牌文字、收紧按钮宽度，保证全部功能入口仍可点。 */
@media (max-width: 760px) {
  .titlebar-brand { padding: 0 8px; }
  .brand-text { display: none; }
  .titlebar-action-btn { width: 30px; }
  .win-btn { width: 38px; }
}
</style>
