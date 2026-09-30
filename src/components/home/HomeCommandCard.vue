<script setup lang="ts">
/**
 * 首页指令卡（spec 2026-09-29-home-command-panel · 用户故事 1–9）
 *
 * 只做下达：发送走 useHomeCommand → ssh-ai.sendMessage 这一条通路；审批档、姿态
 * 都读 store 而不是组件内自存，保证首页选的东西在侧栏同样算数。
 *
 * ＋ 菜单按"后端有消费者"筛过（spec 故事 5 的 2026-09-30 修订记着砍掉的三项与证据），
 * 现在四项齐了：**计划姿态 / 终端现场 / 本地文件 / 技能**。菜单里没有的那几项不是漏了
 * —— 是点亮后后端一字不变，那类开关在这仓里叫「亮的空开关」。
 * 「远端文件」按字面做要另接 sftpReadFile → 注入通路，仍是一片未开的工单。
 */
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useHomeCommand } from '@/composables/useHomeCommand'
import { useModelSelector } from '@/composables/useModelSelector'
import { useSettings } from '@/composables/useSettings'
import { useAgentStore } from '@/stores/agent'
import { useModelsStore } from '@/stores/models'
import { useSettingsStore } from '@/stores/settings'
import { useSshAiStore } from '@/stores/ssh-ai'
import { useSkillStore } from '@/stores/skills'
import { APPROVAL_OPTIONS, PERMISSION_LABELS, resolveRunModes, type ApprovalKey } from '@/utils/run-modes'

const agent = useAgentStore()
const sshAi = useSshAiStore()
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

/** 首页这枚是勾选控件，点已选中的要能取消 —— 但"怎么算重复""怎么摘掉"的算法归
 *  store（addSelectedSkill/removeSelectedSkill），这里只组合勾选语义。侧栏斜杠菜单
 *  用的是另一个组合（只加不减），两边共用同一对原子动作。 */
function toggleSkill(id: string) {
  if (sshAi.selectedSkillIds.includes(id)) sshAi.removeSelectedSkill(id)
  else sshAi.addSelectedSkill(id)
}

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
      placeholder="..."
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
          @click.stop="showPlusMenu = !showPlusMenu"
        >＋</button>
        <div v-if="showPlusMenu" class="hcc-menu wide" @click.stop>
          <button type="button" class="hcc-opt" :class="{ on: isPlan }" @click="togglePlan">
            <span class="ol">计划</span>
            <span class="od">只读调研，写操作直接拒绝</span>
            <span class="ot">{{ isPlan ? '✓' : '' }}</span>
          </button>
          <button
            type="button"
            class="hcc-opt"
            :class="{ on: sshAi.attachTerminalContext }"
            @click="toggleTerminalContext"
          >
            <span class="ol">终端现场</span>
            <span class="od">选区与最近命令随消息带上</span>
            <span class="ot">{{ sshAi.attachTerminalContext ? '✓' : '' }}</span>
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
          <div class="hcc-sep">技能</div>
          <button
            v-for="s in skillOptions"
            :key="s.id"
            type="button"
            class="hcc-opt single"
            :class="{ on: sshAi.selectedSkillIds.includes(s.id) }"
            @click="toggleSkill(s.id)"
          >
            <span class="ol">{{ s.name }}</span>
            <span class="ot">{{ sshAi.selectedSkillIds.includes(s.id) ? '✓' : '' }}</span>
          </button>
          <div v-if="!skillOptions.length" class="hcc-sep">没有已启用的技能</div>
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

      <span v-if="terminalCtxLabel" class="hcc-chip" title="摘掉后这条指令不带终端现场">
        {{ terminalCtxLabel }}
        <button
          type="button"
          class="hcc-chip-x"
          aria-label="本次不带终端现场"
          @click="sshAi.setAttachTerminalContext(false)"
        >×</button>
      </span>

      <span
        v-for="f in sshAi.pendingAttachments"
        :key="f.file_id"
        class="hcc-chip"
        :title="f.truncated ? '文件较大，只注入开头节选 —— 模型看到的是节选，不是全文' : f.filename"
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

      <button type="button" class="hcc-send" :disabled="!canSend" @click="send">执行</button>
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
.hcc-input::placeholder { color: var(--text-tertiary); opacity: .8 }
.hcc-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px 9px 12px;
  /* 底栏那一道折痕与次级纸面都归主题：--card-foot-* 在非手记主题 = 今天的样子
     （透明底 + 实线分隔），组件里不写主题分支（同 --rule-line 的做法） */
  border-top: var(--card-foot-rule, 1px solid var(--divider-primary));
  background: var(--card-foot-bg, transparent);
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
/* 文件选择器不进底栏（故事 5：＋ 之外不常驻"添加"按钮），只借程序化 click 唤起。
   display:none 的 input 在 Chromium 里照样能被 .click() 打开，侧栏用的是同一招。 */
.hcc-file { display: none }
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
/* 菜单里的小节标题（技能区）。刻意不是分隔线：手记那套语言靠字重与留白分组，
   再加一条线会和输入卡的行线抢注意力。 */
.hcc-sep {
  padding: 7px 8px 3px;
  font-size: 10px;
  letter-spacing: .08em;
  color: var(--text-tertiary);
}
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
.hcc-opt:hover:not(:disabled) { background: var(--bg-hover) }
/* 上传中：菜单项禁用要有可见差别，否则用户以为点了没反应，再点一次 */
.hcc-opt:disabled { opacity: .55; cursor: default }
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
  /* --accent-on 归主题/applyAccent 管，但 HEAD 里还没有它：variables.css 无定义，
     applyAccent 也只写 --accent-primary/-rgb。不带 fallback 这条声明就在计算期失效，
     前景退化成继承色 —— 全部 8 套主题的发送按钮都中招。先退回改动前的 #fff，等那边
     把令牌定义上，fallback 自动失效，这里的语义不用改。 */
  color: var(--accent-on, #fff);
  font-size: 13px;
  font-weight: 600;
  transition: filter var(--transition-fast), opacity var(--transition-fast);
}
.hcc-send:hover:not(:disabled) { filter: brightness(1.15) }
.hcc-send:disabled { opacity: .45; cursor: default }
</style>
