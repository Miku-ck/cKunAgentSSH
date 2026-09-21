<template>
  <Teleport to="body">
    <Transition name="settings-fade">
      <div v-if="visible" class="settings-overlay" @click.self="close">
        <div class="settings-dialog" role="dialog" aria-modal="true">
          <!-- Header -->
          <header class="settings-header">
            <h2>设置</h2>
            <button class="close-btn" @click="close" aria-label="关闭">
              <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                <line x1="18" y1="6" x2="6" y2="18"/>
                <line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </header>

          <!-- Body -->
          <div class="settings-body">
            <!-- 左侧导航 -->
            <nav class="settings-nav">
              <button
                v-for="cat in categories"
                :key="cat.id"
                class="nav-item"
                :class="{ active: activeCategory === cat.id }"
                @click="activeCategory = cat.id"
              >
                <component :is="cat.icon" class="nav-icon" />
                <span>{{ cat.label }}</span>
              </button>
            </nav>

            <!-- 右侧内容 -->
            <main class="settings-content">
              <component :is="currentPanel" />
            </main>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, watch, markRaw, onBeforeUnmount } from 'vue'
import type { Component } from 'vue'
import type { SettingCategory } from '@/stores/settings'

// 面板组件
import ModelsPanel from '@/components/settings/panels/ModelsPanel.vue'
import ChatPanel from '@/components/settings/panels/ChatPanel.vue'
import AppearancePanel from '@/components/settings/panels/AppearancePanel.vue'
import SkillsPanel from '@/components/settings/panels/SkillsPanel.vue'
import AgentPanel from '@/components/settings/panels/AgentPanel.vue'

// 导航图标（自绘 SVG 组件）
const IconModels = markRaw({
  template: `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>`,
})
const IconChat = markRaw({
  template: `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`,
})
const IconAppearance = markRaw({
  template: `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>`,
})
const IconAgent = markRaw({
  template: `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v4"/><rect x="5" y="6" width="14" height="10" rx="3"/><circle cx="9.5" cy="11" r="1"/><circle cx="14.5" cy="11" r="1"/><path d="M9 20v-4M15 20v-4M3 10v2M21 10v2"/></svg>`,
})
const IconSkills = markRaw({
  template: `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 4.1 4.6.6-3.4 3.2.9 4.6L12 13.9 7.9 15.5l.9-4.6L5.5 7.7l4.6-.6z"/><path d="M6 19h12"/></svg>`,
})

const visible = ref(false)
const activeCategory = ref<SettingCategory>('models')

const categories: { id: SettingCategory; label: string; icon: Component }[] = [
  { id: 'models',         label: '模型',   icon: IconModels },
  { id: 'chat',           label: '对话',   icon: IconChat },
  { id: 'appearance',     label: '外观',   icon: IconAppearance },
  { id: 'skills',         label: '技能',   icon: IconSkills },
  { id: 'agent',          label: 'Agent',  icon: IconAgent },
]

const panelMap: Record<SettingCategory, Component> = {
  models:         markRaw(ModelsPanel),
  chat:           markRaw(ChatPanel),
  appearance:     markRaw(AppearancePanel),
  skills:         markRaw(SkillsPanel),
  agent:          markRaw(AgentPanel),
}

const currentPanel = computed(() => panelMap[activeCategory.value])

function open(category: SettingCategory = 'models') {
  activeCategory.value = category
  visible.value = true
}

function close() {
  visible.value = false
}

// Esc 关闭（bubble 阶段，让内层弹窗 MemoryPanel/AccountPanel 等优先处理 Esc；
// 内层若调 e.stopPropagation() 可阻止外层 SettingsDialog 关闭）
let keyHandler: ((e: KeyboardEvent) => void) | null = null
watch(visible, (v) => {
  if (v) {
    keyHandler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        close()
      }
    }
    window.addEventListener('keydown', keyHandler)
  } else if (keyHandler) {
    window.removeEventListener('keydown', keyHandler)
    keyHandler = null
  }
})

onBeforeUnmount(() => {
  if (keyHandler) {
    window.removeEventListener('keydown', keyHandler)
  }
})

defineExpose({ open, close })
</script>

<style scoped>
.settings-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 2000;
}

.settings-dialog {
  /* 自适应尺寸：随窗口缩放，范围 960~1280px 宽，640~860px 高 */
  width: clamp(960px, 90vw, 1280px);
  height: clamp(640px, 88vh, 860px);
  max-width: 96vw;
  max-height: 94vh;
  background: var(--bg-base);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  box-shadow: 0 16px 48px rgba(0, 0, 0, 0.5);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  color: var(--text-primary);
  font-family: var(--font-sans);
  -webkit-app-region: drag;
}

.settings-header {
  height: 48px;
  padding: 0 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
  -webkit-app-region: drag;
}

.settings-header h2 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  -webkit-app-region: no-drag;
}

.close-btn {
  -webkit-app-region: no-drag;
  width: 28px;
  height: 28px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s ease;
}
.close-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.settings-body {
  flex: 1;
  display: flex;
  overflow: hidden;
  -webkit-app-region: no-drag;
}

.settings-nav {
  width: 200px;
  flex-shrink: 0;
  background: var(--bg-base);
  border-right: 1px solid var(--border-subtle);
  padding: 12px 8px;
  overflow-y: auto;
}

.nav-item {
  width: 100%;
  height: 34px;
  padding: 0 12px;
  display: flex;
  align-items: center;
  gap: 10px;
  background: transparent;
  border: none;
  cursor: pointer;
  color: var(--text-secondary);
  font-size: 13px;
  border-radius: var(--radius-sm);
  margin-bottom: 2px;
  text-align: left;
  position: relative;
  transition: background 0.15s ease, color 0.15s ease;
  font-family: inherit;
}
.nav-item:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}
.nav-item.active {
  background: color-mix(in srgb, var(--accent-primary) 8%, transparent);
  color: var(--text-primary);
}
.nav-item.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 8px;
  bottom: 8px;
  width: 2px;
  background: var(--accent-primary);
  border-radius: 1px;
}

.nav-icon {
  width: 16px;
  height: 16px;
  flex-shrink: 0;
}

.settings-content {
  flex: 1;
  overflow-y: auto;
  padding: 24px 28px;
}

/* 过渡动画 */
.settings-fade-enter-active,
.settings-fade-leave-active {
  transition: opacity 0.15s ease;
}
.settings-fade-enter-active .settings-dialog,
.settings-fade-leave-active .settings-dialog {
  transition: transform 0.15s ease, opacity 0.15s ease;
}
.settings-fade-enter-from,
.settings-fade-leave-to {
  opacity: 0;
}
.settings-fade-enter-from .settings-dialog,
.settings-fade-leave-to .settings-dialog {
  transform: scale(0.97);
  opacity: 0;
}
</style>
