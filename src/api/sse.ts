/** Agent SSE 域：流式运行事件契约与 streamAgentRun/postToolResult（原 client.ts 732-1066 行，ARCH-008 拆分） */

import { PERMISSION_MODE_DEFAULT, type PermissionModeKey } from '@/utils/run-modes'
import type { RunTerminalState } from '@/utils/agent-run-status'
import { backendErrorDetail, ensureApiToken, getBaseUrl, toFriendlyMessage, withApiToken } from './base'

// ── Agent SSE 流式运行 ──

/** 澄清向导单步（后端 reins.clarification._step_to_dict 的形状） */
export interface AgentClarificationStep {
  question: string
  options: Array<{ id: string; label: string }>
  allow_custom: boolean
}

export interface AgentSSEEvent {
  // 全集由后端 SSEAdapter._KIND_MAP 决定，两侧一致性由
  // backend/tests/test_trace_projection_keys.py 钉住（新增事件先补后端清单）
  type: 'state' | 'content' | 'reasoning' | 'tool_call_start' | 'tool_call_result' | 'warning' | 'error' | 'human_help_needed' | 'permission_needed' | 'permission_expired' | 'permission_explain' | 'intention' | 'tool_deliver' | 'budget_extended' | 'reflection_request' | 'plan_mode_block' | 'clarification_needed' | 'clarification_done' | 'done'
  state?: string
  iteration?: number
  content?: string
  reasoning?: string
  tool?: string
  args?: Record<string, unknown>
  result?: string
  success?: boolean
  /** tool_call_result 专用：后端实测的该次调用耗时。SSE 一直在发
   *  （backend/agent/harness/telemetry.py 的 tool_call_result 分支），
   *  这里此前没声明也没读 —— 不是新字段，是接住一个被丢弃的既有读数。 */
  duration_ms?: number
  message?: string
  iterations?: number
  // human_help_needed 事件专用字段
  reason?: string
  /** 后端按失败模式诊断给出的可执行建议（ConfidenceMonitor._diagnose_blocker），
   *  面板在 reason 下方渲染「建议：…」行；无诊断时后端不下发该字段 */
  suggestion?: string
  // permission_needed 事件专用字段
  risk?: string
  reversible?: boolean
  /** 归一化展示命令（问题 2 改进：前端据此展示实际授权键绑定的命令形态） */
  normalized_command?: string
  /** 命令后果的中文说明（审批卡片给非专业用户看，不参与任何授权判定） */
  explain?: string
  /** 后果说明来源：static=静态后果表 fallback=通用兜底 llm=模型生成（仅供参考）
   *  tool=工具级审批，文案取工具自己声明的 description（没有命令可言） */
  explain_source?: 'static' | 'fallback' | 'llm' | 'tool'
  /** 目标环境（test/prod），prod 卡片需额外警示 */
  environment?: string
  /** 环境标签的判定来源（服务端按主机连接配置判定 / 客户端声明未核实） */
  environment_source?: string
  /** 上者的中文说明，直接展示给审批人 */
  environment_source_label?: string
  /** 用户本次任务原话（后端截断 200 字符），供用户核对"这是否我要做的事" */
  task?: string
  /** 目标 SSH 会话 id（前端据此反查连接名/主机展示） */
  ssh_session_id?: string
  /** 本次 run 的目标主机（服务端从连接配置反查）。审批卡的「始终允许」只写入
   *  `@host|键` 一行；空/缺失 = 未核实，授权落全局条目。 */
  host?: string
  /** permission_explain 事件专用字段：后到的命令后果解释 */
  explanation?: string
  // P7: done 事件携带的成本快照 + run_id
  cost?: AgentCostSnapshot
  run_id?: string
  // H14: intention 事件携带的意图识别结果
  intention?: AgentIntentionInfo
  // tool_deliver 事件专用字段：可见终端执行模式待交付命令
  tool_id?: string
  command?: string
  session_id?: string
  requires_confirm?: boolean
  /** tool_deliver 专用：整串命令是否被证明只读（prod 自动下发依据） */
  readonly?: boolean
  /** 只读证明命中的新规则 ID；旧白名单证明为空（不冒领） */
  readonly_rule?: string
  /** tool_deliver 专用：复合命令逐段拆解（意图 + 段级风险），单段命令为空数组 */
  segments?: Array<{ command: string; intent: string; level: string; note?: string }>
  // clarification_needed 事件专用字段
  tool_call_id?: string
  current_step?: number
  total_steps?: number
  question?: string
  options?: Array<{ id: string; label: string }>
  allow_custom?: boolean
  /** 全量步骤（后端一次推送）：前端本地逐步推进时按索引取题 */
  steps?: AgentClarificationStep[]
  /** clarification_done 专用：true = 等待超时 / 题目被回收后关闭向导（不是用户作答），
   *  run 已按未答继续，用户若仍想按原意图操作需要重新发起 */
  expired?: boolean
}

export interface AgentCostSnapshot {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cost_usd: number
  tool_call_count: number
  llm_call_count: number
  duration_ms: number
  tool_duration_ms: number
  iterations: number
}

/** H14: 意图识别结果（后端 intention.py classify() 输出） */
export interface AgentIntentionInfo {
  level: 'trivial' | 'simple' | 'moderate' | 'complex' | 'debug'
  task_type: 'query' | 'implement' | 'modify' | 'refactor' | 'debug' | 'research' | 'conversation'
  reasoning_budget: number | null
  allow_planning_phase: boolean
  confidence: number
}

/**
 * Agent 运行过程快照 — 用于固化到 ChatMessage.agentTrace 字段
 * 使历史会话切换后仍能显示该次运行的执行过程摘要
 */
export interface AgentTraceSnapshot {
  /**
   * 终态 —— S9-13：原只声明 completed | failed，而生产者写的是后端原始终态
   * （权威集合 contracts.TERMINAL_RUN_STATES：completed/failed/human_help/max_reached）。
   * 类型窄于能力面时，snapshot() 只能折叠，固化下来的记录就分不清「迭代耗尽」
   * 和「失败」—— 这两种结局用户下一步该做的事完全不同。
   */
  status: RunTerminalState
  /** 当前 Agent 状态机状态：thinking / acting / observing / completed / failed 等 */
  currentState: string
  /** 实际完成的迭代次数 */
  iterations: number
  /** 运行耗时（秒） */
  duration?: number
  /**
   * 用户主动中止。中止没有独立终态串，status 仍记 failed；这里是历史重载后
   * 区分「已中止」与「失败」的唯一依据。旧记录没有该字段时按 status 展示。
   */
  aborted?: boolean
  /** 工具调用记录数组（结构与 agentStore.toolCalls 一致） */
  toolCalls: Array<{
    id: string
    tool: string
    args: Record<string, unknown>
    result?: string
    success?: boolean
    timestamp: number
    iteration?: number
    /** 后端实测耗时；内联过程按批次求和展示。旧记录没有该字段时组头只显示步号 */
    durationMs?: number
  }>
  /** 思考过程文本（reasoning 累积） */
  reasoning: string
  /** 关联的 agent_run id（用于打开 TraceViewer） */
  lastRunId: string
  /** 成本快照 */
  costInfo?: AgentCostSnapshot
  /** 失败时的错误信息 */
  errorMessage?: string
}

export interface StreamAgentOptions {
  task: string
  conversationId: string
  modelId: string
  systemPrompt?: string
  sshSessionId?: string
  environment?: 'test' | 'prod'
  enableThinking?: boolean
  enableTools?: boolean
  maxIterations?: number
  enableDynamicBudget?: boolean
  /** E1：意图识别 + 思考预算控制，关闭 = 降级为无规划阶段 / 无引导语注入 */
  enableIntentionGuide?: boolean
  /** 成本熔断上限（USD）。不传 = 后端默认；显式 0 = 后端解释为「不设上限」 */
  maxCostUsd?: number
  /** token 熔断上限。不传 = 后端默认；显式 0 = 不设上限 */
  maxTotalTokens?: number
  skillIds?: string[]
  /** 是否启用触发词自动技能匹配（设置"自动技能匹配"，默认开启） */
  autoSkillMatch?: boolean
  /** 历史上下文条数（设置"上下文窗口"，1-100） */
  contextWindow?: number
  visibleTerminal?: boolean
  /**
   * 对话姿态（goal/plan）—— 后端据此注入对应的姿态 system prompt。
   * undefined = 未表态，字段省略、后端不注入任何姿态段（chip 的 × 走这条）。
   */
  workflowMode?: 'goal' | 'plan'
  /**
   * 权限模式（normal=手动审批 / auto=模型代判 / plan=只读强制）。
   * 'plan' 必须与 workflowMode **成对**下发：只发姿态提示词时，用户以为进了
   * 计划模式，写操作却照样执行（「亮的空开关」）。
   * 'auto' 仅放宽审批卡的「谁来点头」那一维（fail-closed 在后端裁决器），
   * 不越过 hardline / prod 预授权 / 用户拒绝记忆任何一条边界。缺省 normal。
   */
  permissionMode?: PermissionModeKey
  /**
   * 长任务结构化（spec 2026-10-24-task-structuring）：本次下达是否为已采用的结构稿。
   * 不传 = 未增强（老行为不变）；enhanced 为真时后端把原文留进审计 agent_trace 供对照。
   */
  enhanced?: boolean
  /** 结构化前的原文（仅 enhanced 为真时有意义），后端落进用户消息 agent_trace。 */
  sourceRawText?: string
  /** 结构稿含"新增写步骤"：后端据此把 auto 审批 fail-closed 回落 normal。 */
  enhancedHasAddedWriteSteps?: boolean
  onEvent: (event: AgentSSEEvent) => void
  onError: (error: string) => void
  onDone: () => void
}

/** 连接停滞看门狗超时：后端每 15s 推一行 ': heartbeat' 注释（backend/routes/agent.py
 *  HEARTBEAT_INTERVAL=15），连续 6 个心跳周期（90s）收不到任何字节即判为黑洞连接
 *  （休眠唤醒 / NAT 静默丢流 / 无 RST）—— read 永不返回，run 会永远停在 running、
 *  审批卡挂着、计时无限涨。到点主动 cancel 并报错，把决定权交还给用户（可重试）。 */
const STALL_TIMEOUT_MS = 90_000
/** 看门狗巡检步长：停滞判定粒度 = 90s + 至多一个巡检周期，约 95s 内必报 */
const STALL_CHECK_INTERVAL_MS = 5_000

export function streamAgentRun(options: StreamAgentOptions): () => void {
  const abortController = new AbortController()

  const run = async () => {
    try {
      // 惰性获取 token 后拼入 SSE 请求头；abort 仍可在 fetch 前生效（AbortController 已同步创建）
      await ensureApiToken()
      const url = `${getBaseUrl()}/agent/run`
      const headers: Record<string, string> = { 'Content-Type': 'application/json' }
      const res = await fetch(url, {
        method: 'POST',
        headers: withApiToken(headers),
        body: JSON.stringify({
          task: options.task,
          conversation_id: options.conversationId,
          model_id: options.modelId,
          system_prompt: options.systemPrompt,
          ssh_session_id: options.sshSessionId,
          environment: options.environment || 'test',
          // 未表态时省略该字段（后端 None = 允许演化配置接管）；
          // 原实现 ?? false 会把「没设置」压成「显式关闭」，用户无法再关也无法让演化生效
          ...(options.enableThinking === undefined
            ? {}
            : { enable_thinking: options.enableThinking }),
          enable_tools: options.enableTools ?? true,
          max_iterations: options.maxIterations ?? 100,
          enable_dynamic_budget: options.enableDynamicBudget ?? true,
          enable_intention_guide: options.enableIntentionGuide ?? true,
          // 显式 0 也要发出去：`options.maxCostUsd ? …` 会把「关闭熔断」这个合法
          // 取值连同 0 一起抹掉，于是后端永远收不到禁用指令（仍按 $2 熔断）。
          ...(options.maxCostUsd !== undefined ? { max_cost_usd: options.maxCostUsd } : {}),
          ...(options.maxTotalTokens !== undefined ? { max_total_tokens: options.maxTotalTokens } : {}),
          skill_ids: options.skillIds || [],
          auto_skill_match: options.autoSkillMatch ?? true,
          context_window: options.contextWindow ?? 20,
          visible_terminal: options.visibleTerminal ?? false,
          // 未表态时省略该字段（后端 None = 不注入任何姿态段）。
          // 与上方 enable_thinking 同一教训：`?? 'goal'` 会把「用户按 × 关掉了」
          // 压成「又选了一次目标」，chip 消失了但行为什么都没变 —— 能关、关的是摆设。
          ...(options.workflowMode === undefined
            ? {}
            : { workflow_mode: options.workflowMode }),
          permission_mode: options.permissionMode ?? PERMISSION_MODE_DEFAULT,
          // 结构化增强：仅在有意义时发（未增强不污染既有请求体）。raw 只在 enhanced 时带。
          ...(options.enhanced ? { enhanced: true } : {}),
          ...(options.enhanced && options.sourceRawText
            ? { source_raw_text: options.sourceRawText } : {}),
          ...(options.enhancedHasAddedWriteSteps
            ? { enhanced_has_added_write_steps: true } : {}),
        }),
        signal: abortController.signal,
      })

      if (!res.ok) {
        // 审查修复：onError 回调包 try/catch — 原实现回调自身抛异常时
        // 变成 unhandled rejection
        const err = await res.json().catch(() => ({ detail: res.statusText }))
        try {
          options.onError(backendErrorDetail(err) || res.statusText)
        } catch (cbErr) {
          console.error('[streamAgentRun] onError 回调异常:', cbErr)
        }
        return
      }

      if (!res.body) {
        options.onError('Response body is empty')
        return
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      // 连接停滞看门狗：黑洞连接（休眠唤醒 / NAT 静默丢流 / 无 RST）上挂起的 read
      // 永不返回，纯靠循环内判定无法自救 —— 只能由独立定时器巡检 lastActivityAt，
      // 停滞到点后 cancel 把挂起的 read 以 done 解开（WHATWG 流语义），循环才能退出。
      let lastActivityAt = Date.now()
      let stalled = false
      const watchdog = setInterval(() => {
        if (Date.now() - lastActivityAt < STALL_TIMEOUT_MS) return
        clearInterval(watchdog)
        stalled = true
        reader.cancel().catch(() => { /* already closed */ })
      }, STALL_CHECK_INTERVAL_MS)

      try {
        while (true) {
          const { done, value } = await reader.read()
          // 看门狗 cancel 会把挂起的 read 以 done 解开：必须先于 done 判定识别停滞，
          // 否则会落进下方「响应中断」分支，专属报错文案被吞
          if (stalled) {
            try {
              options.onError('连接停滞，请检查网络后重试')
            } catch (cbErr) {
              console.error('[streamAgentRun] onError callback threw:', cbErr)
            }
            return
          }
          if (done) break

          // 任何字节到达都算活动 —— ': heartbeat' 注释行同样走这里刷新，
          // 它是后端唯一保证的存活信号（backend/routes/agent.py 的 event_stream）
          lastActivityAt = Date.now()
          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''

          for (const line of lines) {
            if (line.startsWith('data: ')) {
              const data = line.slice(6)
              if (data === '[DONE]') {
                // 与 onError/onEvent 同一口径：回调自己抛异常不许冒到外层 catch ——
                // 那样会在一个流已经正常收尾之后再去调 onError，把终态改写成"失败"。
                try {
                  options.onDone()
                } catch (cbErr) {
                  console.error('[streamAgentRun] onDone 回调异常:', cbErr)
                }
                // 审查修复：显式取消底层读取，避免 [DONE] 后流仍占用连接直到服务端关闭
                reader.cancel().catch(() => { /* already closed */ })
                return
              }
              try {
                const parsed = JSON.parse(data) as AgentSSEEvent
                // H3 修复：onEvent 异常单独捕获，避免被当作 heartbeat 解析错误吞掉
                try {
                  options.onEvent(parsed)
                } catch (e) {
                  console.error('[streamAgentRun] onEvent 处理异常:', e, 'event:', parsed)
                }
              } catch {
                // JSON.parse 失败：心跳或非 JSON 数据，忽略
              }
            }
          }
        }

        // B22 修复：正常结束但没有 [DONE] = 半截 Agent 流，不能走 onDone —
        // 原实现会把未完成状态当完成，导致结果固化/状态错乱
        try {
          options.onError('响应中断')
        } catch (cbErr) {
          console.error('[streamAgentRun] onError callback threw:', cbErr)
        }
      } finally {
        // M5 修复：显式释放 reader，避免 abort 后底层流仍被占用导致资源泄漏
        try { reader.releaseLock() } catch { /* already released */ }
        // 看门狗只在读取循环存活期间有意义（[DONE] return / done / 停滞 return 都经过这里）
        clearInterval(watchdog)
      }
    } catch (e: unknown) {
      if (e instanceof Error && e.name !== 'AbortError') {
        // H13 修复：用 toFriendlyMessage 转换网络错误，避免直接显示 "Failed to fetch"
        // H15 包裹 onError 回调，防止回调自身抛异常导致 unhandled promise rejection
        try {
          options.onError(toFriendlyMessage(e, 'AI 运行失败，请稍后重试'))
        } catch (cbErr) {
          console.error('[streamAgentRun] onError callback threw:', cbErr)
        }
      }
    }
  }

  run()
  return () => abortController.abort()
}

/**
 * 可见终端执行模式：回传命令执行结果，恢复后端挂起的 Agent 循环。
 * 成功回传走 /agent/tool-result（后端 resume）；用户拒绝走同一接口但必须带
 * rejected=true（后端 reject）—— 只有该标志能让后端把结果写成
 * "[用户拒绝执行] <cmd>" 而非 "[已写入可见终端] <cmd>"，否则模型会以为命令已执行。
 */
export async function postToolResult(
  toolId: string,
  output: string,
  success: boolean,
  exitCode: number | null,
  rejected = false,
  runId?: string,
  confirmed = false,
): Promise<void> {
  await ensureApiToken()
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const url = `${getBaseUrl()}/agent/tool-result`
  const res = await fetch(url, {
    method: 'POST',
    headers: withApiToken(headers),
    // S1-03：run_id 让服务端能认出「认领对了但越权」的回传；confirmed 是对
    // requires_confirm 交付的显式主张（后端拒绝未经确认的闭合）
    body: JSON.stringify({
      tool_id: toolId,
      output,
      success,
      exit_code: exitCode,
      rejected,
      run_id: runId ?? null,
      confirmed,
    }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(backendErrorDetail(err) || res.statusText)
  }
}

