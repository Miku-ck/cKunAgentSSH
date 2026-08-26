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
      <button class="titlebar-action-btn" title="AI 运维助手" @click="emit('action-ai')">
        <span class="ai-text">AI</span>
      </button>
      <button class="titlebar-action-btn" title="远程终端" @click="emit('action-remote')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m6 8 4 4-4 4"/><line x1="12" y1="16" x2="18" y2="16"/></svg>
      </button>
      <button class="titlebar-action-btn" title="SFTP" @click="emit('action-sftp')">
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
const api = window.electronAPI

const emit = defineEmits<{
  'action-ai': []
  'action-remote': []
  'action-sftp': []
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
  height: 32px;
  border-bottom: 1px solid var(--border-subtle, #2d2d30);
  background: var(--bg-base);
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
  color: var(--text-secondary, #ccc);
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
  color: var(--text-secondary, #ccc);
  cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast);
}

.titlebar-action-btn:hover {
  background: var(--bg-hover, rgba(255,255,255,0.1));
  color: var(--text-primary, #fff);
}

.titlebar-action-btn .ai-text {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.5px;
}

.win-btn {
  width: 46px;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: #cccccc;
  cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast);
}

.win-btn:hover {
  background: var(--bg-hover);
  color: #ffffff;
}

.win-btn.close:hover {
  background: #e81123;
  color: #fff;
}

:global(.app.has-background) .titlebar {
  background: transparent !important;
}
</style>
