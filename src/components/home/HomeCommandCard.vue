<script setup lang="ts">
/**
 * 首页指令卡（spec 2026-09-29-home-command-panel · 用户故事 1–9）
 *
 * 只做下达：发送走 useHomeCommand → ssh-ai.sendMessage 这一条通路；审批档、姿态
 * 都读 store 而不是组件内自存，保证首页选的东西在侧栏同样算数。
 *
 * ＋ 菜单按"后端有消费者"筛过（spec 故事 5 的 2026-09-30 修订记着砍掉的三项与证据），
 * 菜单里是：**目标 / 计划 / 终端现场 / 本地文件 / 远端文件 / 技能**。没有的那几项不是
 * 漏了 —— 是点亮后后端一字不变，那类开关在这仓里叫「亮的空开关」。
 * 远端文件取进来后与 /chat/upload 的返回同形，注入通路仍只有一条（store 的
 * addRemoteAttachment 负责截断口径，见那里的注释）。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import type { ChatAttachment } from '@/api/client'
import { useHomeCommand } from '@/composables/useHomeCommand'
import { useModelSelector } from '@/composables/useModelSelector'
import { useSettings } from '@/composables/useSettings'
import { useAgentStore } from '@/stores/agent'
import { useModelsStore } from '@/stores/models'
import { useSettingsStore } from '@/stores/settings'
import { REMOTE_ID_PREFIX, useSshAiStore } from '@/stores/ssh-ai'
import { useSkillStore } from '@/stores/skills'
import { useSSHStore } from '@/stores/ssh'
import HomeRemoteFilePicker from './HomeRemoteFilePicker.vue'
import { APPROVAL_OPTIONS, PERMISSION_LABELS, PERM_MARKS, PERM_SHIELD, resolveRunModes, type ApprovalKey } from '@/utils/run-modes'

const agent = useAgentStore()
const sshAi = useSshAiStore()
const ssh = useSSHStore()
const modelsStore = useModelsStore()
const skillsStore = useSkillStore()
const settings = useSettingsStore()
const { openSettings } = useSettings()
const { draft, canSend, submit, attachFiles, removeAttachment, uploading } = useHomeCommand()

/** 本地文件选择：input 藏在卡片里，＋ 菜单只负责唤起 —— 与侧栏那个回形针按钮
 *  走的是同一个 store.uploadFiles 通路，不是第二条上传实现。 */
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

/** ＋ 菜单的两个面：默认那几项，与远端文件的目录列表。每次重开都回默认面 ——
 *  停在某个目录里重开，用户会以为自己点错了地方。 */
const plusPane = ref<'root' | 'remote'>('root')

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
 *  而不是替用户选另一个姿态（与侧栏 togglePlanMode 同语义）。两者共用 posture
 *  这一个槽位（run-modes.ts 的 PostureKey = 'goal' | 'plan'），所以点一个会顶掉
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

/** 图标与侧栏审批钮共用一份（run-modes 的 PERM_SHIELD / PERM_MARKS），档位记号
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
  const cur = sshAi.selectedSkillIds
  sshAi.setSelectedSkillIds(cur.includes(id) ? cur.filter(x => x !== id) : [...cur, id])
}

/** 终端现场开关，缺省开着（关掉＝走纯文本入口那条空快照分支，默认行为一字不变）。
 *  这枚开关必须在：底栏 chip 的 × 写的是同一个 attachTerminalContext，只有 × 就是
 *  单向门 —— 手滑摘一次，这一整个会话都带不上终端现场，除非刷新页面。 */
function toggleTerminalContext() {
  sshAi.setAttachTerminalContext(!sshAi.attachTerminalContext)
  showPlusMenu.value = false
}

/** 这枚 chip 只在**真会带东西**时出现：开关开着但既没选区也没最近命令时，
 *  写着「终端现场」就是谎 —— 而故事 6 要 chip 的初衷正是"看得见带了什么"。 */
const terminalCtxLabel = computed(() => {
  if (!sshAi.attachTerminalContext) return ''
  const c = sshAi.terminalContext
  if (c.selection) return `终端选区 · ${c.selection.split('\n').filter(l => l.trim()).length} 行`
  if (c.recent.length) return `终端现场 · 最近 ${c.recent.length} 条`
  return ''
})

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

// 草稿是共享态（store 的 sharedDraft）：侧栏那边打字/清空也会落到这份 textarea，
// 高度与行线要跟着重算 —— 否则切回首页时内容被 overflow-y:hidden 裁掉下半截。
watch(draft, () => nextTick(grow))

function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Enter') return
  // 与侧栏同一口径：发送快捷键可在设置里换档，硬编码 Enter 会让两处行为不一致
  const wantShift = settings.settings.chat.sendShortcut === 'shiftEnter'
  if (e.shiftKey !== wantShift) return
  e.preventDefault()
  void send()
}

onMounted(() => {
  void sshAi.fetchDefaultModel()
  // 技能清单原先只在 SideChatPanel 的 setup 里装载（:513），而右侧 AI 面板没开时
  // 那个组件根本不挂载 —— 首页的 ＋ 菜单就会永远空着。空表才装，不重复扫盘。
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
          <template v-else>
          <!-- 五项：目标 / 计划 / 终端现场 / 本地文件 / 远端文件（技能在下面）。对勾紧跟字后面（原来钉在网格
               第二列，隔着整行的宽度，读起来不像"这条被选了"而像另一列数据）。 -->
          <button type="button" class="hcc-opt" :class="{ on: isGoal }" @click="toggleGoal">
            <span class="ol">目标<span v-if="isGoal" class="ot">✓</span></span>
            <span class="od">聚焦要达成什么、怎么算成功</span>
          </button>
          <button type="button" class="hcc-opt" :class="{ on: isPlan }" @click="togglePlan">
            <span class="ol">计划<span v-if="isPlan" class="ot">✓</span></span>
            <span class="od">只读调研，写操作直接拒绝</span>
          </button>
          <button
            type="button"
            class="hcc-opt"
            :class="{ on: sshAi.attachTerminalContext }"
            @click="toggleTerminalContext"
          >
            <span class="ol">终端现场<span v-if="sshAi.attachTerminalContext" class="ot">✓</span></span>
            <span class="od">选区与最近命令随消息带上</span>
          </button>
          <button
            type="button"
            class="hcc-opt"
            :disabled="uploading"
            @click="pickFiles"
          >
            <span class="ol">{{ uploading ? '上传中…' : '本地文件' }}</span>
            <span class="od">内容随指令注入，不是发给模型的路径</span>
          </button>
          <button
            type="button"
            class="hcc-opt"
            :disabled="!remoteSession"
            @click="plusPane = 'remote'"
          >
            <span class="ol">远端文件</span>
            <span class="od">{{ remoteSession ? `从 ${remoteSession} 取一个文件的内容` : '需要先连上一台服务器' }}</span>
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

      <span v-if="terminalCtxLabel" class="hcc-chip" title="这枚 chip 的 × 会关掉终端现场开关，＋ 菜单里可以开回来">
        {{ terminalCtxLabel }}
        <button
          type="button"
          class="hcc-chip-x"
          aria-label="关掉终端现场（＋ 菜单里可以重新打开）"
          @click="sshAi.setAttachTerminalContext(false)"
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

      <!-- 模型选择器坐行尾（与侧栏 toolbar-right 的 [模型][发送] 同一座位）：
           margin-left:auto 把它带着执行钮一起推到右边；下拉改从右缘向左展开，
           面板按 left:0 开会伸出卡外。 -->
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
            class="hcc-opt"
            :class="{ on: m.id === sshAi.selectedModelId }"
            @click="selectModel(m.id)"
          >
            <span class="ol">{{ m.name || m.model_id || m.id }}<span v-if="m.id === sshAi.selectedModelId" class="ot">✓</span></span>
          </button>
        </div>
      </div>

      <!-- 执行钮与侧栏提交件（SideChatPanel 的 .send-btn）完全同款：同一枚上行箭头、
           同一张皮（底走主题 hover 层、字走主文字色），身形也一并对齐成 24×24。
           它先前是 28px 高的宽体文字钮 —— 皮早已一致，这回连身形与语义也统一。 -->
      <button
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
/* 没有 placeholder：输入提示由上面那句问话承担（"今天想让 X 替你做什么？"），
   框内再摆一个占位符是重复，而它先前是三个点 —— 读起来像框坏了。
   所以这条 ::placeholder 规则也一并删掉：不匹配任何节点的规则就是死代码。 */
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
/* 文件选择器不进底栏（故事 5：＋ 之外不常驻"添加"按钮），只借程序化 click 唤起。
   display:none 的 input 在 Chromium 里照样能被 .click() 打开，侧栏用的是同一招。 */
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
.hcc-menu {
  position: absolute;
  left: 0;
  /* 朝下开，与同屏的 .htt-menu 一个方向。先前是 bottom: calc(100% + 6px) 朝上，
     而它在 .home-pane{overflow-y:auto} 里：实测菜单顶到 y=21、画布顶到 y=66，
     上面 45px 被裁掉且**滚不回来**（负方向没有滚动区）。朝下溢出至少还在滚动
     范围内。预览稿朝上开，但它不在被裁的滚动容器里。 */
  top: calc(100% + 6px);
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
/* 模型选择器坐底栏行尾：margin-left:auto 把它和执行钮一起推到右边（与侧栏
   toolbar-right 的 [模型][发送] 同一座位）。下拉从右缘向左展开 —— 220px 的
   面板按 left:0 开会伸出卡外。 */
.hcc-sel.model-sel { margin-left: auto }
.hcc-sel.model-sel .hcc-menu { left: auto; right: 0 }
/* 与 AI 运维助手侧栏的提交件同一枚（SideChatPanel 的 .send-btn）：24×24 方块
   箭头钮，底走主题的 hover 层、字走主题的主文字色，强调色彻底不参与。它先前是
   28px 高的宽体文字钮 —— 皮早已一致，这回连身形与语义（上行箭头=发出）也统一。
   圆角与 .send-btn 同为 4px 字面值：手记主题的 --radius-sm 是 2px，两处会对不上。 */
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
</style>
