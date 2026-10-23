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
import agentRunPanel from '@/components/agent/AgentRunPanel.vue?raw'
import approvalDock from '@/components/agent/ApprovalDock.vue?raw'
import approvalCard from '@/components/agent/ApprovalCard.vue?raw'
import deliveryCard from '@/components/agent/DeliveryCard.vue?raw'
import approvalCardCss from '@/styles/approval-card.css?raw'
import clarifyCard from '@/components/agent/ClarifyingWizard.vue?raw'
import navSource from '@/composables/useSshNavigation.ts?raw'

/** 取某个类选择器的声明体（这几个选择器在文件里都只定义一次） */
function cssBody(source: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return source.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`))?.[1] ?? ''
}

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

describe('终端交付确认也在决策面', () => {
  it('待确认交付只挂在输入区上方的 ApprovalDock，不回到消息流末尾', () => {
    expect(approvalDock).toContain('<DeliveryCard')
    expect(agentRunPanel).not.toContain('pendingDeliveries')
    expect(agentRunPanel).not.toContain('delivery-section')
  })

  it('交付卡只有确认/拒绝两条结算路径', () => {
    expect(deliveryCard).toContain('confirmDeliver')
    expect(deliveryCard).toContain('rejectDeliver')
  })
})

describe('审批卡的动作栏常驻可视区', () => {
  // 复合命令的逐段拆解曾把整卡撑过审批区限高，外层滚动把确认/拒绝裁到第一屏
  // 之外 —— 用户看到的是一张没有按钮的卡，run 就停在 pending_confirm。
  it('卡骨架固定头尾，只有正文滚动', () => {
    const card = cssBody(approvalCardCss, '.ap-card')
    expect(card).toMatch(/display:\s*flex/)
    expect(card).toMatch(/flex-direction:\s*column/)
    expect(cssBody(approvalCardCss, '.ap-body')).toMatch(/overflow-y:\s*auto/)
    expect(cssBody(approvalCardCss, '.ap-body')).toMatch(/min-height:\s*0/)
    expect(cssBody(approvalCardCss, '.ap-head')).toMatch(/flex:\s*none/)
    expect(cssBody(approvalCardCss, '.ap-actions')).toMatch(/flex:\s*none/)
  })

  it('两类卡自身限高，且不低于外层的卡队列限高', () => {
    expect(cssBody(approvalCard, '.ap-perm')).toMatch(/max-height:\s*min\(38vh,\s*340px\)/)
    expect(cssBody(deliveryCard, '.delivery-card')).toMatch(/max-height:\s*min\(38vh,\s*340px\)/)
    // 交付区若再单独压一个比卡更矮的限高，卡底部的动作栏会重新被裁掉
    expect(cssBody(approvalDock, '.ad-deliveries')).not.toMatch(/max-height/)
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

describe('意图澄清并入决策面（不再是弹窗）', () => {
  // 澄清卡曾是 Teleport 居中模态；2026-10-23 起与审批同为输入区上方非模态决策卡。
  // 这组守卫钉住：唯一渲染位在 ApprovalDock、AgentRunPanel 不再挂它、卡本体不是模态。
  it('澄清卡只挂在 ApprovalDock，不再挂在消息流末尾的 AgentRunPanel', () => {
    expect(approvalDock).toContain('ClarifyingWizard')
    expect(approvalDock).toContain('clarificationWizard')
    expect(agentRunPanel).not.toContain('ClarifyingWizard')
  })

  it('澄清卡不是模态弹窗：无 Teleport / 遮罩 / aria-modal / 全局 Esc', () => {
    expect(clarifyCard).not.toContain('Teleport')
    expect(clarifyCard).not.toContain('clarify-overlay')
    expect(clarifyCard).not.toContain('aria-modal')
    expect(clarifyCard).not.toMatch(/Escape/)
    expect(clarifyCard).toContain('ap-card')     // 复用审批卡骨架
  })

  it('澄清卡与审批卡同限高，头/动作常驻、正文内滚', () => {
    expect(cssBody(clarifyCard, '.ap-clarify')).toMatch(/max-height:\s*min\(38vh,\s*340px\)/)
  })
})
