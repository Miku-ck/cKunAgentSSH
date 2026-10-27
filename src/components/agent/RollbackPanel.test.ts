/**
 * RollbackPanel 守卫（vitest 为 node 环境、无 DOM 挂载 —— 组件契约用 ?raw
 * 源码守卫钉住，先例见 approval-trace-boundary.test.ts）。
 *
 * 守的点（设计文档 §2.5）：
 * - 可用性闸：仅 pending+终态显示按钮，且无匹配活动 SSH 会话时禁用（fail-closed，
 *   杜绝回滚命令打错机器）—— 三条闸缺一不可；
 * - 逐项渲染：confidence 徽章语义、delete_created 危险标记反色；
 * - 调用序：plan → runAgent → register，conversationId 必须取 plan.conversation_id
 *   （回滚消息落原会话）；
 * - 挂点：AgentFullTrace 元信息条下方挂 RollbackPanel，且不改展开行结构
 *   （approval-trace-boundary.test.ts:103 锁定的 <AgentFullTrace :run-id="r.id" /> 不受影响）。
 */
import { describe, expect, it } from 'vitest'
import rollbackPanel from '@/components/agent/RollbackPanel.vue?raw'
import fullTrace from '@/components/agent/AgentFullTrace.vue?raw'
import runsTable from '@/components/ssh/AgentRunsTable.vue?raw'

describe('回滚可用性闸（fail-closed）', () => {
  it('按钮禁用条件 = canRollback 判定，canRollback 三条闸齐全', () => {
    expect(rollbackPanel).toContain(':disabled="!canRollback || starting"')
    // 终态 + 有 pending 非 manual 项 + 匹配到活动会话 + 无进行中 run
    expect(rollbackPanel).toContain('terminal.value')
    expect(rollbackPanel).toContain("it.status === 'pending' && it.confidence !== 'manual'")
    expect(rollbackPanel).toContain('!!matchedSessionId.value')
    expect(rollbackPanel).toContain('!agentStore.isRunning')
  })

  it('无匹配会话 → 禁用并点名「先连接目标主机」，不做自动重连', () => {
    expect(rollbackPanel).toContain('请先连接目标主机')
    expect(rollbackPanel).toContain('不做自动重连')
    // 会话匹配必须按连接配置的 host 对上，而不是拿 activeSessionId 顶上
    expect(rollbackPanel).toContain('conn?.host === host')
    // 目标主机未知（undo 行没记 host）时同样拒绝
    expect(rollbackPanel).toContain('无法确定目标主机')
  })

  it('匹配会话限定远程类型', () => {
    expect(rollbackPanel).toContain("x.type !== 'remote'")
  })
})

describe('逐项渲染：可信度徽章与危险标记', () => {
  it('confidence 徽章语义与 delete_created 反色', () => {
    expect(rollbackPanel).toContain("if (confidence === 'snapshot') return '快照还原'")
    expect(rollbackPanel).toContain("if (confidence === 'derived') return '反向命令'")
    expect(rollbackPanel).toContain("if (confidence === 'delete_created') return '将删除文件'")
    expect(rollbackPanel).toContain('return \'仅记录\'')
    expect(rollbackPanel).toContain("if (it.confidence === 'delete_created') return 'bad'")
    // 后端派生的警示语必须渲染出来
    expect(rollbackPanel).toContain('<span v-if="it.warning" class="rb-warn">{{ it.warning }}</span>')
  })

  it('已自动回滚（FAILED run）与已还原状态对用户可见', () => {
    expect(rollbackPanel).toContain("it.status === 'auto_rolled_back'")
    expect(rollbackPanel).toContain("it.status === 'rolled_back'")
  })
})

describe('调用序：plan → runAgent → register', () => {
  it('startRollback 按序发起，conversationId 取 plan.conversation_id', () => {
    const planIdx = rollbackPanel.indexOf('rollbackApi.plan(props.runId)')
    const runIdx = rollbackPanel.indexOf('agentStore.runAgent({')
    const regIdx = rollbackPanel.indexOf('rollbackApi.register(props.runId, id)')
    expect(planIdx).toBeGreaterThan(-1)
    expect(runIdx).toBeGreaterThan(planIdx)   // plan 先于 runAgent
    expect(regIdx).toBeGreaterThan(runIdx)    // register 在 run_id 事件后（waitAndRegister 内）
    expect(rollbackPanel).toContain('conversationId: plan.conversation_id')
    // 回滚 run 显式走交互式 normal 档：危险命令照常弹卡
    expect(rollbackPanel).toContain("permissionMode: 'normal'")
  })

  it('runAgent 用 plan 的环境与匹配会话，不碰 client.ts 桶导出', () => {
    expect(rollbackPanel).toContain("environment: plan.environment === 'prod' ? 'prod' : 'test'")
    expect(rollbackPanel).toContain('sshSessionId: matchedSessionId.value || undefined')
    expect(rollbackPanel).toContain("from '@/api/rollback'")
    expect(rollbackPanel).toContain("from '@/api/base'")
    expect(rollbackPanel).not.toContain("from '@/api/client'")
  })

  it('register 由 lastRunId 变化触发，且换 run 才登记', () => {
    expect(rollbackPanel).toContain('watch(() => agentStore.lastRunId')
    expect(rollbackPanel).toContain('id === preRunId')
    // 模型选择与任务台同一份单例（sshAi.selectedModelId，Pinia 解包读值）
    expect(rollbackPanel).toContain('modelId: sshAi.selectedModelId')
    expect(rollbackPanel).toContain("from '@/stores/ssh-ai'")
    // 绝不新建第二份持久化层（useChatPersistence 是工厂，误用会产生平行会话状态）
    expect(rollbackPanel).not.toContain('useChatPersistence')
  })
})

describe('加载失败可见性与 404 静默', () => {
  it('非 404 错误在无数据时也渲染（埋进 hasData 块 = 永不渲染的死文案）', () => {
    // error <p> 必须在 rb-panel 的 v-if="hasData" 之外
    expect(rollbackPanel).toContain('<p v-if="!hasData && error" class="rb-hint err">{{ error }}</p>')
    // 404（无 undo 记录/run 不存在）静默，其余错误可见
    expect(rollbackPanel).toContain('NOT_FOUND_RE.test(msg)')
    expect(rollbackPanel).toContain('/(无 undo 记录|run 不存在|HTTP 404)/')
  })

  it('prod 强化明示：不弹临时审批卡、需预登记（undo_restore 与 shell 命令两条通道都说破）', () => {
    expect(rollbackPanel).toContain("environment.value === 'prod'")
    expect(rollbackPanel).toContain('预授权 allowlist')
    expect(rollbackPanel).toContain('不弹临时审批卡')
    expect(rollbackPanel).toContain('预登记工具 undo_restore')
    expect(rollbackPanel).toContain('shell 反向命令需逐条预登记')
  })
})

describe('挂点：AgentFullTrace 元信息条下方，展开行契约不动', () => {
  it('AgentFullTrace 挂 RollbackPanel 且传入 runId', () => {
    const mountIdx = fullTrace.indexOf('<RollbackPanel :run-id="runId" />')
    const metaIdx = fullTrace.indexOf('</div>', fullTrace.indexOf('ft-meta'))
    expect(mountIdx).toBeGreaterThan(-1)
    expect(mountIdx).toBeGreaterThan(metaIdx)
  })

  it('AgentRunsTable 展开行结构保持原样（三方列契约不受影响）', () => {
    expect(runsTable).toContain('<AgentFullTrace :run-id="r.id" />')
    expect(runsTable).not.toContain('RollbackPanel')
  })
})
