/**
 * server-monitor 解析逻辑测试（B 阶段）
 * fixture 取自真实 Linux（WSL2 Ubuntu 26.04）上 METRICS_CMD 的输出
 */
import { describe, expect, it } from 'vitest'
import { parseMetricsRaw, computeCpuDiff, describeMonitorError } from './server-monitor-core'

const FIXTURE = [
  '===CKUN_MON===',
  'STAT=321399|319801|288132',
  'LOAD=2.13 0.96 0.37',
  'MEM_TOTAL=15895',
  'MEM_USED=677',
  'MEM_PCT=4.3',
  'DISK=/|1031019|128627|14%',
  'PROC=1212|7.4|0.0|bash',
  'PROC=784|4.8|0.6|dockerd',
  'PORT=tcp|0.0.0.0|22',
  'PORT=tcp|[::]|22',
  'PORT=udp|127.0.0.1|323',
  'PORT=udp|[::1]|323',
  'GPU=0|NVIDIA A100-PCIE-40GB|87|15360|40960|62',
  'GPU=1|NVIDIA A100-PCIE-40GB|12|4096|40960|45',
  '===END===',
].join('\n')

describe('parseMetricsRaw', () => {
  it('解析 CPU 原始计数器', () => {
    const m = parseMetricsRaw(FIXTURE)
    expect(m).not.toBeNull()
    expect(m!.rawStat).toEqual({ total: 321399, idle: 319801, iowait: 288132 })
  })

  it('解析负载三元组', () => {
    const m = parseMetricsRaw(FIXTURE)
    expect(m!.load).toEqual([2.13, 0.96, 0.37])
  })

  it('解析内存（MB + 百分比）', () => {
    const m = parseMetricsRaw(FIXTURE)
    expect(m!.mem.total).toBe(15895)
    expect(m!.mem.used).toBe(677)
    expect(m!.mem.percent).toBe(4.3)
  })

  it('解析磁盘挂载', () => {
    const m = parseMetricsRaw(FIXTURE)
    expect(m!.disks).toHaveLength(1)
    expect(m!.disks[0]).toEqual({ mount: '/', total: 1031019, used: 128627, percent: 14 })
  })

  it('解析进程 TOP', () => {
    const m = parseMetricsRaw(FIXTURE)
    expect(m!.procs).toHaveLength(2)
    expect(m!.procs[0]).toEqual({ pid: 1212, cpu: 7.4, mem: 0, name: 'bash' })
    expect(m!.procs[1]).toEqual({ pid: 784, cpu: 4.8, mem: 0.6, name: 'dockerd' })
  })

  it('解析监听端口（含 IPv6）', () => {
    const m = parseMetricsRaw(FIXTURE)
    expect(m!.ports).toHaveLength(4)
    expect(m!.ports[0]).toEqual({ proto: 'tcp', addr: '0.0.0.0', port: 22 })
    expect(m!.ports[1]).toEqual({ proto: 'tcp', addr: '[::]', port: 22 })
    expect(m!.ports[3]).toEqual({ proto: 'udp', addr: '[::1]', port: 323 })
  })

  it('解析 GPU（多卡，显存百分比自算）', () => {
    const m = parseMetricsRaw(FIXTURE)
    expect(m!.gpus).toHaveLength(2)
    expect(m!.gpus[0]).toEqual({
      index: 0,
      name: 'NVIDIA A100-PCIE-40GB',
      utilization: 87,
      memUsed: 15360,
      memTotal: 40960,
      memPercent: 37.5,
      temperature: 62,
    })
    expect(m!.gpus[1].utilization).toBe(12)
  })

  it('无 GPU 行时 gpus 为空数组（不影响其它指标）', () => {
    const m = parseMetricsRaw('===CKUN_MON===\nSTAT=1|2|3\nMEM_TOTAL=100\n===END===')
    expect(m!.gpus).toEqual([])
  })

  it('缺少 SENTINEL 时返回 null', () => {
    expect(parseMetricsRaw('hello world')).toBeNull()
  })

  it('缺少 MEM 行时返回 null（/proc/meminfo 不可读视为非 Linux）', () => {
    const bad = '===CKUN_MON===\nSTAT=1|2|3\n===END==='
    expect(parseMetricsRaw(bad)).toBeNull()
  })

  it('host 透传', () => {
    expect(parseMetricsRaw(FIXTURE, '1.2.3.4')!.host).toBe('1.2.3.4')
  })
})

describe('computeCpuDiff', () => {
  it('首轮（无 prev）返回 0', () => {
    expect(computeCpuDiff(null, { total: 100, idle: 90, iowait: 5 })).toEqual({ used: 0, iowait: 0 })
  })

  it('计数器无前进时返回 0（防负值/除零）', () => {
    const prev = { total: 100, idle: 90, iowait: 5 }
    expect(computeCpuDiff(prev, prev)).toEqual({ used: 0, iowait: 0 })
  })

  it('差分计算使用率', () => {
    // total +100（其中 idle +80, iowait +10, busy +10）→ used 20%, iowait 10%
    const prev = { total: 1000, idle: 900, iowait: 100 }
    const cur = { total: 1100, idle: 980, iowait: 110 }
    const r = computeCpuDiff(prev, cur)
    expect(r.used).toBeCloseTo(20, 5)
    expect(r.iowait).toBeCloseTo(10, 5)
  })

  it('idle 异常倒退时 used 钳制到上限 100', () => {
    const prev = { total: 100, idle: 90, iowait: 0 }
    // idle 倒退 → (1 - dIdle/dTotal) > 1 → used>100 被钳到 100
    const r = computeCpuDiff(prev, { total: 200, idle: 50, iowait: 0 })
    expect(r.used).toBe(100)
  })

  it('idle 增量超总增量（异常）时 used 钳制到下界 0', () => {
    const prev = { total: 100, idle: 90, iowait: 0 }
    // dTotal=50, dIdle=59 → used = 1 - 59/50 = -0.18 → 钳到 0
    const r = computeCpuDiff(prev, { total: 150, idle: 149, iowait: 0 })
    expect(r.used).toBe(0)
  })
})

describe('describeMonitorError', () => {
  it('结构化 SSH 错误（name 回退格式）→ 按 code 还原中文文案', () => {
    const e = new Error('SSH 会话不存在或已断开')
    e.name = 'SSHError:SESSION_NOT_FOUND'
    const r = describeMonitorError(e)
    expect(r.code).toBe('SESSION_NOT_FOUND')
    expect(r.message).toBe('SSH 会话不存在或已断开')
    expect(r.retryable).toBe(false)
  })

  it('__sshError transport（Electron 32+ 直传）→ 精确 code + 可重试标记', () => {
    const e: any = new Error('x')
    e.__sshError = {
      __sshError: true,
      code: 'EXEC_TIMEOUT',
      message: '命令执行超时（10秒），可增大命令超时时间',
      retryable: true,
      detail: '10秒',
    }
    const r = describeMonitorError(e)
    expect(r.code).toBe('EXEC_TIMEOUT')
    expect(r.retryable).toBe(true)
    expect(r.message).toContain('命令执行超时')
  })

  it('本地解析失败 → 专用文案（非 Linux/不可读 /proc）', () => {
    const r = describeMonitorError(new Error('metrics parse failed'))
    expect(r.code).toBe('MON_PARSE')
    expect(r.message).toContain('无法解析服务器指标')
    expect(r.retryable).toBe(false)
  })

  it('Electron IPC 外壳 → 剥离前缀只留根因', () => {
    const r = describeMonitorError(
      new Error("Error invoking remote method 'ssh:exec': 连接被远端重置"),
    )
    expect(r.code).toBe('UNKNOWN')
    expect(r.message).toBe('连接被远端重置')
    expect(r.message).not.toContain('Error invoking')
  })

  it('空错误 → 兜底文案', () => {
    const r = describeMonitorError(null)
    expect(r.code).toBe('UNKNOWN')
    expect(r.message).toBe('指标采集失败，请检查连接后重试')
  })
})
