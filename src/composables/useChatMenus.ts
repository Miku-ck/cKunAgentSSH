/**
 * 输入区三浮层的状态收编（ARCH-009）。
 *
 * SideChatPanel 的三套弹层（+ 姿态菜单 / / 技能菜单 / 审批下拉）原各持一份
 * 「状态 + 过滤/索引 + document 外点关闭监听」的手抄实现。抽成 composables 后：
 * - 每套菜单的状态与行为自含（含各自的 mousedown 外点守卫）；
 * - 主组件保留编排：互斥 watch、输入框的集中式键盘分发、发送装配 —— 模板与
 *   样式零改动（键盘事件落在主组件的 textarea 上、触发按钮也在主组件工具条，
 *   因此抽子组件会跟焦点拓扑打架，composable 才是本项目已有拆分习惯的正解）。
 *
 * 语义保持原样的三处守卫口径：
 * - 技能菜单外点：命中 .skill-menu 或输入框内不关；
 * - + 菜单外点：命中 .plus-wrap（含触发按钮）不关；
 * - 审批菜单外点：命中 .perm-wrap 不关。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, type Ref } from 'vue'
import { APPROVAL_OPTIONS as PERM_OPTIONS, type ApprovalKey } from '@/utils/run-modes'

/** 技能菜单需要的最小形状（store 的 Skill 结构兼容，字段只窄不限） */
export interface SkillMenuSkill {
  id: string
  name: string
  slug: string
  description?: string
  category?: string
}

/** 三套浮层共用的「点击外部关闭」mousedown 监听（原三份手抄的注册/注销对） */
function useDismissOnOutsideClick(opts: {
  isOpen: () => boolean
  /** 返回 true = 点击目标在浮层/触发器内，不关闭 */
  shouldIgnore: (target: HTMLElement | null) => boolean
  close: () => void
}): void {
  function onDocMouseDown(e: MouseEvent) {
    if (!opts.isOpen()) return
    if (opts.shouldIgnore(e.target as HTMLElement | null)) return
    opts.close()
  }
  onMounted(() => document.addEventListener('mousedown', onDocMouseDown))
  onUnmounted(() => document.removeEventListener('mousedown', onDocMouseDown))
}

// ── / 斜杠技能菜单 ──────────────────────────────────────────────
/**
 * 触发双通道：「/」检测输入框斜杠（query 来自光标前文本，选中后抹除 "/query"）
 * 与 + 菜单「技能」项直开（不经文本，skillTriggerPos = -1 → pickSkill 跳过抹除）。
 * 选中的技能写入选 store 由父层负责（selectedSkillIds 单一数据源在 ssh-ai store，
 * 首页 + 菜单与这里必须同一份）。
 */
export function useSkillMenu(opts: {
  inputRef: Ref<HTMLTextAreaElement | null>
  inputText: Ref<string>
  /** 已启用技能清单（响应式来源，如 () => skillStore.enabledSkills） */
  enabledSkills: () => SkillMenuSkill[]
  /** 选中技能写入 store（父层闭包，保持 skillIds 单一数据源） */
  addSelectedSkill: (id: string) => void
  /** 点选后父层收尾（textarea 高度重算） */
  afterPick: () => void
}) {
  const showSkillMenu = ref(false)
  const skillQuery = ref('')
  const skillActiveIdx = ref(0)
  /** 触发菜单的 '/' 在输入框中的下标（选中技能后要抹掉整段 "/query"） */
  let skillTriggerPos = -1

  /** 根据查询串过滤技能 */
  const filteredSkills = computed(() => {
    const q = skillQuery.value.toLowerCase().trim()
    const list = opts.enabledSkills()
    if (!q) return list.slice(0, 8)
    return list
      .filter(s =>
        s.name.toLowerCase().includes(q) ||
        s.slug.toLowerCase().includes(q) ||
        (s.description || '').toLowerCase().includes(q),
      )
      .slice(0, 8)
  })

  /** aria-activedescendant 目标 id（与 + 菜单同一无障碍模式） */
  const activeSkillOptionId = computed(() => {
    const item = filteredSkills.value[skillActiveIdx.value]
    return item ? `skill-opt-${item.id}` : undefined
  })

  /** 检测 / 触发技能菜单（onInput 时调用） */
  function detectSkillTrigger() {
    const el = opts.inputRef.value
    if (!el) return
    const pos = el.selectionStart
    const text = opts.inputText.value.slice(0, pos)
    const lastSlash = text.lastIndexOf('/')
    if (lastSlash === -1) { closeSkillMenu(); return }

    // 触发符前必须是空白或行首（避免路径、URL 误触发）
    if (lastSlash > 0) {
      const charBefore = text[lastSlash - 1]
      if (charBefore && !/\s/.test(charBefore)) { closeSkillMenu(); return }
    }

    // / 后到光标位置之间不能有空白
    const segment = text.slice(lastSlash + 1, pos)
    if (/\s/.test(segment)) { closeSkillMenu(); return }

    skillQuery.value = segment
    skillActiveIdx.value = 0
    skillTriggerPos = lastSlash
    showSkillMenu.value = true
  }

  function closeSkillMenu() {
    showSkillMenu.value = false
    skillQuery.value = ''
    skillActiveIdx.value = 0
    skillTriggerPos = -1
  }

  /**
   * 从 + 菜单打开技能清单：与「/」触发复用同一份 filteredSkills/键盘导航，
   * 区别只是不经输入框文本 —— 故触发位置置 -1，pickSkill 会跳过「抹除 /query」
   * 那一步（它已按 skillTriggerPos >= 0 守卫），点选后焦点回到输入框。
   */
  function openSkillMenuFromPlus() {
    skillQuery.value = ''
    skillActiveIdx.value = 0
    skillTriggerPos = -1
    showSkillMenu.value = true
  }

  /** 选中技能：加入 selectedSkillIds（输入框上方以标签展示）。
   *  C-review NIT：原实现同时在输入框插入 "@技能名" 文本 — 文本与 skillIds
   *  双重注入可漂移（手动删文本技能仍附加 / 删标签文本残留在消息里）。
   *  现以 skillIds 为单一数据源，标签提供视觉反馈。 */
  function pickSkill(skill: { id: string; name: string }) {
    const el = opts.inputRef.value
    // 抹掉唤起菜单的 "/query" 片段：斜杠只是触发符，留在输入框里会被当成正文发出去。
    // （早先的实现用 "@技能名" 文本替换它，改为 skillIds 单一数据源时把替换一起删了）
    let caretPos = -1
    if (skillTriggerPos >= 0) {
      const end = el?.selectionStart ?? opts.inputText.value.length
      opts.inputText.value = opts.inputText.value.slice(0, skillTriggerPos) + opts.inputText.value.slice(end)
      caretPos = skillTriggerPos
    }
    // 语义保持原样：斜杠菜单点已选中的技能是"不加"，不是取消（取消走标签的 ×）
    opts.addSelectedSkill(skill.id)
    closeSkillMenu()
    requestAnimationFrame(() => {
      if (el && caretPos >= 0) {
        el.focus()
        el.setSelectionRange(caretPos, caretPos)
      } else {
        opts.inputRef.value?.focus()
      }
      opts.afterPick()
    })
  }

  // 审查修复（P2）：点击菜单外区域关闭技能菜单 — 原实现只有 Esc/选中/失焦
  // 能关，鼠标点输入区外（如消息列表）菜单残留，视觉像死菜单
  useDismissOnOutsideClick({
    isOpen: () => showSkillMenu.value,
    // 菜单自身或输入框内的点击不关闭（菜单项点击走 pickSkill 自行关闭）
    shouldIgnore: t => !!(t && t.closest?.('.skill-menu')) || !!opts.inputRef.value?.contains(t),
    close: closeSkillMenu,
  })

  return {
    showSkillMenu,
    skillActiveIdx,
    filteredSkills,
    activeSkillOptionId,
    detectSkillTrigger,
    closeSkillMenu,
    openSkillMenuFromPlus,
    pickSkill,
  }
}

// ── + 号姿态菜单（目标 / 计划 / 技能）────────────────────────────
/**
 * 键盘导航顺序必须与 DOM 视觉顺序一致，否则 ↑↓ 会「跳着走」。行上的高亮/悬停
 * 一律走 plusIdx(key) 取，不写死数字：写过一次死索引，DOM 里删了一行而数组没动，
 * aria-activedescendant 就指到一个不存在的元素上。
 * 姿态与开关本身（activeMode / toggleGoalMode / togglePlanMode）留在父层 ——
 * 它们同时驱动工具条 chip 与发送装配，菜单只是入口之一。
 */
export type CapKey = 'goal' | 'plan' | 'skill'
const CAP_KEYS: CapKey[] = ['goal', 'plan', 'skill']

export function usePlusMenu(opts: {
  agentRunning: () => boolean
  inputRef: Ref<HTMLTextAreaElement | null>
  toggleGoalMode: () => void
  togglePlanMode: () => void
  openSkillMenuFromPlus: () => void
}) {
  const showPlusMenu = ref(false)
  const plusActiveIdx = ref(0)

  const plusIdx = (key: CapKey) => CAP_KEYS.indexOf(key)

  /** aria-activedescendant 目标 id（与 / 技能菜单同一无障碍模式） */
  const plusActiveOptionId = computed(() => {
    const key = CAP_KEYS[plusActiveIdx.value]
    return key ? `plus-opt-${key}` : undefined
  })

  function stepPlusMenu(dir: 1 | -1) {
    const n = CAP_KEYS.length
    plusActiveIdx.value = (plusActiveIdx.value + dir + n) % n
  }

  function togglePlusMenu() {
    if (showPlusMenu.value) {
      closePlusMenu()
      return
    }
    if (opts.agentRunning()) return
    showPlusMenu.value = true
    plusActiveIdx.value = 0
    // 焦点留在输入框：键盘导航由父层 handleKeydown 统一处理（同 / 技能菜单的手感）
    nextTick(() => opts.inputRef.value?.focus())
  }

  function closePlusMenu(refocus = false) {
    if (!showPlusMenu.value) return
    showPlusMenu.value = false
    plusActiveIdx.value = 0
    if (refocus) nextTick(() => opts.inputRef.value?.focus())
  }

  /** 菜单项选择：目标 → 姿态开关（点亮即注入目标姿态，再点回到未表态）；
   *  计划 → 权限模式开关（再点一次退出，回到未表态）；
   *  技能 → 打开既有技能清单（与「/」触发同源），选中的以 skill-tags 呈现。 */
  function pickCap(key: CapKey) {
    // 目标：纵向姿态开关，随下一次发送传给后端作为姿态提示；点亮态同时驱动工具条 chip
    if (key === 'goal') {
      opts.toggleGoalMode()
      closePlusMenu(true)
      return
    }
    // 计划：不是「选一个姿态」而是开关 —— 点亮即只读强制 + 计划提示词成对下发，
    // 再点退出（回到未表态，不是目标姿态）。它是独立于审批下拉的另一条轴（plan 姿态下下拉休眠）。
    if (key === 'plan') {
      opts.togglePlanMode()
      closePlusMenu(true)
      return
    }
    // 技能：打开既有技能清单（与「/」触发同一条路径），选中的以 skill-tags 呈现
    if (key === 'skill') {
      closePlusMenu(true)
      opts.openSkillMenuFromPlus()
      return
    }
  }

  /** 键盘 Enter/Tab 的执行体：主组件 handleKeydown 在 preventDefault 后调用 */
  function pickActiveCap() {
    const key = CAP_KEYS[plusActiveIdx.value]
    if (key) pickCap(key)
  }

  // 点菜单/按钮以外区域关闭。.plus-wrap 内含触发按钮与菜单，按钮自行处理开合，
  // 故容器内的 mousedown 不在此关闭。
  useDismissOnOutsideClick({
    isOpen: () => showPlusMenu.value,
    shouldIgnore: t => !!(t && t.closest?.('.plus-wrap')),
    close: () => closePlusMenu(),
  })

  return {
    showPlusMenu,
    plusActiveIdx,
    plusActiveOptionId,
    plusIdx,
    stepPlusMenu,
    togglePlusMenu,
    closePlusMenu,
    pickCap,
    pickActiveCap,
  }
}

// ── 审批档下拉（normal 手动 / auto 自动；plan 姿态下休眠）────────────
/**
 * 菜单项（role=option）键盘导航直接作用于真实 DOM，不另存一份索引状态，
 * 避免索引与 DOM 顺序（未来增删选项时）静默错位。
 * 审批档真值住 ssh-ai store（首页输入卡与这里共享同一档），父层经 onPick 写入。
 */
export function usePermMenu(opts: {
  agentRunning: () => boolean
  isPlanMode: () => boolean
  approvalMode: () => ApprovalKey
  permMenuRef: Ref<HTMLElement | null>
  permTriggerRef: Ref<HTMLButtonElement | null>
  /** 打开时关闭其他浮层（父层编排：与 + 菜单 / 技能菜单互斥） */
  closeOthers: () => void
  /** 审批档写入（父层闭包写 store；纯审批轴，不触碰姿态） */
  onPick: (key: ApprovalKey) => void
}) {
  const showPermMenu = ref(false)

  function permOptionEls(): HTMLElement[] {
    const root = opts.permMenuRef.value
    if (!root) return []
    return Array.from(root.querySelectorAll<HTMLElement>('[role="option"]'))
  }

  function openPermMenu() {
    if (opts.agentRunning() || opts.isPlanMode()) return
    opts.closeOthers()
    showPermMenu.value = true
    // 焦点移进菜单（button + role=listbox 的标准做法）：不这么做 ↑↓ 没有落点，
    // 而输入框的 handleKeydown 不接管这个菜单。
    nextTick(() => {
      const els = permOptionEls()
      const idx = PERM_OPTIONS.findIndex(o => o.key === opts.approvalMode())
      els[idx >= 0 ? idx : 0]?.focus()
    })
  }

  function closePermMenu(refocus = false) {
    if (!showPermMenu.value) return
    showPermMenu.value = false
    if (refocus) nextTick(() => opts.permTriggerRef.value?.focus())
  }

  function togglePermMenu() {
    if (showPermMenu.value) closePermMenu(true)
    else openPermMenu()
  }

  /** 审批档选择（纯审批轴）：不再触碰姿态 —— plan 的开关只在 + 菜单，
   *  两轴交汇发生在 resolveRunModes（plan 姿态下审批档休眠）。 */
  function pickPerm(key: ApprovalKey) {
    opts.onPick(key)
    closePermMenu(true)
  }

  /** Enter/Space 由按钮自身的 click 处理，这里只管方向键与退出 */
  function handlePermMenuKeydown(e: KeyboardEvent) {
    const els = permOptionEls()
    if (els.length === 0) return
    const cur = els.indexOf(document.activeElement as HTMLElement)
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      els[(cur + 1 + els.length) % els.length]?.focus()
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      els[(cur - 1 + els.length) % els.length]?.focus()
      return
    }
    if (e.key === 'Home') {
      e.preventDefault()
      els[0]?.focus()
      return
    }
    if (e.key === 'End') {
      e.preventDefault()
      els[els.length - 1]?.focus()
      return
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      closePermMenu(true)
      return
    }
    // Tab 移出即关（焦点交给下一个控件），不抢它的默认行为
    if (e.key === 'Tab') closePermMenu()
  }

  // 点 .perm-wrap 以外区域关闭（同 .skill-menu / .plus-menu 的外点策略）
  useDismissOnOutsideClick({
    isOpen: () => showPermMenu.value,
    shouldIgnore: t => !!(t && t.closest?.('.perm-wrap')),
    close: () => closePermMenu(),
  })

  return {
    showPermMenu,
    openPermMenu,
    closePermMenu,
    togglePermMenu,
    pickPerm,
    handlePermMenuKeydown,
  }
}
