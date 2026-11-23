/**
 * GoalCreateDialog 形状守卫（无组件测试环境，?raw 静态契约）。
 *
 * 钉住三件事：
 * 1. 复用 BaseModal 居中弹窗（DESIGN.md 禁抽屉）；
 * 2. 字段与后端 GoalCreateRequest 对齐，criteria 每行一条、最多 10 条；
 * 3. 提交走 store.createGoal（内部补 conversation_id），异常有展示不静默失败。
 */
import { describe, expect, it } from 'vitest'
import rawDialog from './GoalCreateDialog.vue?raw'
import rawStore from '@/stores/goal-plan.ts?raw'

const dialog = rawDialog.replace(/\r\n/g, '\n')
const store = rawStore.replace(/\r\n/g, '\n')

describe('GoalCreateDialog 空态新建', () => {
  it('复用 BaseModal，不另造弹窗骨架', () => {
    expect(dialog).toContain('<BaseModal')
    expect(dialog).toContain('@close="emit(\'close\')"')
    expect(dialog).toContain('#actions')
  })

  it('字段与后端契约对齐：陈述必填、criteria 逐行且截断 10 条', () => {
    expect(dialog).toContain('v-model="statement"')
    expect(dialog).toContain('maxlength="2000"')
    expect(dialog).toContain('canSubmit = computed(() => statement.value.trim().length > 0)')
    expect(dialog).toContain("criteriaText.value.split('\\n')")
    expect(dialog).toContain('slice(0, 10)')
    expect(dialog).toContain('v-model="context"')
  })

  it('提交走 store.createGoal（store 内补会话 id），错误有展示', () => {
    expect(dialog).toContain('goalPlan.createGoal({')
    expect(dialog).toContain('error.value = e instanceof Error')
    expect(dialog).toContain('gc-error')
    // store 层：conversation_id 服务端会话 id，不信任调用方传值
    expect(store).toContain('function createGoal(payload:')
    expect(store).toContain('conversation_id: conversationId.value')
    expect(store).toContain("useToastStore().info('请先进入一个会话")
  })
})
