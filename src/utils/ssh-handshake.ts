/**
 * ssh-handshake — 新建远程终端的「shell 就绪」确认式握手
 *
 * connectRemote 之后写命令的时机此前靠固定延时（500ms）：慢主机（登录横幅 /
 * .profile 执行超过该值）时命令与横幅交错、执行结果静默不可辨。这里改为探针
 * 确认：先写一条带一次性标记的 echo，从输出流里等到标记（shell 真正执行了
 * 探针）再写真命令；探针超时回落为固定延时照常写入，并由调用方提示用户。
 * DockerView.doExec 与 GitReposPanel.onTerm 两处调用点共用这一份实现。
 *
 * 注意保留一段最短等待（SHELL_GRACE_FLOOR_MS）：新终端标签的 RemoteTerminal
 * 只订阅实时流、无回放，快主机上探针毫秒级即回，若立刻写真命令，标签还没挂载
 * 订阅完，命令回显与输出会在屏幕上整段丢失 —— 固定延时原来还兼着「等标签挂载」
 * 的职责，不能删；它与探针等待并行取较晚者，快主机行为与原实现一致。
 */

// 终端回显常带 CSI 着色/OSC 标题序列，整体匹配对它们零容错会误判超时，
// 故匹配前剥掉；强实现（CSI+OSC+Fe）收敛在 shared/ansi-core.ts
import { stripAnsi } from '../../shared/ansi-core'

/** 探针等待上限：超过即视为就绪无法确认（超长横幅/登录挂起），回落固定延时 */
export const HANDSHAKE_TIMEOUT_MS = 5000

/**
 * 新终端标签挂载订阅的地板：命令回显不丢帧的最低保障。即 connectRemote 后的
 * 原 500ms 固定延时 —— 语义从「猜 shell 就绪」收窄为「等组件订阅」，就绪判定
 * 交给探针回显。
 */
export const SHELL_GRACE_FLOOR_MS = 500

/** 会话 IO 抽象：写入与订阅输出流（注入以便测试，运行时经 sshSessionIo 组装） */
export interface ShellSessionIo {
  write: (data: string) => Promise<unknown>
  /** 订阅该会话的输出流；返回退订函数 */
  onData: (cb: (chunk: string) => void) => () => void
}

export interface HandshakeOptions {
  /** 覆盖探针等待上限（缺省 HANDSHAKE_TIMEOUT_MS） */
  timeoutMs?: number
}

/** 从全局 electronAPI 组装某条 SSH 会话的 IO；API 未就绪（测试环境/极早期渲染）返回 null */
export function sshSessionIo(sessionId: string): ShellSessionIo | null {
  const api = typeof window === 'undefined' ? undefined : window.electronAPI
  if (!api) return null
  return {
    write: data => api.sshWrite(sessionId, data),
    onData: cb => api.onSshData(({ id, data }) => { if (id === sessionId) cb(data) }),
  }
}

/** 一次性标记：时间戳 + 随机段，登录横幅/提示符不可能预先包含这串组合 */
export function makeHandshakeMarker(): string {
  return `ckunrdy${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`
}

/**
 * 探针行：标记拆两段、中间插引号。终端对输入的回显是逐字原样，两段之间永远
 * 隔着引号；只有 shell 真正执行 echo、把两段拼接输出时，连续的标记串才会出现。
 * 「看到连续标记」因此等于「shell 已执行探针」而非「PTY 回显了输入」—— 慢主机
 * 在横幅期间就把输入回显出来也不会误判就绪。行尾 \r 在这里统一带上。
 */
export function buildProbeLine(token: string): string {
  const half = Math.ceil(token.length / 2)
  return `echo ${token.slice(0, half)}"${token.slice(half)}"\r`
}

/** 匹配缓冲上限：握手只需最近几屏输出，滚动窗口防止超长横幅把缓冲无界累加 */
const ECHO_BUFFER_CAP = 8192

/**
 * 握手等待逻辑（可测纯函数）：订阅输出流并累积，剥 ANSI 后找连续标记串；
 * 等到即 resolve(true) 并退订，超时 resolve(false)。探针的写入由调用方负责，
 * 且须在本函数返回后（订阅已同步建立）进行，才不会漏掉探针的回显与输出。
 */
export function awaitProbeEcho(
  onData: (cb: (chunk: string) => void) => () => void,
  token: string,
  timeoutMs: number,
): Promise<boolean> {
  return new Promise(resolve => {
    let buf = ''
    let timer: ReturnType<typeof setTimeout> | undefined
    let unsubscribe: () => void = () => {}
    const finish = (ok: boolean): void => {
      clearTimeout(timer)
      unsubscribe()
      resolve(ok)
    }
    unsubscribe = onData(chunk => {
      buf = (buf + chunk).slice(-ECHO_BUFFER_CAP)
      if (stripAnsi(buf).includes(token)) finish(true)
    })
    timer = setTimeout(() => finish(false), timeoutMs)
  })
}

/**
 * 确认式握手整体编排：写探针 → 等回显（与挂载地板并行）→ 写真命令。
 * 返回 confirmed=false 表示探针超时、shell 就绪未能确认（已按固定延时回落写入），
 * 调用方应据此提示用户；探针本身写入失败（会话/通道已坏）原样抛出，真命令不再
 * 尝试 —— 与此前直接 sshWrite 失败的路径一致。探针写入抛错时探针等待仍会挂到
 * 超时才退订（它只会 resolve 不会 reject，无未处理拒绝），属可接受的短暂残留。
 */
export async function writeAfterShellReady(
  io: ShellSessionIo,
  command: string,
  opts: HandshakeOptions = {},
): Promise<{ confirmed: boolean }> {
  const token = makeHandshakeMarker()
  // 先订阅（awaitProbeEcho 同步登记）后写探针：探针的回显/输出一条不能漏，
  // 先于探针到达的横幅与本次握手无关
  const probe = awaitProbeEcho(io.onData, token, opts.timeoutMs ?? HANDSHAKE_TIMEOUT_MS)
  await io.write(buildProbeLine(token))
  // 探针等待与挂载地板并行：快主机上地板保证新标签先挂载订阅（回显不丢帧），
  // 慢主机上探针等待更长、地板早已耗尽不额外拖延；探针超时即按原固定延时回落
  const [confirmed] = await Promise.all([probe, delay(SHELL_GRACE_FLOOR_MS)])
  await io.write(command)
  return { confirmed }
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}
