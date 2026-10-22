/**
 * 审批与轨迹的边界守卫
 *
 * 审批 = 运行期的瞬时决策（控制面），只活在输入区上方的 ApprovalDock；
 * 执行过程 = DB 权威记录（记录面），只活在运维轨迹页。
 * 两边各抄一份渲染，就会出现「消息里有过程、轨迹页里也有过程」或
 * 「审批卡跟着历史消息滚走」这类边界塌陷。
 */
import { describe, expect, it } from 'vitest'
import messageItem from '@/components/chat/MessageItem.vue?raw'
import homeWorkspace from '@/components/home/HomeWorkspace.vue?raw'
import auditView from '@/views/ssh/AuditView.vue?raw'
import sshView from '@/views/ssh/SshView.vue?raw'
import runsTable from '@/components/ssh/AgentRunsTable.vue?raw'
import fullTrace from '@/components/agent/AgentFullTrace.vue?raw'
import navSource from '@/composables/useSshNavigation.ts?raw'

describe('AI 界面不再渲染执行过程', () => {
  it('消息里没有内联过程块，也没有内联审批卡', () => {
    expect(messageItem).not.toContain('AgentTraceInline')
    expect(messageItem).not.toContain('AgentFullTrace')
    expect(messageItem).not.toContain('ApprovalCard')
  })

  it('消息只发出带 run 号的跳转', () => {
    expect(messageItem).toContain("'open-trace': [runId: string]")
    expect(messageItem).toContain("emit('open-trace', traceRunId.value)")
  })

  it('任务台把跳转接到导航，并把审批面挂在输入区上方', () => {
    expect(homeWorkspace).toContain('@open-trace="emit(\'open-trace\', $event)"')
    expect(homeWorkspace).toMatch(/<ApprovalDock\s*\/>/)
  })
})

describe('运维轨迹页按 run_id 直达', () => {
  it('SshView 把 openTrace 的焦点交给轨迹页', () => {
    expect(sshView).toContain('@open-trace="openTrace"')
    expect(sshView).toContain(':focus-run-id="auditEntry?.runId"')
    expect(sshView).toContain(':focus-seq="auditEntry?.seq"')
  })

  it('从轨迹 tab 进入时清掉上一次直达的筛选', () => {
    expect(navSource).toContain("runId: ''")
    expect(runsTable).toMatch(/else\s*\{\s*traceFilter\.value = ''\s*expanded\.value = ''/)
  })

  it('轨迹页切到运行列表分段，表格预填筛选并展开该 run', () => {
    expect(auditView).toContain("segment.value = 'runs'")
    expect(runsTable).toContain('traceFilter.value = props.focusRunId')
    expect(runsTable).toContain('expanded.value = props.focusRunId')
  })

  it('展开行回放工具调用时间线，不再写「明细已隐藏」', () => {
    expect(runsTable).toContain('<AgentFullTrace :run-id="r.id" />')
    expect(fullTrace).not.toContain('工具调用明细已隐藏')
    expect(fullTrace).toContain('process.events')
  })
})
