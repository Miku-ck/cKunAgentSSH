<script setup lang="ts">
/**
 * 首页常驻输入区（任务台 Composer）
 *
 * 发送只有 useHomeCommand → ssh-ai.sendMessage 一条通路，审批档与姿态读 store，
 * 与发送装配同源 —— 界面宣称的选择必须真的随这一次下达出去。
 *
 * 底栏顺序：＋ → 分隔线 → 审批档 → 落点 chip（1~2 枚）
 *           → 目标标记 → chips → 模型 → 发送/停止。
 * ＋ 菜单左栏六项（附件 / 目标 / 计划 / 技能 / 专家 / 插件）：附件在目标上面
 * （用户 2026-11-10 指定），本地与远端文件收进附件的二级面板；落点不占 ＋。
 * 一级左栏保持静止不带动效，动画只允许出现在二级面板（用户明确要求）。
 * 落点收敛到同一个事实源 activeSessionId（stores/exec-target）：后端只认
 * ssh_session_id，visible_terminal 把命令交付回前端后按 session.type 分流到
 * ptyWrite / sshWrite。「工作树」= 以 git worktree 路径为 cwd 的本地 PTY 会话，
 * 因此不需要给 run 请求新增 workdir 字段。换落点不切走首页、不聚焦终端 ——
 * 用户正在输入框里打字。目标标记仍是这台落点的读数（远程地址 / 本地 shell 名）：
 * 命令落到哪，得在放行之前就读得到，而不是等审批卡里那一行「目标主机」。
 *
 * 落点不是三个并列的选项，而是「位置 + 目录 + 分支」三段读数（2026-11-10 定稿）：
 *   ① 位置 —— 本地 / 云端 / 工作树 三选一（本机或云上），SSH 是另一台机器，与它们互斥；
 *   ② 目录 —— 三态常驻，只换语义：本地读会话 cwd，工作树读仓库，SSH 读远程目录；
 *   ③ 分支 —— 只有工作树长这一枚。
 * 所以底栏 chip 数是 2 / 2 / 3：本地「本地 + 文件夹」、SSH「主机 + 远程目录」、
 * 工作树再加一枚分支。第二枚不能省 —— 少了它，「本地」读不出在哪个目录，
 * 用户第一眼要问的就是“这是哪个文件夹”。
 *
 * 每一枚 chip 都可展开自己的浮层，且都只有一层（2026-11-10 用户纠正）：在位置面
 * 点「工作树」就是选落点、菜单收起，直接落到当前仓库的主工作树；分支留给随之
 * 出现的第三枚 chip 再挑。没有「点一下还要点第二下」的钻取，也没有「‹ 返回」。
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
import { useExpertsStore } from '@/stores/experts'
import { dirKey, useExecTargetStore } from '@/stores/exec-target'
import { usePluginsStore } from '@/stores/plugins'
import { useSSHStore } from '@/stores/ssh'
import { useToastStore } from '@/stores/toast'
import type { Plugin } from '@/api/core'
import HomeRemoteFilePicker from './HomeRemoteFilePicker.vue'
import RemoteDirPicker from '@/components/ssh/RemoteDirPicker.vue'
import QuickSshDialog from '@/components/ssh/QuickSshDialog.vue'
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
const expertsStore = useExpertsStore()
const pluginsStore = usePluginsStore()
const landing = useExecTargetStore()
const toast = useToastStore()
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

/** 本地文件选择：input 藏在卡片里，「附件」菜单只负责唤起 —— 走 store.uploadFiles
 *  这条唯一通路，不另开第二份上传实现。 */
const fileInputRef = ref<HTMLInputElement | null>(null)

function pickFiles() {
  fileInputRef.value?.click()
  closePlusMenu()
}

async function onFilesPicked(e: Event) {
  const input = e.target as HTMLInputElement
  const files = input.files ? Array.from(input.files) : []
  // 立刻清空 value：否则"再选一次同一个文件"不触发 change，用户以为没挂上
  input.value = ''
  await attachFiles(files)
}

/** ＋ 菜单是双栏级联：左栏六项常驻（附件在目标上面），右栏是附件 / 技能 / 专家 /
 *  插件的二级面板。root = 只有左栏；每次重开都回 root，停在某个子面板里重开会
 *  让人以为点错了地方。 */
type PlusPane = 'root' | 'attach' | 'skill' | 'expert' | 'plugin'
const plusPane = ref<PlusPane>('root')
const cascadeQuery = ref('')
const cascadeSearchRef = ref<HTMLInputElement | null>(null)
const cascadeLabel = computed(() =>
  plusPane.value === 'skill' ? '技能'
    : plusPane.value === 'expert' ? '专家'
      : plusPane.value === 'plugin' ? '插件' : '附件',
)
function openCascade(which: Exclude<PlusPane, 'root'>) {
  plusPane.value = which
  cascadeQuery.value = ''
  attachPane.value = 'root'
  // 打开右栏即聚焦搜索框：级联面板的用途就是「搜索并挑选」，少一次点击
  // （附件面板没有搜索框，optional chaining 自然跳过）
  void nextTick(() => cascadeSearchRef.value?.focus())
}
/** 搜索框里的 Esc 分两级退：先清查询，再回左栏，第三次才关整个 ＋ 菜单。
 *  .stop 挡住全局 Esc（onKeydownEsc），两级退出由更近的这层说了算。 */
function onCascadeEsc() {
  if (cascadeQuery.value) { cascadeQuery.value = ''; return }
  if (plusPane.value !== 'root') { plusPane.value = 'root'; return }
  showPlusMenu.value = false
}
function cascadeMatch(haystack: string): boolean {
  const q = cascadeQuery.value.trim().toLowerCase()
  return !q || haystack.toLowerCase().includes(q)
}
/** 落点只有一个浮层，四个面：kind（本地/云端/工作树 + 连接 SSH/WSL）、
 *  dir（本地模式的文件夹）、repo（工作树模式的仓库）、branch（该仓的分支）。
 *  与 ＋ 菜单互斥。 */
type LandingPane = '' | 'kind' | 'dir' | 'repo' | 'branch'
const landingMenu = ref<LandingPane>('')
const landingRef = ref<HTMLElement | null>(null)

/** 附件面板的内部二级状态：root = 本地 / 远端两个动作，remote = 远端目录选择器。
 *  附件现在是 ＋ 菜单左栏第一项（用户指定），不再有独立的底栏入口。 */
const attachPane = ref<'root' | 'remote'>('root')

function closePlusMenu() {
  showPlusMenu.value = false
  plusPane.value = 'root'
  cascadeQuery.value = ''
  attachPane.value = 'root'
  landingMenu.value = ''
}

function togglePlusMenu() {
  showPlusMenu.value = !showPlusMenu.value
  plusPane.value = 'root'
  if (showPlusMenu.value) {
    landingMenu.value = ''
    // 自愈：首屏那次 /skills?mode=ssh 可能在后端就绪前失败，remoteSkills 空了就
    // 不再有第二次机会（onMounted 的空表守卫已被本地扫盘结果填掉）。用户每次点开
    // ＋ 都补拉一次，后端重启/网络抖动也能自己恢复，不必重启应用。
    if (!skillsStore.enabledSkills.length) void skillsStore.loadRemoteSkills()
  }
}

/** 第一枚 chip：在哪里执行。SSH 时直接显示 host —— 比起「SSH」两个字，
 *  用户更认得出是哪台机器。 */
const landingLabel = computed(() => {
  if (landing.kind === 'worktree') return '工作树'
  if (landing.kind === 'ssh') return landing.activeRemoteLabel || 'SSH'
  return '本地'
})
/** 图标跟读同一来源；无会话时按「本地」画（首次进入的隐式默认落点）。 */
const landingIcon = computed<'local' | 'worktree' | 'ssh'>(() => landing.kind ?? 'local')
/** 第二枚 chip：在哪个目录。三态常驻，语义随落点变（文件夹 / 仓库 / 远程目录）。 */
/** 第二枚 chip 的读数。**没有绑定目录时不编「主目录」**：主目录不是用户选的项目，
 *  写上去等于把「没选」谎报成一个位置（2026-11-11 用户口径）。这一枚写「不指定工作区」——
 *  描述的是**状态**（没指定），不是动作（打开文件夹）；动作在它展开的菜单里。 */
const dirLabel = computed(() =>
  landing.activeDirLabel || (landing.kind === 'worktree' ? '仓库' : '不指定工作区'),
)
const dirChipTitle = computed(() => {
  if (landing.kind === 'worktree') return landing.activeRepoPath || '选择本机 git 仓库'
  // 悬停始终给全路径：chip 上只有末级名，远程落点尤其需要知道自己在哪台机器的哪个目录
  if (landing.kind === 'ssh') {
    if (landing.activeDirUnavailable) return '连接已断开 · 点开切换主机或重新连接'
    return landing.activeDirPath ? `远程目录：${landing.activeDirPath}` : '远程目录未知 · 点开切换主机'
  }
  return landing.activeDirPath ? `当前目录：${landing.activeDirPath}` : '不在项目中工作 · 点击选一个文件夹'
})
const repoLabel = computed(() => landing.activeRepoLabel || '仓库')
const branchLabel = computed(() => landing.activeBranch === '—' ? '分支' : landing.activeBranch)

/** 第二枚 chip 开哪个面：工作树读仓库列表，本地与 SSH 读目录。 */
const dirPane = computed<Exclude<LandingPane, ''>>(() => landing.kind === 'worktree' ? 'repo' : 'dir')
function toggleDirChip() { toggleLanding(dirPane.value) }

/** 五面共用一个浮层；每次展开都顺手补一次列表（已加载时 loadWorktrees 只被
 *  这里挡住，不重复跑 git）。与 ＋ 菜单互斥。新建工作树表单随面切换收起。 */
watch(landingMenu, () => { wtFormOpen.value = false })
function toggleLanding(which: Exclude<LandingPane, ''>) {
  const open = landingMenu.value !== which
  landingMenu.value = open ? which : ''
  showPlusMenu.value = false
  if (open && which === 'dir') {
    // 每次展开都是全量列表；搜索框直接接管焦点，打开就能打字筛。
    dirQuery.value = ''
    void nextTick(() => dirSearchRef.value?.focus())
  }
  if (open && !landing.hasRepos && !landing.error && !landing.loading) {
    void landing.loadWorktrees()
  }
}

async function chooseLocalChip() {
  landingMenu.value = ''
  await landing.chooseLocal()
}

/** 点「工作树」只是选落点，不再往二级菜单里钻（2026-11-10 用户纠正）：直接落到
 *  当前仓库的主工作树，菜单收起，底栏随即长出分支 chip。分支在分支 chip 上换，
 *  那枚是并列的一级读数，不是这一步的必答题。 */
async function pickWorktreeKind() {
  await landing.chooseWorktreeDefault()
  if (!landing.error) landingMenu.value = ''
}

async function chooseWslChip() {
  landingMenu.value = ''
  await landing.chooseWsl()
}

/** 目录面的搜索词。展开目录面时清空，收起后不残留上次过滤 —— 下次打开还是全量。 */
const dirQuery = ref('')
const dirSearchRef = ref<HTMLInputElement | null>(null)

/** 目录面的一行 = 一条目录记录，**外加当前落点那一行**。
 *
 *  为什么当前目录必须单独补一行：记录只由「在这里打开过文件夹」写入，而落点也可能
 *  来自别处（终端页打开、上一次热更前就存在的会话）。只列记录的话，会出现 chip 写着
 *  `pose-label`、列表里却一行都没打勾 —— 用户看不到自己在哪（2026-11-11 截图反馈）。
 *  所以：当前绑定目录永远置顶且打勾，它代表事实而不是候选，因此不给 ×（删了也还在，
 *  等于哑按钮）；其余记录保持可删。
 *
 *  记录以外的目录一概不预读 —— 没打开过就是空列表，只剩底部两行固定入口。 */
const dirRows = computed(() => {
  const active = landing.activeBoundDir
  const activeKey = active ? dirKey(active) : ''
  const seen = new Set<string>()
  const rows: { path: string; name: string; active: boolean; removable: boolean }[] = []
  for (const p of [active, ...landing.recentDirs]) {
    if (!p) continue
    // 按 dirKey 去重：同一目录的 `D:\a\b` / `D:/a/b/` 两种写法必须是同一行
    const k = dirKey(p)
    if (seen.has(k)) continue
    seen.add(k)
    rows.push({ path: p, name: shortPath(p), active: k === activeKey, removable: k !== activeKey })
  }
  const q = dirQuery.value.trim().toLowerCase()
  if (!q) return rows
  return rows.filter(r => r.name.toLowerCase().includes(q) || r.path.toLowerCase().includes(q))
})

/** 删掉一条目录记录：只从候选里移除，不关掉正在那个目录里跑的会话。 */
function removeDirRecord(path: string) {
  landing.removeRecentDir(path)
}

/** 当前落点就是「不在项目中」那种（本地会话没绑定任何目录）：底部那行打勾。
 *  与目录行互斥 —— 绑定目录为空时不会有任何一行被选中，不会出现两个 ✓。 */
const atHomeRoot = computed(() => landing.kind === 'local' && !landing.activeBoundDir)

async function chooseDirChip(path: string) {
  landingMenu.value = ''
  await landing.chooseLocalDir(path)
}

/** 「不在项目中工作」：回到不绑定目录的本地会话（主目录），chip 上不再挂文件夹。 */
async function chooseHomeChip() {
  landingMenu.value = ''
  await landing.chooseLocalHome()
}

/** 搜索框里回车 = 选第一条匹配项，手不用离开键盘。 */
function onDirEnter() {
  const first = dirRows.value[0]
  if (first) void chooseDirChip(first.path)
}

async function pickDirChip() {
  landingMenu.value = ''
  await landing.pickLocalDir()
}

/** 云端是菜单里的固定位置，但执行环境还没接 —— 点了说实话，不留一个哑按钮。 */
function onCloudClick() {
  toast.info('云执行环境还没接入，先用本地或工作树')
}

/** 选仓库 = 落点跟着落到这个仓库的主工作树，菜单收起。以前这里会跳进分支面，
 *  等于点一下要点两下；仓库与分支是两枚并列 chip，各自开各自的浮层。 */
async function chooseRepoChip(repo: string) {
  landing.selectRepo(repo)
  await landing.chooseWorktreeDefault()
  if (!landing.error) landingMenu.value = ''
}

/** 新建工作树的行内表单（参考 Trae 的任务驱动形态）：预填自动名，Enter 创建。
 *  成功后 store 已把落点切到新分支，这里只收菜单；失败原因落 landing.error 可见。 */
const wtFormOpen = ref(false)
const wtBranchName = ref('')
const wtFormInputRef = ref<HTMLInputElement | null>(null)
/** 有失效工作树（目录已被手动删除）时才露「清理失效记录」。 */
const hasPrunable = computed(() => !!landing.activeGroup?.items?.some(w => w.prunable))
function openWtForm() {
  const rand = Math.random().toString(36).slice(2, 6)
  wtBranchName.value = `feat/ai-${rand}`
  wtFormOpen.value = true
  void nextTick(() => wtFormInputRef.value?.focus())
}
function closeWtForm() {
  wtFormOpen.value = false
}
async function submitWtForm() {
  if (!landing.activeRepoPath) return
  const name = wtBranchName.value.trim()
  if (!name) return
  const wt = await landing.createWorktree(landing.activeRepoPath, name)
  if (wt) {
    wtFormOpen.value = false
    landingMenu.value = ''
  }
}

function chooseRemoteChip(sessionId: string) {
  pickTarget(sessionId)
  landingMenu.value = ''
}

/** 幽灵会话手动回拨：只在 chip 已判定「未连接」时出现。
 *  自动那一次由 exec-target 负责（每个会话只试一次），这里给的是失败之后的第二次机会 ——
 *  网络恢复、或密码需要重输时，用户不必再跑去终端页关标签重连。 */
async function redialActiveRemote() {
  const id = ssh.activeSessionId
  if (!id) return
  try {
    await ssh.redial(id)
    landingMenu.value = ''
  } catch (e) {
    // 菜单留在原地：失败原因写进同一个 error 槽，用户一眼能看到为什么连不上
    landing.error = `重新连接失败：${e instanceof Error ? e.message : String(e)}`
  }
}

/** SSH 目录面里浏览到的目录：**纯浏览不改落点**，点「在此目录工作」才生效。
 *  否则在 /home 里翻两层，chip 就被改了三次，读数是浏览痕迹而不是工作目录。 */
const remoteDirPick = ref('')

/** 让远端 shell 切到所选目录。远端落点的目录不是配置，而是那条 shell 的 cwd ——
 *  后端只认 ssh_session_id，命令最终写回同一个 pty，所以这里落成一句 cd，
 *  不给 run 请求新增任何 workdir 字段（见 exec-target 头部注释）。 */
function applyRemoteDir() {
  const id = ssh.activeSessionId
  if (!id || !remoteDirPick.value) return
  ssh.cdRemote(id, remoteDirPick.value)
  landingMenu.value = ''
}

/** SSH 走弹窗（2026-11-10 按参照实现重做）：一行地址吃掉主机/端口/用户，
 *  下面直接是主机历史，点「连接」就进 —— 不再让人先在四个字段里把同一件事拆开。 */
const showNewConn = ref(false)
function openConnDialog() {
  landingMenu.value = ''
  showNewConn.value = true
}

type WorktreeChoice = Parameters<typeof landing.chooseWorktree>[0]
async function chooseWorktreeChip(w: WorktreeChoice) {
  await landing.chooseWorktree(w)
  if (!landing.error) landingMenu.value = ''
}

/** 取到一个远端文件：挂上 chip（底栏那枚就是反馈），菜单收起。
 *  不再补一条 toast —— 同一个动作弹两下反而更像没生效。 */
function onRemotePicked() {
  closePlusMenu()
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
  if (landingRef.value && !landingRef.value.contains(t)) landingMenu.value = ''
}

/** Esc 收起所有浮层（含模型下拉之外的两枚）。级联搜索框的 Esc 被 .stop 拦在
 *  更近的一层（onCascadeEsc 两级退出），不会走到这里。菜单本来都关着时是空操作，
 *  不与 TaskStructureDialog 这类居中弹窗自己的 Esc 抢语义。 */
function onKeydownEsc(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  showPermMenu.value = false
  showPlusMenu.value = false
  plusPane.value = 'root'
  cascadeQuery.value = ''
  attachPane.value = 'root'
  landingMenu.value = ''
}

onMounted(() => {
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onKeydownEsc)
  // 专家 / 插件定义在设置页可增删改，首页打开时各拉一次；后续变更由对应面板刷新
  void expertsStore.fetchExperts()
  void pluginsStore.fetchPlugins()
  // 顶栏「仓库 / 分支」chip 常驻显示读数，启动就得探一次 git（store 有 20 仓上限）。
  // 以前是「展开工作树下拉才拉」，现在读数挪到 chip 上，不拉就会一直显示占位。
  void landing.loadWorktrees()
})
onUnmounted(() => {
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onKeydownEsc)
})

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

/** SSH 面只列远程会话；本地 / WSL 会话归 kind 面「本地」下面那一小组。 */
const remoteTargetOptions = computed(() => targetOptions.value.filter(o => {
  const s = ssh.sessions.find(x => x.id === o.sessionId)
  return !!s && s.type === 'remote'
}))

/** 仓库 chip 只显示目录短名，完整路径走 title —— 底栏放不下整条绝对路径。 */
function shortPath(p: string): string {
  const parts = p.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] || p
}

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

/** 专家是「跟随下一次发送」的选择：与目标 chip 同语义，逐条消息可换。
 *  chip 只认查得到的 id —— 专家在设置页被删掉后不留一枚空白 chip。 */
const expertOptions = computed(() => expertsStore.enabledExperts)
const pickedExpert = computed(() =>
  sshAi.selectedExpertId ? expertsStore.byId(sshAi.selectedExpertId) : null,
)

function chooseExpert(id: string) {
  sshAi.setSelectedExpert(id)
  closePlusMenu()
}

/** ＋ 菜单「插件」只反映本次生效范围，不新开权限：已启用且已同意的插件，再与
 *  当前专家的允许集求交。专家 plugin_ids_json 为 NULL 时继承全部、'[]' 时显式
 *  不授予；坏 JSON 一律 fail-closed，与后端 expert_policy 同姿态。逐项临时收紧
 *  需要 per-run 请求字段，本期不做 —— 这个面板是只读的。 */
const pluginOptions = computed(() => pluginsStore.enabled)
const runPlugins = computed(() => {
  const all = pluginOptions.value
  const expert = pickedExpert.value
  if (!expert) return all
  const raw: unknown = expert.plugin_ids_json
  if (raw == null || String(raw).trim() === '') return all
  try {
    const allow = JSON.parse(String(raw)) as string[]
    if (!Array.isArray(allow)) return []
    return all.filter(p => allow.includes(p.id))
  } catch {
    return []
  }
})

function pluginSummary(p: Plugin): string {
  const tools = pluginsStore.toolsOf(p).length
  if (!p.enabled || !p.consented_at) return '未启用 · 需到设置里授权'
  if (!runPlugins.value.some(x => x.id === p.id)) return `${tools} 个工具 · 当前专家未授予`
  return `${tools} 个工具 · 已授权 · 逐项只读确认`
}

/** 右栏三个面板共用一枚搜索框：按名称 + 描述过滤，空查询不过滤。 */
const filteredSkills = computed(() =>
  skillOptions.value.filter(s => cascadeMatch(`${s.name} ${s.description || ''}`)),
)
const filteredExperts = computed(() =>
  expertOptions.value.filter(e => cascadeMatch(`${e.name} ${e.description || ''}`)),
)
const filteredPlugins = computed(() =>
  pluginsStore.plugins.filter(p => cascadeMatch(`${p.name} ${pluginSummary(p)}`)),
)

function openPluginSettings() {
  closePlusMenu()
  openSettings('plugins')
}

/** 技能一个都没启用时，菜单里得有一条能走出去的路，而不是只写一句空态。 */
function openSkillSettings() {
  closePlusMenu()
  openSettings('skills')
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
  // 面板的 setup 里，面板不开就永远空着。本地扫盘空表才装，不重复扫盘。
  if (!skillsStore.skills.length) void skillsStore.loadBuiltinSkills()
  // 菜单真正的数据源是后端 /skills?mode=ssh（带设置页的 per-mode 开关），它走
  // HTTP。loadBuiltinSkills 的 finally 里那次拉取会抢跑失败并被 catch 吞掉，而
  // builtinSkills 一旦填上，空表守卫就再也不会触发 —— 技能区会永远显示
  //「没有已启用的技能」。这里在就绪闸门后再补拉一次，失败还由打开 ＋ 自愈。
  void app.readyPromise.then(() => skillsStore.loadRemoteSkills()).catch(() => { /* 防御位 */ })
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
        <!-- 双栏级联：左栏五层常驻，技能 / 专家 / 插件带 › 在右栏展开二级面板。
             不再是「点一下整卡替换」或「技能内联折叠」—— 左栏始终看得见，
             右栏带搜索框与图标 + 标题 + 描述的列表，切换层级不迷路。 -->
        <div v-if="showPlusMenu" class="hcc-menu cascade" @click.stop>
          <div class="hcc-cascade-left">
            <button
              type="button"
              class="hcc-opt casc-opt"
              :class="{ current: plusPane === 'attach' }"
              :aria-expanded="plusPane === 'attach'"
              title="添加附件"
              @click="openCascade('attach')"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="15" height="15" aria-hidden="true">
                <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
              </svg>
              <span class="ol">附件</span>
              <span class="casc-arrow">›</span>
            </button>
            <button
              type="button"
              class="hcc-opt casc-opt"
              :class="{ on: isGoal }"
              title="本次要达成什么、怎么算成功"
              @click="toggleGoal"
            >
              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" width="15" height="15" aria-hidden="true">
                <circle cx="8" cy="8" r="5.5" /><circle cx="8" cy="8" r="2" />
              </svg>
              <span class="ol">目标<span v-if="isGoal" class="ot">✓</span></span>
            </button>
            <button
              type="button"
              class="hcc-opt casc-opt"
              :class="{ on: isPlan }"
              title="只读拆解步骤，写操作被直接拒绝"
              @click="togglePlan"
            >
              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" width="15" height="15" aria-hidden="true">
                <path d="M3 4.5h10M3 8h10M3 11.5h7" />
              </svg>
              <span class="ol">计划<span v-if="isPlan" class="ot">✓</span></span>
            </button>
            <div class="hcc-rule"></div>
            <button
              type="button"
              class="hcc-opt casc-opt"
              :class="{ current: plusPane === 'skill' }"
              :aria-expanded="plusPane === 'skill'"
              title="过程性知识：怎么做"
              @click="openCascade('skill')"
            >
              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" width="15" height="15" aria-hidden="true">
                <rect x="3" y="3" width="10" height="10" rx="1" /><path d="M8 3v10M5 6h1.4M5 9h1.4" />
              </svg>
              <span class="ol">技能</span>
              <span class="casc-arrow">›</span>
            </button>
            <button
              type="button"
              class="hcc-opt casc-opt"
              :class="{ current: plusPane === 'expert' }"
              :aria-expanded="plusPane === 'expert'"
              :title="pickedExpert ? `当前：${pickedExpert.name}` : '运行策略配置包'"
              @click="openCascade('expert')"
            >
              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" width="15" height="15" aria-hidden="true">
                <circle cx="8" cy="5.5" r="2.5" /><path d="M3.5 13a4.5 4.5 0 0 1 9 0" />
              </svg>
              <span class="ol">专家<span v-if="pickedExpert" class="ot">✓</span></span>
              <span class="casc-arrow">›</span>
            </button>
            <button
              type="button"
              class="hcc-opt casc-opt"
              :class="{ current: plusPane === 'plugin' }"
              :aria-expanded="plusPane === 'plugin'"
              :title="`本次 ${runPlugins.length} 个可用 · 只读`"
              @click="openCascade('plugin')"
            >
              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" width="15" height="15" aria-hidden="true">
                <path d="M6 3v3M10 3v3M4 6h8v3a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2zM8 11v2" />
              </svg>
              <span class="ol">插件</span>
              <span class="casc-arrow">›</span>
            </button>
          </div>
        </div>
        <!-- 二级面板独立成层（用户 2026-11-11：卡片尺寸固定不动）：与卡片并列，
             贴在卡片右侧同一底边。放 .hcc-sel 内，外部点击判定仍算「点在菜单里」。 -->
        <div v-if="showPlusMenu && plusPane !== 'root'" class="hcc-cascade-right" :key="plusPane" @click.stop>
            <div v-if="plusPane !== 'attach'" class="casc-search">
              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" width="14" height="14" aria-hidden="true">
                <circle cx="7" cy="7" r="4" /><path d="M10 10l3.5 3.5" />
              </svg>
              <input
                ref="cascadeSearchRef"
                v-model="cascadeQuery"
                type="text"
                :placeholder="`搜索${cascadeLabel}`"
                :aria-label="`搜索${cascadeLabel}`"
                spellcheck="false"
                @keydown.esc.stop.prevent="onCascadeEsc"
              />
            </div>
            <div class="casc-list">
              <template v-if="plusPane === 'attach'">
                <HomeRemoteFilePicker
                  v-if="attachPane === 'remote'"
                  @back="attachPane = 'root'"
                  @picked="() => onRemotePicked()"
                />
                <template v-else>
                  <button
                    type="button"
                    class="casc-item"
                    :disabled="uploading"
                    title="从本机选择，挂到本次发送"
                    @click="pickFiles"
                  >
                    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" width="15" height="15" aria-hidden="true">
                      <path d="M8 3v10M3 8h10" />
                    </svg>
                    <span class="ci-body">
                      <span class="ci-name">{{ uploading ? '上传中…' : '本地文件' }}</span>
                      <span class="ci-desc">从本机选择，挂到本次发送</span>
                    </span>
                  </button>
                  <button
                    type="button"
                    class="casc-item"
                    :disabled="!remoteSession"
                    :title="remoteSession ? `从 ${remoteSession} 的目录选取` : '需要先连接一台远程会话'"
                    @click="attachPane = 'remote'"
                  >
                    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" width="15" height="15" aria-hidden="true">
                      <path d="M8 3v10M3 8h10" />
                    </svg>
                    <span class="ci-body">
                      <span class="ci-name">远端文件</span>
                      <span class="ci-desc">{{ remoteSession ? `从 ${remoteSession} 的目录选取` : '需要先连接一台远程会话' }}</span>
                    </span>
                  </button>
                </template>
              </template>
              <template v-else-if="plusPane === 'skill'">
                <button
                  v-for="s in filteredSkills"
                  :key="s.id"
                  type="button"
                  class="casc-item"
                  :class="{ on: sshAi.selectedSkillIds.includes(s.id) }"
                  :title="s.description || s.name"
                  @click="toggleSkill(s.id)"
                >
                  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" width="15" height="15" aria-hidden="true">
                    <rect x="3" y="3" width="10" height="10" rx="1" /><path d="M8 3v10M5 6h1.4M5 9h1.4" />
                  </svg>
                  <span class="ci-body">
                    <span class="ci-name">{{ s.name }}<span v-if="sshAi.selectedSkillIds.includes(s.id)" class="ot">✓</span></span>
                    <span class="ci-desc">{{ s.description || '过程性知识 · 无描述' }}</span>
                  </span>
                </button>
                <div v-if="!filteredSkills.length" class="casc-empty">
                  {{ skillOptions.length ? '没有匹配的技能' : '还没有已启用的技能' }}
                </div>
                <button
                  v-if="!skillOptions.length"
                  type="button"
                  class="casc-item"
                  title="打开设置 · 技能面板，启用后回到这里挂载"
                  @click="openSkillSettings"
                >
                  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" width="15" height="15" aria-hidden="true">
                    <path d="M8 3v10M3 8h10" />
                  </svg>
                  <span class="ci-body">
                    <span class="ci-name">去启用技能…</span>
                    <span class="ci-desc">打开设置 · 技能面板</span>
                  </span>
                </button>
              </template>
              <template v-else-if="plusPane === 'expert'">
                <button
                  type="button"
                  class="casc-item"
                  :class="{ on: !pickedExpert }"
                  title="沿用默认运维智能体与全局设置"
                  @click="chooseExpert('')"
                >
                  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" width="15" height="15" aria-hidden="true">
                    <circle cx="8" cy="12" r="4" /><path d="M8 10v4M6 12h4" />
                  </svg>
                  <span class="ci-body">
                    <span class="ci-name">不指定专家<span v-if="!pickedExpert" class="ot">✓</span></span>
                    <span class="ci-desc">沿用默认运维智能体与全局设置</span>
                  </span>
                </button>
                <button
                  v-for="e in filteredExperts"
                  :key="e.id"
                  type="button"
                  class="casc-item"
                  :class="{ on: sshAi.selectedExpertId === e.id }"
                  :title="e.description || `r${e.revision} · 运行策略配置`"
                  @click="chooseExpert(e.id)"
                >
                  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" width="15" height="15" aria-hidden="true">
                    <circle cx="8" cy="5.5" r="2.5" /><path d="M3.5 13a4.5 4.5 0 0 1 9 0" />
                  </svg>
                  <span class="ci-body">
                    <span class="ci-name">{{ e.name }}<span v-if="sshAi.selectedExpertId === e.id" class="ot">✓</span></span>
                    <span class="ci-desc">{{ e.description || `r${e.revision} · 运行策略配置` }}</span>
                  </span>
                </button>
                <div v-if="!filteredExperts.length" class="casc-empty">
                  {{ expertOptions.length ? '没有匹配的专家' : '还没有可用的专家，可在设置里新建' }}
                </div>
              </template>
              <template v-else>
                <div class="hcc-sep">本次运行可用（只读）</div>
                <div
                  v-for="p in filteredPlugins"
                  :key="p.id"
                  class="casc-item ro"
                  :class="{ on: runPlugins.some(x => x.id === p.id) }"
                  :title="pluginSummary(p)"
                >
                  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" width="15" height="15" aria-hidden="true">
                    <path d="M6 3v3M10 3v3M4 6h8v3a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2zM8 11v2" />
                  </svg>
                  <span class="ci-body">
                    <span class="ci-name">{{ p.name }}<span v-if="runPlugins.some(x => x.id === p.id)" class="ot">✓</span></span>
                    <span class="ci-desc">{{ pluginSummary(p) }}</span>
                  </span>
                </div>
                <div v-if="!filteredPlugins.length" class="casc-empty">
                  {{ pluginsStore.plugins.length ? '没有匹配的插件' : '还没有安装插件，可在设置里新增' }}
                </div>
                <button type="button" class="casc-item" title="打开设置 · 插件面板" @click="openPluginSettings">
                  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" width="15" height="15" aria-hidden="true">
                    <path d="M8 3v10M3 8h10" />
                  </svg>
                  <span class="ci-body">
                    <span class="ci-name">管理插件</span>
                    <span class="ci-desc">安装与授权在「设置 → 插件」，此处不新开权限</span>
                  </span>
                </button>
              </template>
            </div>
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
            :title="o.desc"
            @click="pickPerm(o.key)"
          >
            <span class="ol">{{ o.label }}<span v-if="approvalMode === o.key" class="ot">✓</span></span>
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

      <!-- 执行落点：第一枚「在哪里执行」，第二枚「哪个目录」，第三枚只属于工作树。
           本地也必须有目录 chip —— 少了它，「本地」就是两个无名的地方，用户第一眼
           要问的是“这是哪个文件夹”（2026-11-10 按参照实现纠正）。 -->
      <div ref="landingRef" class="hcc-sel landing-sel">
        <div class="hcc-landing">
          <button
            type="button"
            class="hcc-btn landing kind-chip"
            :class="{ on: landingMenu === 'kind' }"
            :aria-expanded="landingMenu === 'kind'"
            :title="`当前落点：${landingLabel} · 点击切换`"
            @click.stop="toggleLanding('kind')"
          >
            <!-- 图标跟读当前落点：本地=显示器、工作树=分支、SSH=终端 —— 与 kind 面
                 里对应行的图标同源，chip 与菜单说的是同一套视觉词汇。 -->
            <svg v-if="landingIcon === 'local'" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" width="12" height="12" aria-hidden="true">
              <rect x="2" y="3" width="12" height="8.5" rx="1" /><path d="M6 14h4M8 11.5V14" />
            </svg>
            <svg v-else-if="landingIcon === 'ssh'" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" width="12" height="12" aria-hidden="true">
              <rect x="2" y="3" width="12" height="10" rx="1.5" /><path d="M5 6.5 7 8l-2 1.5M9 10.5h2" />
            </svg>
            <svg v-else viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" width="12" height="12" aria-hidden="true">
              <circle cx="4.5" cy="3.5" r="1.5" /><circle cx="4.5" cy="12.5" r="1.5" /><circle cx="11.5" cy="5" r="1.5" />
              <path d="M4.5 5v6M11.5 6.5v1.2a2.8 2.8 0 0 1-2.8 2.8H6" />
            </svg>
            <b>{{ landingLabel }}</b>
            <svg class="caret" :class="{ up: landingMenu === 'kind' }" viewBox="0 0 8 8" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" width="8" height="8" aria-hidden="true"><path d="M1.8 3 4 5.2 6.2 3" /></svg>
          </button>
          <!-- 第二枚：在哪个目录。三态常驻，只换语义 —— 本地读文件夹、工作树读仓库、
               SSH 读远程目录。命令落到哪里，这枚就写哪里，不靠猜。 -->
          <button
            type="button"
            class="hcc-btn landing dir-chip"
            :class="{ on: landingMenu === dirPane }"
            :aria-expanded="landingMenu === dirPane"
            :title="dirChipTitle"
            @click.stop="toggleDirChip()"
          >
            <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" width="12" height="12" aria-hidden="true">
              <path d="M2 4.2h4L7.2 5.7H14v6.1a.9.9 0 0 1-.9.9H2.9a.9.9 0 0 1-.9-.9Z" />
            </svg>
            <b>{{ dirLabel }}</b>
            <svg class="caret" :class="{ up: landingMenu === dirPane }" viewBox="0 0 8 8" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" width="8" height="8" aria-hidden="true"><path d="M1.8 3 4 5.2 6.2 3" /></svg>
          </button>
          <!-- 第三枚只有工作树才有：本地是「就在这个文件夹」，SSH 是「另一台机器上的
               某个目录」，分支对两者都无意义，渲染出来就是无中生有。 -->
          <button
            v-if="landing.showBranch"
            type="button"
            class="hcc-btn landing"
            :class="{ on: landingMenu === 'branch' }"
            :aria-expanded="landingMenu === 'branch'"
            :title="`git worktree 分支 · ${landing.activeRepoPath || '先选一个仓库'}`"
            @click.stop="toggleLanding('branch')"
          >
            <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" width="12" height="12" aria-hidden="true">
              <circle cx="4.5" cy="3.5" r="1.5" /><circle cx="4.5" cy="12.5" r="1.5" /><circle cx="11.5" cy="5" r="1.5" />
              <path d="M4.5 5v6M11.5 6.5v1.2a2.8 2.8 0 0 1-2.8 2.8H6" />
            </svg>
            <b>{{ branchLabel }}</b><svg class="caret" viewBox="0 0 8 8" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" width="8" height="8" aria-hidden="true"><path d="M1.8 3 4 5.2 6.2 3" /></svg>
          </button>
        </div>
        <!-- 这块是一个 select，不是菜单：每行一行字 + 右侧勾选/箭头，没有图标、
             没有分组小标题。选中即收起，不往二级面里钻（2026-11-10 用户口径）。 -->
        <div v-if="landingMenu" class="hcc-menu wide landing-menu" @click.stop>
          <template v-if="landingMenu === 'kind'">
            <button
              type="button"
              class="hcc-opt landing-opt"
              :class="{ current: landing.kind === 'local' }"
              title="就在当前文件夹操作 · 不涉及 git 分支"
              @click="chooseLocalChip"
            >
              <span class="ol">本地</span>
              <span v-if="landing.kind === 'local'" class="tick">✓</span>
            </button>
            <button
              type="button"
              class="hcc-opt landing-opt is-soon"
              title="云执行环境还没接入"
              @click="onCloudClick"
            >
              <span class="ol">云端</span>
              <span class="meta">未接入</span>
            </button>
            <button
              type="button"
              class="hcc-opt landing-opt"
              :class="{ current: landing.kind === 'worktree' }"
              title="按 git worktree 隔离 · 直接落到当前仓库的主工作树"
              @click="pickWorktreeKind"
            >
              <span class="ol">工作树</span>
              <span v-if="landing.kind === 'worktree'" class="tick">✓</span>
            </button>
            <!-- 已连接的远程主机：执行目标本来就是「本地 / 某台机器」这一个维度，
                 主机列表属于这里。原先是塞在第二枚 chip（目录）的浮层里 —— 点「目录」
                 弹出一串主机、外加一行 `user@host` 勾选，用户 2026-11-11 明确否掉：
                 那枚 chip 只管目录，主机归第一枚。 -->
            <button
              v-for="o in remoteTargetOptions"
              :key="o.sessionId"
              type="button"
              class="hcc-opt landing-opt"
              :class="{ current: o.active }"
              :title="o.hint"
              @click="chooseRemoteChip(o.sessionId)"
            >
              <span class="ol">{{ o.label }}</span>
              <span v-if="o.active" class="tick">✓</span>
            </button>
            <div class="hcc-rule"></div>
            <button type="button" class="hcc-opt landing-opt" title="弹窗粘贴 ssh 命令或点主机历史，连上即切落点" @click="openConnDialog">
              <span class="ol">连接 SSH…</span>
            </button>
            <button type="button" class="hcc-opt landing-opt" title="新建一个 WSL 本地终端并切过去" @click="chooseWslChip">
              <span class="ol">连接 WSL…</span>
            </button>
            <button v-if="canBulkDispatch" type="button" class="hcc-opt landing-opt" title="批量下发 · 不改 AI 目标" @click="toBulk">
              <span class="ol">全部 {{ remoteCount }} 台</span>
            </button>
            <button type="button" class="hcc-opt landing-opt" title="打开终端页的服务器管理面板" @click="openRemoteManager">
              <span class="ol">管理服务器…</span>
            </button>
            <div v-if="landing.error" class="hcc-sep">{{ landing.error }}</div>
          </template>
          <!-- 第二枚 chip 的本地面：按目录选，不像菜单像项目选择器（2026-11-11 参考实现）：
               一行一个**打开过的**目录，当前项右侧绿 ✓，hover 浮出 × 删掉这条记录；
               底部「不在项目中工作」是默认态（不绑定任何文件夹）。
               列表不预读会话 / 工作树 / 仓库 —— 没打开过就只剩底部两行。 -->
          <template v-else-if="landingMenu === 'dir' && landing.kind !== 'ssh'">
            <div v-if="landing.recentDirs.length" class="casc-search dir-search">
              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" width="13" height="13" aria-hidden="true">
                <circle cx="7" cy="7" r="4.3" /><path d="M10.2 10.2 13 13" />
              </svg>
              <input
                ref="dirSearchRef"
                v-model="dirQuery"
                type="text"
                placeholder="搜索目录"
                aria-label="搜索目录"
                spellcheck="false"
                @keydown.enter.prevent="onDirEnter"
                @keydown.esc.stop.prevent="landingMenu = ''"
              />
            </div>
            <div v-for="r in dirRows" :key="r.path" class="dir-row-wrap">
              <button
                type="button"
                class="hcc-opt landing-opt dir-row"
                :class="{ current: r.active }"
                :title="r.path"
                @click="chooseDirChip(r.path)"
              >
                <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" width="13" height="13" aria-hidden="true">
                  <path d="M2 4.2h4L7.2 5.7H14v6.1a.9.9 0 0 1-.9.9H2.9a.9.9 0 0 1-.9-.9Z" />
                </svg>
                <span class="ol">{{ r.name }}</span>
                <span v-if="r.active" class="tick">✓</span>
              </button>
              <button
                v-if="r.removable"
                type="button"
                class="dir-del"
                title="从列表移除"
                aria-label="从列表移除"
                @click.stop="removeDirRecord(r.path)"
              >
                <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" width="11" height="11" aria-hidden="true">
                  <path d="M4 4l8 8M12 4l-8 8" />
                </svg>
              </button>
            </div>
            <div v-if="dirQuery && !dirRows.length" class="hcc-sep">没有匹配的目录</div>
            <template v-if="!dirQuery">
              <div v-if="dirRows.length" class="hcc-rule"></div>
              <button type="button" class="hcc-opt landing-opt" title="以主目录为落点，不绑定任何文件夹" @click="chooseHomeChip">
                <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" width="13" height="13" aria-hidden="true">
                  <path d="M4 4l8 8M12 4l-8 8" />
                </svg>
                <span class="ol">不在项目中工作</span>
                <span v-if="atHomeRoot" class="tick">✓</span>
              </button>
              <button type="button" class="hcc-opt landing-opt" title="目录选择器，选中即以该目录落一个本地会话" @click="pickDirChip">
                <span class="ol">打开文件夹…</span>
              </button>
            </template>
            <div v-if="landing.error" class="hcc-sep">{{ landing.error }}</div>
          </template>
          <!-- SSH 的第二枚 chip = 远端目录。这里不再列主机（那属于第一枚 chip），
               而是一份真的远端文件/目录列表：用户在首页要的是「这台机器的哪个目录」，
               只列主机等于点开文件夹却给了一张主机清单（2026-11-11 用户否掉这版）。 -->
          <template v-else-if="landingMenu === 'dir'">
            <!-- 断了的会话排第一：此时这枚 chip 写的是「未连接」，用户点开它要的是
                 「把它接回来」，而不是「换一台主机」。 -->
            <template v-if="landing.activeDirUnavailable">
              <button
                type="button"
                class="hcc-opt landing-opt"
                title="这条连接已断开（主进程那边已经没有它了），点一下重新拨号"
                @click="redialActiveRemote"
              >
                <span class="ol">重新连接…</span>
              </button>
              <div class="hcc-sep">连上之后这里会列出远端目录</div>
            </template>
            <template v-else>
              <RemoteDirPicker
                :session-id="ssh.activeSessionId"
                mode="dir"
                show-files
                :initial="landing.activeDirPath || '/'"
                @update:dir="remoteDirPick = $event"
              />
              <button
                type="button"
                class="hcc-opt landing-opt"
                :disabled="!remoteDirPick"
                :title="remoteDirPick ? `在 ${remoteDirPick} 下执行后续命令` : '先选一个文件夹'"
                @click="applyRemoteDir"
              >
                <span class="ol">在此目录工作</span>
              </button>
            </template>
            <div v-if="landing.error" class="hcc-sep">{{ landing.error }}</div>
          </template>
          <template v-else-if="landingMenu === 'repo'">
            <div v-if="landing.loading" class="hcc-sep">正在读取工作树…</div>
            <div v-if="landing.error" class="hcc-sep">{{ landing.error }}</div>
            <button
              v-for="g in landing.groups"
              :key="g.repo"
              type="button"
              class="hcc-opt landing-opt"
              :class="{ current: landing.activeRepoPath === g.repo }"
              :title="g.repo"
              @click="chooseRepoChip(g.repo)"
            >
              <span class="ol">{{ shortPath(g.repo) }}<span class="count">{{ g.items.length }}</span></span>
              <span v-if="landing.activeRepoPath === g.repo" class="tick">✓</span>
            </button>
            <div v-if="!landing.groups.length" class="hcc-sep">还没有登记本机仓库</div>
            <div class="hcc-rule"></div>
            <button type="button" class="hcc-opt landing-opt" :disabled="landing.loading" title="目录选择器，登记后刷新列表" @click="landing.registerRepo()">
              <span class="ol">登记本机仓库…</span>
            </button>
          </template>
          <template v-else-if="landingMenu === 'branch'">
            <!-- 仓库维度不占 chip：这一行是「换仓库」，箭头说明它还有下一层（仓库列表）。 -->
            <button
              type="button"
              class="hcc-opt landing-opt"
              :title="landing.activeRepoPath || '选择本机 git 仓库'"
              @click="toggleLanding('repo')"
            >
              <span class="ol">{{ repoLabel }}</span>
              <span class="arrow">›</span>
            </button>
            <div class="hcc-rule"></div>
            <div v-if="landing.loading" class="hcc-sep">正在读取工作树…</div>
            <div v-if="landing.error" class="hcc-sep">{{ landing.error }}</div>
            <button
              v-for="w in landing.activeGroup?.items || []"
              :key="w.path"
              type="button"
              class="hcc-opt landing-opt"
              :class="{ current: landing.activeWorktree?.path === w.path }"
              :disabled="w.locked || w.prunable"
              :title="`${w.path}${w.main ? ' · 主仓' : ''}${w.locked ? ' · 已锁定' : ''}${w.prunable ? ' · 待清理' : ''}`"
              @click="chooseWorktreeChip(w)"
            >
              <span class="ol">{{ w.branch || (w.detached ? 'detached HEAD' : 'bare') }}</span>
              <span v-if="landing.activeWorktree?.path === w.path" class="tick">✓</span>
            </button>
            <div v-if="landing.activeGroup?.error" class="hcc-sep">{{ landing.activeGroup.error }}</div>
            <div v-if="!landing.activeGroup?.items?.length" class="hcc-sep">这个仓库还没有工作树</div>
            <div class="hcc-rule"></div>
            <!-- 新建工作树（参考 Trae）：自动建分支 + 仓库旁目录，创建后落点直接切过去。
                 输入框预填自动名（feat/ai-xxxx），Enter 创建，Esc 收起表单。 -->
            <div v-if="wtFormOpen" class="wt-form">
              <input
                ref="wtFormInputRef"
                v-model="wtBranchName"
                type="text"
                placeholder="feat/my-feature"
                aria-label="新分支名"
                spellcheck="false"
                @keydown.enter.prevent="submitWtForm"
                @keydown.esc.stop.prevent="closeWtForm"
              />
              <button
                type="button"
                class="wt-go"
                :disabled="landing.loading || !wtBranchName.trim()"
                title="创建分支与独立目录，并把落点切过去"
                @click="submitWtForm"
              >创建</button>
            </div>
            <div v-if="wtFormOpen" class="hcc-sep">目录将建在仓库旁 · 名 = 仓库名-分支名（/ 换 -）</div>
            <button
              v-else-if="landing.activeRepoPath"
              type="button"
              class="hcc-opt landing-opt"
              :disabled="landing.loading"
              title="自动建分支 + 独立目录（git worktree add），创建后落点直接切过去"
              @click="openWtForm"
            >
              <span class="ol">新建工作树…</span>
            </button>
            <button
              v-if="hasPrunable"
              type="button"
              class="hcc-opt landing-opt"
              :disabled="landing.loading"
              title="git worktree prune：清掉目录已被手动删除的失效记录，不动任何现存目录"
              @click="landing.pruneWorktrees(landing.activeRepoPath)"
            >
              <span class="ol">清理失效记录</span>
            </button>
            <button type="button" class="hcc-opt landing-opt" :disabled="landing.loading" title="目录选择器，登记后刷新列表" @click="landing.registerRepo()">
              <span class="ol">登记本机仓库…</span>
            </button>
            <button type="button" class="hcc-opt landing-opt" :disabled="landing.loading" @click="landing.loadWorktrees()">
              <span class="ol">刷新工作树</span>
            </button>
          </template>
        </div>
      </div>

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

      <!-- 专家 chip 紧跟目标 chip：两者都是「这一条消息落到哪里 / 以什么策略跑」，
           放在会话头部会谎称整段会话只有一个专家。 -->
      <span
        v-if="pickedExpert"
        class="hcc-chip"
        :title="`专家：${pickedExpert.name}（r${pickedExpert.revision}）· 只收紧不放宽`"
      >
        {{ pickedExpert.name }}
        <button
          type="button"
          class="hcc-chip-x"
          :aria-label="`移除专家 ${pickedExpert.name}`"
          @click="sshAi.setSelectedExpert('')"
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

  <!-- SSH 连接弹窗：复用终端页那份新增连接表单（SSH / TLS 双页签、密钥与密码认证
       都在里面），不另造一套字段。保存后由 onSshSaved 决定是否立刻连。 -->
  <QuickSshDialog
    v-if="showNewConn"
    @close="showNewConn = false"
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
  padding: var(--spacing-14) var(--spacing-16) var(--spacing-6);
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
  font-size: 1.1538rem;
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
  gap: var(--spacing-8);
  padding: var(--spacing-8) var(--spacing-10) 9px var(--spacing-12);
  /* 底栏那一道折痕与次级纸面都归主题：--card-foot-* 在非手记主题 = 今天的样子
     （透明底 + 实线分隔），组件里不写主题分支（同 --rule-line 的做法） */
  border-top: var(--card-foot-rule);
  background: var(--card-foot-bg);
  /* 底栏背景必须跟着卡片内圆角收口：手记主题的次级纸面是矩形，不裁就会把下面
     两角压成直角，与上沿圆角对不上。减 1px 是卡片自身边框的宽度。 */
  border-radius: 0 0 calc(var(--radius-lg) - 1px) calc(var(--radius-lg) - 1px);
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
  font-size: 0.9231rem;
  transition: background var(--transition-fast);
}
.hcc-btn b { font-weight: 500; color: var(--text-primary) }
.hcc-btn:hover:not(:disabled) { background: var(--bg-hover) }
.hcc-btn:disabled { opacity: .5; cursor: default }
.hcc-btn.empty b { color: var(--accent-orange) }
/* ＋ 是底栏唯一的上下文入口（故事 5），做成方角小方块让它一眼是"加"而不是一个词 */
.hcc-btn.plus { width: 26px; padding: 0; justify-content: center; font-size: 1.1538rem }
.hcc-divider {
  width: 1px;
  height: 14px;
  margin: 0 var(--spacing-2);
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
  gap: var(--spacing-4);
  height: 22px;
  padding: 0 var(--spacing-4) 0 var(--spacing-8);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--text-primary) 7%, transparent);
  color: var(--text-secondary);
  font-size: 0.8462rem;
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
  font-size: 0.9231rem;
  line-height: 1;
}
.hcc-chip-x:hover { background: var(--bg-hover); color: var(--text-primary) }
/* 审批档右边的目标标记（远程地址 / 本地 shell 名）。等宽字是「这是一个落点不是一个词」最快的信号；不铺底：
   有了底就会被读成旁边那排 chip 里的一员，而那些 chip 的 × 摘掉的是附加上下文，
   这枚 × 断开的是连接 —— 两者代价不同，不能共用一个外观语言。 */
/* 落点三选一：与审批档 / 模型共用 .hcc-btn 底座，不另造背景层级。
   选中只给强调字色 + alpha 描边（DESIGN.md 禁实心 CTA）。 */
.hcc-landing { display: inline-flex; align-items: center; gap: 4px }
.hcc-btn.landing { padding: 0 7px; font-size: 0.8462rem; color: var(--text-tertiary) }
/* 目录名可能很长（node_modules 之类）：chip 上截断，完整路径留在 title 里。
   两枚 chip 一起撑长会把模型 / 发送挤出去，这里必须给上限。 */
.hcc-btn.landing.dir-chip b { display: inline-block; max-width: 132px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom }
/* 选中只提亮字色 + alpha 描边：纯文字按钮读 --accent-text 会在浅色主题隐形，
   读 --accent-primary 又过不了 variables.test.ts 的对比度守卫。 */
.hcc-btn.landing.on { color: var(--text-primary); font-weight: 500; border: 1px solid rgba(var(--accent-primary-rgb), .4) }
.hcc-sel.landing-sel .hcc-menu.wide { width: 224px; max-height: 320px }
/* 落点 chip：图标 + 短名 + caret。常驻 1 枚（本地 / SSH），工作树态多一枚分支 ——
   「有几枚 chip」本身就是落点类型的读数，不必再另画一行说明。
   caret 是 8px chevron：开合只转 180 度，不再用文本箭头字符（字形随字体漂）。 */
.hcc-btn.landing .caret { color: var(--text-tertiary); margin-left: 1px; flex-shrink: 0; transition: transform .15s }
.hcc-btn.landing .caret.up { transform: rotate(180deg) }
.hcc-btn.landing svg { flex-shrink: 0; opacity: .85 }
/* 落点浮层是一个 select，不是菜单：每行一行字，右侧只放勾选 / 箭头。
   只剩目录面例外 —— 那一面是「项目选择器」（2026-11-11 参考实现）：搜索框 +
   文件夹图标 + 底部逃生口，所以图标只在这一面出现。 */
.hcc-opt.landing-opt {
  display: flex;
  align-items: center;
  gap: var(--spacing-8);
  min-height: 28px;
  padding: 0 var(--spacing-10);
  font-size: 0.9231rem;
}
.hcc-opt.landing-opt svg { flex-shrink: 0; color: var(--text-tertiary) }
.hcc-opt.landing-opt .ol { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap }
.hcc-opt.landing-opt.current { background: var(--bg-hover) }
/* 目录面的搜索框贴在浮层顶部：列表滚的时候它不动。浮层自带 4px 内边距，
   用负外边距把背景铺满到边，滚动内容才不会从缝里透出来。 */
.landing-menu .dir-search {
  position: sticky;
  top: calc(-1 * var(--spacing-4));
  z-index: 1;
  margin: calc(-1 * var(--spacing-4)) calc(-1 * var(--spacing-4)) 0;
  padding: 6px var(--spacing-10) 9px;
  background: var(--bg-elevated);
}
/* 目录记录行：整行是「切到这个目录」，右侧 × 只删这条记录 —— 两枚独立按钮，
   不把按钮套进按钮。× 平时不占视觉，hover / 键盘聚焦才浮出来。 */
.dir-row-wrap { position: relative; display: flex }
.dir-row { padding-right: 30px }
.dir-del {
  position: absolute;
  right: 6px;
  top: 50%;
  transform: translateY(-50%);
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  border-radius: var(--radius-sm);
  color: var(--text-tertiary);
  background: transparent;
  opacity: 0;
  transition: opacity var(--transition-fast), background var(--transition-fast), color var(--transition-fast);
}
.dir-row-wrap:hover .dir-del,
.dir-row-wrap:focus-within .dir-del { opacity: 1 }
.dir-del:hover { background: var(--bg-hover); color: var(--text-primary) }
/* 云端占位项：位置固定但功能未接，字色压一档 + 右侧「未接入」，
   点了给一句实话，不留哑按钮。 */
.hcc-opt.landing-opt.is-soon { color: var(--text-tertiary) }
.hcc-opt.landing-opt:disabled { opacity: .6; cursor: default }
.hcc-opt.landing-opt:disabled:hover { background: transparent }
.hcc-opt.landing-opt .tick { color: var(--accent-green); font-family: var(--font-mono); font-size: 0.9231rem }
.hcc-opt.landing-opt .count {
  margin-left: var(--spacing-6);
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  font-size: 0.7692rem;
}
/* 当前读数的次级文字：等宽小字，退到读数位而不是抢主标签的位置 */
.hcc-opt.landing-opt .meta {
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  font-size: 0.7692rem;
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 「还有一层」的箭头：分支面里的仓库行点进去才是仓库列表 */
.hcc-opt.landing-opt .arrow { color: var(--text-tertiary); font-size: 0.9231rem; line-height: 1 }
/* 新建工作树的行内表单：输入框压一道可聚焦的底线（与级联搜索同语言），
   创建钮走 hover 层，不引入新的强调色档。 */
.wt-form {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: var(--spacing-4) var(--spacing-8);
}
.wt-form input {
  flex: 1;
  min-width: 0;
  background: transparent;
  border: 0;
  outline: 0;
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-primary);
  font-family: var(--font-mono);
  font-size: 0.9231rem;
  padding: 2px 2px 4px;
}
.wt-form input:focus { border-bottom-color: rgba(var(--accent-primary-rgb), .55) }
.wt-form input::placeholder { color: var(--text-tertiary) }
.wt-go {
  height: 22px;
  padding: 0 var(--spacing-8);
  border-radius: var(--radius-sm);
  background: var(--bg-hover);
  color: var(--text-primary);
  font: inherit;
  font-size: 0.8462rem;
  cursor: pointer;
  flex-shrink: 0;
  transition: background var(--transition-fast);
}
.wt-go:hover:not(:disabled) { background: var(--bg-active) }
.wt-go:disabled { opacity: .5; cursor: default }
.hcc-rule { height: 1px; margin: 6px var(--spacing-8); background: var(--border-subtle) }
.hcc-target {
  display: inline-flex;
  align-items: center;
  gap: var(--spacing-4);
  font-family: var(--font-mono);
  font-size: 0.8462rem;
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
  padding: var(--spacing-4);
  background: var(--bg-elevated);
  /* 不画框：与卡面同色的一层浮起，靠 shadow 与卡分离（边框是"框"这件事本身） */
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  z-index: 30;
  /* 一级卡片静止不带动效（用户明确要求）：菜单本体开着就是开着，不淡入不位移。 */
}
.hcc-menu.wide { width: 220px; max-height: 300px; overflow-y: auto }
/* ＋ 卡片只有左栏这六项，尺寸固定（用户 2026-11-11 指定：就这么大、不要动）：
   尺寸不再随二级面板变化 —— 二级面板是贴在右侧的**独立浮层**，不是把这张卡片
   撑宽的第二个栏位。卡片宽 = 左栏 196 + 左右 padding 8 = 204，高由六项决定。 */
.hcc-menu.cascade {
  display: block;
  width: 204px;
  padding: var(--spacing-4);
  overflow: visible;
}
.hcc-cascade-left { width: 196px; overflow-y: auto }
/* 二级面板：与卡片同一水平线（底边对齐、朝上开），left = 卡片宽 204 + 6 间隙。
   自己是一张浮起的卡（同色 + shadow + 圆角），所以不再借左边框分栏。 */
.hcc-cascade-right {
  position: absolute;
  left: calc(204px + 6px);
  bottom: calc(100% + 6px);
  width: 288px;
  max-width: calc(100vw - 240px);
  max-height: 340px;
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: var(--spacing-4);
  background: var(--bg-elevated);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  z-index: 30;
  /* 动效只允许在二级面板（用户明确要求）：右栏容器带 :key，左栏切换层级 /
   * 菜单重开时重挂载，120ms 淡入只发生在这一层。 */
  animation: casc-in 0.12s ease-out;
}
@keyframes casc-in {
  from { opacity: 0; transform: translateY(2px) }
  to { opacity: 1; transform: translateY(0) }
}
.hcc-opt.casc-opt { display: flex; align-items: center; gap: var(--spacing-8) }
.hcc-opt.casc-opt svg { flex-shrink: 0; color: var(--text-secondary) }
.hcc-opt.casc-opt .ol { flex: 1; min-width: 0; display: flex; align-items: center; gap: 4px }
.hcc-opt.casc-opt.current { background: var(--bg-hover) }
.casc-arrow { margin-left: auto; color: var(--text-tertiary); font-size: 1rem }
.casc-search {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px var(--spacing-8) 9px;
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-tertiary);
  flex-shrink: 0;
  transition: border-color var(--transition-fast), color var(--transition-fast);
}
/* 聚焦提亮底线与图标：border-color 用 accent 的 alpha 描边（不是文字色，
   variables.test.ts 的对比度守卫只拦 color: var(--accent-primary)）。 */
.casc-search:focus-within {
  border-bottom-color: rgba(var(--accent-primary-rgb), .55);
  color: var(--text-secondary);
}
.casc-search input {
  flex: 1;
  min-width: 0;
  background: transparent;
  border: 0;
  outline: 0;
  color: var(--text-primary);
  font: inherit;
  font-size: 0.9231rem;
}
.casc-search input::placeholder { color: var(--text-tertiary) }
.casc-list { flex: 1; min-height: 0; overflow-y: auto; padding-top: var(--spacing-4) }
.casc-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  width: 100%;
  text-align: left;
  padding: 6px var(--spacing-8);
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  font: inherit;
  cursor: pointer;
  transition: background var(--transition-fast);
}
.casc-item:hover:not(.ro) { background: var(--bg-hover) }
.casc-item.on { background: var(--bg-hover) }
/* 插件清单是只读行，不给 hover 反馈，免得看起来像能点开授权 */
.casc-item.ro, .casc-item.ro:hover { cursor: default; background: transparent }
.casc-item svg { flex-shrink: 0; margin-top: 2px; color: var(--text-secondary) }
.ci-body { min-width: 0; display: flex; flex-direction: column; gap: 1px }
.ci-name {
  font-size: 0.9231rem;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 二级列表是浏览/搜索场景：描述是辨识信息，按手记截图保留一行；
   一级菜单那五层仍然不铺小字，说明只走 title。 */
.ci-desc {
  font-size: 0.7692rem;
  color: var(--text-tertiary);
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 空态居中排一行：它下面通常还跟着一条出口按钮（去启用 / 管理插件），
   两行并排读起来是「状态 + 怎么办」，左对齐时这对关系会散掉。 */
.casc-empty {
  padding: 14px var(--spacing-8);
  font-size: 0.8462rem;
  color: var(--text-tertiary);
  text-align: center;
  line-height: 1.5;
}
/* 菜单里的小节标题（技能区）。刻意不是分隔线：手记那套语言靠字重与留白分组，
   再加一条线会和输入卡的行线抢注意力。 */
.hcc-sep {
  padding: 7px var(--spacing-8) 3px;
  font-size: 0.7692rem;
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
.fold-caret { transition: transform .15s; font-size: 0.8462rem }
.fold-caret.open { transform: rotate(90deg) }
.fold-n { margin-left: auto; letter-spacing: 0; color: var(--text-secondary) }
.hcc-opt {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 1px;
  width: 100%;
  text-align: left;
  padding: var(--spacing-6) var(--spacing-8);
  border-radius: var(--radius-sm);
  transition: background var(--transition-fast);
}
.hcc-opt:hover:not(:disabled) { background: var(--bg-hover) }
/* 插件清单是只读行，不是按钮：不给 hover 反馈，免得看起来像可以点开授权 */
.hcc-opt.ro, .hcc-opt.ro:hover { background: transparent; cursor: default }
/* 上传中：菜单项禁用要有可见差别，否则用户以为点了没反应，再点一次 */
.hcc-opt:disabled { opacity: .55; cursor: default }
.hcc-opt .ol { font-size: 0.9231rem; color: var(--text-primary) }
/* 菜单项只留一行：原来的第二行灰字（.od）一律改成按钮的 title，hover 才浮出提示。
   原生 title 不会被 .hcc-menu 的 overflow-y:auto 裁掉，自绘 tooltip 会。 */
/* 技能主项与其余四项同层级，条数 / caret 靠右 */
.hcc-opt.fold-toggle { display: flex; align-items: center; gap: var(--spacing-6) }
.hcc-opt.fold-toggle .ol { display: inline-flex; align-items: center; gap: 5px }
.hcc-opt.fold-toggle .fold-n {
  color: var(--text-tertiary);
  font-size: 0.8462rem;
  font-family: var(--font-mono);
}
/* 对勾跟在名字后面：它先前独占网格第二列、垂直跨两行，隔着整行宽度钉在最右，
   读起来像另一列数据而不像"这一条被选了" */
.hcc-opt .ot {
  margin-left: var(--spacing-6);
  font-family: var(--font-mono);
  font-size: 0.8462rem;
  color: var(--accent-green);
}
.hcc-note { font-size: 0.8462rem; color: var(--text-tertiary) }
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
  border-radius: var(--radius-sm);
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
