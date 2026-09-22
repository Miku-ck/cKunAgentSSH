/**
 * Agent Store — 管理 Agent 运行状态
 *
 * 职责:
 * - 管理 Agent 运行事件流（state / content / reasoning / tool_call / done）
 * - 提供 runAgent / abort 方法
 * - 持久化运行历史到后端 agent_runs 表
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { agentApi, streamAgentRun, type AgentSSEEvent, type AgentCostSnapshot, type AgentTraceSnapshot, type AgentIntentionInfo, type AgentClarificationStep } from '@/api/client'
import { StreamingThinkScrubber } from '@/utils/think-scrubber'
import { useToastStore } from './toast'
import { useAgentDeliver } from '@/composables/useAgentDeliver'

export interface ToolCallEntry {
  id: string
  tool: string
  args: Record<string, unknown>
  result?: string
  success?: boolean
  timestamp: number
  /** 所属迭代号（用于按批次分组渲染） */
  iteration?: number
  /** 后端 tc.id（SSE tool_call_id）— 结果回填的唯一可靠配对键 */
  callId?: string
  /** 可见终端执行模式：后端下发的 tool_id — 用户拒绝时按它找回这条 tool_call，
   *  把「[用户拒绝执行]」写回轨迹。交付状态本身不在这里存：权威源是
   *  agentDeliver.deliveries（面板读的就是它），原先这里另抄一份 deliveryStatus
   *  没有任何读者，还会与 deliveries 的 writing/done 漂移（中止后停在 capturing）。 */
  deliveryToolId?: string
}

export interface HumanHelpRequest {
  id: string
  reason: string
  tool?: string
  args?: Record<string, unknown>
  timestamp: number
}

export interface PermissionRequest {
  id: string
  tool: string
  args: Record<string, unknown>
  risk: string
  reversible: boolean
  message: string
  timestamp: number
  /**
   * 本卡授权实际绑定的命令原文（后端 payload.command）。
   * 两类来源：命令入口工具的自由文本参数，与实现 bound_command 的工具
   * （file_write 的写盘模板 / schedule_op 的定时命令）报出的真实执行命令。
   * 授权键 = 工具名 + 这条命令的哈希，所以它必须原样回传给 grant 端点。
   */
  command?: string
  /**
   * 卡片上有一次决策在途（授权或拒绝的 HTTP 请求未返回）。覆盖两枚按钮：
   * 曾在一次往返内先 grant 后 deny，本 run 内 deny 优先，但 always 档的
   * 持久授权已经落下 —— 换个 run 这条命令不再询问。
   */
  settling?: boolean
  /** 是否已处理（已授权或已拒绝） */
  resolved?: boolean
  /** 已处理的卡片是否为「已授权」（已拒绝/过期的卡片文案不同） */
  granted?: boolean
  /**
   * 审批超时标记（permission_expired）：按新语义「到期保留等待集」，
   * 前端不再删卡也不置 resolved，仅打此标 + 留在队列供追溯；
   * 焦点只由用户操作/运行结束推进。重发同命令时仍按未处理复用此卡。
   */
  expired?: boolean
  /** 归一化展示命令（问题 2 改进：让用户看到实际授权的规范化形态） */
  normalizedCommand?: string
  /** 命令后果的中文说明（纯展示，授权键仍绑定命令原文） */
  explain?: string
  /** 后果说明来源：static/fallback/llm */
  explainSource?: 'static' | 'fallback' | 'llm'
  /** 目标环境（test/prod） */
  environment?: string
  /** 用户本次任务原话 — 让用户核对命令是否服务于他要做的事 */
  task?: string
  /** 目标 SSH 会话 id（反查连接名/主机展示） */
  sshSessionId?: string
  /** 发起审批的 run_id（once 授权按 run 键控，防跨 run 消费） */
  runId?: string
  /** 发起审批的会话 ID（session 授权按会话键控，C-review M11） */
  conversationId?: string
}

/** 澄清向导运行时状态（clarification_needed 事件触发后填充） */
export interface ClarificationWizardState {
  runId: string
  tool: string
  toolCallId: string
  args: Record<string, unknown>
  /** 后端触发原因（如 "命令使用通配符/递归操作但未指定具体目标文件"） */
  reason: string
  currentStep: number
  totalSteps: number
  question: string
  options: Array<{ id: string; label: string }>
  allowCustom: boolean
  /** 后端一次推送的全量步骤 — 逐步推进时按索引取题（question/options 只是当前步） */
  steps: AgentClarificationStep[]
  /** 用户已答步骤：step_index 0-based → { choice, custom_text? } */
  answers: Record<string, { choice: string; custom_text?: string }>
  /** 已提交到后端（clarification_done 后置 true） */
  submitted: boolean
}

/**
 * 授权卡片标识键：带命令原文时按「工具 + 命令」区分（同一工具针对不同目标 —— 写
 * /etc/nginx vs 写 /tmp —— 各自一张卡，批了 A 不会放行 B），无命令的纯工具级授权
 * 按工具名。permission_needed 去重、permission_expired 标记过期、permission_explain
 * 回填三处共用同一表达式，避免键形态漂移导致事件找不到卡片。
 *
 * 命令取自后端 payload.command 而非 args：只有后端知道本次实际执行了什么
 * （file_write 的 args 里只有 path/content，落盘命令是它自己拼的）。前端自此不再
 * 自带一份"哪些工具算命令工具"的名单 —— 旧名单只有 shell_exec，漏了后端的
 * job_run，两条不同命令的卡片曾被合成一张（批准第一条等于放行第二条）。
 */
function permCardKey(tool: string, command?: string): string {
  return command ? `${tool}::${command}` : tool
}

export type AgentRunStatus = 'idle' | 'running' | 'completed' | 'failed'

export const useAgentStore = defineStore('agent', () => {
  // ── State ──
  const status = ref<AgentRunStatus>('idle')
  const content = ref('')
  const reasoning = ref('')
  const currentState = ref('')
  const iteration = ref(0)
  const toolCalls = ref<ToolCallEntry[]>([])
  const warnings = ref<string[]>([])
  const errorMessage = ref('')
  const totalIterations = ref(0)
  const humanHelpRequests = ref<HumanHelpRequest[]>([])
  const permissionRequests = ref<PermissionRequest[]>([])
  // P7: 成本信息 + 最近 run_id（供 TraceViewer 使用）
  const costInfo = ref<AgentCostSnapshot | null>(null)
  // 流式 think 块擦洗器（reasoning 模型可能在 content 流里混入 <think> 块）
  const _contentScrubber = new StreamingThinkScrubber()
  const _reasoningScrubber = new StreamingThinkScrubber()
  const lastRunId = ref<string>('')
  // C3 修复：记录当前运行模式，避免 keep-alive 下跨组件 watch 重复 push
  const currentMode = ref<'ssh' | ''>('')
  // 审查 M-19：记录本次运行发起时的【源会话 ID】。
  // Agent 运行期间用户可能切换会话 — 完成固化必须落回源会话（临时占位消息
  // 推在哪个会话就固化哪个），不能按"完成时刻的当前会话"决定
  const currentConversationId = ref('')
  // H14: 意图识别结果（用于规划列表展示）
  const intentionInfo = ref<AgentIntentionInfo | null>(null)
  // 澄清向导状态 — clarification_needed 事件触发时填充，ClarifyingWizard 组件据此渲染
  const clarificationWizard = ref<ClarificationWizardState | null>(null)
  // H14 修复：人工介入去重标志（同一 run 只接受一次，独立于数组长度）
  let _helpRequestedThisRun = false
  // M17：用户中止标记 — abort() 置位、_reset() 清除。固化 watch 据此把
  // running→idle 区分"用户中止"（需固化为"(已中止)"）与 C10 并发保护的
  // 直接 abortFn()（不固化，避免误判为失败/中止）
  const aborted = ref(false)
  // 定时任务变更信号：聊天里 schedule_op 的 create/cancel 直接写后端库、
  // 绕过 REST 路由，SchedulesView 靠 watch 本计数做失效刷新。
  // 不进 _reset()——它跨 run 单调递增，重置会丢掉运行间隙的变更通知
  const scheduleRev = ref(0)

  let abortFn: (() => void) | null = null
  // 记录运行开始时间，用于 snapshot() 计算 duration
  let runStartTime: number = 0

  // 可见终端执行模式：交付编排器（模块级单例，跨组件共享）
  const agentDeliver = useAgentDeliver()

  // ── Computed ──
  const isRunning = computed(() => status.value === 'running')
  const hasHumanHelpRequest = computed(() => humanHelpRequests.value.length > 0)

  // 「运行面板有东西要显示」的唯一判定：逐项对齐 AgentRunPanel 实际渲染的区块
  // （任务规划 / 警告 / 人工介入 / 工具授权 / 可见终端交付 / 错误）。
  // 原先这份条件是抄在宿主 SshAiPanel.vue 的 v-if 里的，而那份漏了任务规划、待确认
  // 交付与澄清向导：后端发 clarification_needed 时面板压根没挂载，向导渲染不出来，
  // 用户只看到一条 toast，运行干等到后端 TTL 超时。面板加区块而宿主忘记同步必然复发，
  // 所以条件搬回数据侧，宿主只负责无条件挂载。
  const hasRunNotice = computed(() =>
    !!intentionInfo.value
    || warnings.value.length > 0
    || hasHumanHelpRequest.value
    || permissionRequests.value.length > 0
    || agentDeliver.deliveries.value.length > 0
    || !!errorMessage.value
  )

  // ── 单一焦点审批队列（spec refactor-approval-center） ──
  // 待审批 = 未 resolved 且未 expired（仍在等待用户操作）；数组 append 序即年龄序，
  // 队首恒为最旧一条 → 焦点。expired 单独归入追溯区，不再占用可操作焦点。
  const pendingPermissions = computed(() =>
    permissionRequests.value.filter(r => !r.resolved && !r.expired),
  )
  /** 超时保留项（可追溯展示，不参与焦点/批量操作） */
  const expiredPermissions = computed(() =>
    permissionRequests.value.filter(r => r.expired && !r.resolved),
  )


  // ── Actions ──

  function runAgent(options: {
    task: string
    conversationId: string
    modelId: string
    mode?: 'ssh'
    systemPrompt?: string
    sshSessionId?: string
    environment?: 'test' | 'prod'
    enableThinking?: boolean
    enableTools?: boolean
    maxIterations?: number
    enableDynamicBudget?: boolean
    /** E1：意图识别 + 思考预算控制总开关 */
    enableIntentionGuide?: boolean
    /** 成本熔断上限（USD）；undefined/0 = 后端默认 */
    maxCostUsd?: number
    /** token 熔断上限；undefined/0 = 后端默认 */
    maxTotalTokens?: number
    skillIds?: string[]
    autoSkillMatch?: boolean
    contextWindow?: number
    visibleTerminal?: boolean
    sessionType?: 'local' | 'remote'
    /** 对话姿态（goal/plan）—— 透传后端注入姿态 system prompt */
    workflowMode?: 'goal' | 'plan'
    /** 权限模式（normal=手动审批 / auto=模型代判 / plan=只读强制）—— 缺省 normal */
    permissionMode?: 'normal' | 'auto' | 'plan'
  }) {
    // C10 修复：并发保护 — 如果有正在运行的流，先中止它，避免多个流并行导致状态混乱
    if (abortFn) {
      abortFn()
      abortFn = null
    }

    // 重置状态
    _reset()

    runStartTime = Date.now()

    status.value = 'running'
    currentState.value = 'thinking'
    // C3 修复：记录当前模式，watch 内据此判断是否为本组件的运行
    currentMode.value = options.mode || 'ssh'
    // 审查 M-19：锁定源会话（_reset 已清空，这里重新写入）
    currentConversationId.value = options.conversationId || ''

    abortFn = streamAgentRun({
      ...options,
      sshSessionId: options.sshSessionId,
      onEvent: (event: AgentSSEEvent) => _handleEvent(event),
      onError: (err: string) => {
        // 审查修复（P2）：与 onDone 的 guard 对称 — 原实现无条件置 failed，
        // 窄窗口竞态（服务端先发 done 再发 error / 中止后迟到的 error）
        // 会把已完成/已中止的 run 状态翻回 failed
        if (status.value === 'completed') {
          console.warn('[Agent] 运行已完成，忽略迟到 onError:', err)
          return
        }
        errorMessage.value = err?.trim()
          ? err
          : 'AI 运行失败，后端未返回具体错误，请查看后端日志'
        status.value = 'failed'
        currentState.value = 'failed'
        // B11 修复：run 结束清理权限请求状态，防止面板里残留过期授权按钮
        permissionRequests.value = []
        // H14 修复：流结束后清除 abortFn，避免对已结束的流调用 abort 重置状态
        abortFn = null
      },
      onDone: () => {
        if (status.value === 'running') {
          status.value = 'completed'
        }
        // H14 修复：流结束后清除 abortFn
        abortFn = null
      },
    })
  }

  function abort() {
    if (abortFn) {
      abortFn()
      abortFn = null
    }
    status.value = 'idle'
    // M17：标记用户中止，供固化 watch 生成"(已中止)"文案
    aborted.value = true
    // B11 修复：中止即 run 结束 — 清理权限请求状态，防止残留过期授权按钮
    permissionRequests.value = []
    // 可见终端执行模式：中止所有在途交付捕获，避免 dangling POST
    // 问题 1 兜底：传 run_id 释放该 run 未消费的 once 授权（用户中止可能绕过终态清理）
    agentDeliver.abortRun(lastRunId.value || undefined)
  }

  /** 生成当前运行状态的快照（不重置状态），用于固化到 ChatMessage.agentTrace */
  function snapshot(): AgentTraceSnapshot {
    const durationMs = runStartTime > 0 ? Date.now() - runStartTime : 0
    return {
      // H14 修复：human_help 状态在快照中显示为 failed 以兼容类型，currentState 保留 human_help
      // 中止同样是"没跑完"：原实现只认 failed，用户中止后固化的 trace 卡片写着
      // 「已完成」，而消息正文是「(已中止)」——同一条消息自相矛盾。
      status: status.value === 'failed' || aborted.value || currentState.value === 'human_help' ? 'failed' : 'completed',
      currentState: currentState.value || 'completed',
      iterations: totalIterations.value || iteration.value + 1,
      duration: Math.round(durationMs / 100) / 10, // 秒，保留1位小数
      // 深拷贝 toolCalls 避免引用共享
      toolCalls: toolCalls.value.map(tc => ({
        id: tc.id,
        tool: tc.tool,
        args: { ...tc.args },
        result: tc.result,
        success: tc.success,
        timestamp: tc.timestamp,
        iteration: tc.iteration,
      })),
      reasoning: reasoning.value,
      lastRunId: lastRunId.value,
      costInfo: costInfo.value || undefined,
      errorMessage: errorMessage.value || undefined,
    }
  }

  function _reset() {
    status.value = 'idle'
    content.value = ''
    reasoning.value = ''
    // 重置流式擦洗器，避免上一轮残留 buf 污染新一轮
    _contentScrubber.reset()
    _reasoningScrubber.reset()
    currentState.value = ''
    iteration.value = 0
    toolCalls.value = []
    warnings.value = []
    errorMessage.value = ''
    totalIterations.value = 0
    humanHelpRequests.value = []
    permissionRequests.value = []
    agentDeliver.resetDeliveries()
    costInfo.value = null
    lastRunId.value = ''
    currentMode.value = ''
    currentConversationId.value = ''
    intentionInfo.value = null
    clarificationWizard.value = null
    _helpRequestedThisRun = false
    aborted.value = false
  }

  /** 用户确认/忽略某次求助请求 */
  function dismissHelp(id: string) {
    humanHelpRequests.value = humanHelpRequests.value.filter(r => r.id !== id)
  }

  /** 用户忽略某次权限请求（仅关闭卡片，后端仍在等待，超时后按拒绝处理） */
  function dismissPermission(id: string) {
    permissionRequests.value = permissionRequests.value.filter(r => r.id !== id)
  }

  /**
   * 用户明确拒绝该命令/工具 — 回传后端登记拒绝标记，本 run 内 AI 重发同一条
   * 不再弹卡片（区别于 dismissPermission 的"先关掉不理会"）。
   * 非命令工具传空串，后端 _grant_key 会退化为按工具名建键。
   */
  async function denyPermission(id: string): Promise<boolean> {
    const req = permissionRequests.value.find(r => r.id === id)
    if (!req || req.resolved || req.settling) return false
    if (!req.runId) {
      // 拒绝记录按 run_id 隔离，无 run_id 后端直接 400 — 只能本地关闭卡片
      dismissPermission(id)
      return true
    }
    // 与 grant 同源：拒绝登记的键必须是后端实际执行的那条串（args.command 对
    // job_run/file_write 都不成立，退化成裸工具名会让"拒绝过一次"形同虚设）
    const command = req.command ?? ''
    req.settling = true
    try {
      await agentApi.denyCommand(req.tool, command, req.runId)
    } catch (e) {
      console.error('拒绝登记失败:', e)
      req.settling = false
      return false  // 卡片保留，用户可重试；否则后端仍会等待审批
    }
    dismissPermission(id)
    return true
  }

  /** 标记权限请求已处理 */
  function resolvePermission(id: string, granted: boolean) {
    const req = permissionRequests.value.find(r => r.id === id)
    if (req) {
      req.resolved = true
      req.granted = granted
      req.settling = false
    }
  }

  // ── 可见终端执行模式：待交付命令 ──
  /** 有待确认执行的交付（UI 渲染"命令待确认"条） */
  const pendingDeliveries = computed(() =>
    agentDeliver.deliveries.value.filter(d => d.status === 'pending_confirm'),
  )

  /** 用户确认：把命令写入可见终端（状态由 agentDeliver 的 deliveries 播报） */
  function confirmDeliver(toolId: string) {
    agentDeliver.confirmDeliver(toolId)
  }

  /** 用户拒绝：不回写命令，直接回传失败结果 */
  async function rejectDeliver(toolId: string) {
    await agentDeliver.rejectDeliver(toolId)
    const tc = toolCalls.value.find(t => t.deliveryToolId === toolId)
    if (tc) {
      tc.result = '[用户拒绝执行]'
      tc.success = false
    }
  }

  function _handleEvent(event: AgentSSEEvent) {
    switch (event.type) {
      case 'state':
        if (event.state) currentState.value = event.state
        if (event.iteration !== undefined) iteration.value = event.iteration
        // 问题 1/4：实时捕获当前 run_id（SSE 全事件透传），供授权键控与中止清理使用
        if (event.run_id) lastRunId.value = event.run_id
        break

      case 'content':
        if (event.content) {
          // 流式擦洗 <think> 块（reasoning 模型可能把 CoT 混入 content）
          const visible = _contentScrubber.feed(event.content)
          if (visible) content.value += visible
        }
        break

      case 'reasoning':
        if (event.reasoning) {
          // reasoning 流也擦洗（防止外层重复显示 think 块）
          const visible = _reasoningScrubber.feed(event.reasoning)
          if (visible) reasoning.value += visible
        }
        break

      case 'tool_call_start':
        if (event.tool) {
          const entry: ToolCallEntry = {
            id: `${event.tool}_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
            tool: event.tool,
            args: event.args || {},
            timestamp: Date.now(),
            iteration: event.iteration ?? iteration.value,
            callId: event.tool_call_id,
          }
          toolCalls.value.push(entry)
        }
        break

      case 'tool_call_result':
        if (event.tool) {
          // 匹配"未回填"的条目必须用 result === undefined：原实现写 !t.result，
          // 而 shell_exec 执行无输出命令时 result 就是空串 —— 空串条目会被当成
          // 未回填，同迭代的第二个结果盖到第一个头上（结果张冠李戴）。
          // 首选 tool_call_id（后端 tc.id，start/result 成对透传）：同名多调用时
          // 只有它能确定归属，iteration/名称匹配只是事件缺 id 时的兜底。
          const eventIter = event.iteration
          let tc: ToolCallEntry | undefined
          if (event.tool_call_id) {
            tc = toolCalls.value.find(t => t.callId === event.tool_call_id && t.result === undefined)
          }
          if (!tc && eventIter !== undefined) {
            tc = [...toolCalls.value].reverse().find(t => t.tool === event.tool && t.iteration === eventIter && t.result === undefined)
          }
          // 降级：反向找同名未回填
          if (!tc) {
            tc = [...toolCalls.value].reverse().find(t => t.tool === event.tool && t.result === undefined)
          }
          if (tc) {
            // 落库归一为字符串，保证「undefined = 未回填」这个判据成立
            tc.result = event.result ?? ''
            tc.success = event.success
            if (event.tool === 'schedule_op' && event.success && (tc.args?.action ?? 'create') !== 'list') {
              scheduleRev.value++
            }
          }
        }
        break

      case 'tool_deliver': {
        // 可见终端执行模式：后端等待前端把命令写入当前可见终端并回传结果
        if (!event.tool_id) break
        // 关联到 shell_exec 的 tool_call 条目，标记为待交付
        // （判据同 tool_call_result：空串结果算已回填，否则会挂到无输出的旧条目上）
        const pendingTc = [...toolCalls.value].reverse().find(t => t.tool === 'shell_exec' && t.result === undefined)
        if (pendingTc) {
          pendingTc.deliveryToolId = event.tool_id
        }
        agentDeliver.handleDeliver({
          tool_id: event.tool_id,
          command: event.command || '',
          session_id: event.session_id || '',
          requires_confirm: !!event.requires_confirm,
          // 问题 4：透传 run_id — 拒绝时用于标记 deny_command
          runId: event.run_id || undefined,
        })
        break
      }

      case 'warning':
        if (event.message) {
          warnings.value.push(event.message)
          // 把导致运行终止的关键警告同步到 errorMessage，
          // 避免最终 content 为空时前端显示 "未产生输出"
          if (/达到最大迭代数|强制终止|运行失败/.test(event.message)) {
            errorMessage.value = event.message
          }
        }
        break

      case 'human_help_needed':
        // H14 修复：显式标志位去重，避免用户忽略后 length===0 导致重复接受
        if (!_helpRequestedThisRun) {
          _helpRequestedThisRun = true
          humanHelpRequests.value.push({
            id: `help_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
            reason: event.reason || 'AI 请求人类介入',
            tool: event.tool,
            args: event.args,
            timestamp: Date.now(),
          })
        }
        break

      case 'permission_needed': {
        // 审查 M-3 + 本轮 A6：按"工具 + 实际执行命令"去重，同一工具的不同目标
        // 各自出卡片（写 /etc/nginx 的授权不会顺带放行写 /tmp）
        const evtTool = event.tool || ''
        const evtCommand = typeof event.command === 'string' && event.command ? event.command : undefined
        const dupKey = permCardKey(evtTool, evtCommand)
        // 去重判据只看【未处理】的卡片：已处理的老卡片仍留在数组里（resolvePermission
        // 只打标记、不摘卡，面板要显示「已授权」），而它在后端已经不代表有人在等 ——
        // driver 在命令执行成功后就把该签名移出「等待审批」集合，同一命令在后续迭代
        // 再次请求时后端会重发 permission_needed。把它算进 dup 判据就等于吞掉这张新卡：
        // 用户无从批准（老卡显示「已授权」），后端干等 120s 后按拒绝处理并注入强拒绝。
        const alreadyPending = permissionRequests.value.some(
          r => !r.resolved && permCardKey(r.tool, r.command) === dupKey,
        )
        // B11 修复：权限卡片只存在于 AI 面板内，面板关闭期间用户无法响应 —
        // 从非 pending → pending 转换时 toast 提醒（重复请求不重复弹）
        const hadPending = permissionRequests.value.some(r => !r.resolved)
        if (!alreadyPending) {
          permissionRequests.value.push({
            id: `perm_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
            tool: evtTool,
            args: event.args || {},
            risk: event.risk || 'moderate',
            reversible: event.reversible ?? false,
            message: event.message || `需要授权: ${evtTool}`,
            timestamp: Date.now(),
            command: evtCommand,
            // 问题 2 改进：传递归一化展示命令（仅命令执行工具）
            normalizedCommand: typeof event.normalized_command === 'string' ? event.normalized_command : undefined,
            // 卡片可读性：中文后果说明 + 环境/用户原任务/目标会话
            explain: event.explain || undefined,
            explainSource: event.explain_source || undefined,
            environment: event.environment || undefined,
            task: event.task || undefined,
            sshSessionId: event.ssh_session_id || undefined,
            // once 授权按 run 键控 / session 按会话键控：创建时捕获上下文，
            // 授权时回传后端（后端 PermissionGate 据此键控）
            runId: event.run_id || lastRunId.value || undefined,
            conversationId: currentConversationId.value || undefined,
          })
          if (!hadPending) {
            useToastStore().warning('AI 需要权限确认，请打开 AI 面板')
          }
        }
        break
      }

      case 'permission_explain': {
        // 后果解释由后端在卡片弹出后异步补发（未知命令走 LLM，≤6s）。
        // 该事件只带顶层 command（不带 args），据此拼出与卡片一致的标识键。
        // 找不到未处理卡片 = 用户已授权/已拒绝/审批超时/run 已结束 → 直接丢弃。
        const text = typeof event.explanation === 'string' ? event.explanation.trim() : ''
        if (!text) break
        const key = permCardKey(event.tool || '', event.command)
        for (const r of permissionRequests.value) {
          if (!r.resolved && permCardKey(r.tool, r.command) === key) {
            r.explain = text
            r.explainSource = 'llm'
          }
        }
        break
      }

      case 'permission_expired': {
        // 审批窗口语义重构（spec refactor-approval-center）：permission_expired 不再
        // "移除并拒绝"——后端改在到期后仍把签名留在等待集、等待至 grant/deny/会话结束。
        // 前端相应不再置 resolved/摘卡，而是打 expired 标记保留在队列（供追溯），
        // 焦点递进只由用户操作或运行结束触发；同一命令重发会复用本卡而非重复建卡。
        const key = permCardKey(event.tool || '', event.command)
        for (const r of permissionRequests.value) {
          if (r.resolved) continue
          if (permCardKey(r.tool, r.command) === key) {
            r.expired = true
            r.settling = false
            r.message = `${r.message}（审批超时）`
          }
        }
        break
      }

      case 'error':
        errorMessage.value = event.message?.trim() || 'AI 运行失败，后端未返回具体错误'
        status.value = 'failed'
        // H13 修复：同步更新 currentState，避免 UI 仍显示"思考中"
        currentState.value = 'failed'
        // B11 修复：run 结束清理权限请求状态，防止残留过期授权按钮
        permissionRequests.value = []
        // run 已终止：澄清向导继续挂着的话，用户答题只会 POST 到一个
        // 后端不再等待的 run_id（clarification_done 永不回来，卡片永不消失）
        clarificationWizard.value = null
        // H1 修复：不在 error 事件清理 abortFn，此时流可能仍未关闭
        // abortFn 由 onDone/onError 回调（真正代表流结束）清理
        break

      case 'intention':
        if (event.intention) {
          intentionInfo.value = event.intention
        }
        break

      case 'clarification_needed': {
        // 澄清向导触发：后端 PRE_TOOL_CALL 拦截器发现意图不明的工具调用（如 rm -rf *）
        // 一次推送全部步骤 + 当前步；前端据此弹出卡片组
        const wizRunId = event.run_id || lastRunId.value || ''
        const wizToolCallId = event.tool_call_id || ''
        const prevWiz = clarificationWizard.value
        // 同一张题的重发（用户还没答完、模型重试了改写类工具）：就地刷新题面。
        // 整体替换对象会命中 ClarifyingWizard 的 watch(wizard) → localStep 归零，
        // 加上 answers 重建，用户答到一半的选择被抹掉，只能从头再答一遍。
        if (prevWiz && !prevWiz.submitted
          && prevWiz.runId === wizRunId && prevWiz.toolCallId === wizToolCallId) {
          Object.assign(prevWiz, {
            currentStep: event.current_step ?? prevWiz.currentStep,
            totalSteps: event.total_steps ?? prevWiz.totalSteps,
            question: event.question || prevWiz.question,
            options: event.options || prevWiz.options,
            allowCustom: event.allow_custom ?? prevWiz.allowCustom,
            steps: event.steps || prevWiz.steps,
          })
          break
        }
        clarificationWizard.value = {
          runId: wizRunId,
          tool: event.tool || '',
          toolCallId: wizToolCallId,
          args: event.args || {},
          reason: event.reason || '',
          currentStep: event.current_step ?? 0,
          totalSteps: event.total_steps ?? 0,
          question: event.question || '',
          options: event.options || [],
          allowCustom: event.allow_custom ?? true,
          steps: event.steps || [],
          // 累积答案：每步选中后写入
          answers: {},
          submitted: false,
        }
        useToastStore().warning('AI 需要对不明确的操作进行确认')
        break
      }

      case 'clarification_done': {
        // 后端已注入澄清答案 + 放行工具执行 — 关闭向导
        // 定时器捕获本次向导对象：800ms 内若已开新 run 并弹出新向导，
        // 无条件置 null 会把新向导一起抹掉（用户再也答不了题）
        const wiz = clarificationWizard.value
        if (wiz) {
          wiz.submitted = true
        }
        // 短暂延迟后清除（让组件播放一个完成动画/提示）
        setTimeout(() => {
          if (clarificationWizard.value === wiz) {
            clarificationWizard.value = null
          }
        }, 800)
        break
      }

      case 'done':
        // 流结束：flush 擦洗器，输出残留 buf（仍在 think 块内则丢弃，防泄露 partial CoT）
        {
          const contentTail = _contentScrubber.flush()
          if (contentTail) content.value += contentTail
          const reasoningTail = _reasoningScrubber.flush()
          if (reasoningTail) reasoning.value += reasoningTail
        }
        if (event.iterations !== undefined) totalIterations.value = event.iterations
        // P7: 捕获成本快照 + run_id
        if (event.cost) costInfo.value = event.cost
        if (event.run_id) lastRunId.value = event.run_id
        // 判据同 error 分支：run 已经收尾，向导继续挂着就是让用户答一张没人收的题
        // （用户离席期间模型放弃重试、迭代数耗尽 → done，向导却还开着）。答完
        // POST 到已结束的 run_id 永远等不到 clarification_done，卡片只能靠下一次
        // runAgent 的 _reset 才被抹掉。
        clarificationWizard.value = null
        // B11 修复：run 结束清理权限请求状态，防止面板里残留过期授权按钮
        permissionRequests.value = []
        // H14 修复：支持 human_help 终止态，保留 currentState 供 UI 显示"需要人工介入"
        status.value = event.state === 'failed' || event.state === 'human_help' ? 'failed' : 'completed'
        // 保留有意义的 currentState（如 human_help），仅在 event.state 为空时回退
        if (event.state) {
          currentState.value = event.state
        } else if (status.value !== 'failed') {
          currentState.value = 'completed'
        }
        break
    }
  }

  return {
    // state
    status,
    content,
    reasoning,
    toolCalls,
    warnings,
    errorMessage,
    humanHelpRequests,
    permissionRequests,
    costInfo,
    lastRunId,
    currentMode,
    currentConversationId,
    intentionInfo,
    clarificationWizard,
    aborted,
    scheduleRev,
    // computed
    isRunning,
    hasRunNotice,
    pendingPermissions,
    expiredPermissions,
    pendingDeliveries,
    deliveries: agentDeliver.deliveries,
    // actions
    runAgent,
    abort,
    snapshot,
    dismissHelp,
    dismissPermission,
    denyPermission,
    resolvePermission,
    confirmDeliver,
    rejectDeliver,
    // P1-4 fix: 显式返回 Promise 让前端 await 有效（防止重复提交）
    submitClarification: (runId: string, answers: Record<string, { choice: string; custom_text?: string }>): Promise<{ run_id: string; status: string; steps: number }> =>
      agentApi.submitClarification(runId, answers),
    skipClarification: (runId: string): Promise<{ run_id: string; status: string }> =>
      agentApi.skipClarification(runId),
    // P0-2 fix: 前端提交成功后立即关闭的清理动作（不依赖 SSE 兜底）
    clearClarification: () => {
      clarificationWizard.value = null
    },
  }
})
