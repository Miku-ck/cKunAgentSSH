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
import { ref, reactive } from 'vue'
import { postToolResult } from '@/api/client'
import { useSSHStore } from '@/stores/ssh'

export type DeliveryStatus =
  | 'pending_confirm'
  | 'writing'
  | 'capturing'
  | 'done'
  | 'rejected'
  | 'aborted'

export interface ToolDelivery {
  tool_id: string
  command: string
  session_id: string
  requires_confirm: boolean
  status: DeliveryStatus
  output: string
  exit_code: number | null
  error?: string
}

interface ToolDeliverPayload {
  tool_id: string
  command: string
  session_id: string
  requires_confirm: boolean
}

/** 单次捕获的解析状态（进程内非响应式即可，纯文本解析用） */
interface CaptureState {
  tool_id: string
  session_id: string
  marker: string
  started: boolean
  /** 当前不完整的一行（缓存跨 chunk 撕裂的片段） */
  partialLine: string
  /** 已采集的输出（首行起，基于整行） */
  output: string
  finished: boolean
  timer: ReturnType<typeof setTimeout> | null
}

// ── 模块级状态 ──
const deliveries = ref<ToolDelivery[]>([])
/** session_id → tool_id：当前每个会话的活动捕获 */
const activeCaptureBySession = reactive<Record<string, string>>({})
/** tool_id → CaptureState */
const captures = new Map<string, CaptureState>()

const CAPTURE_TIMEOUT_MS = 60_000
const REJECT_OUTPUT = '[用户拒绝执行]'

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
  if (activeCaptureBySession[cap.session_id] === toolId) {
    delete activeCaptureBySession[cap.session_id]
  }
}

async function finishAndPost(toolId: string, output: string, success: boolean, exitCode: number | null): Promise<void> {
  const delivery = findDelivery(toolId)
  if (delivery) {
    delivery.output = output
    delivery.exit_code = exitCode
    // 拒绝/中止是更明确的终态，不覆盖回 done
    if (delivery.status !== 'rejected' && delivery.status !== 'aborted') {
      delivery.status = 'done'
    }
  }
  // 通知父层状态机，完成即不再参与后续捕获
  clearCapture(toolId)
  try {
    await postToolResult(toolId, output, success, exitCode)
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
    const note = '\n[执行超时：已超过 ' + CAPTURE_TIMEOUT_MS / 1000 + 's 无返回，结束捕获并回传]'
    void finishAndPost(delivery.tool_id, (c.output || '') + note, false, null)
  }, CAPTURE_TIMEOUT_MS)
}

/**
 * 把命令写入可见终端并按 BEGIN/END 哨兵开始捕获。
 * 自动交付与确认通过共用此路径。
 */
async function deliverCommand(toolId: string): Promise<void> {
  const delivery = findDelivery(toolId)
  if (!delivery) return
  if (delivery.status === 'done' || delivery.status === 'rejected') return

  const ssh = useSSHStore()
  const session = ssh.sessions.find(s => s.id === delivery.session_id)
  if (!session) {
    delivery.status = 'done'
    delivery.output = '无可写终端'
    void postToolResult(toolId, '无可写终端', false, null).catch(() => {})
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
    partialLine: '',
    output: '',
    finished: false,
    timer: null,
  }
  captures.set(toolId, cap)
  activeCaptureBySession[delivery.session_id] = toolId
  armTimeout(delivery)

  const api = window.electronAPI
  if (!api) {
    const c0 = captures.get(toolId)
    if (c0) c0.finished = true
    clearCapture(toolId)
    delivery.status = 'done'
    delivery.output = 'Electron API 未就绪，无法写入终端'
    void postToolResult(toolId, 'Electron API 未就绪，无法写入终端', false, null).catch(() => {})
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
    void postToolResult(toolId, '写入终端失败', false, null).catch(() => {})
    return
  }

  delivery.status = 'capturing'
}

/**
 * 入口：agent store 收到 tool_deliver 事件时调用。
 * requires_confirm → 挂起等待 UI 确认/拒绝；否则自动交付。
 */
function handleDeliver(payload: ToolDeliverPayload): void {
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
  }
  deliveries.value.push(delivery)
  if (payload.requires_confirm) {
    delivery.status = 'pending_confirm'
  } else {
    // 危险命令确认后或自动交付
    void deliverCommand(payload.tool_id)
  }
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
  if (!delivery) return
  clearCapture(toolId)
  delivery.status = 'rejected'
  delivery.output = REJECT_OUTPUT
  delivery.exit_code = null
  await finishAndPost(toolId, REJECT_OUTPUT, false, null)
}

/**
 * 纯文本输出解析：终端组件把每个输出 chunk 喂进来。
 * 无活动捕获时立即返回（开销可忽略）。
 */
function ingestOutput(sessionId: string, data: string): void {
  const toolId = activeCaptureBySession[sessionId]
  if (!toolId) return
  const cap = captures.get(toolId)
  if (!cap || cap.finished) return

  cap.partialLine += data
  while (true) {
    const nl = cap.partialLine.indexOf('\n')
    if (nl === -1) break
    const line = cap.partialLine.slice(0, nl).trim()
    cap.partialLine = cap.partialLine.slice(nl + 1)

    if (!cap.started) {
      // 精确整行匹配，规避终端回显原始命令行造成的误判
      if (line === `__AGENT_BEGIN__ ${cap.marker}`) {
        cap.started = true
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
      void finishAndPost(toolId, captured, success, Number.isFinite(exitCode) ? exitCode : null)
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

/** 中止整个 run：标记所有活动交付为 aborted 并清除捕获（不发回传，避免尾巴 POST） */
function abortRun(): void {
  for (const d of deliveries.value) {
    if (d.status === 'pending_confirm' || d.status === 'writing' || d.status === 'capturing') {
      d.status = 'aborted'
      clearCapture(d.tool_id)
    }
  }
}

export function useAgentDeliver() {
  return {
    deliveries,
    handleDeliver,
    confirmDeliver,
    rejectDeliver,
    ingestOutput,
    abortRun,
  }
}

// 便捷直出：终端组件只需按 chunk 喂数据，无需构造编排器实例
export { ingestOutput }