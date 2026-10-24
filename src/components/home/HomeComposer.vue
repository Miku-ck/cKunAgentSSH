<script setup lang="ts">
/**
 * 首页常驻输入区（任务台 Composer）
 *
 * 发送只有 useHomeCommand → ssh-ai.sendMessage 一条通路，审批档与姿态读 store，
 * 与发送装配同源 —— 界面宣称的选择必须真的随这一次下达出去。
 *
 * 底栏顺序：＋ → 分隔线 → 审批档 → 目标标记 → chips → 模型 → 发送/停止。
 * 换目标仍走＋ 菜单第一项 SSH 面板（openRemoteManager → SshView 开远程管理页），
 * 面板切换只改 ssh store 的 activeSessionId（useHomeTarget），不切走首页、不聚焦
 * 终端 —— 用户正在输入框里打字。审批档右边常驻的是这台目标的落点（远程地址 /
 * 本地 shell 名）：命令落到哪，得在放行之前就读得到，而不是等审批卡里那一行「目标主机」。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import type { ChatAttachment } from '@/api/client'
import { useHomeCommand } from '@/composables/useHomeCommand'
import { useHomeTarget } from '@/composables/useHomeTarget'
import { useModelSelector } from '@/composables/useModelSelector'
import { useSettings } from '@/composables/useSettings'
import { useAgentStore } from '@/stores/agent'
import { useAppStore } from '@/stores/app'
import { useModelsStore } from '@/stores/models'
import { useSettingsStore } from '@/stores/settings'
import { REMOTE_ID_PREFIX, useSshAiStore } from '@/stores/ssh-ai'
import { useSkillStore } from '@/stores/skills'
import { useSSHStore } from '@/stores/ssh'
import HomeRemoteFilePicker from './HomeRemoteFilePicker.vue'
import TaskStructureDialog from './TaskStructureDialog.vue'
import { useTaskStructuring } from '@/composables/useTaskStructuring'
import { APPROVAL_OPTIONS, PERMISSION_LABELS, PERM_MARKS, PERM_SHIELD, resolveRunModes, type ApprovalKey } from '@/utils/run-modes'

const emit = defineEmits<{ bulk: [], 'remote-mgr': [] }>()

const agent = useAgentStore()
const sshAi = useSshAiStore()
const ssh = useSSHStore()
const app = useAppStore()
const modelsStore = useModelsStore()
const skillsStore = useSkillStore()
const settings = useSettingsStore()
const { openSettings } = useSettings()
const { draft, canSend, submit, attachFiles, removeAttachment, uploading } = useHomeCommand()
const { targetChip, targetHint, options: targetOptions, pick: pickTarget, remoteCount, canBulkDispatch } = useHomeTarget()

// ── 长任务结构化（spec 2026-10-24-task-structuring）──
// 解构进 setup 作用域，模板才能拿到自动 unwrap 的 ref（structure 对象里的 ref
// 不解构直接用会是个永远 truthy 的 Ref，触发条件形同虚设）。
const {
  loading: structLoading,
  structure: structResp,
  confirmedAdded: structConfirmed,
  allAddedConfirmed: structAllConfirmed,
  canTrigger: structCanTrigger,
  open: openStruct,
  accept: acceptStruct,
  revert: revertStruct,
  close: closeStruct,
  toggleConfirm: toggleStructConfirm,
} = useTaskStructuring()

// ── 目标主机（＋ 菜单 SSH 面板）──
function chooseTarget(sessionId: string) {
  pickTarget(sessionId)
  closePlusMenu()
}

function toBulk() {
  emit('bulk')
  closePlusMenu()
}

/** 「连接远程服务器」：去远程管理页选主机连接。只开不关 —— 复用 toggle 会在
 *  页面已开时把它关掉，与「去连接」的语义相反。 */
function openRemoteManager() {
  emit('remote-mgr')
  closePlusMenu()
}

/** 断开当前目标这台：走终端页标签 × 的同一个出口（store.closeSession →
 *  sshDisconnect），所以首页不另定一套断开语义，也不弹确认 —— 那两个决策位
 *  若行为不同，用户按哪一处学到的经验都会有一半是错的。
 *  剩下的会话由 removeSession 决定落点：还剩别的就切到相邻那台，一台不剩才回「服务器」。 */
function disconnectTarget() {
  const id = ssh.activeSessionId
  if (!id || running.value) return
  void ssh.closeSession(id)
}

/** 本地文件选择：input 藏在卡片里，＋ 菜单只负责唤起 —— 走 store.uploadFiles
 *  这条唯一通路，不另开第二份上传实现。 */
const fileInputRef = ref<HTMLInputElement | null>(null)

function pickFiles() {
  fileInputRef.value?.click()
  showPlusMenu.value = false
}

async function onFilesPicked(e: Event) {
  const input = e.target as HTMLInputElement
  const files = input.files ? Array.from(input.files) : []
  // 立刻清空 value：否则"再选一次同一个文件"不触发 change，用户以为没挂上
  input.value = ''
  await attachFiles(files)
}

/** ＋ 菜单的三个面：默认那几项 / SSH 目标面板 / 远端文件的目录列表。
 *  每次重开都回默认面 —— 停在某个目录里重开，用户会以为自己点错了地方。 */
const plusPane = ref<'root' | 'remote' | 'target'>('root')

function closePlusMenu() {
  showPlusMenu.value = false
  plusPane.value = 'root'
}

function togglePlusMenu() {
  showPlusMenu.value = !showPlusMenu.value
  plusPane.value = 'root'
}

/** 取到一个远端文件：挂上 chip（底栏那枚就是反馈），菜单收起。
 *  不再补一条 toast —— 同一个动作弹两下反而更像没生效。 */
function onRemotePicked() {
  plusPane.value = 'root'
  showPlusMenu.value = false
}

const approvalMode = computed(() => sshAi.approvalMode)
const showPermMenu = ref(false)
const permRef = ref<HTMLElement | null>(null)
const showPlusMenu = ref(false)
/** 技能区开合（默认折起）。菜单是定高滚动区，展开状态跟着组件活，不随菜单关闭重置。 */
const skillsOpen = ref(false)
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

/** 「计划」与「目标」都是开关不是单选：点亮即表态，再点退出回到**未表态**，
 *  而不是替用户选另一个姿态。两者共用 posture 这一个槽位
 *  （run-modes.ts 的 PostureKey = 'goal' | 'plan'），所以点一个会顶掉
 *  另一个，而不是叠出第三种状态。 */
function togglePlan() {
  sshAi.setPosture(sshAi.posture === 'plan' ? null : 'plan')
  showPlusMenu.value = false
}

function toggleGoal() {
  sshAi.setPosture(sshAi.posture === 'goal' ? null : 'goal')
  showPlusMenu.value = false
}

const isPlan = computed(() => sshAi.posture === 'plan')
const isGoal = computed(() => sshAi.posture === 'goal')
/** 档位只算一次：标签与图标都从它派生，避免"字写着自动、图标还是那张盾"。 */
const permMode = computed(() =>
  resolveRunModes(sshAi.posture ?? undefined, approvalMode.value).permissionMode,
)
/** 审批档按钮上那一行字与发送装配同源：plan 姿态下 resolveRunModes 恒压出 'plan'
 *  （permission-mode.test.ts 锁着），所以这里显示「只读计划」而不是用户原档 ——
 *  界面不能宣称一个不会生效的选择。禁用是**展示**层，用户的 approvalMode 没被改写。 */
const permLabel = computed(() => PERMISSION_LABELS[permMode.value])

/** 图标与审批档共用一份（run-modes 的 PERM_SHIELD / PERM_MARKS），档位记号
 *  按上面的 permMode 派生 —— 标签与图标永远说的是同一个档。 */
const permMark = computed(() => PERM_MARKS[permMode.value])

/** ＋ 菜单的技能区。清单来自 skills store（后端那份，带设置页的开关状态），
 *  首页只挑已启用的 —— 未启用的技能点了也不会进 skill_ids（agent.py:594 按 mode 过滤）。 */
const skillOptions = computed(() => skillsStore.enabledSkills)
/** chip 只给查得到名字的：技能在设置页被删掉后，选中的 id 会悬空，
 *  印一枚空白 chip 比不印更骗人。flatMap 兼作收窄类型，不另写谓词。 */
const pickedSkills = computed(() =>
  sshAi.selectedSkillIds.flatMap(id => {
    const s = skillsStore.enabledSkills.find(sk => sk.id === id)
    return s ? [s] : []
  }),
)

/** 远端文件取的是**当前 AI 目标那台机器**的文件 —— 与 shell_exec 落地同一口径
 *  （useHomeTarget 改的也是 activeSessionId）。本地 shell 会话没有远端文件系统，
 *  这时整项禁用并把理由写在副标题里，而不是点了弹一句"会话未连接"。 */
const remoteSession = computed(() => {
  const s = ssh.sessions.find(x => x.id === ssh.activeSessionId)
  return s && s.type === 'remote' ? (s.shell || s.name) : ''
})

/** chip 的提示要说清"内容从哪来、模型看到多少"。远端与本地附件挂的是同一份列表、
 *  同一枚 chip —— 不写来源就分不出这两类，同名文件尤其骗人。 */
function attachmentTitle(f: ChatAttachment): string {
  const from = f.file_id.startsWith(REMOTE_ID_PREFIX)
    ? `远端 ${f.file_id.slice(REMOTE_ID_PREFIX.length)}`
    : `本地 ${f.filename}`
  return f.truncated ? `${from} · 文件较大，只注入开头节选 —— 模型看到的是节选，不是全文` : from
}

function toggleSkill(id: string) {
  // 去重与摘除的算法在 store（add/removeSelectedSkill），这里只组合"点一下是加上还是摘掉"
  if (sshAi.selectedSkillIds.includes(id)) sshAi.removeSelectedSkill(id)
  else sshAi.addSelectedSkill(id)
}

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

// 草稿是 store 级共享态（sharedDraft）：预填与打字都落到这份 textarea，
// 高度与行线要跟着重算 —— 否则内容被 overflow-y:hidden 裁掉半截。
// 带 immediate：预填若发生在挂载之前，只守变化的 watch 就永远不补这一次高度。
watch(draft, () => nextTick(grow), { immediate: true })

// 「问 AI」预填（终端选区 / 批量下发结果 / 监控指标诊断）统一落进这份草稿。
// SshView 的 sync watch 先把用户带回任务台，这里只负责消费一次：置 null 后
// 再进首页不会把同一段问题重放一遍，用户改过的草稿也不会被覆盖。
watch(() => sshAi.pendingPrefill, (req) => {
  if (!req) return
  sshAi.pendingPrefill = null
  sshAi.setSharedDraft(req.text)
  void nextTick(() => ta.value?.focus())
}, { immediate: true })

function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Enter') return
  // 发送快捷键可在设置里换档：硬编码 Enter 会让首页输入框与设置说的不是一回事
  const wantShift = settings.settings.chat.sendShortcut === 'shiftEnter'
  if (e.shiftKey !== wantShift) return
  e.preventDefault()
  void send()
}

onMounted(() => {
  // 启动可靠性：模型列表/默认模型要 GET 后端，gate 在就绪信号上 —— 后端通过健康检查才发，
  // 消除首屏"fetchModels failed：后端服务未就绪"的抢跑 warn。闸门只在真正就绪时 resolve，
  // 后端不起则不开、不发抢跑请求（模型选择器置空，不谎报）。
  void app.readyPromise.then(() => sshAi.fetchDefaultModel()).catch(() => { /* 防御位：readyPromise 设认不 reject */ })
  // 技能清单只在首页装载（右侧 AI 面板退役后 ＋ 菜单是唯一入口）：那边曾经挂在
  // 面板的 setup 里，面板不开就永远空着。空表才装，不重复扫盘。
  // 这条走本地 electronAPI 扫盘、不碰后端 HTTP，无需 gate 就绪。
  if (!skillsStore.skills.length) void skillsStore.loadBuiltinSkills()
})
</script>

<template>
  <div class="hcc">
    <textarea
      ref="ta"
      v-model="draft"
      class="hcc-input"
      rows="2"
      placeholder="输入指令…"
      aria-label="输入指令"
      spellcheck="false"
      @input="grow"
      @keydown="onKeydown"
    />
    <input
      ref="fileInputRef"
      type="file"
      multiple
      class="hcc-file"
      :disabled="running || uploading"
      @change="onFilesPicked"
    />
    <div class="hcc-foot">
      <div ref="plusRef" class="hcc-sel">
        <button
          type="button"
          class="hcc-btn plus"
          :disabled="running"
          :aria-expanded="showPlusMenu"
          title="添加上下文"
          @click.stop="togglePlusMenu"
        >＋</button>
        <div v-if="showPlusMenu" class="hcc-menu wide" @click.stop>
          <HomeRemoteFilePicker
            v-if="plusPane === 'remote'"
            @back="plusPane = 'root'"
            @picked="onRemotePicked"
          />
          <!-- SSH 目标面板：已打开的会话列表 + 批量下发 + 连接远程服务器（去远程管理页） -->
          <template v-else-if="plusPane === 'target'">
            <button
              v-for="o in targetOptions"
              :key="o.sessionId"
              type="button"
              class="hcc-opt"
              :class="{ on: o.active }"
              @click="chooseTarget(o.sessionId)"
            >
              <span class="ol">{{ o.label }}<span v-if="o.active" class="ot">✓</span></span>
              <span class="od">{{ o.hint }}</span>
            </button>
            <div v-if="!targetOptions.length" class="hcc-sep">还没有打开的会话</div>
            <button v-if="canBulkDispatch" type="button" class="hcc-opt" @click="toBulk">
              <span class="ol">全部 {{ remoteCount }} 台</span>
              <span class="od">批量下发 · 不改 AI 目标</span>
            </button>
            <button type="button" class="hcc-opt" @click="openRemoteManager">
              <span class="ol">连接远程服务器</span>
            </button>
          </template>
          <template v-else>
          <!-- 五项：SSH / 目标 / 计划 / 本地文件 / 远端文件（技能在下面）。对勾紧跟字后面（原来钉在网格
               第二列，隔着整行的宽度，读起来不像"这条被选了"而像另一列数据）。 -->
          <button type="button" class="hcc-opt" @click="plusPane = 'target'">
            <span class="ol">SSH</span>
          </button>
          <button type="button" class="hcc-opt" :class="{ on: isGoal }" @click="toggleGoal">
            <span class="ol">目标<span v-if="isGoal" class="ot">✓</span></span>
          </button>
          <button type="button" class="hcc-opt" :class="{ on: isPlan }" @click="togglePlan">
            <span class="ol">计划<span v-if="isPlan" class="ot">✓</span></span>
          </button>
          <button
            type="button"
            class="hcc-opt"
            :disabled="uploading"
            @click="pickFiles"
          >
            <span class="ol">{{ uploading ? '上传中…' : '本地文件' }}</span>
          </button>
          <button
            type="button"
            class="hcc-opt"
            :disabled="!remoteSession"
            @click="plusPane = 'remote'"
          >
            <span class="ol">远端文件</span>
          </button>
          <!-- 技能有 N 条时折起来：这枚菜单是定高滚动区（.wide 260px），技能一多
               前五项就被挤出视野，而那五项才是每次下达都要碰的。折叠标题带条数；
               已挂载的技能在卡面上有 chip，收起列表不会把"我挂了谁"藏起来。 -->
          <button
            v-if="skillOptions.length"
            type="button"
            class="hcc-sep fold"
            :aria-expanded="skillsOpen"
            @click="skillsOpen = !skillsOpen"
          >
            <span class="fold-caret" :class="{ open: skillsOpen }">▸</span>技能
            <span class="fold-n">{{ skillOptions.length }}</span>
          </button>
          <div v-show="skillsOpen" class="hcc-fold-body">
            <button
              v-for="s in skillOptions"
              :key="s.id"
              type="button"
              class="hcc-opt"
              :class="{ on: sshAi.selectedSkillIds.includes(s.id) }"
              @click="toggleSkill(s.id)"
            >
              <span class="ol">{{ s.name }}<span v-if="sshAi.selectedSkillIds.includes(s.id)" class="ot">✓</span></span>
            </button>
          </div>
          <div v-if="!skillOptions.length" class="hcc-sep">没有已启用的技能</div>
          </template>
        </div>
      </div>

      <span class="hcc-divider" aria-hidden="true"></span>

      <div ref="permRef" class="hcc-sel">
        <button
          type="button"
          class="hcc-btn"
          :disabled="running || isPlan"
          :aria-expanded="showPermMenu"
          :title="isPlan ? '计划姿态下写操作被直接拒绝，「谁来批准」不参与本次裁决' : ''"
          @click.stop="showPermMenu = !showPermMenu"
        >
          <!-- 图标 + 档位名，不再写「审批 ·」：这三枚并排在底栏，各自是什么已经由
               图标与菜单位置说明，重复一遍前缀只是把按钮撑宽。
               盾形 = 谁来放行，盾里的记号分档位（见 PERM_MARKS）。 -->
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12" aria-hidden="true">
            <path :d="PERM_SHIELD" />
            <path v-if="permMark" :d="permMark" />
          </svg>
          <b>{{ permLabel }}</b>
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
            <span class="ol">{{ o.label }}<span v-if="approvalMode === o.key" class="ot">✓</span></span>
            <span class="od">{{ o.desc }}</span>
          </button>
        </div>
      </div>

      <!-- 长任务结构化（spec 2026-10-24-task-structuring）：草稿够长够模糊时浮现的
           旁路入口。点击只开对照弹窗、绝不动输入框；采用/回退都在弹窗里显式裁决。
           已增强后收起入口、换成「已增强」徽标 + 一键回到原文。 -->
      <button
        v-if="structCanTrigger && !sshAi.enhancedActive"
        type="button"
        class="hcc-btn"
        :disabled="structLoading || running"
        title="把这条长指令拆解为明确步骤并补全运维字段 · 原文可对照、可回退"
        @click="openStruct"
      >
        <b>{{ structLoading ? '结构化中…' : '结构化' }}</b>
      </button>
      <span
        v-if="sshAi.enhancedActive"
        class="hcc-chip"
        title="已采用结构化草稿 · 可一键回到原文"
      >
        已增强
        <button
          type="button"
          class="hcc-chip-x"
          aria-label="恢复原文"
          title="恢复原文"
          @click="revertStruct"
        >×</button>
      </span>

      <span v-if="targetChip" class="hcc-target" :title="targetHint">
        {{ targetChip }}
        <button
          type="button"
          class="hcc-chip-x"
          aria-label="关闭这个终端会话"
          :disabled="running"
          :title="running ? '运行中不能关闭当前目标，请先停止本轮' : '关闭这台终端（与终端页标签 × 同一个出口）'"
          @click="disconnectTarget"
        >×</button>
      </span>

      <!-- 故事 6：加进来的每一项都在底栏留一枚可移除的 chip。姿态这枚必须连目标态
           一起显形 —— ＋ 菜单里两个都能点亮，只显计划就是从中间截走的故事。 -->
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

      <span
        v-for="s in pickedSkills"
        :key="s.id"
        class="hcc-chip"
        :title="s.description || s.name"
      >
        {{ s.name }}
        <button
          type="button"
          class="hcc-chip-x"
          :aria-label="`移除技能 ${s.name}`"
          @click="toggleSkill(s.id)"
        >×</button>
      </span>

      <span
        v-for="f in sshAi.pendingAttachments"
        :key="f.file_id"
        class="hcc-chip"
        :title="attachmentTitle(f)"
      >
        {{ f.filename }}
        <button
          type="button"
          class="hcc-chip-x"
          :aria-label="`移除附件 ${f.filename}`"
          @click="removeAttachment(f.file_id)"
        >×</button>
      </span>

      <span v-if="running" class="hcc-note">AI 正在运行中</span>

      <!-- 模型选择器坐行尾：margin-left:auto 把它带着执行钮一起推到右边；
           下拉改从右缘向左展开，面板按 left:0 开会伸出卡外。 -->
      <div ref="modelSelectorRef" class="hcc-sel model-sel">
        <button
          v-if="!noModel"
          type="button"
          class="hcc-btn"
          title="切换模型"
          aria-haspopup="listbox"
          :aria-expanded="showModelDropdown"
          @click.stop="showModelDropdown = !showModelDropdown"
        >
          <b>{{ currentModelName }}</b>
        </button>
        <button
          v-else
          type="button"
          class="hcc-btn empty"
          title="去设置里选择模型"
          @click="openSettings('models')"
        >
          <b>请选择模型</b>
        </button>
        <div v-if="showModelDropdown && !noModel" class="hcc-menu wide" @click.stop>
          <button
            v-for="m in modelsStore.models"
            :key="m.id"
            type="button"
            class="hcc-opt"
            :class="{ on: m.id === sshAi.selectedModelId }"
            @click="selectModel(m.id)"
          >
            <span class="ol">{{ m.name || m.model_id || m.id }}<span v-if="m.id === sshAi.selectedModelId" class="ot">✓</span></span>
          </button>
        </div>
      </div>

      <!-- 运行中发送键换成停止键；输入框仍可编辑（下一轮草稿），Enter 由 submit()
           的早退 toast 挡住，不排队、不清草稿。 -->
      <button
        v-if="running"
        type="button"
        class="hcc-send hcc-stop"
        title="停止"
        aria-label="停止"
        @click="agent.abort()"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <rect x="7" y="7" width="10" height="10" rx="1" fill="currentColor" stroke="none"/>
        </svg>
      </button>
      <button
        v-else
        type="button"
        class="hcc-send"
        :disabled="!canSend"
        title="执行"
        aria-label="执行"
        @click="send"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <line x1="12" y1="19" x2="12" y2="5"/>
          <polyline points="5 12 12 5 19 12"/>
        </svg>
      </button>
    </div>
  </div>

  <!-- 对照弹窗：居中模态（DESIGN.md 禁抽屉）。只呈现事实与回抛意图，store 由
       useTaskStructuring 裁决；v-if=structResp 保证只在拿到结构稿后才挂载。 -->
  <TaskStructureDialog
    v-if="structResp"
    :structure="structResp"
    :original="sshAi.draftBaseline"
    :confirmed-added="structConfirmed"
    :all-added-confirmed="structAllConfirmed"
    @close="closeStruct"
    @accept="acceptStruct"
    @revert="revertStruct"
    @toggle-confirm="toggleStructConfirm"
  />
</template>

<style scoped>
/* 只用现有主题令牌，不写死颜色 —— 主题层是第四片的事 */
.hcc {
  width: 100%;
  max-width: 820px;
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
  /* 必须 local：高度封顶 4 行 + overflow-y:hidden，长草稿在框内自己滚。默认的
     scroll = 背景钉在元素上，文字滚、线不滚 → 线与字错开一格（正是上面注释
     声明要避免的那个失败）。预览稿 grow() 不封顶所以碰不到，app 封顶就有。 */
  background-attachment: local;
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
/* 空态问句已经不再兼任输入提示（有消息后标题换成会话名），所以输入框
   回到自己说明自己：一行 placeholder，颜色用三级文字，不与正文抢。 */
.hcc-input::placeholder {
  color: var(--text-tertiary);
}
.hcc-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px 9px 12px;
  /* 底栏那一道折痕与次级纸面都归主题：--card-foot-* 在非手记主题 = 今天的样子
     （透明底 + 实线分隔），组件里不写主题分支（同 --rule-line 的做法） */
  border-top: var(--card-foot-rule);
  background: var(--card-foot-bg);
  flex-wrap: wrap;
}
.hcc-sel { position: relative }
/* 底栏这三枚不画自己的皮：透明底 + 无边框，直接坐在卡面上（手记的卡脚另有那道
   次级纸面渐变，它们跟着一起走）。先前它们是 --bg-base，比卡面 --bg-elevated 还暗
   一档，看着像卡上挖的三个洞。UA chrome 由 global.css 的 button reset 断掉。 */
.hcc-btn {
  display: flex;
  align-items: center;
  gap: 5px;
  height: 24px;
  padding: 0 9px;
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  font-size: 12px;
  transition: background var(--transition-fast);
}
.hcc-btn b { font-weight: 500; color: var(--text-primary) }
.hcc-btn:hover:not(:disabled) { background: var(--bg-hover) }
.hcc-btn:disabled { opacity: .5; cursor: default }
.hcc-btn.empty b { color: var(--accent-orange) }
/* ＋ 是底栏唯一的上下文入口（故事 5），做成方角小方块让它一眼是"加"而不是一个词 */
.hcc-btn.plus { width: 26px; padding: 0; justify-content: center; font-size: 15px }
.hcc-divider {
  width: 1px;
  height: 14px;
  margin: 0 2px;
  background: var(--border-subtle);
  flex-shrink: 0;
}
/* 文件选择器不进底栏（故事 5：＋ 之外不常驻"添加"按钮），只借程序化 click 唤起。
   display:none 的 input 在 Chromium 里照样能被 .click() 打开。 */
.hcc-file { display: none }
/* 可移除的 chip（故事 6）：底是"主题自己的墨按的一道渍"—— text-primary 7% 压在
   卡面上，卡面是什么它就跟着是什么（手记的卡脚还有那道次级纸面渐变，chip 叠上去
   照样读得出）。先前用的是 --bg-input：那是一个固定深色档，在蓝屏这类浅一档的卡面
   上就成了一块比周围更黑的补丁，而不是"这条消息多带了个东西"。
   边框同底栏其他控件一起撤掉（flat 到底，靠这道墨渍分辨边界）。 */
.hcc-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 22px;
  padding: 0 4px 0 8px;
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--text-primary) 7%, transparent);
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
/* 审批档右边的目标标记（远程地址 / 本地 shell 名）。等宽字是「这是一个落点不是一个词」最快的信号；不铺底：
   有了底就会被读成旁边那排 chip 里的一员，而那些 chip 的 × 摘掉的是附加上下文，
   这枚 × 断开的是连接 —— 两者代价不同，不能共用一个外观语言。 */
.hcc-target {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-secondary);
  white-space: nowrap;
}
.hcc-target .hcc-chip-x:disabled { opacity: .5; cursor: default }
.hcc-menu {
  position: absolute;
  left: 0;
  /* 朝上开：Composer 是消息滚动区之外的常驻底栏，下方就是窗口底边。
     朝下开在 960px 高窗口里会被窗口裁掉，且底栏不能滚。 */
  top: auto;
  bottom: calc(100% + 6px);
  width: 236px;
  padding: 4px;
  background: var(--bg-elevated);
  /* 不画框：与卡面同色的一层浮起，靠 shadow 与卡分离（边框是"框"这件事本身） */
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  z-index: 30;
}
.hcc-menu.wide { width: 220px; max-height: 260px; overflow-y: auto }
/* 菜单里的小节标题（技能区）。刻意不是分隔线：手记那套语言靠字重与留白分组，
   再加一条线会和输入卡的行线抢注意力。 */
.hcc-sep {
  padding: 7px 8px 3px;
  font-size: 10px;
  letter-spacing: .08em;
  color: var(--text-tertiary);
}
/* 折叠标题仍是"分组小字"这套语言，只补交互件该有的三件事：撑满一行、hover 有反馈、
   caret 说清开合。不画框也不铺底 —— 它和上面那几枚 .hcc-opt 一样，是浮在卡面上的。 */
.hcc-sep.fold {
  display: flex;
  align-items: center;
  gap: 5px;
  width: 100%;
  border: 0;
  background: none;
  cursor: pointer;
  font-family: inherit;
  border-radius: var(--radius-sm);
  transition: background var(--transition-fast);
}
.hcc-sep.fold:hover { background: var(--bg-hover) }
.fold-caret { transition: transform .15s; font-size: 11px }
.fold-caret.open { transform: rotate(90deg) }
.fold-n { margin-left: auto; letter-spacing: 0; color: var(--text-secondary) }
.hcc-opt {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 1px;
  width: 100%;
  text-align: left;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
  transition: background var(--transition-fast);
}
.hcc-opt:hover:not(:disabled) { background: var(--bg-hover) }
/* 上传中：菜单项禁用要有可见差别，否则用户以为点了没反应，再点一次 */
.hcc-opt:disabled { opacity: .55; cursor: default }
.hcc-opt .ol { font-size: 12px; color: var(--text-primary) }
.hcc-opt .od { font-size: 11px; color: var(--text-tertiary); line-height: 1.45 }
/* 对勾跟在名字后面：它先前独占网格第二列、垂直跨两行，隔着整行宽度钉在最右，
   读起来像另一列数据而不像"这一条被选了" */
.hcc-opt .ot {
  margin-left: 6px;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--accent-green);
}
.hcc-note { font-size: 11px; color: var(--text-tertiary) }
/* 模型选择器坐底栏行尾：margin-left:auto 把它和执行钮一起推到右边。
   下拉从右缘向左展开 —— 220px 的面板按 left:0 开会伸出卡外。 */
.hcc-sel.model-sel { margin-left: auto }
.hcc-sel.model-sel .hcc-menu { left: auto; right: 0 }
/* 提交件：24×24 方块箭头钮，底走主题的 hover 层、字走主题的主文字色，强调色彻底
   不参与。它先前是 28px 高的宽体文字钮 —— 身形与语义（上行箭头=发出）都收成一枚。
   圆角写 4px 字面值：手记主题的 --radius-sm 是 2px，与相邻控件会对不上。 */
.hcc-send {
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 0;
  outline: none;
  appearance: none;
  border-radius: 4px;
  background: var(--bg-hover);
  color: var(--text-primary);
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
  flex-shrink: 0;
}
.hcc-send:hover:not(:disabled) { background: var(--bg-active); color: var(--text-primary) }
.hcc-send:disabled { opacity: .5; cursor: not-allowed }
.hcc-send svg { width: 14px; height: 14px }
/* 停止键用危险色底：运行中它不是"再发一条"的替代，而是这轮唯一的出口。 */
.hcc-stop { background: var(--danger-bg); color: var(--danger-text) }
.hcc-stop:hover:not(:disabled) { background: var(--danger-border); color: var(--danger-text) }
</style>
