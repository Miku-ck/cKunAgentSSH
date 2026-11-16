/**
 * 远程 exec 通道登记表 —— bridge `ssh_kill` op 的查杀依据（C-review M9 落地）。
 *
 * 背景：ssh2 的 client.exec 句柄只在回调参数里，原先没有任何外部登记，run 取消时
 * 后端只能中断等待、远端命令继续跑到自然结束。现在 execCommandWithExitCode 在
 * client.exec() 调用时按后端生成的 execId 登记，kill 到达时按 id 查杀。
 *
 * 两个窗口的处理：
 * - 通道未建立（kill 早于回调）：登记 pendingKill，回调到达时立即补杀 —— 窗口从
 *   「杀不到」收窄为「延迟杀」；
 * - sshd 不支持 exec 通道 signal：signal 抛错时降级为 stream.close()（连接关闭
 *   通常令远端进程收 SIGHUP），两层都失败则如实上报，绝不假装杀成功。
 *
 * 本类只管登记与查杀，不持有 ssh2 真实类型（KillableStream 结构化最小面），
 * 因此可以脱离 SSH 连接独立单测。
 */

/** exec 通道的最小查杀面（ssh2 ClientChannel 的 signal/close 子集） */
export interface KillableStream {
  signal(name: string): void
  close(): void
}

export interface KillOutcome {
  /** 是否存在这条 exec 的登记（false = 已结束或从未存在） */
  found: boolean
  /** 是否已向通道发出 KILL 信号 */
  signaled: boolean
  /** 通道尚未建立、已登记 pendingKill（回调到达时补杀） */
  pending: boolean
}

interface ExecRegEntry {
  stream: KillableStream | null
  pendingKill: boolean
}

export class ExecRegistry {
  private entries = new Map<string, ExecRegEntry>()

  /** exec 发起时登记（此刻通道尚未建立，stream 为 null） */
  register(execId: string): void {
    this.entries.set(execId, { stream: null, pendingKill: false })
  }

  /** exec 回调到达时挂上真实通道；若 kill 已先行到达，立即补杀 */
  attachStream(execId: string, stream: KillableStream): void {
    const entry = this.entries.get(execId)
    if (!entry) return
    entry.stream = stream
    if (entry.pendingKill) this.kill(execId)
  }

  /** exec 正常结束/失败/超时后的摘除（防登记表泄漏） */
  settle(execId: string): void {
    this.entries.delete(execId)
  }

  /** 按 id 查杀。三态结果见 KillOutcome；绝不抛（查杀失败如实上报为未 kill）。 */
  kill(execId: string): KillOutcome {
    const entry = this.entries.get(execId)
    if (!entry) return { found: false, signaled: false, pending: false }
    if (!entry.stream) {
      entry.pendingKill = true
      return { found: true, signaled: false, pending: true }
    }
    try {
      entry.stream.signal('KILL')
      this.entries.delete(execId)
      return { found: true, signaled: true, pending: false }
    } catch {
      // sshd 不支持 exec 通道 signal：关通道兜底
      try {
        entry.stream.close()
        this.entries.delete(execId)
      } catch {
        /* 通道可能已自行结束 */
      }
      return { found: true, signaled: false, pending: false }
    }
  }

  has(execId: string): boolean {
    return this.entries.has(execId)
  }

  clear(): void {
    this.entries.clear()
  }
}
