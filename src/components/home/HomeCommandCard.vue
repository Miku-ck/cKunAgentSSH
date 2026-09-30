<script setup lang="ts">
/**
 * 首页指令卡（spec 2026-09-29-home-command-panel · 用户故事 1–9）
 *
 * 只做下达：发送走 useHomeCommand → ssh-ai.sendMessage 这一条通路；审批档、姿态
 * 都读 store 而不是组件内自存，保证首页选的东西在侧栏同样算数。
 *
 * ＋ 菜单按"后端有消费者"筛过（spec 故事 5 的 2026-09-30 修订记着砍掉的三项与证据）：
 * 这一片接 **计划姿态**，技能 / 终端现场 / 本地附件各是一片。菜单里没有的那几项
 * 不是漏了 —— 是点亮后后端一字不变，那类开关在这仓里叫「亮的空开关」。
 */
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useHomeCommand } from '@/composables/useHomeCommand'
import { useModelSelector } from '@/composables/useModelSelector'
import { useSettings } from '@/composables/useSettings'
import { useAgentStore } from '@/stores/agent'
import { useModelsStore } from '@/stores/models'
import { useSettingsStore } from '@/stores/settings'
import { useSshAiStore } from '@/stores/ssh-ai'
import { APPROVAL_OPTIONS, PERMISSION_LABELS, resolveRunModes, type ApprovalKey } from '@/utils/run-modes'

const agent = useAgentStore()
const sshAi = useSshAiStore()
const modelsStore = useModelsStore()
const settings = useSettingsStore()
const { openSettings } = useSettings()
const { draft, canSend, submit } = useHomeCommand()

const approvalMode = computed(() => sshAi.approvalMode)
const showPermMenu = ref(false)
const permRef = ref<HTMLElement | null>(null)
const showPlusMenu = ref(false)
const plusRef = ref<HTMLElement | null>(null)

/** 点外面收起。useModelSelector 自带这一层，审批菜单不能只有它能被点外面关掉 */
function onDocClick(e: MouseEvent) {
  const t = e.target as Node
  if (permRef.value && !permRef.value.contains(t)) showPermMenu.value = false
  if (plusRef.value && !plusRef.value.contains(t)) showPlusMenu.value = false
}

onMounted(() => document.addEventListener('click', onDocClick))
onUnmounted(() => document.removeEventListener('click', onDocClick))

function pickPerm(key: ApprovalKey) {
  sshAi.setApprovalMode(key)
  showPermMenu.value = false
}

/** 「计划」是开关不是单选：点亮即只读强制，再点退出回到**未表态**，
 *  而不是替用户选一个目标姿态（与侧栏 togglePlanMode 同语义）。 */
function togglePlan() {
  sshAi.setPosture(sshAi.posture === 'plan' ? null : 'plan')
  showPlusMenu.value = false
}

const isPlan = computed(() => sshAi.posture === 'plan')
/** 审批档按钮上那一行字与发送装配同源：plan 姿态下 resolveRunModes 恒压出 'plan'
 *  （permission-mode.test.ts 锁着），所以这里显示「只读计划」而不是用户原档 ——
 *  界面不能宣称一个不会生效的选择。禁用是**展示**层，用户的 approvalMode 没被改写。 */
const permLabel = computed(() =>
  PERMISSION_LABELS[resolveRunModes(sshAi.posture ?? undefined, approvalMode.value).permissionMode],
)

const { showModelDropdown, modelSelectorRef, currentModelName, selectModel } = useModelSelector({
  getSelectedId: () => sshAi.selectedModelId,
  setSelectedId: (id) => sshAi.setSelectedModel(id),
  models: () => modelsStore.models,
})

const noModel = computed(() => modelsStore.models.length === 0)
const running = computed(() => agent.isRunning)

const ta = ref<HTMLTextAreaElement | null>(null)
/**
 * 高度按行格算，不按像素拍：横线是按 --hcc-lh 重复画的，若 max 落在两行之间
 * （原来写的 96px），第四行会被裁成半条线 —— 线在、字没了。
 * 22px 行高 + 14/6 padding 与样式里的 --hcc-lh 同值，改动要两处一起看。
 */
const LH = 22
const PAD = 20
function grow() {
  const el = ta.value
  if (!el) return
  el.style.height = 'auto'
  const h = Math.min(LH * 4 + PAD, Math.max(LH * 2 + PAD, el.scrollHeight))
  el.style.height = `${h}px`
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
      <div ref="plusRef" class="hcc-sel">
        <button
          type="button"
          class="hcc-btn plus"
          :disabled="running"
          :aria-expanded="showPlusMenu"
          title="添加上下文"
          @click.stop="showPlusMenu = !showPlusMenu"
        >＋</button>
        <div v-if="showPlusMenu" class="hcc-menu" @click.stop>
          <button type="button" class="hcc-opt" :class="{ on: isPlan }" @click="togglePlan">
            <span class="ol">计划</span>
            <span class="od">只读调研，写操作直接拒绝</span>
            <span class="ot">{{ isPlan ? '✓' : '' }}</span>
          </button>
        </div>
      </div>

      <div ref="permRef" class="hcc-sel">
        <button
          type="button"
          class="hcc-btn"
          :disabled="running || isPlan"
          :aria-expanded="showPermMenu"
          :title="isPlan ? '计划姿态下写操作被直接拒绝，「谁来批准」不参与本次裁决' : ''"
          @click.stop="showPermMenu = !showPermMenu"
        >
          审批 · <b>{{ permLabel }}</b>
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

      <!-- 故事 6：加进来的每一项都在底栏留一枚可移除的 chip。姿态这枚必须连目标态
           一起显形 —— 侧栏也能点亮它，首页若只显计划就是从中间截走的故事。 -->
      <span
        v-if="sshAi.posture"
        class="hcc-chip"
        :title="isPlan ? '本轮只读：写操作会被直接拒绝' : '本轮聚焦要达成什么、怎么算成功'"
      >
        {{ isPlan ? '计划' : '目标' }}
        <button
          type="button"
          class="hcc-chip-x"
          aria-label="移除该姿态"
          @click="sshAi.setPosture(null)"
        >×</button>
      </span>

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
  /* 行线间距 = 行高。这两个值必须同源，否则线与字错开一格 —— 预览稿当初就是
     靠把它收成一个变量才对齐的（用户标注："线和输入光标对齐"） */
  --hcc-lh: 22px;
  display: block;
  width: 100%;
  box-sizing: border-box;
  padding: 14px 16px 6px;
  background: transparent;
  /* 横线画在 textarea 自己的内容盒上：折行也跟着行走，不需要数行数。
     --rule-line 只在手记主题有值，其余主题是 transparent（组件不写主题分支）。 */
  background-image: linear-gradient(
    to bottom,
    transparent calc(var(--hcc-lh) - 1px),
    var(--rule-line) calc(var(--hcc-lh) - 1px)
  );
  background-size: 100% var(--hcc-lh);
  background-repeat: repeat-y;
  background-origin: content-box;
  border: 0;
  outline: 0;
  resize: none;
  overflow-y: hidden;
  color: var(--text-primary);
  font-family: var(--font-sans);
  font-size: 15px;
  line-height: var(--hcc-lh);
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
/* ＋ 是底栏唯一的上下文入口（故事 5），做成方角小方块让它一眼是"加"而不是一个词 */
.hcc-btn.plus { width: 26px; padding: 0; justify-content: center; font-size: 15px }
/* 可移除的 chip（故事 6）：底色用 --bg-input 而不是 elevated —— 它表示"这条消息
   会带上它"，比卡面暗一档才读得出是附加物，不是卡片自身的装饰 */
.hcc-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 22px;
  padding: 0 4px 0 8px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  color: var(--text-secondary);
  font-size: 11px;
}
.hcc-chip-x {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 15px;
  height: 15px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-tertiary);
  font-size: 12px;
  line-height: 1;
}
.hcc-chip-x:hover { background: var(--bg-hover); color: var(--text-primary) }
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
