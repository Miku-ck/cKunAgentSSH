<script setup lang="ts">
/**
 * 首页指令卡（spec 2026-09-29-home-command-panel · 用户故事 1–9）
 *
 * 只做下达：发送走 useHomeCommand → ssh-ai.sendMessage 这一条通路；审批档与
 * 模型都读 store 而不是组件内自存，保证首页选的东西在侧栏同样算数。
 * ＋ 菜单（技能 / 附件 / 终端选区）本片未接：那几项状态目前仍散在 SideChatPanel
 * 内部，先放个按不动的按钮就是假入口，留到后续切片一并提出来。
 */
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useHomeCommand } from '@/composables/useHomeCommand'
import { useModelSelector } from '@/composables/useModelSelector'
import { useSettings } from '@/composables/useSettings'
import { useAgentStore } from '@/stores/agent'
import { useModelsStore } from '@/stores/models'
import { useSettingsStore } from '@/stores/settings'
import { useSshAiStore } from '@/stores/ssh-ai'
import { APPROVAL_OPTIONS, type ApprovalKey } from '@/utils/run-modes'

const agent = useAgentStore()
const sshAi = useSshAiStore()
const modelsStore = useModelsStore()
const settings = useSettingsStore()
const { openSettings } = useSettings()
const { draft, canSend, submit } = useHomeCommand()

const approvalMode = computed(() => sshAi.approvalMode)
const showPermMenu = ref(false)
const permRef = ref<HTMLElement | null>(null)

/** 点外面收起。useModelSelector 自带这一层，审批菜单不能只有它能被点外面关掉 */
function onDocClick(e: MouseEvent) {
  if (permRef.value && !permRef.value.contains(e.target as Node)) showPermMenu.value = false
}

onMounted(() => document.addEventListener('click', onDocClick))
onUnmounted(() => document.removeEventListener('click', onDocClick))

function pickPerm(key: ApprovalKey) {
  sshAi.setApprovalMode(key)
  showPermMenu.value = false
}

const { showModelDropdown, modelSelectorRef, currentModelName, selectModel } = useModelSelector({
  getSelectedId: () => sshAi.selectedModelId,
  setSelectedId: (id) => sshAi.setSelectedModel(id),
  models: () => modelsStore.models,
})

const noModel = computed(() => modelsStore.models.length === 0)
const running = computed(() => agent.isRunning)

const ta = ref<HTMLTextAreaElement | null>(null)
/** 行数随内容长，但不超过四行 —— 再高首页就被自己的输入框挤走了 */
function grow() {
  const el = ta.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(96, Math.max(52, el.scrollHeight))}px`
}

async function send() {
  if (!await submit()) return
  await nextTick()
  grow()
}

function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Enter') return
  // 与侧栏同一口径：发送快捷键可在设置里换档，硬编码 Enter 会让两处行为不一致
  const wantShift = settings.settings.chat.sendShortcut === 'shiftEnter'
  if (e.shiftKey !== wantShift) return
  e.preventDefault()
  void send()
}

onMounted(() => { void sshAi.fetchDefaultModel() })
</script>

<template>
  <div class="hcc">
    <textarea
      ref="ta"
      v-model="draft"
      class="hcc-input"
      rows="2"
      spellcheck="false"
      placeholder="说人话就行 —— 例如：昨天 21 点之后 web-01 内存一直在涨，看看是什么进程，先别动它"
      @input="grow"
      @keydown="onKeydown"
    />
    <div class="hcc-foot">
      <div ref="permRef" class="hcc-sel">
        <button
          type="button"
          class="hcc-btn"
          :disabled="running"
          :aria-expanded="showPermMenu"
          @click.stop="showPermMenu = !showPermMenu"
        >
          审批 · <b>{{ APPROVAL_OPTIONS.find(o => o.key === approvalMode)?.label }}</b>
        </button>
        <div v-if="showPermMenu" class="hcc-menu" @click.stop>
          <button
            v-for="o in APPROVAL_OPTIONS"
            :key="o.key"
            type="button"
            class="hcc-opt"
            :class="{ on: approvalMode === o.key }"
            @click="pickPerm(o.key)"
          >
            <span class="ol">{{ o.label }}</span>
            <span class="od">{{ o.desc }}</span>
            <span class="ot">{{ approvalMode === o.key ? '✓' : '' }}</span>
          </button>
        </div>
      </div>

      <div ref="modelSelectorRef" class="hcc-sel">
        <button
          v-if="!noModel"
          type="button"
          class="hcc-btn"
          :aria-expanded="showModelDropdown"
          @click.stop="showModelDropdown = !showModelDropdown"
        >
          模型 · <b>{{ currentModelName }}</b>
        </button>
        <button
          v-else
          type="button"
          class="hcc-btn empty"
          title="尚未配置模型，点击去设置"
          @click="openSettings('models')"
        >
          尚未配置模型 · <b>去设置</b>
        </button>
        <div v-if="showModelDropdown && !noModel" class="hcc-menu wide" @click.stop>
          <button
            v-for="m in modelsStore.models"
            :key="m.id"
            type="button"
            class="hcc-opt single"
            :class="{ on: m.id === sshAi.selectedModelId }"
            @click="selectModel(m.id)"
          >
            <span class="ol">{{ m.name || m.model_id || m.id }}</span>
            <span class="ot">{{ m.id === sshAi.selectedModelId ? '✓' : '' }}</span>
          </button>
        </div>
      </div>

      <span v-if="running" class="hcc-note">AI 正在运行中</span>

      <button type="button" class="hcc-send" :disabled="!canSend" @click="send">交给它</button>
    </div>
  </div>
</template>

<style scoped>
/* 只用现有主题令牌，不写死颜色 —— 主题层是第四片的事 */
.hcc {
  width: 100%;
  max-width: 720px;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}
.hcc:focus-within {
  border-color: var(--accent-primary);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent-primary) 16%, transparent);
}
.hcc-input {
  display: block;
  width: 100%;
  box-sizing: border-box;
  padding: 14px 16px 6px;
  background: transparent;
  border: 0;
  outline: 0;
  resize: none;
  overflow-y: hidden;
  color: var(--text-primary);
  font-family: var(--font-sans);
  font-size: 15px;
  line-height: 22px;
  caret-color: var(--accent-primary);
}
.hcc-input::placeholder { color: var(--text-tertiary); opacity: .8 }
.hcc-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px 9px 12px;
  border-top: 1px solid var(--divider-primary);
  flex-wrap: wrap;
}
.hcc-sel { position: relative }
.hcc-btn {
  display: flex;
  align-items: center;
  gap: 5px;
  height: 24px;
  padding: 0 9px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-base);
  color: var(--text-secondary);
  font-size: 12px;
  transition: background var(--transition-fast), border-color var(--transition-fast);
}
.hcc-btn b { font-weight: 500; color: var(--text-primary) }
.hcc-btn:hover:not(:disabled) { background: var(--bg-hover); border-color: var(--text-tertiary) }
.hcc-btn:disabled { opacity: .5; cursor: default }
.hcc-btn.empty b { color: var(--accent-orange) }
.hcc-menu {
  position: absolute;
  left: 0;
  bottom: calc(100% + 6px);
  width: 236px;
  padding: 4px;
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  z-index: 30;
}
.hcc-menu.wide { width: 220px; max-height: 260px; overflow-y: auto }
.hcc-opt {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 1px 8px;
  width: 100%;
  text-align: left;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
  transition: background var(--transition-fast);
}
.hcc-opt:hover { background: var(--bg-hover) }
.hcc-opt .ol { grid-column: 1; font-size: 12px; color: var(--text-primary) }
.hcc-opt .od { grid-column: 1; font-size: 11px; color: var(--text-tertiary); line-height: 1.45 }
.hcc-opt .ot {
  grid-row: 1 / 3;
  align-self: center;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--accent-green);
}
.hcc-opt.single .ot { grid-row: 1 }
.hcc-note { font-size: 11px; color: var(--text-tertiary) }
.hcc-send {
  margin-left: auto;
  height: 28px;
  padding: 0 18px;
  border: 0;
  border-radius: var(--radius-sm);
  background: var(--accent-primary);
  color: #fff;
  font-size: 13px;
  font-weight: 600;
  transition: filter var(--transition-fast), opacity var(--transition-fast);
}
.hcc-send:hover:not(:disabled) { filter: brightness(1.15) }
.hcc-send:disabled { opacity: .45; cursor: default }
</style>
