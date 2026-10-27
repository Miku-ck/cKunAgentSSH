/**
 * RunbookPanel 守卫 —— 没有组件测试环境（未装 @vue/test-utils/jsdom，vitest 为 node
 * 环境），沿用本仓既有的「代码形状」守卫法（先例 RemoteAudit.guards.test.ts /
 * DockerView.guards.test.ts）：?raw 读源码钉契约。
 *
 * 钉的是四类契约的**存在性与次序**（后端校验在同型测试里钉值域，这里钉前端必须有的那道）：
 * 1. 步骤编辑校验（command 必填 / 长度 / ≤20 步）且校验非空时禁用保存；
 * 2. 执行中按钮禁用 + runNow 3s 短轮询与激活期 30s 轮询的启停与卸载清理；
 * 3. 步骤状态徽章四态（success/failed/blocked/skipped）渲染；
 * 4. 历史 modal 挂 diagnosis 字段（自动诊断结论可见）。
 */
import panelRaw from './RunbookPanel.vue?raw'
import { describe, expect, it } from 'vitest'

const src = panelRaw.replace(/\r\n/g, '\n')
const style = src.slice(src.indexOf('<style scoped>'))

function sliceFunction(header: string): string {
  const start = src.indexOf(header)
  expect(start, `找不到 ${header}`).toBeGreaterThanOrEqual(0)
  const end = src.indexOf('\n}\n', start)
  expect(end).toBeGreaterThan(start)
  return src.slice(start, end)
}

describe('步骤编辑校验', () => {
  const stepError = sliceFunction('function stepError(')

  it('command 必填：空白命令报「不能为空」', () => {
    expect(stepError).toContain("(s.command || '').trim()")
    expect(stepError).toContain('不能为空')
  })

  it('长度上限与后端同口径：命令/验证命令 2000、说明 500', () => {
    expect(src).toContain('const COMMAND_MAX = 2000')
    expect(src).toContain('const NOTE_MAX = 500')
    expect(stepError).toContain('COMMAND_MAX')
    expect(stepError).toContain('NOTE_MAX')
  })

  it('步骤数上限 20：添加按钮与 formError 双重闸', () => {
    expect(src).toContain('const MAX_STEPS = 20')
    const formError = sliceFunction('const formError = computed(')
    expect(formError).toContain('form.steps.length === 0')
    expect(formError).toContain('form.steps.length > MAX_STEPS')
    expect(src).toContain('form.steps.length >= MAX_STEPS') // addStep 守卫
  })

  it('校验未过禁用保存：save 直接 return，按钮 :disabled 绑定 formError', () => {
    const save = sliceFunction('async function save(')
    expect(save.indexOf('if (formError.value) return'))
      .toBeGreaterThan(-1)
    expect(save.indexOf('if (formError.value) return'))
      .toBeLessThan(save.indexOf('await runbookApi.create'))
    expect(src).toContain(':disabled="saving || !!formError"')
  })
})

describe('执行中禁用与轮询启停', () => {
  it('isRunning 判据是 last_status === running，执行/编辑按钮据此禁用', () => {
    expect(src).toContain("r.last_status === 'running'")
    expect(src).toContain(':disabled="isRunning(r)" @click="runNow(r)"')
    expect(src).toContain(':disabled="isRunning(r)" @click="openEdit(r)"')
  })

  it('runNow 后 3s 短轮询，离开 running 即停（防 setInterval 泄漏）', () => {
    expect(src).toContain('const RUN_POLL_MS = 3000')
    const runNow = sliceFunction('async function runNow(')
    expect(runNow).toContain('setInterval')
    expect(runNow).toContain('RUN_POLL_MAX')
    expect(runNow).toContain('stopPoll(t)')
  })

  it('激活期 30s 静默轮询：active 开、切走停', () => {
    expect(src).toContain('const LIST_POLL_MS = 30000')
    expect(src).toContain('startListPoll()')
    expect(src).toContain('stopListPoll()')
    const watch = sliceFunction('watch(() => props.active,')
    expect(watch.indexOf('startListPoll()')).toBeGreaterThan(-1)
    expect(watch.indexOf('stopListPoll()')).toBeGreaterThan(-1)
  })

  it('卸载清理全部定时器（列表轮询 + runNow 轮询集合）', () => {
    const cleanup = sliceFunction('onUnmounted(')
    expect(cleanup).toContain('stopListPoll()')
    expect(cleanup).toContain('for (const t of runPollTimers) clearInterval(t)')
    expect(cleanup).toContain('runPollTimers.clear()')
  })
})

describe('步骤状态徽章渲染', () => {
  it('四态 + 待执行齐全，且徽章 class 绑定状态值', () => {
    const labels = sliceFunction('const STEP_STATUS_LABEL')
    for (const s of ['success', 'failed', 'blocked', 'skipped', 'pending']) {
      expect(labels).toContain(`${s}:`)
    }
    expect(src).toContain(':class="st.status"')
    // 样式为四态各配了色
    expect(src).toContain('.step-badge.success')
    expect(src).toContain('.step-badge.failed')
    expect(src).toContain('.step-badge.blocked')
    expect(src).toContain('.step-badge.skipped')
  })
})

describe('历史 modal 挂 diagnosis', () => {
  it('自动诊断结论经 #extra slot 渲染（失败执行的落点）', () => {
    expect(src).toContain('#extra="{ row }"')
    expect(src).toContain('diagnosisOf(row.id)')
    expect(src).toContain('AI 诊断')
  })
})

describe('历史接线 HistoryList', () => {
  it('列表主体经 HistoryList + toRunbookRow，旧内联列表已移除', () => {
    expect(src).toContain('<HistoryList')
    expect(src).toContain('toRunbookRow')
    expect(src).not.toContain('class="history-list"')
  })
  it('步骤徽章仍在 #extra 内渲染（四态不因重构丢失）', () => {
    expect(src).toContain('hist-steps')
    expect(src).toContain('stepStatusLabel(st.status)')
  })
  it('请求失败渲染错误条 + 重试', () => {
    expect(src).toContain('historyError')
    expect(src).toContain('fetchHistory()')
  })
})

describe('弹窗承载 BaseModal', () => {
  it('历史/编辑弹窗都走 BaseModal', () => {
    expect(src.match(/<BaseModal/g)).toHaveLength(2)
    expect(src).toContain("@close=\"historyFor = null\"")
    expect(src).toContain("@close=\"editing = null\"")
  })
  it('旧自绘遮罩/TELEPORT 已移除', () => {
    expect(src).not.toContain('.modal-mask')
    expect(src).not.toContain('<Teleport')
    expect(src).not.toContain('rgba(0,0,0,0.6)')
    expect(src).not.toContain('border-radius: 10px')
  })
  it('Esc 交给 BaseModal，面板不再重复监听', () => {
    expect(src).not.toContain('handleKeydown')
  })
})

describe('圆角令牌', () => {
  it('样式块内不用字面 px 圆角（手记主题会把 8px 改小，字面值破相）', () => {
    expect(style).not.toMatch(/border-radius:\s*\d+px/)
  })
})

describe('清空历史', () => {
  it('弹窗底部有清空历史按钮，进行中禁用', () => {
    expect(src).toContain('clearHistory()')
    expect(src).toContain(':disabled="historyHasRunning"')
  })
  it('清空走危险确认 + clearRuns + 成功后刷新', () => {
    expect(src).toContain('runbookApi.clearRuns')
    expect(src).toContain('danger: true')
    // 清空后靠 fetchHistory 回到空态
    expect(src).toMatch(/clearRuns[\s\S]*?await fetchHistory\(\)/)
  })
})

describe('入口挂载', () => {
  it('SchedulesView 以分段 tab 挂载本面板（C 的唯一既有文件改动）', async () => {
    // 守卫 SchedulesView.vue 的接线：tab 状态、RunbookPanel 只在 runbooks 段挂载
    // —— 漏任一处表现为按钮不亮/面板不加载且类型检查可过（useSshNavigation 同款教训）
    const mod = await import('@/views/ssh/SchedulesView.vue?raw')
    const view = String(mod.default).replace(/\r\n/g, '\n')
    expect(view).toContain("entryTab === 'runbooks'")
    expect(view).toContain('<RunbookPanel')
    expect(view).toContain(':active="entryTab === \'runbooks\'"')
  })
})
