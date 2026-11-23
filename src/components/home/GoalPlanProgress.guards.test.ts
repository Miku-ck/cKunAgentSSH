/**
 * GoalPlanProgress 挂载边界守卫（?raw 静态契约，仓库先例模式 —— 无组件测试环境）。
 *
 * 钉住三件事：
 * 1. 整卡 v-if="gp.hasContent"：无 goal 无 plan 时不渲染（挂载边界）；
 * 2. 面板挂在 HomeWorkspace 的 composer-wrap、ApprovalDock 与 HomeComposer 之间
 *    （不进 home-messages 消息流 —— 结构化状态不属于消息）；
 * 3. 三个用户动作都经 useConfirm 二次确认，批准只对 awaiting_approval 露出。
 */
import { describe, expect, it } from 'vitest'
import rawPanel from './GoalPlanProgress.vue?raw'
import rawWorkspace from '@/components/home/HomeWorkspace.vue?raw'

const panel = rawPanel.replace(/\r\n/g, '\n')
const workspace = rawWorkspace.replace(/\r\n/g, '\n')

describe('GoalPlanProgress 挂载边界', () => {
  it('整卡 v-if="gp.hasContent"：无内容不渲染', () => {
    expect(panel).toContain('v-if="gp.hasContent"')
    expect(panel).toContain('data-testid="goal-plan-progress"')
  })

  it('面板位于 composer-wrap 内、不进 home-messages', () => {
    const wrapStart = workspace.indexOf('class="composer-wrap"')
    const panelAt = workspace.indexOf('<GoalPlanProgress')
    const composerAt = workspace.indexOf('<HomeComposer')
    const messagesAt = workspace.indexOf('class="home-messages"')
    expect(panelAt).toBeGreaterThan(wrapStart)
    // 面板不得出现在 home-messages 消息流内
    expect(panelAt).toBeGreaterThan(messagesAt)
    expect(panelAt).toBeLessThan(composerAt)
  })

  it('三个动作都经 useConfirm 二次确认；批准只对 awaiting_approval 露出', () => {
    expect(panel).toContain("const { confirm } = useConfirm()")
    expect(panel).toContain('await confirm({')
    // 批准入口门控在 awaitingApproval 上；重置/放弃门控在 planActionable 上
    expect(panel).toContain('v-if="gp.awaitingApproval"')
    expect(panel).toContain('v-if="gp.planActionable && !gp.awaitingApproval"')
    // 放弃是危险色
    expect(panel).toContain("danger: true")
  })

  it('＋ 菜单聚焦：Goal 空态开新建弹窗，Plan 空态用用户语言引导（不暴露工具名）', () => {
    expect(panel).toContain('defineExpose({ focusSection })')
    expect(panel).toContain('function focusSection(stage:')
    // Goal 空态 = 当场建目标（用户 2026-11-23 指定的「点有所用」）
    expect(panel).toContain("if (stage === 'goal')")
    expect(panel).toContain('showCreate.value = true')
    expect(panel).toContain('GoalCreateDialog')
    // Plan 空态 = 用户语言引导；红线：不向用户暴露模型侧工具名
    expect(panel).toContain('助手会先给出步骤清单')
    expect(panel).not.toMatch(/plan_propose|goal_propose/)
    // 聚焦卡片由 ref + 高亮态驱动，高亮自动退
    expect(panel).toContain('ref="goalRef"')
    expect(panel).toContain('ref="planRef"')
    expect(panel).toContain("{ 'gp-focus': focused === 'goal' }")
    expect(panel).toContain("{ 'gp-focus': focused === 'plan' }")
    expect(panel).toContain('scrollIntoView')
    expect(panel).toContain('setTimeout')
  })

  it('目标裁决与修订按钮经二次确认，且走乐观锁与 achieved 语义', () => {
    expect(panel).toContain('>确认达成</button>')
    expect(panel).toContain('>修订标准</button>')
    expect(panel).toContain('onConfirmAchieved')
    expect(panel).toContain("adjudication: 'user_confirmed', achieved: true")
    expect(panel).toContain('expected_version: goal.version')
    // 达成由用户裁决，与 criteria 逐条自检解耦（不得用 allCriteriaMet 阻断；
    // 只匹配实际调用，别被注释里提到的同名符号误伤）
    expect(panel).not.toMatch(/\ballCriteriaMet\s*\(/)
    // 修订表单保留用户原文，criteria 一行一条
    expect(panel).toContain('v-model="editStatement"')
    expect(panel).toContain("editCriteria.value.split('\\n')")
  })

  it('进度条与步骤列表消费 store 投影，不自行计算业务状态', () => {
    expect(panel).toContain('gp.snapshot.progress.completed')
    expect(panel).toContain('v-for="s in gp.snapshot.steps"')
    // 状态徽标文案是本地映射表（展示层职责），不引入第二条状态判定通路
    expect(panel).toContain('GOAL_LABELS')
    expect(panel).toContain('PLAN_LABELS')
  })
})
