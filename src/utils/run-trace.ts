/**
 * 运行视图的轨迹派生层（spec 2026-09-29-home-command-panel · 接缝 S1a）
 *
 * 输入只有 agentStore.toolCalls 这一条扁平流水，输出两个视角：左栏按 iteration
 * 归批的时间线、右栏按到达序排布的事件流。两个消费者必须读同一份输出 —— 分组
 * 口径各写一遍，就会出现「时间线第 3 步与事件流第 3 张卡指向不同调用」这种错位。
 *
 * 与 store 的接口是**结构型**的（TraceInputEntry 只声明本模块读的那几个字段），
 * 所以不需要把 agent.ts 的私有 ToolCallEntry 导出来，也不会逼着固化快照
 * （AgentTraceSnapshot）跟着长出这些字段 —— 快照那条路只服务聊天气泡，没有
 * 时长字段可读。
 */

/** 本模块实际读到的字段；agent store 的 ToolCallEntry 结构上满足它 */
export interface TraceInputEntry {
  id: string
  tool: string
  args?: Record<string, unknown>
  /** undefined = 未回填（在途）；空串是"跑完了、没输出"，两者不同 */
  result?: string
  /** undefined = 结果还没回来时的未知，不等于失败 */
  success?: boolean
  timestamp: number
  iteration?: number
  /** 后端实测耗时；undefined = 没量过，0 = 量到 0（见 R0 的同名口径） */
  durationMs?: number
}

export type TraceStepState = 'done' | 'failed' | 'running'

/** 事件流里的一条：原条目 + 它属于时间线第几步（点击定位用） */
export interface TraceEventView extends TraceInputEntry {
  stepIndex: number
}

export interface TraceStepView {
  /** 批次号 = 后端 agent 循环的 iteration；null = 这批条目没带 iteration。
   *  run-head 的「第 n 步」读它，而不是读 steps.length —— 只思考不调工具的迭代
   *  不产生条目，用数组下标当步号会跳号。 */
  iteration: number | null
  state: TraceStepState
  /** 这批占 events 里的哪几格（到达序内的下标） */
  eventIndexes: number[]
  /** 量到的耗时之和；一条都没量到时是 undefined —— 不是 0 */
  durationMs?: number
  /** 时长求和的分母：这批有几条真被量过。小于 eventIndexes.length
   *  即"部分计时"，视图据此标灰，而不是拿一个偏小的总数当全批耗时。 */
  measuredCount: number
}

export interface TraceModel {
  events: TraceEventView[]
  steps: TraceStepView[]
  /** 最新一个在途批次的下标；没有在途批次时 -1。run 收尾后应回落到 -1，
   *  否则定格视图里最后一行还在闪"进行中"。 */
  activeStepIndex: number
}

const NO_ITERATION = '__no_iteration__'

export function buildTraceModel(entries: readonly TraceInputEntry[]): TraceModel {
  const events: TraceEventView[] = []
  // 按首次出现归批：后端重发同一 iteration 时并进原批，而 events 不因归批重排
  const buckets = new Map<number | typeof NO_ITERATION, number[]>()

  entries.forEach((entry, index) => {
    const key = entry.iteration ?? NO_ITERATION
    const bucket = buckets.get(key)
    if (bucket) bucket.push(index)
    else buckets.set(key, [index])
    events.push({ ...entry, stepIndex: -1 })
  })

  const steps: TraceStepView[] = []
  for (const [key, indexes] of buckets) {
    let state: TraceStepState = 'done'
    let durationMs: number | undefined
    let measuredCount = 0
    for (const i of indexes) {
      const e = events[i]
      // 顺序：未回填 > 失败 > 完成。一条没回来的批谈不上成败。
      if (e.result === undefined) state = 'running'
      else if (e.success === false && state !== 'running') state = 'failed'
      if (typeof e.durationMs === 'number') {
        durationMs = (durationMs ?? 0) + e.durationMs
        measuredCount++
      }
    }
    const stepIndex = steps.length
    for (const i of indexes) events[i].stepIndex = stepIndex
    steps.push({
      iteration: key === NO_ITERATION ? null : key,
      state,
      eventIndexes: indexes,
      durationMs,
      measuredCount,
    })
  }

  let activeStepIndex = -1
  steps.forEach((s, i) => { if (s.state === 'running') activeStepIndex = i })

  return { events, steps, activeStepIndex }
}

/** 待决审批的最小形状 —— agent store 的 PermissionRequest 结构上满足它 */
export interface TraceApprovalLike {
  id: string
  tool: string
  command?: string
  timestamp: number
}

/** 带审批项的流元素。A 用实际形状（agent 的 PermissionRequest）参数化，
 *  消费方拿回来还能直接喂给审批卡组件，不用为了过类型再断言一次。 */
export type TraceStreamItem<A extends TraceApprovalLike = TraceApprovalLike> =
  | { kind: 'tool'; key: string; ts: number; stepIndex: number; entry: TraceEventView }
  | { kind: 'approval'; key: string; ts: number; req: A }

/**
 * 把"工具调用"与"待你放行"归并成一条按时间往下长的流。
 *
 * 为什么不能把审批卡挂到"它挡住的那条调用"上：GateRein 是 PRE_TOOL_CALL 卡点
 * （backend/agent/harness/reins/gate.py:79），permission_needed 在
 * tool_call_start **之前** 就发出来了（driver.py:518 发事件、525 才发起调用）——
 * 卡片出现的那一刻，前端根本还没有那条调用可挂。所以只能各按自己的时间戳落位。
 */
export function mergeStream<A extends TraceApprovalLike>(
  model: TraceModel,
  approvals: readonly A[],
): TraceStreamItem<A>[] {
  const items: TraceStreamItem<A>[] = model.events.map(e => ({
    kind: 'tool',
    key: `t:${e.id}`,
    ts: e.timestamp,
    stepIndex: e.stepIndex,
    entry: e,
  }))
  for (const req of approvals) {
    items.push({ kind: 'approval', key: `a:${req.id}`, ts: req.timestamp, req })
  }
  // 同毫秒时（工具刚回填、审批紧随其后）不让两类的相对次序随机：调用在前
  return items.sort((a, b) => a.ts - b.ts
    || (a.kind === b.kind ? 0 : a.kind === 'tool' ? -1 : 1))
}
