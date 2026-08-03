<template>
  <div class="titlebar" @dblclick="toggleMaximize">
    <div class="titlebar-brand">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14">
        <rect x="2" y="4" width="20" height="16" rx="2"/>
        <path d="m6 8 4 4-4 4"/>
        <line x1="12" y1="16" x2="18" y2="16"/>
      </svg>
      <span class="brand-text">cKunAgentSSH</span>
    </div>
    <div class="titlebar-drag"></div>
    <!-- 窗口控制 -->
    <div class="titlebar-right">
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
