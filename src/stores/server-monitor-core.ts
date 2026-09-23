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
/** 单块 GPU 快照（nvidia-smi 采集）；null = 该字段远端返回 N/A（如被动散热卡无风扇、WSL 无功率） */
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
  /** 功耗 W */
  powerDraw: number | null
  /** 功耗上限 W */
  powerLimit: number | null
  /** 风扇转速 % */
  fanSpeed: number | null
  /** SM 时钟 MHz */
  smClock: number | null
  /** 显存时钟 MHz */
  memClock: number | null
  /** nvidia-smi GPU UUID（compute-apps 进程行按此关联到卡） */
  uuid: string
  /** PCI 总线号（如 00000000:41:00.0） */
  busId: string
  /** 性能状态 P0（满载）~ P12（深睡） */
  perfState: string
  /** 持久化模式 Enabled/Disabled */
  persistence: string
  /** 计算模式 Default/Exclusive_Process 等 */
  computeMode: string
  /** 易失区未纠正 ECC 错误计数（消费卡不支持 = null） */
  eccUncorr: number | null
}
/** GPU 占用进程（nvitop 模式进程表；nvidia-smi compute-apps × ps join） */
export interface GpuProcessEntry {
  gpuIndex: number
  pid: number
  user: string
  name: string
  /** 显存占用 MB */
  memMB: number
  /** CPU %（ps 行缺失时 null） */
  cpu: number | null
  /** 内存 %（ps 行缺失时 null） */
  mem: number | null
  /** 运行时长（ps etime 原样，如 "16-05:32:11" / "1:23:45"） */
  etime: string
}
export interface ServerMetrics {
  ts: number
  host?: string
  /** 采集来源的 SSH 会话 ID */
  sshId?: string
  cpu: CpuSample
  load: [number, number, number]
  mem: MemSample
  /** 交换分区 MB（nvtop 的 SWP 行；无 swap 时 total=0） */
  swap: { total: number; used: number }
  disks: DiskMount[]
  procs: ProcessTopEntry[]
  ports: PortEntry[]
  /** GPU 列表（无 NVIDIA 驱动/无卡时为空数组） */
  gpus: GpuSample[]
  /** nvidia-smi 存在但执行失败的原因行（空 = 无此异常；与 gpus 空数组联合区分「无卡」与「采集失败」） */
  gpuError: string
  /** 驱动版本（nvitop 模式头部；无 GPU 时空串） */
  gpuDriver: string
  /** CUDA 版本（nvitop 模式头部，从 nvidia-smi --version 提取；无 GPU 时空串） */
  gpuCuda: string
  /** GPU 占用进程表（nvitop 模式；无占用时为空数组） */
  gpuProcs: GpuProcessEntry[]
}

interface MonitorErrorInfo {
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
  `awk 'NR==1{printf "STAT=%s|%s|%s\\n", $2+$3+$4+$5+$6+$7+$8, $5, $6}' /proc/stat`,
  `echo "LOAD=$(cut -d" " -f1-3 /proc/loadavg)"`,
  `awk '/^MemTotal/{t=$2} /^MemAvailable/{a=$2} /^SwapTotal/{st=$2} /^SwapFree/{sf=$2} END{if (t>0) printf "MEM_TOTAL=%.0f\\nMEM_USED=%.0f\\nMEM_PCT=%.1f\\n", t/1024, (t-a)/1024, (t-a)/t*100; else print "MEM_TOTAL=0\\nMEM_USED=0\\nMEM_PCT=0"; printf "SWAP_TOTAL=%.0f\\nSWAP_USED=%.0f\\n", st/1024, (st-sf)/1024}' /proc/meminfo`,
  `df -m -P 2>/dev/null | awk 'NR>1 && $1 ~ /^\\// {printf "DISK=%s|%s|%s|%s\\n", $6, $2, $3, $5}'`,
  `ps -eo pid,pcpu,pmem,comm --sort=-pcpu 2>/dev/null | awk 'NR>1 && NR<=7{printf "PROC=%s|%s|%s|%s\\n", $1, $2, $3, $4}'`,
  `ss -lnt 2>/dev/null | awk 'NR>1 && match($4, /:[0-9]+$/) {port=substr($4,RSTART+1,RLENGTH-1); addr=substr($4,1,RSTART-1); printf "PORT=tcp|%s|%s\\n", addr, port}'`,
  `ss -lnu 2>/dev/null | awk 'NR>1 && match($4, /:[0-9]+$/) {port=substr($4,RSTART+1,RLENGTH-1); addr=substr($4,1,RSTART-1); printf "PORT=udp|%s|%s\\n", addr, port}'`,
  // GPU 采集四段式：① nvidia-smi 探测覆盖非登录 exec shell 的默认 PATH 盲区（sshd 给非交互 shell 的 PATH
  // 常不含 /usr/sbin/sbin——SUSE 系 RPM 驱动装在 /usr/sbin；WSL2 的桩在 /usr/lib/wsl/lib；runfile 装在
  // /usr/local/sbin）；② 探测全落空但内核驱动在场（/proc/driver/nvidia/version 存在）→ 输出 GPUERR= 自诊断
  // 行（UI 归入「GPU 采集异常」分支并展示原因，与「无 GPU 机器安静无输出」区分开）；③ 全量查询失败的判据是
  // "OUT 中无数字打头的数据行"而非"OUT 为空"——关键实测教训：nvidia-smi 对无效字段（WSL 桩、老驱动）把
  // 'Field "x" is not a valid field to query.' 打到 **stdout**（非 stderr）且 rc≠0，空判据永不触发；此时
  // 降级为 6 列基础查询，缺的字段输出空段、解析层映射为 null（面板渲染 "—"），面板照常出；④ 两查皆无数据行
  // 才输出 GPUERR=（错误文本优先取 stdout 首行——即无效字段报错本身，stdout 空才读 stderr 临时文件；临时
  // 文件每用户可写，多会话并发互相覆盖可容忍——文本无状态且只读当次输出）；⑤ 降级模式（DEG）下 18 列整体
  // 作废 → uuid/驱动版本单独补查（uuid 是 GPROC→卡号 join 的钥匙，缺了进程表必空）：GPUINFO= 行回填
  // index→uuid（uuid 别名不被支持时退 gpu_uuid），GPUDRV= 行从 nvidia-smi --version 提取驱动版本（不触
  // NVML，秒回）；GPUX= 行补查功耗/风扇/时钟/PCI 总线（这些是 R340 时代老字段，18 列被拒多半只因
  // persistence/ecc 等新字段，一般字段集仍可取回）；--query-compute-apps 亦按「输出非空但无 GPU- 打头行 = 无效字段报错」降级为无名字三列。
  // 字段 18 列：功率/风扇/频率/uuid/驱动/PCI 总线/性能状态(P0/P8)/持久化/计算模式/ECC 未纠正计数
  // 均为同一次 --query-gpu 调用取回，N/A 值由解析层映射为 null（消费卡无 ECC 等）。
  // nvitop 模式数据面：--query-compute-apps 取占用进程（GPROC= 行，进程名放最后以容忍含逗号的命令行，≤20 条），
  // 再用其 pid 列表单次 ps 关联 user/cpu/mem/etime（GPS= 行）；uuid→GPU index 的 join 在解析层完成。
  `NS=""; for c in nvidia-smi /usr/bin/nvidia-smi /usr/sbin/nvidia-smi /sbin/nvidia-smi /usr/local/bin/nvidia-smi /usr/local/sbin/nvidia-smi /usr/lib/wsl/lib/nvidia-smi; do command -v "$c" >/dev/null 2>&1 && NS="$c" && break; done; if [ -z "$NS" ]; then if [ -e /proc/driver/nvidia/version ]; then printf 'GPUERR=GPU driver present (kernel), but nvidia-smi not found in PATH or common dirs\\n'; fi; else TMP=/tmp/.ckun_gpu_err_$$; DEG=""; OUT=$("$NS" --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,fan.speed,clocks.sm,clocks.mem,uuid,driver_version,pci.bus_id,performance_state,persistence_mode,compute_mode,ecc.errors.uncorrected.volatile.total --format=csv,noheader,nounits 2>"$TMP"); if ! printf '%s\\n' "$OUT" | grep -q '^[0-9]'; then DEG=1; OUT=$("$NS" --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu --format=csv,noheader,nounits 2>"$TMP"); fi; if ! printf '%s\\n' "$OUT" | grep -q '^[0-9]'; then ERR=$(printf '%s\\n' "$OUT" | head -1); if [ -z "$ERR" ]; then ERR=$(head -c 200 "$TMP" | tr '\\n\\r' '  '); fi; printf 'GPUERR=%s\\n' "$ERR"; fi; rm -f "$TMP"; printf '%s\\n' "$OUT" | awk -F', ' '/^[0-9]/{printf "GPU=%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s\\n", $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18}'; if [ -n "$DEG" ]; then for q in index,uuid index,gpu_uuid; do UI=$("$NS" --query-gpu=$q --format=csv,noheader,nounits 2>/dev/null); if printf '%s\\n' "$UI" | grep -q '^[0-9]'; then printf '%s\\n' "$UI" | awk -F', ' '/^[0-9]/{printf "GPUINFO=%s|%s\\n", $1, $2}'; break; fi; done; XQ=$("$NS" --query-gpu=index,power.draw,power.limit,fan.speed,clocks.sm,clocks.mem,pci.bus_id --format=csv,noheader,nounits 2>/dev/null); if printf '%s\\n' "$XQ" | grep -q '^[0-9]'; then printf '%s\\n' "$XQ" | awk -F', ' '/^[0-9]/{printf "GPUX=%s|%s|%s|%s|%s|%s|%s\\n", $1, $2, $3, $4, $5, $6, $7}'; fi; DR=$( ("$NS" --version 2>/dev/null || "$NS" -v 2>/dev/null) | awk 'tolower($0) ~ /driver/ && !d {n=split($0,a,/[ \\t:]+/); for(i=1;i<=n;i++) if(a[i] ~ /^[0-9]+\\.[0-9.]*[0-9]$/) {print a[i]; d=1; break} }'); if [ -n "$DR" ]; then printf 'GPUDRV=%s\\n' "$DR"; fi; fi; APPS=$("$NS" --query-compute-apps=gpu_uuid,pid,used_memory,process_name --format=csv,noheader,nounits 2>/dev/null); if [ -n "$APPS" ] && ! printf '%s\\n' "$APPS" | grep -q '^GPU-'; then A2=$("$NS" --query-compute-apps=gpu_uuid,pid,used_memory --format=csv,noheader,nounits 2>/dev/null); if printf '%s\\n' "$A2" | grep -q '^GPU-'; then APPS="$A2"; fi; fi; printf '%s\\n' "$APPS" | awk -F', ' 'NF>=3 && c<20{out=$4; for(i=5;i<=NF;i++) out=out", "$i; printf "GPROC=%s|%s|%s|%s\\n", $1, $2, $3, out; c++}'; PIDS=$(printf '%s\\n' "$APPS" | awk -F', ' 'NF>=3{print $2}' | head -20 | paste -sd, -); if [ -n "$PIDS" ]; then ps -o pid=,user=,pcpu=,pmem=,etime= -p "$PIDS" 2>/dev/null | awk '{printf "GPS=%s|%s|%s|%s|%s\\n", $1, $2, $3, $4, $5}'; fi; fi`,
  `[ -n "$NS" ] && { CUV=$( ("$NS" --version 2>/dev/null || "$NS" -v 2>/dev/null) | awk 'BEGIN{IGNORECASE=1} {for(i=1;i<=NF;i++){ if($i=="CUDA"){ for(j=i+1;j<=NF;j++){ if($j ~ /^[0-9][0-9.]*$/) {print $j; exit} } } } }'); [ -n "$CUV" ] && printf 'GPUCUDA=%s\\n' "$CUV"; }`,
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
/** nvidia-smi 对不可得字段返回 "N/A"（被动散热卡无风扇、WSL 无功率等）→ null，UI 渲染 "—" */
function naNum(s: string | undefined): number | null {
  if (s === undefined) return null
  const t = s.trim()
  if (!t || /^n\/a$/i.test(t)) return null
  const n = parseFloat(t)
  return Number.isFinite(n) ? n : null
}
/** 同上但取字符串值（perf/persistence/compute 等模式字段） */
function naStr(s: string | undefined): string {
  if (s === undefined) return ''
  const t = s.trim()
  return /^n\/a$/i.test(t) ? '' : t
}

export type ParsedMetrics = Omit<ServerMetrics, 'cpu' | 'ts'> & { rawStat: RawStat }

export function parseMetricsRaw(raw: string, host?: string): ParsedMetrics | null {
  const lines = raw.split('\n')
  if (!lines.some(l => l.startsWith('===CKUN_MON==='))) return null

  let rawStat: RawStat | null = null
  let load: [number, number, number] = [0, 0, 0]
  let mem: MemSample = { total: 0, used: 0, percent: 0 }
  const swap = { total: 0, used: 0 }
  const disks: DiskMount[] = []
  const procs: ProcessTopEntry[] = []
  const ports: PortEntry[] = []
  const gpus: GpuSample[] = []
  let gpuError = ''
  let gpuDriver = ''
  let gpuCuda = ''
  // GPROC=/GPS= 先按原始行收集，循环后统一 join（GPU 行可能晚于/早于进程行，且 uuid→index 映射要先齐）
  const gpuProcRows: Array<{ uuid: string; pid: number; memMB: number; name: string }> = []
  const psRows = new Map<number, { user: string; cpu: number; mem: number; etime: string }>()
  // 降级模式补查行（GPUINFO=/GPUDRV=）的暂存，循环后回填进 gpus/gpuDriver（补查行可能先于 GPU= 行出现）
  const uuidFill = new Map<number, string>()
  const gpuXFill = new Map<number, string[]>()
  let driverFill = ''
  // 磁盘/端口达上限后只跳过同类行（不能 break 整个循环：GPU 行在输出末尾，会被整批丢弃）
  let disksFull = false
  let portsFull = false
  let gpusFull = false

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
    else if (line.startsWith('SWAP_TOTAL=')) swap.total = fnum(line.slice(11))
    else if (line.startsWith('SWAP_USED=')) swap.used = fnum(line.slice(10))
    else if (line.startsWith('DISK=')) {
      if (disksFull) continue
      const [mount, total, used, pct] = line.slice(5).split('|')
      if (!mount || !mount.startsWith('/')) continue
      disks.push({ mount, total: inum(total), used: inum(used), percent: fnum(pct) })
      if (disks.length >= 8) disksFull = true
    } else if (line.startsWith('PROC=')) {
      const [pid, pcpu, pmem, name] = line.slice(5).split('|')
      procs.push({ pid: inum(pid), cpu: fnum(pcpu), mem: fnum(pmem), name: name || 'unknown' })
    } else if (line.startsWith('PORT=')) {
      if (portsFull) continue
      const [proto, addr, port] = line.slice(5).split('|')
      if (proto === 'tcp' || proto === 'udp') {
        ports.push({ proto, addr: addr || '0.0.0.0', port: inum(port) })
        if (ports.length >= 30) portsFull = true
      }
    } else if (line.startsWith('GPUERR=')) {
      gpuError = line.slice(7).trim().slice(0, 200)
    } else if (line.startsWith('GPUINFO=')) {
      // GPUINFO=<idx>|<uuid>（降级模式单独补查，回填 join 用的卡号→uuid 映射）
      const p = line.slice(8).split('|')
      uuidFill.set(inum(p[0]), naStr(p[1]))
    } else if (line.startsWith('GPUX=')) {
      // GPUX=<idx>|<power>|<limit>|<fan>|<smClock>|<memClock>|<busId>（降级模式补查的一般字段，老驱动均支持）
      const p = line.slice(5).split('|')
      gpuXFill.set(inum(p[0]), p.slice(1))
    } else if (line.startsWith('GPUDRV=')) {
      // GPUDRV=<driver>（降级模式从 nvidia-smi --version 提取）
      driverFill = line.slice(7).trim()
    } else if (line.startsWith('GPUCUDA=')) {
      // GPUCUDA=<cuda 版本>（从 nvidia-smi --version 提取；与驱动版本同源）
      gpuCuda = line.slice(8).trim()
    } else if (line.startsWith('GPU=')) {
      // GPU 达上限后只跳过 GPU 行（不能 break：GPROC=/GPS= 进程行排在 GPU 之后，break 会整批丢弃）
      if (gpusFull) continue
      // GPU=<idx>|<name>|<util>|<memUsed>|<memTotal>|<temp>|<power>|<powerLimit>|<fan>|<smClock>|<memClock>|<uuid>|<driver>|<busId>|<perf>|<persist>|<compute>|<eccUncorr>
      const p = line.slice(4).split('|')
      const memTotal = inum(p[4])
      const memUsed = inum(p[3])
      gpus.push({
        index: inum(p[0]),
        name: p[1] || `GPU ${inum(p[0])}`,
        utilization: Math.max(0, Math.min(100, fnum(p[2]))),
        memUsed,
        memTotal,
        memPercent: memTotal > 0 ? Math.max(0, Math.min(100, (memUsed / memTotal) * 100)) : 0,
        temperature: fnum(p[5]),
        powerDraw: naNum(p[6]),
        powerLimit: naNum(p[7]),
        fanSpeed: naNum(p[8]),
        smClock: naNum(p[9]),
        memClock: naNum(p[10]),
        uuid: naStr(p[11]),
        busId: naStr(p[13]),
        perfState: naStr(p[14]),
        persistence: naStr(p[15]),
        computeMode: naStr(p[16]),
        eccUncorr: naNum(p[17]),
      })
      if (!gpuDriver && p[12]) gpuDriver = p[12].trim()
      if (gpus.length >= 8) gpusFull = true
    } else if (line.startsWith('GPROC=')) {
      // GPROC=<uuid>|<pid>|<memMB>|<name>
      const p = line.slice(6).split('|')
      if (p.length >= 4) gpuProcRows.push({ uuid: p[0].trim(), pid: inum(p[1]), memMB: inum(p[2]), name: p.slice(3).join('|') })
    } else if (line.startsWith('GPS=')) {
      // GPS=<pid>|<user>|<pcpu>|<pmem>|<etime>
      const p = line.slice(4).split('|')
      if (p.length >= 5) psRows.set(inum(p[0]), { user: p[1], cpu: fnum(p[2]), mem: fnum(p[3]), etime: p[4] })
    }
  }

  if (!rawStat || !lines.some(l => l.startsWith('MEM_TOTAL='))) return null
  // 降级模式下 GPU= 行的 uuid/驱动为空段 → 用补查行回填，否则 GPROC uuid→卡号 join 必空（进程表消失根因）
  for (const g of gpus) {
    if (!g.uuid) {
      const u = uuidFill.get(g.index)
      if (u) g.uuid = u
    }
    const x = gpuXFill.get(g.index)
    if (x) {
      if (g.powerDraw == null) g.powerDraw = naNum(x[0])
      if (g.powerLimit == null) g.powerLimit = naNum(x[1])
      if (g.fanSpeed == null) g.fanSpeed = naNum(x[2])
      if (g.smClock == null) g.smClock = naNum(x[3])
      if (g.memClock == null) g.memClock = naNum(x[4])
      if (!g.busId) g.busId = naStr(x[5])
    }
  }
  if (!gpuDriver && driverFill) gpuDriver = driverFill
  const idxByUuid = new Map(gpus.filter(g => g.uuid).map(g => [g.uuid, g.index]))
  const gpuProcs: GpuProcessEntry[] = gpuProcRows.flatMap(r => {
    const gpuIndex = idxByUuid.get(r.uuid)
    if (gpuIndex === undefined) return []
    const ps = psRows.get(r.pid)
    return [{
      gpuIndex,
      pid: r.pid,
      memMB: r.memMB,
      name: r.name,
      user: ps?.user ?? '?',
      cpu: ps?.cpu ?? null,
      mem: ps?.mem ?? null,
      etime: ps?.etime ?? '',
    }]
  })
  return { host, rawStat, load, mem, swap, disks, procs, ports, gpus, gpuError, gpuDriver, gpuCuda, gpuProcs }
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
