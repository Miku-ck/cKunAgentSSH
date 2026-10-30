<template>
  <div class="panel">
    <header class="panel-header">
      <h3>关于与更新</h3>
      <p class="panel-desc">版本信息与更新通道。更新整目录替换：下载 → sha256 校验 → 退出后由助手换包并重启，失败自动回滚，用户数据（userData）不受影响</p>
    </header>

    <div class="panel-body">
      <p v-if="!supported" class="hint">当前不在桌面应用内（浏览器模式），这里没有可对话的主进程。</p>
      <p v-else-if="!state" class="hint">正在读取版本信息…</p>

      <template v-else>
        <section class="section">
          <h4 class="section-title">版本</h4>
          <SettingRow label="当前版本">
            <code class="mono">{{ state.currentVersion }}</code>
          </SettingRow>
          <SettingRow label="更新通道" desc="清单地址（latest.json）。部署可注入 CKUN_UPDATE_FEED，这里填的值优先，落盘到 userData/update/config.json">
            <div class="feed-row">
              <input
                v-model.trim="feedDraft"
                class="feed-input"
                type="text"
                spellcheck="false"
                placeholder="https://example.com/update/latest.json"
                :disabled="savingFeed"
                @keydown.enter.prevent="saveFeed()"
              />
              <button class="btn" :disabled="savingFeed || !feedDirty" @click="saveFeed()">
                {{ savingFeed ? '保存中…' : '保存' }}
              </button>
            </div>
          </SettingRow>
          <p v-if="feedDirty" class="hint">通道地址改了还没保存 —— 保存后才会按新地址检查。</p>
        </section>

        <section class="section">
          <h4 class="section-title">更新</h4>
          <p v-if="!state.supported" class="hint">
            当前是开发运行（未打包），不参与自助换包：换的目录是 node_modules/electron/dist。
          </p>
          <p v-else-if="!state.feedUrl" class="hint">
            还没配置更新通道：填上面的清单地址，或部署时注入 CKUN_UPDATE_FEED。
          </p>
          <p v-if="state.error" class="panel-error">{{ state.error }}</p>

          <div class="row-actions">
            <button class="btn" :disabled="!canCheck" @click="check()">
              {{ state.phase === 'checking' ? '检查中…' : '检查更新' }}
            </button>
            <button
              v-if="state.phase === 'available' || state.phase === 'downloading'"
              class="btn btn-primary"
              :disabled="state.phase === 'downloading'"
              @click="download()"
            >
              {{ state.phase === 'downloading' ? `下载中 ${state.percent}%` : '下载并准备' }}
            </button>
            <button
              v-if="state.phase === 'ready' || state.phase === 'applying'"
              class="btn btn-primary"
              :disabled="state.phase === 'applying'"
              @click="apply()"
            >
              {{ state.phase === 'applying' ? '正在退出…' : '重启并更新' }}
            </button>
          </div>

          <div v-if="state.phase === 'downloading'" class="progress" role="progressbar" :aria-valuenow="state.percent">
            <div class="progress-bar" :style="{ width: `${state.percent}%` }"></div>
          </div>

          <p v-if="state.phase === 'available' && state.targetVersion" class="hint">
            发现新版本 <strong>{{ state.targetVersion }}</strong>（当前 {{ state.currentVersion }}）。
            <template v-if="state.notes"> 更新说明：{{ state.notes }}</template>
          </p>
          <p v-else-if="state.phase === 'ready'" class="hint">
            {{ state.targetVersion }} 已下载并校验通过；点「重启并更新」后应用退出，助手换目录并重新拉起。
          </p>
          <p v-else-if="state.phase === 'applying'" class="hint">换包助手已就绪，应用即将退出…</p>
          <p v-else-if="lastChecked" class="hint">已是最新版本（{{ state.currentVersion }}）。</p>
        </section>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 「关于与更新」面板。
 *
 * 只做渲染与动作转发：判定全在主进程（electron/updater.ts + shared/update-core.ts），
 * 这里拿到的是一份完整状态快照。三种"看起来像没反应"的情况都在界面上明说：
 * 开发运行不支持换包、通道没配、点了检查但没有新版本。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import SettingRow from '@/components/settings/fields/SettingRow.vue'
import type { UpdateState } from '../../../../shared/update-core'

const api = window.electronAPI
const supported = Boolean(api?.updateGetState)

const state = ref<UpdateState | null>(null)
const feedDraft = ref('')
const savingFeed = ref(false)
/** 至少完整检查过一次且没有可用更新时，才敢说"已是最新" */
const lastChecked = ref(false)

const feedDirty = computed(() => feedDraft.value !== (state.value?.feedUrl ?? ''))
const canCheck = computed(() =>
  Boolean(state.value?.supported && state.value?.feedUrl) && state.value?.phase !== 'checking' && state.value?.phase !== 'downloading',
)

function adopt(next: UpdateState | null) {
  if (!next) return
  state.value = next
  // 用户正在编辑时不要把输入框里的内容冲掉
  if (!feedDirty.value) feedDraft.value = next.feedUrl
  if (next.phase === 'idle') lastChecked.value = true
  if (next.phase === 'checking') lastChecked.value = false
}

let unsubscribe: (() => void) | null = null

onMounted(async () => {
  if (!api?.updateGetState) return
  adopt(await api.updateGetState())
  feedDraft.value = state.value?.feedUrl ?? ''
  unsubscribe = api.onUpdateState?.(adopt) ?? null
})

onBeforeUnmount(() => {
  unsubscribe?.()
  unsubscribe = null
})

async function saveFeed() {
  if (!api?.updateSetFeed) return
  savingFeed.value = true
  try {
    adopt(await api.updateSetFeed(feedDraft.value))
  } finally {
    savingFeed.value = false
  }
}

async function check() {
  if (!api?.updateCheck) return
  adopt(await api.updateCheck())
}

async function download() {
  if (!api?.updateDownload) return
  adopt(await api.updateDownload())
}

async function apply() {
  if (!api?.updateApply) return
  const res = await api.updateApply()
  // ok=false 时主进程不会退出，把失败原因（标记没写成 / 未就绪）显示出来
  if (res?.state) adopt(res.state)
}
</script>

<style scoped src="./settings-panel-base.css"></style>
<style scoped>
.section { margin-bottom: var(--spacing-24); }
.section-title {
  margin: 0 0 var(--spacing-6);
  font-size: 0.95rem;
  font-weight: 600;
  color: var(--text-primary);
}
.hint { margin: 0 0 var(--spacing-8); font-size: 0.8846rem; line-height: 1.5; color: var(--text-secondary); }
.hint strong { color: var(--text-primary); font-weight: 600; }
.panel-error { margin: 0 0 var(--spacing-10); font-size: 0.8846rem; color: var(--status-error-text); }
.mono { font-family: var(--font-mono); font-size: 0.9231rem; color: var(--text-primary); }
.feed-row { display: flex; align-items: center; gap: var(--spacing-8); }
.feed-input {
  width: 320px;
  height: 28px;
  padding: 0 var(--spacing-8);
  border: 1px solid var(--border-input);
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  color: var(--text-primary);
  font: inherit;
  font-size: 0.9231rem;
  outline: none;
}
.feed-input:focus { border-color: var(--accent-primary); }
.feed-input:disabled { opacity: 0.6; }
.row-actions { display: flex; align-items: center; gap: var(--spacing-8); flex-wrap: wrap; margin: var(--spacing-4) 0 var(--spacing-10); }
.progress {
  height: 6px;
  border-radius: var(--radius-xs);
  background: var(--bg-input);
  overflow: hidden;
  margin-bottom: var(--spacing-10);
}
.progress-bar {
  height: 100%;
  background: var(--accent-primary);
  transition: width 0.2s ease;
}
</style>
