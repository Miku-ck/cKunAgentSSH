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
// 哨兵匹配前的 ANSI 剥离（回显常带 CSI 着色/光标序列与 OSC 标题序列）：
// 强实现（CSI+OSC+Fe）收敛在 shared/ansi-core.ts
import { stripAnsi } from '../../shared/ansi-core'

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

/** 拆解层单段：意图 + 段级风险 + 危险段后果（复合命令才有，单段命令缺省） */
export interface DeliverySegment {
  command: string
  intent: string
  level: string
  note?: string
}

export interface ToolDelivery {
  tool_id: string
  command: string
  session_id: string
  requires_confirm: boolean
  /** 整串命令是否被证明只读（prod 是否自动下发的依据） */
  readonly?: boolean
  /** 只读证明命中的新规则 ID；旧白名单证明为空 */
  readonly_rule?: string
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
  /** 命令后果的中文说明（后端解释层随 tool_deliver 下发，纯展示不参与判定） */
  explain?: string
  /** 说明来源：static=静态后果表 fallback=通用兜底 llm=模型生成（仅供参考） */
  explain_source?: string
  /** 复合命令逐段拆解（后端拆解层，纯展示；单段命令缺省） */
  segments?: DeliverySegment[]
  /** 展示用聚合风险（safe/dangerous/hardline）；权威判定在后端授权链 */
  risk?: string
  /** 发起本次命令的任务原话（卡片上下文行，让用户核对命令服务于他的请求） */
  task?: string
  /** 最近一次回传的载荷：自动重试耗尽后供「重发结果」复用（捕获态已清，只存这里） */
  lastPost?: PostPayload
  /** 回传最终失败（自动重试耗尽）：调用方据此呈现「重发结果」入口，不再死路 */
  postFailed?: boolean
}

interface ToolDeliverPayload {
  tool_id: string
  command: string
  session_id: string
  requires_confirm: boolean
  readonly?: boolean
  readonly_rule?: string
  runId?: string
  explain?: string
  explain_source?: string
  segments?: DeliverySegment[]
  risk?: string
  task?: string
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
  /** 已采集的输出（首行起，基于整行；受 CAPTURE_OUTPUT_LIMIT 约束） */
  output: string
  /** 输出是否已达上限停止累加（截断标记只追加一次；超限后只等 END 哨兵收尾） */
  truncated: boolean
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
/** 捕获输出上限（字符数，近似 256KB）：cat 大文件 / yes / tail -f 的回显会在
 *  60s 捕获窗口内逐行无界累加到几十 MB，既撑渲染层内存，又把全量内容
 *  postToolResult 回传进轨迹 —— 达到上限即停止累加，只保留一条截断标记。 */
const CAPTURE_OUTPUT_LIMIT = 256 * 1024
// 措辞与 docker-core 的 EXEC_TRUNCATED_NOTE 同款：让用户/模型一眼看出结果不完整
const CAPTURE_TRUNCATED_NOTE = '[输出已截断，超过 256KB 上限]'
/** partialLine（跨 chunk 的不完整行）的硬上限：与完整行同量级（256KB 口径）。
 *  base64/二进制回显整段无换行时，partialLine 在 60s 捕获窗口内无界累积到
 *  几十 MB —— 超限即截断，已过 BEGIN 的内容属于交付物，须显式打截断标记。 */
const PARTIAL_LINE_LIMIT = 256 * 1024

/** 回传自动重试：初始尝试外最多再补 POST_RETRY_MAX 次，按 1s/2s 退避。
 *  有限次 —— 后端对同一 tool_id 的待交付在 TTL 后清理，无限重试只会刷日志。 */
const POST_RETRY_MAX = 2
const POST_RETRY_BASE_MS = 1000

/** 一次回传的完整载荷（自动重试与手动重发共用同一份） */
interface PostPayload {
  output: string
  success: boolean
  exitCode: number | null
  rejected: boolean
}

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

/** 404 形态的失败：后端已没有这个待交付（本次回传其实已闭合，或 TTL 已清）
 *  —— 重试必然同样 404，不再耗退避次数。 */
function isPostGone(err: unknown): boolean {
  const msg = String((err as Error)?.message || err || '')
  return /tool_id 未知|404|Not Found/i.test(msg)
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

/**
 * 回传一次结果，失败按有限次退避自动重试（Q-F6）。全部失败时把交付置为
 * postFailed（可见的「可手动重发」状态），不再停留在仅 console.error 的死路
 * —— 否则后端挂起的循环只能干等 TTL。
 * 返回 true = 后端确认收到。
 */
async function postResultWithRetry(toolId: string, payload: PostPayload): Promise<boolean> {
  const delivery = findDelivery(toolId)
  if (delivery) delivery.lastPost = { ...payload }
  for (let attempt = 0; ; attempt++) {
    try {
      await withTimeout(
        postToolResult(
          toolId,
          payload.output,
          payload.success,
          payload.exitCode,
          payload.rejected,
          delivery?.runId,
          delivery?.confirmedByUi === true,
        ),
        POST_RESULT_TIMEOUT_MS,
        'postToolResult',
      )
      const d = findDelivery(toolId)
      if (d) {
        d.postFailed = false
        d.error = undefined
      }
      return true
    } catch (err) {
      const gone = isPostGone(err)
      console.error(`[useAgentDeliver] 回传 tool-result 失败（第 ${attempt + 1} 次${gone ? '，后端已无此待交付' : ''}）:`, err)
      const d = findDelivery(toolId)
      // 重试间隙新一轮 run 可能已 resetDeliveries —— 找不到记录就别再折腾
      if (gone || !d || attempt >= POST_RETRY_MAX) {
        if (d) {
          d.postFailed = true
          d.error = gone
            ? '结果回传失败：后端已没有这个待交付命令（可能已恢复或超时清除），无需重发'
            : `结果回传失败（已自动重试 ${POST_RETRY_MAX} 次）：${String((err as Error)?.message || err)}。后端循环可能仍在等待本条结果，可点击「重发结果」`
        }
        return false
      }
      await sleep(POST_RETRY_BASE_MS * (attempt + 1))
    }
  }
}

/** 手动重发：自动重试耗尽后的恢复入口（Q-F6）。只对 postFailed 的交付生效。 */
async function retryPostResult(toolId: string): Promise<boolean> {
  const d = findDelivery(toolId)
  if (!d || !d.postFailed || !d.lastPost) return false
  d.postFailed = false
  return postResultWithRetry(toolId, d.lastPost)
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
  await postResultWithRetry(toolId, { output, success, exitCode, rejected })
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
/** 交付哨兵模板：按 shell 家族生成 BEGIN/END 包裹行（导出仅为测试缝）。
 *
 * CMD 的退出码必须写 `%%ERRORLEVEL%%` 并经 `call echo` 二次展开：cmd.exe 对
 * `&` 连接的整行在执行前做一次性的解析期展开，直接写 `%ERRORLEVEL%` 携带的
 * 是命令执行前的旧值（干净会话恒报成功、有过失败则后续全误报失败）。
 * PowerShell 的 `$LASTEXITCODE` 对纯 cmdlet 不更新（会话起始为空时 END 行
 * 渲染成 `m:`，被解析端折成 exit 0）——cmdlet 失败可能报成功，属已知取舍。 */
export function sentinelFor(
  shellFamily: 'posix' | 'cmd' | 'powershell',
  marker: string,
  command: string,
): string {
  if (shellFamily === 'cmd') {
    return `echo __AGENT_BEGIN__ ${marker} & ${command} & call echo __AGENT_END__ ${marker}:%%ERRORLEVEL%%`
  }
  if (shellFamily === 'powershell') {
    return `echo __AGENT_BEGIN__ ${marker}; ${command}; echo __AGENT_END__ ${marker}:$LASTEXITCODE`
  }
  return `echo __AGENT_BEGIN__ ${marker}; ${command}; echo __AGENT_END__ ${marker}:$?`
}

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
    // 失败终态置 failed：状态机的失败侧终态就是 failed（done 只属于捕获成功）
    delivery.status = 'failed'
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
  // 哨兵模板按会话 shell 分派（模板见 sentinelFor）：本地会话可能是
  // PowerShell / CMD（LOCAL_SHELLS），纯 bash 哨兵在 PowerShell 下 $? 求值为
  // True/False（Number('True')=NaN，每条成功命令都被回传为失败），在 CMD 下
  // `;` 不是分隔符（BEGIN 永不匹配，每条命令白等超时）。远程会话与
  // Git Bash/WSL 均为 POSIX shell，走 bash 模板。
  const isPosixShell = session.type !== 'local'
    || session.shell === 'Git Bash'
    || session.shell === 'WSL'
    || !session.shell
  const sentinel = sentinelFor(
    isPosixShell ? 'posix' : session.shell === 'CMD' ? 'cmd' : 'powershell',
    marker,
    delivery.command,
  )

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
    truncated: false,
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
    // 失败终态置 failed：状态机的失败侧终态就是 failed（done 只属于捕获成功）
    delivery.status = 'failed'
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
    // 失败终态置 failed：状态机的失败侧终态就是 failed（done 只属于捕获成功）
    delivery.status = 'failed'
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
    readonly: payload.readonly,
    readonly_rule: payload.readonly_rule,
    status: 'pending_confirm',
    output: '',
    exit_code: null,
    runId: payload.runId,
    explain: payload.explain,
    explain_source: payload.explain_source,
    segments: payload.segments,
    risk: payload.risk,
    task: payload.task,
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
      cap.truncated = false
      continue
    }
    // 超限即停：哨兵匹配在上面已完成，END 仍能正常收尾（或走超时兜底），
    // 之后到来的回显行不再累加，截断标记也只追加这一次
    if (cap.truncated) continue
    if (cap.output.length + line.length > CAPTURE_OUTPUT_LIMIT) {
      cap.truncated = true
      if (cap.output) cap.output += '\n'
      cap.output += CAPTURE_TRUNCATED_NOTE
      continue
    }
    if (cap.output) cap.output += '\n'
    cap.output += line
  }
  // Q-F6：partialLine 硬上限（与完整行 256KB 同口径），挂在排空循环之后 ——
  // 只对「真正没有换行收尾的残留」生效（base64/二进制回显整段无换行时，
  // 残留会在 60s 捕获窗口内无界累积到几十 MB）；带换行的整块数据在这里早已
  // 排空，不会误触发。已过 BEGIN 的行内容属于交付物：截断必须显式打标记
  // （复用输出侧的 CAPTURE_TRUNCATED_NOTE，只加一次）；未过 BEGIN 的行不进
  // 交付（BEGIN 处 output 归零），静默丢弃即可，免得标记被 BEGIN 清掉。
  if (cap.partialLine.length > PARTIAL_LINE_LIMIT) {
    cap.partialLine = cap.partialLine.slice(0, PARTIAL_LINE_LIMIT)
    if (cap.started && !cap.truncated) {
      cap.truncated = true
      if (cap.output) cap.output += '\n'
      cap.output += CAPTURE_TRUNCATED_NOTE
    }
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
    // Q-F6：回传失败的恢复入口（面板对 postFailed 的交付提供「重发结果」）
    retryPostResult,
  }
}

// 便捷直出：终端组件只需按 chunk 喂数据，无需构造编排器实例
export { ingestOutput }