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

export interface CompletionCandidate {
  text: string
  type: 'command' | 'file' | 'dir' | 'history'
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

export interface CompletionResult {
  candidates: CompletionCandidate[]
  input: CompletionInput
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

  // 找光标前最后一个空白
  const lastSpaceIdx = beforeCursor.lastIndexOf(' ')

  const prefix = beforeCursor.slice(lastSpaceIdx + 1)

  // 命令前的部分（不含当前 token）
  const beforeToken = beforeCursor.slice(0, lastSpaceIdx + 1).trim()
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

/** 补充历史命令候选（仅在命令段且有前缀时触发） */
function queryHistoryCandidates(
  sessionId: string,
  input: CompletionInput,
  nativeCandidates: CompletionCandidate[],
): CompletionCandidate[] {
  if (!input.isCommand || !input.prefix) return []

  const store = useSSHStore()
  const entries = store.queryHistoryCandidates(sessionId, input.prefix, 10)
  if (entries.length === 0) return []

  const seen = new Set(nativeCandidates.map(c => c.text))
  const result: CompletionCandidate[] = []
  for (const entry of entries) {
    if (seen.has(entry.command)) continue
    seen.add(entry.command)
    result.push({ text: entry.command, type: 'history' })
  }
  return result
}

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

  try {
    let stdout: string
    if (side === 'local') {
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

    // 排序
    candidates.sort((a, b) => {
      const ra = rank[a.type] ?? 9
      const rb = rank[b.type] ?? 9
      if (ra !== rb) return ra - rb
      return a.text.localeCompare(b.text)
    })

    // 命令段且有前缀时，补充历史命令候选
    if (sessionId && input.isCommand && input.prefix) {
      const historyCandidates = queryHistoryCandidates(sessionId, input, candidates)
      candidates.push(...historyCandidates)
    }

    return candidates
  } catch {
    return []
  }
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
