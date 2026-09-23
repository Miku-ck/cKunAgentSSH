/**
 * SSH/本地终端命令补全 composable
 *
 * 职责：
 * 1. 从 xterm 缓冲区读取当前命令行输入
 * 2. 剥离 prompt 得到用户输入
 * 3. 解析输入段，判断补全类型（命令/路径）
 * 4. 通过 IPC 查询候选
 * 5. 去重排序返回
 */
import type { Terminal } from '@xterm/xterm'
import { useSSHStore } from '@/stores/ssh'
import { api } from '@/api/client'
import { useSettingsStore } from '@/stores/settings'

export interface CompletionCandidate {
  text: string
  type: 'command' | 'file' | 'dir' | 'history'
  /** 历史命令执行次数（仅 history 类型有值，弹窗显示统计） */
  count?: number
}

// ── 历史命令 fuzzy 联想（E 阶段） ──
// 纯函数：字符子序列匹配（"lslat" 命中 "ls -lat"），词首/连续字符加分，
// 短命令与词首命中靠前。无外部依赖，可独立单测。

/**
 * 在历史命令中做 fuzzy 匹配（前缀命中是 fuzzy 的特例，得分更高）。
 * @param commands 历史命令列表（去重后的完整命令）
 * @param query 用户已输入的片段
 * @param limit 最多返回条数
 * @returns 按匹配得分降序排列的 [命令, 得分] 列表
 */
export function fuzzyMatchCommand(
  commands: string[],
  query: string,
  limit = 10,
): Array<{ command: string; score: number }> {
  const q = query.toLowerCase()
  if (!q) return []
  // 单字符不触发 fuzzy 联想：一个字母会命中几乎全部命令，纯属噪音。
  // 用户明确输入 ≥2 字符后再开始匹配（与 CompletionPopup 的展示阈值一致）。
  if (q.length < 2) return []
  const results: Array<{ command: string; score: number }> = []
  for (const cmd of commands) {
    const lower = cmd.toLowerCase()
    const startsWith = lower.startsWith(q)
    let qi = 0
    let score = 0
    let lastIdx = -1
    for (let i = 0; i < lower.length && qi < q.length; i++) {
      if (lower[i] === q[qi]) {
        // 词首（命令/参数开头、行首、空白后）加分 — 让 "git l" 优先命中 "git log"
        const wordStart = i === 0 || /[\s=\/:-]/.test(lower[i - 1])
        // 连续字符（紧跟上一个命中字符）加分
        const consecutive = i === lastIdx + 1
        score += 2 + (wordStart ? 3 : 0) + (consecutive ? 2 : 0)
        lastIdx = i
        qi++
      }
    }
    if (qi === q.length) {
      // 前缀精确命中大幅加分；短命令更具体，轻微加分
      if (startsWith) score += 10
      score += Math.max(0, 8 - Math.floor(lower.length / 10))
      results.push({ command: cmd, score })
    }
  }
  results.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score
    // 同分时短命令优先（"ls" 排在 "ls -lat" 前，更精确的命中排前）
    return a.command.length - b.command.length
  })
  return results.slice(0, limit)
}

export interface CompletionInput {
  /** 用户完整输入（已剥离 prompt） */
  line: string
  /** 光标在 line 中的位置（0-based） */
  cursorPos: number
  /** 当前要补全的 token（光标所在 token） */
  prefix: string
  /** 当前是命令段（第 1 段）还是参数段 */
  isCommand: boolean
  /** 命令名（第 1 段，参数补全时用） */
  cmd: string
  /** 第 2 段开始的所有段（不含 cmd），用于 bash-completion 上下文 */
  prevSegments: string[]
}

// 自定义 OSC 6973 标记序列（注入到 prompt 末尾，用于精确定位命令起点）
const OSC_MARK = '\x1b]6973;\x07'
// 回退正则：匹配常见 prompt 末尾分隔符 $ # > %，后可跟空白或位于行尾
const PROMPT_TAIL_RE = /[#$>%](?:\s+|$)/g

/** 去除 ANSI 转义序列（CSI / OSC / 其他单字节 escape） */
function stripAnsi(text: string): string {
  return text
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '') // CSI
    .replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, '') // OSC
    .replace(/\x1b[@-Z\\-_]/g, '') // 其他 escape
}

/** 估算单个字符在终端中占据的列宽（考虑全角/半角/组合字符） */
function charWidth(char: string): number {
  const code = char.codePointAt(0) ?? 0
  // 组合字符宽度为 0
  if (
    (code >= 0x0300 && code <= 0x036f) ||
    (code >= 0x1ab0 && code <= 0x1aff) ||
    (code >= 0x1dc0 && code <= 0x1dff) ||
    (code >= 0x20d0 && code <= 0x20ff) ||
    (code >= 0xfe20 && code <= 0xfe2f)
  ) {
    return 0
  }
  // 控制字符宽度为 0
  if (code < 0x20 || code === 0x7f) return 0
  // CJK / 全角字符宽度为 2
  if (
    (code >= 0x1100 && code <= 0x115f) ||
    (code >= 0x2e80 && code <= 0x9fff) ||
    (code >= 0xa960 && code <= 0xa97f) ||
    (code >= 0xac00 && code <= 0xd7af) ||
    (code >= 0xf900 && code <= 0xfaff) ||
    (code >= 0xfe10 && code <= 0xfe19) ||
    (code >= 0xfe30 && code <= 0xfe6f) ||
    (code >= 0xff01 && code <= 0xff5e) ||
    (code >= 0xffe0 && code <= 0xffe6) ||
    (code >= 0x20000 && code <= 0x2fffd) ||
    (code >= 0x30000 && code <= 0x3fffd)
  ) {
    return 2
  }
  return 1
}

/** 计算字符串在终端中的显示宽度 */
function displayWidth(text: string): number {
  let width = 0
  for (const char of text) {
    width += charWidth(char)
  }
  return width
}

/**
 * 将终端列坐标转换为字符串索引（处理全角/多字节字符）。
 * 用于把 xterm 的 cursorX（列）转成 JS 字符串索引，供 parseInput 使用。
 */
export function columnToIndex(text: string, col: number): number {
  if (col <= 0) return 0
  let idx = 0
  let currentCol = 0
  for (const char of text) {
    const w = charWidth(char)
    if (currentCol + w > col) break
    currentCol += w
    idx += char.length
  }
  return idx
}

/**
 * 从 xterm 缓冲区读取当前行并剥离 prompt
 */
export function readCurrentLine(terminal: Terminal): { line: string; cursorPos: number } | null {
  const buffer = terminal.buffer.active
  const cursorY = buffer.cursorY
  const cursorX = buffer.cursorX
  const line = buffer.getLine(cursorY)
  if (!line) return null
  const fullText = line.translateToString(true)
  return { line: fullText, cursorPos: cursorX }
}

/**
 * 剥离 prompt，返回用户输入和命令起点列号（均为终端显示列坐标）。
 *
 * 策略：
 * 1. 优先在原始行中查找 OSC 6973 标记，标记后即为用户输入起点。
 * 2. 无标记时回退正则，匹配最后一个 [$#>%] 分隔符（后可跟空白或行尾）。
 * 3. 仍无匹配时按行首空白缩进处理。
 *
 * 验证用例：
 * - 'user@host:~/dir$ git '        -> input 'git ',     startCol 17
 * - 'root@host:/# ls '             -> input 'ls ',      startCol 13
 * - '> git'                        -> input 'git',      startCol 2
 * - 'PS C:\\> git'                 -> input 'git',      startCol 8
 * - 'C:\\> git'                    -> input 'git',      startCol 5
 * - '用户@主机:~$ git '             -> input 'git ',     startCol 13（中文按宽 2 计）
 * - '\x1b[32muser:~$\x1b[0m git'  -> input 'git',      startCol 8
 * - '\x1b]6973;\x07git'           -> input 'git',      startCol 0
 * - 'user@host:~$'                 -> input '',         startCol 12
 */
export function stripPrompt(line: string): { input: string; startCol: number } {
  // 1. 优先用 OSC 标记（在原始行中匹配，避免被 stripAnsi 误伤）
  const oscIdx = line.indexOf(OSC_MARK)
  if (oscIdx >= 0) {
    const visiblePrompt = stripAnsi(line.slice(0, oscIdx))
    const startCol = displayWidth(visiblePrompt)
    const input = stripAnsi(line.slice(oscIdx + OSC_MARK.length))
    return { input, startCol }
  }

  // 2. 回退：去除 ANSI 后，找行内最后一个 prompt 分隔符
  const clean = stripAnsi(line)
  const promptMatches = [...clean.matchAll(PROMPT_TAIL_RE)]
  if (promptMatches.length > 0) {
    const last = promptMatches[promptMatches.length - 1]
    const promptEnd = (last.index ?? 0) + last[0].length
    const visiblePrompt = clean.slice(0, promptEnd)
    const startCol = displayWidth(visiblePrompt)
    const input = clean.slice(promptEnd)
    return { input, startCol }
  }

  // 3. 最终回退：按行首空白缩进处理
  const input = clean.trimStart()
  const leading = clean.slice(0, clean.length - input.length)
  const startCol = displayWidth(leading)
  return { input, startCol }
}

/**
 * 解析输入行，提取当前补全 token 信息
 */
export function parseInput(line: string, cursorPos: number): CompletionInput | null {
  if (!line) return null
  // 光标位置在剥离后的 input 内
  const beforeCursor = line.slice(0, cursorPos)

  // P3 修复：按"空白运行"（/\s+/，含多空格/Tab）切分，而非单个空格字符。
  // 原 lastIndexOf(' ') 只在单空格处定位边界 — 光标前有多空格/Tab 间隔时
  // prefix 边界取错（如 "ls\t-a" 无空格会整个 token 被当成 prefix）。
  // prefix = 光标前末尾的连续非空白（当前正在输入的 token）。
  const prefix = (beforeCursor.match(/\S+$/) || [''])[0]

  // 命令前的部分（不含当前 token），剥掉 token 前的一段空白
  const beforeToken = beforeCursor.slice(0, beforeCursor.length - prefix.length).trim()
  // 段数：beforeToken 按空白分割
  const segments = beforeToken ? beforeToken.split(/\s+/).filter(Boolean) : []

  const isCommand = segments.length === 0
  const cmd = segments.length > 0 ? segments[0] : ''

  return {
    line,
    cursorPos,
    prefix,
    isCommand,
    cmd,
    // 第 2 段开始的所有段（不含 cmd），用于 bash-completion 上下文
    prevSegments: segments.slice(1),
  }
}

/** 补充历史命令候选（命令段且有前缀时触发；E 阶段改为 fuzzy 联想 + 次数统计） */
function queryHistoryCandidates(
  sessionId: string,
  input: CompletionInput,
  nativeCandidates: CompletionCandidate[],
): CompletionCandidate[] {
  if (!input.isCommand || !input.prefix) return []

  const store = useSSHStore()
  // 本会话历史命令做 fuzzy 匹配（前缀命中是特例且得分更高）
  const matched = fuzzyMatchCommand(
    store.getSessionCommands(sessionId),
    input.prefix,
    10,
  )
  if (matched.length === 0) return []

  const seen = new Set(nativeCandidates.map(c => c.text))
  const result: CompletionCandidate[] = []
  for (const m of matched) {
    if (seen.has(m.command)) continue
    seen.add(m.command)
    result.push({
      text: m.command,
      type: 'history',
      count: store.getCommandCount(m.command, sessionId),
    })
  }
  return result
}

/** 补充命令链推荐候选（命令段且无前缀时，基于上一个命令推荐下一个） */
function queryChainCandidates(
  sessionId: string,
  input: CompletionInput,
  nativeCandidates: CompletionCandidate[],
  lastCmd: string | undefined,
): CompletionCandidate[] {
  if (!input.isCommand || input.prefix || !lastCmd) return []

  const store = useSSHStore()
  const chains = store.queryCommandChainCandidates(sessionId, lastCmd, 5)
  if (chains.length === 0) return []

  const seen = new Set(nativeCandidates.map(c => c.text))
  const result: CompletionCandidate[] = []
  for (const chain of chains) {
    if (seen.has(chain.nextCmd)) continue
    seen.add(chain.nextCmd)
    result.push({ text: chain.nextCmd, type: 'history' })
  }
  return result
}

/** 补充 AI 预测候选（命令段且无前缀时，基于上下文预测下一步命令） */
/** AI 预测的本地等待预算（毫秒），超过就先出本地候选 */
const AI_PREDICT_BUDGET_MS = 4000
/** 正在等 AI 预测的会话（防止连按 Tab 叠加 LLM 请求） */
const _aiPredictInFlight = new Set<string>()

async function queryAIPredictions(
  sessionId: string,
  input: CompletionInput,
  nativeCandidates: CompletionCandidate[],
): Promise<CompletionCandidate[]> {
  if (!input.isCommand || input.prefix) return []

  const settings = useSettingsStore()
  if (!settings.settings.appearance?.terminal?.aiPredictionEnabled) return []

  const store = useSSHStore()
  const lastCmd = store.getLastCommand(sessionId)
  if (!lastCmd) return []

  if (_aiPredictInFlight.has(sessionId)) return []
  // 后端对这个 LLM 往返的保护是 15s，而补全弹窗是同步等它结果的 —— 慢模型会把
  // Tab 补全整个卡住。本地只给 4s 预算，超时先返回本地候选。
  const predict = api.predictTerminalCommand({
    session_id: sessionId,
    current_input: input.line || '',
    recent_commands: store.getRecentCommands(sessionId, 10),
    cwd: '',
    platform: typeof navigator !== 'undefined' && navigator.userAgent?.includes('Windows') ? 'windows' : 'linux',
  })
  // 闸门在请求真正结束时才放开（不是 4s 放弃结果时）—— 否则连按 Tab 会叠加 LLM 请求
  _aiPredictInFlight.add(sessionId)
  void predict.catch(() => null).finally(() => { _aiPredictInFlight.delete(sessionId) })
  try {
    const result = await Promise.race([
      predict,
      new Promise<null>(resolve => setTimeout(() => resolve(null), AI_PREDICT_BUDGET_MS)),
    ])
    if (!result?.predictions?.length) return []

    const seen = new Set(nativeCandidates.map(c => c.text))
    const aiCandidates: CompletionCandidate[] = []
    for (const pred of result.predictions) {
      const trimmed = pred.trim()
      if (!trimmed || seen.has(trimmed)) continue
      seen.add(trimmed)
      aiCandidates.push({ text: trimmed, type: 'command' })
    }
    return aiCandidates
  } catch {
    return []
  }
}

// 修复 + 审查 M-20：请求序号按【会话】隔离，用于取消过期请求。
// 原实现是模块级单一计数器 — 用户在终端 A 打字期间切到终端 B 触发一次补全，
// A 的在途请求返回时因 B 的序号更大而被误丢弃（两个会话互相取消对方的补全）。
// 现在每个 sessionId 独立计数，只取消本会话的更旧请求。
const _completionRequestIds = new Map<string, number>()

/**
 * 查询补全候选
 * @param side 'local' | 'remote'
 * @param sessionId 会话 ID（本地为 ptyId，远程为 sshId）
 * @param cwd 本地工作目录（本地必填）
 * @param input 补全输入
 * @param shell 本地 shell 标识（Windows 上区分 powershell/cmd）
 */
export async function queryCompletion(
  side: 'local' | 'remote',
  sessionId: string,
  cwd: string,
  input: CompletionInput,
  shell?: string,
): Promise<CompletionCandidate[]> {
  const api = window.electronAPI
  if (!api) return []

  // 审查 M-20：按会话生成请求序号，用于取消本会话的过期请求
  const requestId = (_completionRequestIds.get(sessionId) ?? 0) + 1
  _completionRequestIds.set(sessionId, requestId)

  try {
    let stdout: string
    if (side === 'local') {
      if (!sessionId) return []
      stdout = await api.ptyCompleteCommand({
        ptyId: sessionId,
        cmd: input.cmd,
        prefix: input.prefix,
        cwd,
        isCommand: input.isCommand,
        prevSegments: input.prevSegments || [],
        shell,
      })
    } else {
      if (!sessionId) return []
      stdout = await api.sshCompleteCommand(sessionId, {
        cmd: input.cmd,
        prefix: input.prefix,
        isCommand: input.isCommand,
        prevSegments: input.prevSegments || [],
      })
    }

    // 解析 stdout：每行 "type:text"
    // 后端只输出 file:（文件目录）和 arg:（bash-completion 函数提供的子命令补全，如 git add）
    // 用户需求：只显示 文件目录/路径 + 历史记录命令
    // 1) 过滤 function/alias 类型（后端已不输出，前端兜底）
    // 2) 过滤双下划线开头的 bash 内部辅助函数
    // 3) 过滤纯标点符号（如 . : [ [[ ]] { } !）
    const lines = stdout.split('\n').map(s => s.trim()).filter(Boolean)
    const seen = new Set<string>()
    const candidates: CompletionCandidate[] = []
    // 优先级权重：command=0（bash-completion 子命令）, dir=1, file=2, history=3
    const rank: Record<string, number> = { command: 0, dir: 1, file: 2, history: 3 }
    const typeMap: Record<string, CompletionCandidate['type']> = {
      arg: 'command',  // bash-completion 函数提供的子命令补全（如 git add/commit）
      file: 'file',
      dir: 'dir',
    }
    // 用户明确不需要的类型
    const hiddenTags = new Set(['func', 'alias', 'cmd'])

    for (const ln of lines) {
      // 解析 "type:text" 格式
      const colonIdx = ln.indexOf(':')
      let typeTag = 'file'
      let text = ln
      if (colonIdx > 0 && colonIdx < 10) {
        const tag = ln.slice(0, colonIdx)
        // 跳过 func/alias/cmd 类型
        if (hiddenTags.has(tag)) continue
        if (tag in typeMap) {
          typeTag = tag
          text = ln.slice(colonIdx + 1)
        }
      }
      if (!text) continue
      // 过滤 bash 内部辅助函数（双下划线开头）
      if (text.startsWith('__')) continue
      // 过滤纯标点符号（如 . : [ [[ ]] { } !）作为兜底
      if (/^\p{Punctuation}+$/u.test(text)) continue
      if (seen.has(text)) continue
      seen.add(text)
      let type: CompletionCandidate['type'] = typeMap[typeTag] || 'file'
      if (text.endsWith('/')) {
        type = 'dir'
      }
      candidates.push({ text, type })
    }

    // 排序：命令类型优先，同类型内按频率+时间衰减加权排序
    candidates.sort((a, b) => {
      const ra = rank[a.type] ?? 9
      const rb = rank[b.type] ?? 9
      if (ra !== rb) return ra - rb
      // 同类型时，history 类型按频率+时间衰减排序
      if (a.type === 'history' && b.type === 'history') {
        const store = useSSHStore()
        const scoreA = store.getCommandScore(a.text, sessionId)
        const scoreB = store.getCommandScore(b.text, sessionId)
        if (scoreB !== scoreA) return scoreB - scoreA
      }
      return a.text.localeCompare(b.text)
    })

    // 命令段且有前缀时，补充历史命令候选
    if (sessionId && input.isCommand && input.prefix) {
      const historyCandidates = queryHistoryCandidates(sessionId, input, candidates)
      candidates.push(...historyCandidates)
    }

    // 命令段且无前缀时，基于上一个命令推荐下一个命令（命令链推荐）
    if (sessionId && input.isCommand && !input.prefix) {
      const store = useSSHStore()
      const lastCmd = store.getLastCommand(sessionId)
      const chainCandidates = queryChainCandidates(sessionId, input, candidates, lastCmd)
      candidates.push(...chainCandidates)

      // AI 智能预测：基于上下文预测下一步命令
      const aiCandidates = await queryAIPredictions(sessionId, input, candidates)
      candidates.push(...aiCandidates)
    }

    // 审查 M-20：请求序号检查（按会话），如果本会话已有更新的请求发出，丢弃本次结果
    if (requestId !== _completionRequestIds.get(sessionId)) {
      return []
    }

    return candidates
  } catch (e) {
    // 修复：记录错误日志，便于排查 IPC 通信失败等问题
    console.error('[completion] 查询补全失败:', e)
    return []
  }
}

/**
 * C27 修复：会话关闭时清理该会话的请求序号，_completionRequestIds 不再无界增长。
 * 由终端组件在 onPtyExit / onSshClose 现有钩子中调用。
 */
export function releaseCompletionSession(sessionId: string): void {
  if (!sessionId) return
  _completionRequestIds.delete(sessionId)
  _aiPredictInFlight.delete(sessionId)
}

/**
 * 计算补全后需要发送的剩余字符
 * @param candidate 用户选中的候选词
 * @param prefix 当前已输入的前缀
 * @param isUnique 是否唯一候选（唯一则追加空格）
 * @returns 需要发送到 shell 的字符串
 */
export function buildCompletionText(candidate: string, prefix: string, isUnique: boolean): string {
  if (!candidate.startsWith(prefix)) {
    // 候选不以 prefix 开头（如补全是路径展开），直接发送完整候选 + 空格
    return candidate + (isUnique ? ' ' : '')
  }
  const remaining = candidate.slice(prefix.length)
  return remaining + (isUnique ? ' ' : '')
}
