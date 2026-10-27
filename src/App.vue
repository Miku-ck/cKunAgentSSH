<template>
  <div class="app" :class="{ 'has-background': hasBackground }">
    <div
      v-if="hasBackground && backgroundImageUrl"
      class="app-background-layer"
      :style="{ backgroundImage: `url(&quot;${backgroundImageUrl}&quot;)` }"
      aria-hidden="true"
    ></div>
    <div
      v-if="hasBackground && backgroundImageUrl"
      class="app-overlay-layer"
      aria-hidden="true"
    ></div>
    <TitleBar
      :active-page="activePage"
      @action-ai="sshViewRef?.goHome()"
      @action-bot="sshViewRef?.toggleBot()"
      @action-terminal="sshViewRef?.showTerminal()"
      @action-schedules="sshViewRef?.toggleSchedules()"
      @action-audit="sshViewRef?.toggleAudit()"
      @action-dashboard="sshViewRef?.toggleDashboard()"
      @action-sftp="sshViewRef?.toggleSftp()"
      @action-tunnels="sshViewRef?.toggleTunnels()"
      @action-docker="sshViewRef?.toggleDocker()"
      @action-git="sshViewRef?.toggleGit()"
      @action-settings="sshViewRef?.openSettings()"
    />
    <div class="main">
      <SshView ref="sshViewRef" />
    </div>
    <ToastContainer />
    <ConfirmDialogHolder />
    <SettingsDialogHolder />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useSettingsStore } from './stores/settings'
import { useAppStore } from './stores/app'
import { useAppearance } from './composables/useAppearance'
import type { TitlebarPage } from './composables/useSshNavigation'
import TitleBar from './layouts/TitleBar.vue'
import ToastContainer from './components/common/ToastContainer.vue'
import ConfirmDialogHolder from './components/common/ConfirmDialogHolder.vue'
import SettingsDialogHolder from './components/common/SettingsDialogHolder.vue'
import SshView from './views/ssh/SshView.vue'

const settingsStore = useSettingsStore()
const appStore = useAppStore()
useAppearance()

const sshViewRef = ref<InstanceType<typeof SshView> | null>(null)

// 当前激活的功能页：用于 TitleBar 图标高亮（ref 未挂载时为 'home'）
// ARCH-008 已收口：可高亮页键单一来源 = useSshNavigation 的 TitlebarPage
const activePage = computed<TitlebarPage>(() => {
  const nav = sshViewRef.value?.activeNavPage
  if (nav && nav !== 'home') return nav
  // 终端在工作区前台：点亮「终端」，此时 activeNavPage 也是 'home'
  if (sshViewRef.value?.terminalVisible) return 'terminal'
  // AI 入口就是首页任务台：任务台在前台时点亮，终端/独立页盖上来时熄灭。
  // 不用 activeNavPage 单独判：终端态下它也是 'home'，会把 AI 图标误点亮。
  if (sshViewRef.value?.homeTabActive) return 'ai'
  return 'home'
})

const backgroundImageUrl = ref('')
let firstBgLoad = true
let bgLoadSeq = 0

async function loadBackgroundImage(fileName: string, silent = false) {
  if (!fileName || !window.electronAPI?.fsLoadBackgroundImage) {
    backgroundImageUrl.value = ''
    return
  }
  const mySeq = ++bgLoadSeq
  try {
    const newUrl = await window.electronAPI.fsLoadBackgroundImage(fileName)
    if (mySeq !== bgLoadSeq) return
    backgroundImageUrl.value = newUrl
  } catch (e) {
    if (mySeq !== bgLoadSeq) return
    backgroundImageUrl.value = ''
    if (!silent) console.warn('[App] 背景图加载失败:', e)
  }
}

watch(
  () => [
    settingsStore.settings.appearance.backgroundImage,
    settingsStore.settings.appearance.backgroundEnabled,
    settingsStore.settings.appearance.backgroundVersion,
  ] as const,
  ([fileName, enabled, _version]) => {
    const isInitial = firstBgLoad
    firstBgLoad = false
    if (!enabled || !fileName) {
      backgroundImageUrl.value = ''
      return
    }
    loadBackgroundImage(fileName, isInitial)
  },
  { immediate: true }
)

const hasBackground = computed(
  () => settingsStore.settings.appearance.backgroundEnabled && !!backgroundImageUrl.value
)

onMounted(async () => {
  // 初始化设置（从后端同步）：先等后端就绪再拉 —— 这两条 GET 挨个在后端起来前抢跑
  // 只会拿到"后端服务未就绪"并静默失败（面板拿到的是默认值）。
  try {
    await appStore.readyPromise
    await Promise.all([
      settingsStore.loadMemorySettings(),
      settingsStore.loadPostProcessSettings(),
    ])
  } catch (e) {
    console.error('[App] 初始化设置加载失败:', e)
  }
})
</script>

<style scoped>
.app {
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
}

.app-background-layer {
  position: fixed;
  inset: 0;
  /* 修复：不能用 z-index:-1 —— html/body 有不透明背景（var(--bg-base)），
     按层叠规则负 z-index 层会画在 body 背景之下，背景图永远被盖住。
     改为 0 后位于 body 背景之上；overlay(0, DOM 顺序更后) 与 .main(1) 仍在更上层 */
  z-index: 0;
  pointer-events: none;
  background-size: cover;
  background-position: center;
  background-repeat: no-repeat;
  opacity: var(--app-bg-opacity, 1);
  transition: opacity 0.3s ease;
  will-change: opacity;
}

.app-overlay-layer {
  position: fixed;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  background: rgba(0, 0, 0, var(--app-bg-overlay, 0.6));
  transition: background 0.3s ease;
}

.main {
  flex: 1;
  display: flex;
  overflow: hidden;
  position: relative;
  z-index: 1;
}
</style>
