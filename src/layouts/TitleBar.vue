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
    <!-- 功能按钮：按语义分组（常用工具 / 会话与网络 / 追溯），组间分隔线。
         常用工具组（AI + Docker / GitHub / SFTP）紧贴左侧品牌区——这四枚是最高频入口；
         会话与网络组（监控 / 定时 / 远程 / 隧道）随后，审计与设置靠近窗口控制。
         ≥1024px 时按钮常显 2 字标签（可发现性优先），以下收纳为纯图标；
         title 里的快捷键提示来自 useGlobalShortcuts.PAGE_SHORTCUTS，单一来源。 -->
    <div class="titlebar-actions" @dblclick.stop>
      <!-- 常用工具：AI 助手 + Docker / GitHub / SFTP -->
      <button class="titlebar-action-btn" :class="{ active: activePage === 'ai' }" :title="aiTitle" aria-label="AI 运维助手" @click="emit('action-ai')">
        <span class="ai-text">AI</span>
        <!-- 两个入口在视觉上是同一个助手的两扇门：徽章与首页状态点同源
             （useAgentPresence），优先级待审批 > 运行中 > 未读；文案同步说明状态，
             不单靠颜色传义。 -->
        <span v-if="presence === 'pending'" class="ai-badge pending" aria-hidden="true"></span>
        <span v-else-if="presence === 'running'" class="ai-badge run" aria-hidden="true"></span>
        <span v-else-if="presence === 'unread'" class="ai-badge unread" aria-hidden="true"></span>
      </button>
      <button class="titlebar-action-btn" :class="{ active: activePage === 'docker' }" :title="'Docker 管理' + pageHint('docker')" aria-label="Docker 管理" @click="emit('action-docker')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" width="14" height="14"><path d="M6 9.5h3v3H6zM11 9.5h3v3h-3zM11 6h3v3h-3z"/><path d="M3.5 12.5h13c1.6 0 3-.7 3.8-2 .7 2.3-.4 5-3.2 5H7a3.5 3.5 0 0 1-3.5-3z"/><path d="M3.5 12.5 2 10"/></svg>
        <span class="tb-label">Docker</span>
      </button>
      <button class="titlebar-action-btn" :class="{ active: activePage === 'github' }" :title="'GitHub 加速' + pageHint('github') + ghHealthTitle" aria-label="GitHub 加速" @click="emit('action-github')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"/></svg>
        <!-- 巡检异常红点：只在 degraded 亮，不做绿点（ok 不打扰） -->
        <span v-if="ghDegraded" class="gh-badge" aria-hidden="true"></span>
        <span class="tb-label">GitHub</span>
      </button>
      <button class="titlebar-action-btn" :class="{ active: activePage === 'sftp' }" :title="'SFTP' + pageHint('sftp')" aria-label="SFTP 文件传输" @click="emit('action-sftp')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        <span class="tb-label">SFTP</span>
      </button>
      <!-- 会话与网络：监控 / 定时 / 远程终端 / 隧道 -->
      <span class="titlebar-sep" aria-hidden="true"></span>
      <button class="titlebar-action-btn" :class="{ active: activePage === 'dashboard' }" :title="'监控仪表盘' + pageHint('dashboard')" aria-label="监控仪表盘" @click="emit('action-dashboard')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
        <span class="tb-label">监控</span>
      </button>
      <button class="titlebar-action-btn" :class="{ active: activePage === 'schedules' }" :title="'定时任务' + pageHint('schedules')" aria-label="定时任务" @click="emit('action-schedules')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 16 14"/></svg>
        <span class="tb-label">定时</span>
      </button>
      <button class="titlebar-action-btn" :class="{ active: activePage === 'remote-mgr' }" :title="'远程终端' + pageHint('remote-mgr')" aria-label="远程终端" @click="emit('action-remote')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m6 8 4 4-4 4"/><line x1="12" y1="16" x2="18" y2="16"/></svg>
        <span class="tb-label">远程</span>
      </button>
      <button class="titlebar-action-btn" :class="{ active: activePage === 'tunnels' }" :title="'隧道' + pageHint('tunnels')" aria-label="SSH 隧道" @click="emit('action-tunnels')">
        <!-- 隧道图标：拱形洞口 + 地面 + 通道中线，与 TunnelView 页头同一枚 -->
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14"><path d="M4 20v-7a8 8 0 0 1 16 0v7"/><path d="M12 20v-7"/><path d="M2 20h20"/></svg>
        <span class="tb-label">隧道</span>
      </button>
      <!-- 追溯类 -->
      <span class="titlebar-sep" aria-hidden="true"></span>
      <button class="titlebar-action-btn" :class="{ active: activePage === 'audit' }" :title="'运维审计' + pageHint('audit')" aria-label="运维审计" @click="emit('action-audit')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M9 12l2 2 4-4"/><path d="M20 12a8 8 0 1 1-2.34-5.66"/></svg>
        <span class="tb-label">审计</span>
      </button>
      <span class="titlebar-sep" aria-hidden="true"></span>
      <button class="titlebar-action-btn" title="设置 (Ctrl+,)" aria-label="设置" @click="emit('action-settings')">
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
import { computed, onMounted } from 'vue'
import { useAgentPresence } from '@/composables/useAgentPresence'
import { useGithubAccelStore } from '@/stores/github-accel'
import { pageShortcutHint as pageHint } from '@/composables/useGlobalShortcuts'
import type { TitlebarPage } from '@/composables/useSshNavigation'

const api = window.electronAPI

const { presence, pendingCount } = useAgentPresence()
const ghAccel = useGithubAccelStore()

const AI_SHORTCUT = 'Ctrl+Shift+B'
const aiTitle = computed(() => {
  if (presence.value === 'pending') return `AI 运维助手（${pendingCount.value} 条待审批，${AI_SHORTCUT}）`
  if (presence.value === 'running') return `AI 运维助手（运行中，${AI_SHORTCUT}）`
  if (presence.value === 'unread') return `AI 运维助手（有新回复，${AI_SHORTCUT}）`
  return `AI 运维助手 (${AI_SHORTCUT})`
})

/** 红点只看巡检结论；标题补充具体是哪一项异常（镜像优先） */
const ghDegraded = computed(() => ghAccel.health?.state === 'degraded')
const ghHealthTitle = computed(() => {
  const h = ghAccel.health
  if (h?.state !== 'degraded') return ''
  if (h.error) return '（当前镜像不可用）'
  if (h.hosts && !h.hosts.ok) return '（hosts 未生效）'
  return '（巡检异常）'
})

// 轻量初始化：只拉巡检状态 + 订阅推送，不加载整份加速配置（页面未打开也要能亮红点）
onMounted(() => { void ghAccel.initHealth() })

defineProps<{
  /** 当前激活的功能页，对应图标高亮（'ai' 是面板开关态，不在独立页清单里） */
  activePage: TitlebarPage
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
  'action-github': []
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

/* 组间分隔线：会话 / 资源 / 追溯三个语义族的视觉断口 */
.titlebar-sep {
  width: 1px;
  height: 14px;
  margin: 0 3px;
  background: var(--border-subtle);
  flex-shrink: 0;
}

.titlebar-action-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  min-width: 36px;
  height: 100%;
  padding: 0 7px;
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

/* 常显 2 字标签：≥1024px 展开（可发现性优先），以下由断点收纳为纯图标。
   取 --text-tertiary：标签是图标的辅助信道，视觉权重必须低于图标本身 */
.tb-label {
  font-size: 11px;
  line-height: 1;
  color: inherit;
  white-space: nowrap;
}

/* AI 按钮状态徽章：绝对定位钉在按钮右上角，不挤动 "AI" 字本身。
   待审批 = 警示色实心；运行中 = info 蓝呼吸；未读 = 强调色实心。
   与首页标签状态点同源同优先级，全部用主题令牌。 */
.titlebar-action-btn {
  position: relative;
}
.ai-badge, .gh-badge {
  position: absolute;
  top: 9px;
  right: 9px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  pointer-events: none;
}
.ai-badge.pending {
  background: var(--status-warn-text);
}
.ai-badge.run {
  background: var(--status-info-text);
  animation: ai-badge-pulse 1.6s ease-in-out infinite;
}
.ai-badge.unread {
  background: var(--accent-primary);
}
.gh-badge {
  background: var(--danger-text);
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

/* ── 宽度收纳（三级）──
   ≥1024px：图标 + 2 字标签（估算：10 枚按钮约 640px + 品牌 + 窗控 ≈ 900px）；
   <1024px：标签收纳，回到纯图标（36px/枚），全部入口保留；
   <760px：进一步收紧按钮与品牌区，保证 10+3 枚按钮全可点。 */
@media (max-width: 1024px) {
  .tb-label { display: none; }
  .titlebar-action-btn { min-width: 36px; padding: 0; gap: 0; }
  .titlebar-sep { margin: 0 1px; }
}

@media (max-width: 760px) {
  .titlebar-brand { padding: 0 8px; }
  .brand-text { display: none; }
  .titlebar-action-btn { min-width: 30px; }
  .win-btn { width: 38px; }
}
</style>
