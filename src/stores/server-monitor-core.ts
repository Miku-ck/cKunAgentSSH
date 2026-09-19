/**
 * 服务器监控 — 纯核心（无 vue/pinia 依赖，可独立单测）
 *
 * 远端聚合采集命令 + /proc 输出解析 + CPU 跨轮次差分。
 * Pinia store（server-monitor.ts）在此之上做轮询编排与状态管理。
 */

import { extractSSHError } from '../ssh-error-types'

// ── 数据类型 ──

export interface CpuSample {
  used: number          // 0-100
  iowait: number        // 0-100
}
export interface MemSample {
  total: number         // MB
  used: number          // MB
  percent: number       // 0-100
}
export interface DiskMount {
  mount: string
  total: number         // MB
  used: number          // MB
  percent: number       // 0-100
}
export interface ProcessTopEntry {
  pid: number
  cpu: number
  mem: number
  name: string
}
export interface PortEntry {
  proto: 'tcp' | 'udp'
  addr: string
  port: number
}
/** 单块 GPU 快照（nvidia-smi 采集） */
export interface GpuSample {
  index: number
  name: string
  /** 利用率 0-100 */
  utilization: number
  /** 显存已用 MB */
  memUsed: number
  /** 显存总量 MB */
  memTotal: number
  /** 显存使用率 0-100 */
  memPercent: number
  /** 温度 ℃ */
  temperature: number
}
export interface ServerMetrics {
  ts: number
  host?: string
  /** 采集来源的 SSH 会话 ID */
  sshId?: string
  cpu: CpuSample
  load: [number, number, number]
  mem: MemSample
  disks: DiskMount[]
  procs: ProcessTopEntry[]
  ports: PortEntry[]
  /** GPU 列表（无 NVIDIA 驱动/无卡时为空数组） */
  gpus: GpuSample[]
}

export interface MonitorErrorInfo {
  code: string
  /** 用户友好提示（中文，可直接展示） */
  message: string
  /** 是否可重试（决定 UI 是否给"重连后自动恢复"预期 / 重试按钮） */
  retryable: boolean
}

/**
 * 监控采集错误 → 用户友好提示。
 *
 * 与 ssh:exec 的 IPC 错误契约对齐：主进程 execCommandWithExitCode 抛出的
 * SSHError 经 normalizeIpcError 序列化为 __sshError transport 透传，此处
 * 优先按结构化 code 还原精确文案；非结构化错误（本地解析失败 / 会话在采集
 * 期间消失 / Electron IPC 外壳）按语义映射兜底。返回的 message 已可直接展示。
 */
export function describeMonitorError(e: unknown): MonitorErrorInfo {
  // 结构化 SSH 错误：transport.message 已是共享中文文案（单一事实来源）
  const t = extractSSHError(e)
  if (t) {
    return { code: t.code, message: t.message, retryable: t.retryable }
  }
  const msg = e instanceof Error ? e.message : String(e || '')
  if (/metrics parse failed/i.test(msg)) {
    return {
      code: 'MON_PARSE',
      message: '无法解析服务器指标（远端可能不是 Linux 或 /proc 不可读）',
      retryable: false,
    }
  }
  // 兜底：剥掉 Electron IPC 的 "Error invoking remote method 'xxx':" 外壳，
  // 仅保留根因消息，避免把内部 IPC 细节暴露给用户
  const root = msg.replace(/^Error invoking remote method '[^']+'\s*:\s*/, '')
  return {
    code: 'UNKNOWN',
    message: root || '指标采集失败，请检查连接后重试',
    retryable: false,
  }
}

/** /proc/stat CPU 原始计数器（单遍采样） */
export interface RawStat {
  total: number
  idle: number
  iowait: number
}

// ── 远端聚合采集命令（POSIX；单遍采样取原始值，CPU 百分比由 store 层跨轮次差分计算） ──
export const METRICS_CMD = [
  'echo "===CKUN_MON==="' ,
  `awk 'NR==1{printf "STAT=%s|%s|%s\\n", $2+$3+$4+$5+$6+$7+$8, $5+$6, $5}' /proc/stat`,
  `echo "LOAD=$(cut -d" " -f1-3 /proc/loadavg)"`,
  `awk '/^MemTotal/{t=$2} /^MemAvailable/{a=$2} END{if (t>0) printf "MEM_TOTAL=%.0f\\nMEM_USED=%.0f\\nMEM_PCT=%.1f\\n", t/1024, (t-a)/1024, (t-a)/t*100; else print "MEM_TOTAL=0\\nMEM_USED=0\\nMEM_PCT=0"}' /proc/meminfo`,
  `df -m -P 2>/dev/null | awk 'NR>1 && $1 ~ /^\\// {printf "DISK=%s|%s|%s|%s\\n", $6, $2, $3, $5}'`,
  `ps -eo pid,pcpu,pmem,comm --sort=-pcpu 2>/dev/null | awk 'NR>1 && NR<=7{printf "PROC=%s|%s|%s|%s\\n", $1, $2, $3, $4}'`,
  `ss -lnt 2>/dev/null | awk 'NR>1 && match($4, /:[0-9]+$/) {port=substr($4,RSTART+1,RLENGTH-1); addr=substr($4,1,RSTART-1); printf "PORT=tcp|%s|%s\\n", addr, port}'`,
  `ss -lnu 2>/dev/null | awk 'NR>1 && match($4, /:[0-9]+$/) {port=substr($4,RSTART+1,RLENGTH-1); addr=substr($4,1,RSTART-1); printf "PORT=udp|%s|%s\\n", addr, port}'`,
  `command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu --format=csv,noheader,nounits 2>/dev/null | awk -F', ' '{printf "GPU=%s|%s|%s|%s|%s|%s\\n", $1, $2, $3, $4, $5, $6}'`,
  'echo "===END==="' ,
].join('; ')

// ── 解析 ──

function fnum(s: string): number {
  const n = parseFloat(s)
  return Number.isFinite(n) ? n : 0
}
function inum(s: string): number {
  const n = parseInt(s, 10)
  return Number.isFinite(n) ? n : 0
}

export type ParsedMetrics = Omit<ServerMetrics, 'cpu' | 'ts'> & { rawStat: RawStat }

export function parseMetricsRaw(raw: string, host?: string): ParsedMetrics | null {
  const lines = raw.split('\n')
  if (!lines.some(l => l.startsWith('===CKUN_MON==='))) return null

  let rawStat: RawStat | null = null
  let load: [number, number, number] = [0, 0, 0]
  let mem: MemSample = { total: 0, used: 0, percent: 0 }
  const disks: DiskMount[] = []
  const procs: ProcessTopEntry[] = []
  const ports: PortEntry[] = []
  const gpus: GpuSample[] = []

  for (const line of lines) {
    if (line.startsWith('STAT=')) {
      const [total, idle, iowait] = line.slice(5).split('|')
      rawStat = { total: fnum(total), idle: fnum(idle), iowait: fnum(iowait) }
    } else if (line.startsWith('LOAD=')) {
      const parts = line.slice(5).split(' ')
      load = [fnum(parts[0]), fnum(parts[1]), fnum(parts[2])]
    } else if (line.startsWith('MEM_TOTAL=')) mem.total = fnum(line.slice(10))
    else if (line.startsWith('MEM_USED=')) mem.used = fnum(line.slice(9))
    else if (line.startsWith('MEM_PCT=')) mem.percent = fnum(line.slice(8))
    else if (line.startsWith('DISK=')) {
      const [mount, total, used, pct] = line.slice(5).split('|')
      if (!mount || !mount.startsWith('/')) continue
      disks.push({ mount, total: inum(total), used: inum(used), percent: fnum(pct) })
      if (disks.length >= 8) break
    } else if (line.startsWith('PROC=')) {
      const [pid, pcpu, pmem, name] = line.slice(5).split('|')
      procs.push({ pid: inum(pid), cpu: fnum(pcpu), mem: fnum(pmem), name: name || 'unknown' })
    } else if (line.startsWith('PORT=')) {
      const [proto, addr, port] = line.slice(5).split('|')
      if (proto === 'tcp' || proto === 'udp') {
        ports.push({ proto, addr: addr || '0.0.0.0', port: inum(port) })
        if (ports.length >= 30) break
      }
    } else if (line.startsWith('GPU=')) {
      // GPU=<idx>|<name>|<util>|<memUsed>|<memTotal>|<temp>
      const [idx, name, util, mu, mt, temp] = line.slice(4).split('|')
      const memTotal = inum(mt)
      const memUsed = inum(mu)
      gpus.push({
        index: inum(idx),
        name: name || `GPU ${inum(idx)}`,
        utilization: Math.max(0, Math.min(100, fnum(util))),
        memUsed,
        memTotal,
        memPercent: memTotal > 0 ? Math.max(0, Math.min(100, (memUsed / memTotal) * 100)) : 0,
        temperature: fnum(temp),
      })
      if (gpus.length >= 8) break
    }
  }

  if (!rawStat || !lines.some(l => l.startsWith('MEM_TOTAL='))) return null
  return { host, rawStat, load, mem, disks, procs, ports, gpus }
}

/**
 * CPU 使用率跨轮次差分：/proc/stat 计数器单调递增，需两次采样求差。
 * prev 为 null（首轮）或计数器无前进时返回全 0。
 */
export function computeCpuDiff(prev: RawStat | null, cur: RawStat): CpuSample {
  if (!prev || cur.total <= prev.total) return { used: 0, iowait: 0 }
  const dTotal = cur.total - prev.total
  const dIdle = cur.idle - prev.idle
  const dIo = cur.iowait - prev.iowait
  return {
    used: Math.max(0, Math.min(100, (1 - dIdle / dTotal) * 100)),
    iowait: Math.max(0, Math.min(100, (dIo / dTotal) * 100)),
  }
}
