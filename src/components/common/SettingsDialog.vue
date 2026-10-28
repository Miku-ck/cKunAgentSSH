<template>
  <Teleport to="body">
    <Transition name="settings-fade">
      <div v-if="visible" class="settings-overlay" @click.self="close">
        <div
          ref="dialogRef"
          class="settings-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="settings-dialog-title"
          tabindex="-1"
        >
          <!-- Header -->
          <header class="settings-header">
            <h2 id="settings-dialog-title">设置</h2>
            <button class="close-btn" @click="close" aria-label="关闭">×</button>
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
import { ref, computed, watch, markRaw, onBeforeUnmount, nextTick } from 'vue'
import type { Component } from 'vue'
import type { SettingCategory } from '@/stores/settings'
import { FOCUSABLE_SELECTOR, nextFocusIndex } from '@/utils/focus-trap'

// 面板组件
import ModelsPanel from '@/components/settings/panels/ModelsPanel.vue'
import ChatPanel from '@/components/settings/panels/ChatPanel.vue'
import AppearancePanel from '@/components/settings/panels/AppearancePanel.vue'
import SkillsPanel from '@/components/settings/panels/SkillsPanel.vue'
import AgentPanel from '@/components/settings/panels/AgentPanel.vue'
import MemoryPanel from '@/components/settings/panels/MemoryPanel.vue'
import OpsPanel from '@/components/settings/panels/OpsPanel.vue'
import BotPanel from '@/components/settings/panels/BotPanel.vue'

// 导航图标（自绘 SVG，渲染函数组件 —— 见 settings-nav-icons.ts 的说明）
import {
  IconModels, IconChat, IconAppearance, IconAgent, IconSkills, IconMemory, IconOps, IconBot,
} from './settings-nav-icons'

const visible = ref(false)
const activeCategory = ref<SettingCategory>('models')
const dialogRef = ref<HTMLElement | null>(null)
/** 打开前的焦点来源：关闭后还回去（顶栏按钮 / 命令面板都可能打开设置） */
let restoreFocus: HTMLElement | null = null

/**
 * 分类清单的唯一一份。
 *
 * 先前是 `categories` 数组与 `panelMap` 各抄一遍七个 id：panelMap 靠
 * Record<SettingCategory, …> 有编译期兜底（漏 key 报错），数组那一侧没有 ——
 * 漏一项只是侧栏少一个入口，面板仍然存在、也仍能被 openSettings('xxx') 打开，
 * 编译照过、运行照跑，只是用户从此找不到它。本仓这类"抄两份清单"漂过不止一次，
 * 这里合成一份 Record，让标签、图标、面板三者受同一道编译检查。
 */
const PANELS: Record<SettingCategory, { label: string; icon: Component; panel: Component }> = {
  models:     { label: '模型',  icon: IconModels,     panel: markRaw(ModelsPanel) },
  chat:       { label: '对话',  icon: IconChat,       panel: markRaw(ChatPanel) },
  appearance: { label: '外观',  icon: IconAppearance, panel: markRaw(AppearancePanel) },
  skills:     { label: '技能',  icon: IconSkills,     panel: markRaw(SkillsPanel) },
  agent:      { label: 'Agent', icon: IconAgent,      panel: markRaw(AgentPanel) },
  memory:     { label: '记忆',  icon: IconMemory,     panel: markRaw(MemoryPanel) },
  ops:        { label: '运维',  icon: IconOps,        panel: markRaw(OpsPanel) },
  bot:        { label: 'Bot 管理', icon: IconBot,     panel: markRaw(BotPanel) },
}

const categories = (Object.keys(PANELS) as SettingCategory[])
  .map((id) => ({ id, label: PANELS[id].label, icon: PANELS[id].icon }))

const currentPanel = computed(() => PANELS[activeCategory.value].panel)

function open(category: SettingCategory = 'models') {
  activeCategory.value = category
  visible.value = true
}

function close() {
  visible.value = false
}

// Esc 关闭（bubble 阶段，让内层弹窗优先处理 Esc；
// 内层若调 e.stopPropagation() 可阻止外层 SettingsDialog 关闭）
let keyHandler: ((e: KeyboardEvent) => void) | null = null
let tabHandler: ((e: KeyboardEvent) => void) | null = null

const MODAL_SELECTOR =
  '[role="dialog"][aria-modal="true"], [role="alertdialog"][aria-modal="true"]'

function focusablesIn(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
    .filter(el => !el.hasAttribute('disabled') && el.getClientRects().length > 0)
}

/** 同时开着多个模态时，Tab 只该在最上层那个里循环。
 *  设置内的编辑框/确认框都挂在设置之后或内部，DOM 顺序取最后一个。 */
function topmostModal(): HTMLElement | null {
  const modals = Array.from(document.querySelectorAll<HTMLElement>(MODAL_SELECTOR))
    .filter(el => el.getClientRects().length > 0)
  return modals[modals.length - 1] || null
}

/** 焦点在容器上（刚打开）或已漏到模态外时，Tab 先送进最上层模态；
 *  首/尾端的循环交给 nextFocusIndex，中间移动仍由浏览器处理。 */
function onTabKeydown(e: KeyboardEvent) {
  if (e.key !== 'Tab') return
  const modal = topmostModal()
  if (!modal) return
  const items = focusablesIn(modal)
  if (!items.length) {
    e.preventDefault()
    modal.focus()
    return
  }
  const current = items.indexOf(document.activeElement as HTMLElement)
  if (current === -1) {
    e.preventDefault()
    items[e.shiftKey ? items.length - 1 : 0].focus()
    return
  }
  const dir = e.shiftKey ? -1 : 1
  const next = nextFocusIndex(items.length, current, dir)
  if (next === current + dir) return
  e.preventDefault()
  items[next].focus()
}

/** 打开期间背景整块 inert：Tab 与读屏都到不了背后的标题条和主区。
 *  设置层 Teleport 到 body，不在 .app 里，不受影响。 */
function setAppInert(on: boolean) {
  const app = document.querySelector<HTMLElement>('.app')
  if (!app) return
  if (on) {
    app.setAttribute('inert', '')
    app.setAttribute('aria-hidden', 'true')
  } else {
    app.removeAttribute('inert')
    app.removeAttribute('aria-hidden')
  }
}

watch(visible, (v) => {
  if (v) {
    restoreFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    keyHandler = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      // 内层弹窗（如 ModelsPanel 的编辑对话框）先吃掉这次 Esc：它在 window 的**捕获**
      // 阶段注册，比本处理器更早跑到，并已 preventDefault。没有这道判断，按一次 Esc
      // 会把内层与整个设置对话框一起关掉 —— 用户只想关编辑框，结果整页设置连同
      // 未保存的表单一起没了。冒泡阶段的 stopPropagation 救不了：那时外层已经跑过。
      if (e.defaultPrevented) return
      e.preventDefault()
      close()
    }
    tabHandler = onTabKeydown
    window.addEventListener('keydown', keyHandler)
    window.addEventListener('keydown', tabHandler, true)
    void nextTick(() => {
      if (!visible.value) return
      dialogRef.value?.focus()
      setAppInert(true)
    })
  } else {
    if (keyHandler) {
      window.removeEventListener('keydown', keyHandler)
      keyHandler = null
    }
    if (tabHandler) {
      window.removeEventListener('keydown', tabHandler, true)
      tabHandler = null
    }
    setAppInert(false)
    restoreFocus?.focus()
    restoreFocus = null
  }
})

onBeforeUnmount(() => {
  if (keyHandler) {
    window.removeEventListener('keydown', keyHandler)
  }
  if (tabHandler) {
    window.removeEventListener('keydown', tabHandler, true)
  }
  setAppInert(false)
  restoreFocus?.focus()
  restoreFocus = null
})

defineExpose({ open, close })
</script>

<style scoped>
.settings-overlay {
  position: fixed;
  inset: 0;
  background: var(--scrim);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 2000;
}

.settings-dialog {
  /* 自适应尺寸：随窗口缩放，范围 960~1280px 宽，640~860px 高 */  width: clamp(960px, 90vw, 1280px);
  height: clamp(640px, 88vh, 860px);
  max-width: 96vw;
  max-height: 94vh;
  /* 设置窗是「窗口」不是卡片：与主窗口 .ssh-view / .titlebar 同一层结构画布。
     先前这里画 --bg-elevated（卡片层），而侧栏那一列单独画了 --canvas-bg ——
     同一个弹窗里两种材质，右侧整片底色也和全 app 的结构背景对不上。
     收归 shell 一处上色：--canvas-bg = --bg-base，等于侧栏原先那一片颜色，
     只是把整窗统一到它上面；浮起关系仍由 border + box-shadow 负责。 */
  background: var(--canvas-bg);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  color: var(--text-primary);
  font-family: var(--font-sans);
  -webkit-app-region: drag;
}

/* 容器只为接住初始焦点（tabindex=-1），不是可操作控件，不画焦点框 */
.settings-dialog:focus {
  outline: none;
}

.settings-header {
  height: 48px;
  padding: 0 var(--spacing-16);
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
  -webkit-app-region: drag;
}

.settings-header h2 {
  margin: 0;
  font-size: 1.0769rem;
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
  font-size: 1.3846rem;
  line-height: 1;
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
  /* 不自己画底：整窗那一层由 .settings-dialog 上，侧栏只留这条分隔线。
     两处各画一次 --canvas-bg 在手记里会露馅 —— 纸纹的光斑按元素自己的盒子
     取样，侧栏与内容区就成了两张对不齐的纸。 */
  border-right: 1px solid var(--border-subtle);
  padding: var(--spacing-12) var(--spacing-8);
  overflow-y: auto;
}

.nav-item {
  width: 100%;
  height: 34px;
  padding: 0 var(--spacing-12);
  display: flex;
  align-items: center;
  gap: var(--spacing-10);
  background: transparent;
  border: none;
  cursor: pointer;
  color: var(--text-secondary);
  font-size: 1rem;
  border-radius: var(--radius-sm);
  margin-bottom: var(--spacing-2);
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
  border-radius: var(--radius-xs);
}

.nav-icon {
  width: 16px;
  height: 16px;
  flex-shrink: 0;
}

.settings-content {
  flex: 1;
  overflow-y: auto;
  padding: var(--spacing-24) 28px;
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
