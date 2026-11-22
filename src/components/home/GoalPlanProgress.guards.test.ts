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
    const panelAt = workspace.indexOf('<GoalPlanProgress />')
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

  it('进度条与步骤列表消费 store 投影，不自行计算业务状态', () => {
    expect(panel).toContain('gp.snapshot.progress.completed')
    expect(panel).toContain('v-for="s in gp.snapshot.steps"')
    // 状态徽标文案是本地映射表（展示层职责），不引入第二条状态判定通路
    expect(panel).toContain('GOAL_LABELS')
    expect(panel).toContain('PLAN_LABELS')
  })
})
