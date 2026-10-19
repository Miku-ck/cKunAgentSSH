/**
 * useAgentDeliver — 可见终端执行模式 · 交付编排器（模块级单例）
 *
 * 后端在可见终端模式下，会把 shell_exec 命令以 SSE 事件 `tool_deliver`
 * 推给前端，并挂起 ReAct 循环等待回传。本编排器负责：
 *
 * 1. 登记每次待交付命令（keyed by tool_id），维护响应式 deliveries 列表；
 * 2. requires_confirm 时进入 pending_confirm 供 UI 确认/拒绝；
 *    否则自动写入当前可见终端；
 * 3. 通过哨兵（BEGIN/END + 随机 marker）包一层命令，从终端回显里
 *    捕获命令输出，回传 POST /agent/tool-result 恢复后端循环；
 * 4. 用户拒绝 / 写入失败 / 超时等均回传失败结果，避免后端挂死。
 */
import { ref } from 'vue'
import { postToolResult, agentApi } from '@/api/client'
import { useSSHStore } from '@/stores/ssh'

type DeliveryStatus =
  | 'pending_confirm'
  | 'writing'
  | 'capturing'
  | 'done'
  | 'rejected'
  | 'aborted'
  | 'timeout'
  | 'failed'

/** 终态：到达后交付不再流转（确认/交付入口据此短路，防重复回传） */
const TERMINAL_STATUSES: ReadonlySet<DeliveryStatus> = new Set([
  'done',
  'rejected',
  'aborted',
  'timeout',
  'failed',
])

function isTerminalStatus(status: DeliveryStatus): boolean {
  return TERMINAL_STATUSES.has(status)
}

/** P2 修复：给一次性 IPC（postToolResult）加超时，避免后端/渲染层异常导致
 *  promise 永久挂起、阻塞交付状态机收尾（clearCapture / 终态流转）。超时后
 *  外层已推进，底层 IPC 若晚到也只是无人 await，不再拖累当前交付。 */
async function withTimeout<T>(p: Promise<T>, ms: number, label: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} 超时（${ms}ms）`)), ms)
  })
  try {
    return await Promise.race([p, timeout])
  } finally {
    if (timer) clearTimeout(timer)
  }
}
const POST_RESULT_TIMEOUT_MS = 15000

export interface ToolDelivery {
  tool_id: string
  command: string
  session_id: string
  requires_confirm: boolean
  status: DeliveryStatus
  output: string
  exit_code: number | null
  error?: string
  /** 问题 4：发起交付的 run_id（拒绝时用于标记 deny_command） */
  runId?: string
  /**
   * S1-03：本次交付是否走过 deliverCommand（自动交付，或用户点了确认）。
   * 回传时据此声明 confirmed —— 服务端对 requires_confirm 的交付只接受
   * 声明过确认的闭合，未经确认的失败上报不该冒充「用户已确认」。
   */
  confirmedByUi?: boolean
}

interface ToolDeliverPayload {
  tool_id: string
  command: string
  session_id: string
  requires_confirm: boolean
  runId?: string
}

/** 单次捕获的解析状态（进程内非响应式即可，纯文本解析用） */
interface CaptureState {
  tool_id: string
  session_id: string
  marker: string
  started: boolean
  /** 该会话是否收到过任何回显数据（超时诊断：区分订阅断连与哨兵未匹配） */
  receivedAny: boolean
  /** 是否命中过 BEGIN 哨兵（超时诊断：BEGIN 后断流 vs 哨兵行从未匹配上） */
  sawBegin: boolean
  /** 当前不完整的一行（缓存跨 chunk 撕裂的片段） */
  partialLine: string
  /** 已采集的输出（首行起，基于整行） */
  output: string
  finished: boolean
  timer: ReturnType<typeof setTimeout> | null
}

// ── 模块级状态 ──
const deliveries = ref<ToolDelivery[]>([])
/** session_id → tool_id 集合：允许同一会话并发多个捕获（按各自 marker 路由，互不干扰） */
const activeCaptureBySession: Record<string, Set<string>> = {}
/** tool_id → CaptureState */
const captures = new Map<string, CaptureState>()

const CAPTURE_TIMEOUT_MS = 60_000
const REJECT_OUTPUT = '[用户拒绝执行]'

// ── 全局捕获订阅（集中化）──
// 捕获链路原先依赖终端组件实例的 onSshData/onPtyData 订阅回喂数据：组件未挂载/
// 订阅丢失即无人喂 ingestOutput，5 条命令全部误判 60s 超时的线上事故即源于此。
// 改为模块级懒初始化订阅：首次交付时注册一次，随模块存活（不退订）。
let captureListenersRegistered = false

function ensureCaptureListeners(): void {
  if (captureListenersRegistered) return
  // node 测试环境无 window；渲染进程 electronAPI 也可能晚于首条交付就绪 —— 都留到下次再试
  const api = typeof window === 'undefined' ? undefined : window.electronAPI
  if (!api) return
  captureListenersRegistered = true
  api.onSshData(({ id, data }) => ingestOutput(id, data))
  api.onPtyData(({ id, data }) => ingestOutput(id, data))
}

// ── ANSI 转义剥离（哨兵匹配容错）──
// 终端回显常带 CSI 着色/光标序列与 OSC 标题序列，整行匹配对它们零容错会误判超时
const ANSI_CSI_RE = /\x1b\[[0-9;?]*[ -/]*[@-~]/g
const ANSI_OSC_RE = /\x1b\][^\x07]*(?:\x07|\x1b\\)/g

function stripAnsi(line: string): string {
  return line.replace(ANSI_CSI_RE, '').replace(ANSI_OSC_RE, '')
}

function randomMarker(): string {
  return Math.random().toString(36).slice(2, 10)
}

function findDelivery(toolId: string): ToolDelivery | undefined {
  return deliveries.value.find(d => d.tool_id === toolId)
}

function clearCapture(toolId: string): void {
  const cap = captures.get(toolId)
  if (!cap) return
  if (cap.timer) clearTimeout(cap.timer)
  captures.delete(toolId)
  const set = activeCaptureBySession[cap.session_id]
  if (set) {
    set.delete(toolId)
    if (set.size === 0) delete activeCaptureBySession[cap.session_id]
  }
}

async function finishAndPost(toolId: string, output: string, success: boolean, exitCode: number | null, rejected = false, timeout = false): Promise<void> {
  const delivery = findDelivery(toolId)
  if (delivery) {
    delivery.output = output
    delivery.exit_code = exitCode
    // 拒绝/中止是更明确的终态，不覆盖；其余按真实结果细分终态
    if (delivery.status !== 'rejected' && delivery.status !== 'aborted') {
      if (rejected) {
        delivery.status = 'rejected'
      } else if (timeout) {
        delivery.status = 'timeout'
      } else if (exitCode === 0) {
        delivery.status = 'done'
      } else {
        // exit_code 非零，或哨兵退出码不可解析（exitCode=null 且 success=false）
        delivery.status = 'failed'
      }
    }
  }
  // 通知父层状态机，完成即不再参与后续捕获
  clearCapture(toolId)
  try {
    await withTimeout(
      postToolResult(
        toolId,
        output,
        success,
        exitCode,
        rejected,
        delivery?.runId,
        delivery?.confirmedByUi === true,
      ),
      POST_RESULT_TIMEOUT_MS,
      'postToolResult',
    )
  } catch (err) {
    console.error('[useAgentDeliver] 回传 tool-result 失败:', err)
    const d = findDelivery(toolId)
    if (d) d.error = String((err as Error)?.message || err)
  }
}

/** 超时兜底：交互/挂起命令不阻塞后端，把已采集内容按失败回传 */
function armTimeout(delivery: ToolDelivery): void {
  const cap = captures.get(delivery.tool_id)
  if (!cap) return
  cap.timer = setTimeout(() => {
    const c = captures.get(delivery.tool_id)
    if (!c || c.finished) return
    c.finished = true
    // 结构化诊断：区分「没收到任何数据（订阅/会话问题）」与「收到数据但没匹配上（哨兵匹配问题）」
    console.warn('[useAgentDeliver] 捕获超时（未见 END 哨兵）', {
      tool_id: delivery.tool_id,
      session_id: delivery.session_id,
      marker: c.marker,
      receivedAny: c.receivedAny,
      sawBegin: c.sawBegin,
      active_capture_sessions: Object.keys(activeCaptureBySession),
      capture_listeners_registered: captureListenersRegistered,
    })
    const note = '\n[执行超时：已超过 ' + CAPTURE_TIMEOUT_MS / 1000 + 's 无返回，结束捕获并回传]'
    void finishAndPost(delivery.tool_id, (c.output || '') + note, false, null, false, true)
  }, CAPTURE_TIMEOUT_MS)
}

/**
 * 把命令写入可见终端并按 BEGIN/END 哨兵开始捕获。
 * 自动交付与确认通过共用此路径。
 */
async function deliverCommand(toolId: string): Promise<void> {
  const delivery = findDelivery(toolId)
  if (!delivery) return
  if (isTerminalStatus(delivery.status)) return
  // 走到这里就意味着这次交付经过了确认环节（requires_confirm 时由用户点击，
  // 否则本就无需确认）——回传时的 confirmed 主张以此为据（S1-03）
  delivery.confirmedByUi = true

  const ssh = useSSHStore()
  const session = ssh.sessions.find(s => s.id === delivery.session_id)
  if (!session) {
    delivery.status = 'done'
    delivery.output = '无可写终端'
    void withTimeout(
      postToolResult(
        toolId, '无可写终端', false, null, false, delivery.runId, delivery.confirmedByUi === true,
      ),
      POST_RESULT_TIMEOUT_MS, 'postToolResult',
    ).catch(() => {})
    return
  }

  delivery.status = 'writing'

  const marker = randomMarker()
  const sentinel =
    `echo __AGENT_BEGIN__ ${marker}; ${delivery.command}; echo __AGENT_END__ ${marker}:$?`

  // 先登记捕获，避免错过回显
  const cap: CaptureState = {
    tool_id: toolId,
    session_id: delivery.session_id,
    marker,
    started: false,
    receivedAny: false,
    sawBegin: false,
    partialLine: '',
    output: '',
    finished: false,
    timer: null,
  }
  captures.set(toolId, cap)
  const capSet = activeCaptureBySession[delivery.session_id] ?? new Set<string>()
  capSet.add(toolId)
  activeCaptureBySession[delivery.session_id] = capSet
  armTimeout(delivery)

  const api = window.electronAPI
  if (!api) {
    const c0 = captures.get(toolId)
    if (c0) c0.finished = true
    clearCapture(toolId)
    delivery.status = 'done'
    delivery.output = 'Electron API 未就绪，无法写入终端'
    void withTimeout(
      postToolResult(
        toolId, 'Electron API 未就绪，无法写入终端', false, null, false,
        delivery.runId, delivery.confirmedByUi === true,
      ),
      POST_RESULT_TIMEOUT_MS, 'postToolResult',
    ).catch(() => {})
    return
  }
  const writer = session.type === 'local' ? api.ptyWrite : api.sshWrite
  try {
    await writer(delivery.session_id, sentinel + '\r')
  } catch (err) {
    console.error('[useAgentDeliver] 写入终端失败:', err)
    const c = captures.get(toolId)
    if (c) c.finished = true
    clearCapture(toolId)
    delivery.status = 'done'
    delivery.output = '写入终端失败'
    delivery.error = String((err as Error)?.message || err)
    void withTimeout(
      postToolResult(
        toolId, '写入终端失败', false, null, false, delivery.runId, delivery.confirmedByUi === true,
      ),
      POST_RESULT_TIMEOUT_MS, 'postToolResult',
    ).catch(() => {})
    return
  }

  // writer 在途期间这一轮 run 可能已收尾（abortRun / done 会把该交付置终态并
  // clearCapture，把定时器与 captures 记录一起摘掉）。那时不能再把状态改回
  // 'capturing'：命令已经写进活终端，而这一行会永远显示「捕获中」，直到下次
  // run 的 resetDeliveries 才消失。
  if (isTerminalStatus(delivery.status) || !captures.has(toolId)) return

  delivery.status = 'capturing'
}

/**
 * 入口：agent store 收到 tool_deliver 事件时调用。
 * requires_confirm → 挂起等待 UI 确认/拒绝；否则自动交付。
 */
function handleDeliver(payload: ToolDeliverPayload): void {
  // 捕获订阅集中化：首次交付即建立全局 onSshData/onPtyData 订阅（幂等）
  ensureCaptureListeners()
  const existing = findDelivery(payload.tool_id)
  if (existing) return // 去重
  const delivery: ToolDelivery = {
    tool_id: payload.tool_id,
    command: payload.command,
    session_id: payload.session_id,
    requires_confirm: payload.requires_confirm,
    status: 'pending_confirm',
    output: '',
    exit_code: null,
    runId: payload.runId,
  }
  deliveries.value.push(delivery)
  // requires_confirm 的命令停在 pending_confirm 等卡片确认；其余直接写入终端
  if (!payload.requires_confirm) void deliverCommand(payload.tool_id)
}

/** UI 确认执行 */
function confirmDeliver(toolId: string): void {
  const delivery = findDelivery(toolId)
  if (!delivery || delivery.status !== 'pending_confirm') return
  void deliverCommand(toolId)
}

/** UI 拒绝执行：不回写命令，直接回传失败 */
async function rejectDeliver(toolId: string): Promise<void> {
  const delivery = findDelivery(toolId)
  // 只拒绝还在等确认的交付：命令一旦写入终端，结果已由捕获回传，再 POST 一次
  // 等于给同一个 tool_id 发两条终态（快速双击「拒绝」即命中）
  if (!delivery || delivery.status !== 'pending_confirm') return
  clearCapture(toolId)
  delivery.status = 'rejected'
  delivery.output = REJECT_OUTPUT
  delivery.exit_code = null
  // rejected=true：让后端走 reject() 而非 resume()，
  // 否则模型侧收到「[已写入可见终端] rm -rf …」，误判破坏性命令已执行
  await finishAndPost(toolId, REJECT_OUTPUT, false, null, true)
  // 问题 4：通知后端标记该命令被拒绝，避免 LLM 重试时再次弹卡片
  if (delivery.command && delivery.runId) {
    agentApi.denyCommand('shell_exec', delivery.command, delivery.runId).catch((err: unknown) => {
      console.warn('[useAgentDeliver] denyCommand 失败（不阻塞）:', err)
    })
  }
}

/**
 * 把一段输出喂给单个活动捕获做解析。提取为独立函数以便同会话并发捕获时
 * 逐个分发：每个捕获按各自 marker 路由，互不直接读写对方的 output。
 * 依赖后端同会话串行执行（一个捕获的 BEGIN…END 区间不与其他捕获嵌套重叠），
 * 否则并发捕获的输出行可能被错误归集——这是既有串行化假设的延续。
 */
function parseCaptureChunk(toolId: string, data: string): void {
  const cap = captures.get(toolId)
  if (!cap || cap.finished) return

  cap.receivedAny = true
  cap.partialLine += data
  while (true) {
    const nl = cap.partialLine.indexOf('\n')
    if (nl === -1) break
    // 匹配用清理后的行：剥 ANSI 转义再 trim，容忍回显着色与 \r\n 行尾
    const line = stripAnsi(cap.partialLine.slice(0, nl)).trim()
    cap.partialLine = cap.partialLine.slice(nl + 1)

    if (!cap.started) {
      // 整行全等，或以哨兵结尾（容忍提示符粘连在行首）；
      // 回显的原始命令行（含 `__AGENT_BEGIN__ xxx;` 带分号）不满足两种匹配，天然排除
      const beginLine = `__AGENT_BEGIN__ ${cap.marker}`
      if (line === beginLine || line.endsWith(beginLine)) {
        cap.started = true
        cap.sawBegin = true
        cap.output = ''
      }
      continue
    }
    const endPrefix = `__AGENT_END__ ${cap.marker}:`
    if (line.startsWith(endPrefix)) {
      const codeStr = line.slice(endPrefix.length).trim()
      const exitCode = codeStr === '' ? 0 : Number(codeStr)
      cap.finished = true
      const captured = cap.output.trim()
      const success = Number.isFinite(exitCode) && exitCode === 0
      void finishAndPost(cap.tool_id, captured, success, Number.isFinite(exitCode) ? exitCode : null)
      return
    }
    // 跳过哨兵起始行以外的杂项行（如提示符/空行）
    if (line === `__AGENT_BEGIN__ ${cap.marker}`) {
      cap.output = ''
      continue
    }
    if (cap.output) cap.output += '\n'
    cap.output += line
  }
}

/**
 * 入口：全局 onSshData/onPtyData 订阅把每个输出 chunk 喂进来。
 * 按 session 找到全部活动捕获并逐一分发（同一会话可并发多个捕获，按各自 marker 路由），
 * 修复原先「后发的捕获覆盖单插槽、导致前一个捕获的输出被孤儿化、只能等 60s 超时」的问题。
 * 仍单独导出供单测直接驱动；无活动捕获时立即返回。
 */
function ingestOutput(sessionId: string, data: string): void {
  const set = activeCaptureBySession[sessionId]
  if (!set || set.size === 0) return
  // 复制一份：解析过程中 finishAndPost → clearCapture 会改 set，避免迭代异常
  for (const toolId of [...set]) {
    parseCaptureChunk(toolId, data)
  }
}

/** 中止整个 run：标记所有活动交付为 aborted 并清除捕获（不发回传，避免尾巴 POST） */
function abortRun(runId?: string): void {
  for (const d of deliveries.value) {
    if (d.status === 'pending_confirm' || d.status === 'writing' || d.status === 'capturing') {
      d.status = 'aborted'
      clearCapture(d.tool_id)
    }
  }
  // 问题 1 兜底：用户主动停止 run 时，释放该 run 未消费的 once 授权
  //（ReflectionRein 只在正常终态触发，用户中止可能绕过 AGENT_FAILED 清理）
  if (runId) {
    agentApi.releaseRunPermissions(runId).catch((err: unknown) => {
      console.warn('[useAgentDeliver] releaseRunPermissions 失败（不阻塞）:', err)
    })
  }
}

/**
 * 新 run 起手清理：丢弃上一轮的交付徽章与其捕获定时器。
 * store 的 _reset() 会清空 toolCalls/permissionRequests，唯独漏这里 ——
 * 上一轮"已完成 rm -rf …"的徽章会串到新一轮面板里，且列表逐轮累积不清。
 */
function resetDeliveries(): void {
  for (const toolId of [...captures.keys()]) clearCapture(toolId)
  deliveries.value = []
}

export function useAgentDeliver() {
  return {
    deliveries,
    handleDeliver,
    confirmDeliver,
    rejectDeliver,
    ingestOutput,
    abortRun,
    resetDeliveries,
  }
}

// 便捷直出：终端组件只需按 chunk 喂数据，无需构造编排器实例
export { ingestOutput }