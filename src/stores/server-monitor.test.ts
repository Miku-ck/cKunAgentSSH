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
  'SWAP_TOTAL=4096',
  'SWAP_USED=204',
  'GPU=0|NVIDIA A100-PCIE-40GB|87|15360|40960|62|222.75|300|N/A|1965|9802|GPU-abc0|580.173.02|00000000:41:00.0|P0|Enabled|Default|N/A',
  'GPU=1|NVIDIA A100-PCIE-40GB|12|4096|40960|45|111.5|300|60|1500|9802|GPU-abc1|580.173.02|00000000:81:00.0|P8|Enabled|Default|3',
  'GPUCUDA=13.0',
  'GPROC=GPU-abc0|443545|1008|python run_ipose_service.py',
  'GPROC=GPU-abc1|3134324|42961|VLLM::EngineCore',
  'GPS=443545|root|0.0|0.5|16-05:32:11',
  'GPS=3134324|root|1.5|1.0|73:08:55',
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

  it('解析 GPU（多卡，显存百分比自算 + 功耗/风扇/频率）', () => {
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
      powerDraw: 222.75,
      powerLimit: 300,
      fanSpeed: null, // 被动散热卡 fan.speed = N/A
      smClock: 1965,
      memClock: 9802,
      uuid: 'GPU-abc0',
      busId: '00000000:41:00.0',
      perfState: 'P0',
      persistence: 'Enabled',
      computeMode: 'Default',
      eccUncorr: null, // 消费级/不支持 ECC → N/A
    })
    expect(m!.gpus[1].utilization).toBe(12)
    expect(m!.gpus[1].fanSpeed).toBe(60)
    expect(m!.gpus[1].eccUncorr).toBe(3)
    expect(m!.gpuDriver).toBe('580.173.02')
    expect(m!.gpuCuda).toBe('13.0')
  })

  it('解析 swap（nvtop SWP 行）', () => {
    const m = parseMetricsRaw(FIXTURE)
    expect(m!.swap).toEqual({ total: 4096, used: 204 })
    const noSwap = parseMetricsRaw('===CKUN_MON===\nSTAT=1|2|3\nMEM_TOTAL=100\n===END===')
    expect(noSwap!.swap).toEqual({ total: 0, used: 0 })
  })

  it('GPROC × GPS join → gpuProcs（uuid→index、ps 字段关联）', () => {
    const m = parseMetricsRaw(FIXTURE)
    expect(m!.gpuProcs).toHaveLength(2)
    expect(m!.gpuProcs[0]).toEqual({
      gpuIndex: 0,
      pid: 443545,
      memMB: 1008,
      name: 'python run_ipose_service.py',
      user: 'root',
      cpu: 0,
      mem: 0.5,
      etime: '16-05:32:11',
    })
    expect(m!.gpuProcs[1].gpuIndex).toBe(1)
    expect(m!.gpuProcs[1].etime).toBe('73:08:55')
  })

  it('GPROC 进程名含逗号不被截断；未知 uuid 丢弃；ps 行缺失字段兜底', () => {
    const raw = [
      '===CKUN_MON===', 'STAT=1|2|3', 'MEM_TOTAL=100',
      'GPU=0|A100|50|1|2|60|N/A|N/A|N/A|N/A|N/A|GPU-x|',
      'GPROC=GPU-x|77|10|python -c "print(1, 2)"',
      'GPROC=GPU-unknown|88|10|ghost',
      'GPROC=GPU-x|99|20|orphan-no-ps',
      'GPS=77|alice|3.5|1.2|01:02:03',
      '===END===',
    ].join('\n')
    const m = parseMetricsRaw(raw)
    expect(m!.gpuProcs).toHaveLength(2)
    expect(m!.gpuProcs[0]).toMatchObject({ gpuIndex: 0, pid: 77, user: 'alice', cpu: 3.5, etime: '01:02:03', name: 'python -c "print(1, 2)"' })
    expect(m!.gpuProcs[1]).toMatchObject({ pid: 99, user: '?', cpu: null, mem: null, etime: '' })
  })

  it('GPU 扩展字段全 N/A（WSL 等场景）→ null 降级，基础字段不受影响', () => {
    const raw = '===CKUN_MON===\nSTAT=1|2|3\nMEM_TOTAL=100\nGPU=0|Tesla T4|30|512|15360|55|N/A|N/A|N/A|N/A|N/A\n===END==='
    const g = parseMetricsRaw(raw)!.gpus[0]
    expect(g).toMatchObject({ index: 0, utilization: 30, temperature: 55 })
    expect(g.memPercent).toBeCloseTo(100 / 30, 3)
    expect([g.powerDraw, g.powerLimit, g.fanSpeed, g.smClock, g.memClock]).toEqual([null, null, null, null, null])
  })

  it('老驱动降级：18 列查询失败 → 6 列回退行（awk 补空段）→ 扩展字段 null，基础字段完整', () => {
    // 回退时 awk 仍按 18 段 printf，$7..$18 为空串 → 行尾一串空段
    const raw = '===CKUN_MON===\nSTAT=1|2|3\nMEM_TOTAL=100\nGPU=0|Tesla T4|30|512|15360|55||||||||||||\n===END==='
    const g = parseMetricsRaw(raw)!.gpus[0]
    expect(g).toMatchObject({ index: 0, name: 'Tesla T4', utilization: 30, memUsed: 512, memTotal: 15360, temperature: 55 })
    expect([g.powerDraw, g.powerLimit, g.fanSpeed, g.smClock, g.memClock, g.eccUncorr]).toEqual([null, null, null, null, null, null])
    expect([g.uuid, g.busId, g.perfState, g.persistence, g.computeMode]).toEqual(['', '', '', '', ''])
  })

  it('降级模式：GPUINFO/GPUDRV 补查行回填 uuid/驱动 → GPROC join 恢复（进程表不再消失）', () => {
    const raw = [
      '===CKUN_MON===', 'STAT=1|2|3', 'MEM_TOTAL=100',
      'GPU=0|Tesla T4|30|512|15360|55||||||||||||',
      'GPU=1|Tesla T4|10|256|15360|50||||||||||||',
      'GPUINFO=0|GPU-tt0001', 'GPUINFO=1|GPU-tt0002', 'GPUDRV=470.199.02',
      'GPROC=GPU-tt0002|7788|256|python3',
      'GPROC=GPU-tt0001|7787|512|',
      'GPS=7787|root|88.5|2.1|3:20:11',
      '===END===',
    ].join('\n')
    const m = parseMetricsRaw(raw)!
    expect(m.gpuDriver).toBe('470.199.02')
    expect(m.gpus.map(g => g.uuid)).toEqual(['GPU-tt0001', 'GPU-tt0002'])
    // 无名字 GPROC（三列降级查询）→ name 空串；GPUINFO join 生效
    expect(m.gpuProcs).toEqual([
      { gpuIndex: 1, pid: 7788, memMB: 256, name: 'python3', user: '?', cpu: null, mem: null, etime: '' },
      { gpuIndex: 0, pid: 7787, memMB: 512, name: '', user: 'root', cpu: 88.5, mem: 2.1, etime: '3:20:11' },
    ])
  })

  it('降级模式：GPUX 补查行回填功耗/风扇/频率/Bus-Id，不覆盖已有值', () => {
    const raw = [
      '===CKUN_MON===', 'STAT=1|2|3', 'MEM_TOTAL=100',
      'GPU=0|Tesla T4|30|512|15360|55||||||||||||',
      'GPU=1|Tesla T4|10|256|15360|50|99.9|120||||||||||',
      'GPUX=0|80.5|120|55|1590|5001|00000000:3D:00.0',
      'GPUX=1|70|120|50|1600|5001|00000000:3E:00.0',
      '===END===',
    ].join('\n')
    const [g0, g1] = parseMetricsRaw(raw)!.gpus
    expect([g0.powerDraw, g0.powerLimit, g0.fanSpeed, g0.smClock, g0.memClock, g0.busId])
      .toEqual([80.5, 120, 55, 1590, 5001, '00000000:3D:00.0'])
    // GPU= 行已有的 powerDraw 不被 GPUX 覆盖；空 busId 仍回填
    expect(g1.powerDraw).toBe(99.9)
    expect(g1.busId).toBe('00000000:3E:00.0')
  })

  it('无 GPU 行时 gpus 为空数组（不影响其它指标）', () => {
    const m = parseMetricsRaw('===CKUN_MON===\nSTAT=1|2|3\nMEM_TOTAL=100\n===END===')
    expect(m!.gpus).toEqual([])
    expect(m!.gpuError).toBe('')
  })

  it('GPUERR= 行 → gpuError 透传（nvidia-smi 存在但执行失败）', () => {
    const m = parseMetricsRaw('===CKUN_MON===\nSTAT=1|2|3\nMEM_TOTAL=100\nGPUERR=Failed to initialize NVML: Driver/library version mismatch\n===END===')
    expect(m!.gpus).toEqual([])
    expect(m!.gpuError).toContain('Driver/library version mismatch')
  })

  it('端口达上限(30)后 GPU 行仍被解析（回归：曾因 break 整循环丢 GPU）', () => {
    const ports = Array.from({ length: 31 }, (_, i) => `PORT=tcp|0.0.0.0|${1000 + i}`)
    const raw = ['===CKUN_MON===', 'STAT=1|2|3', 'MEM_TOTAL=100', ...ports, 'GPU=0|NVIDIA A100|50|1|2|60', '===END==='].join('\n')
    const m = parseMetricsRaw(raw)
    expect(m!.ports).toHaveLength(30)
    expect(m!.gpus).toHaveLength(1)
    expect(m!.gpus[0].powerDraw).toBeNull() // 短行（缺扩展段）不丢行，扩展字段 null
  })

  it('磁盘达上限(8)后 GPU 行仍被解析（回归）', () => {
    const disks = Array.from({ length: 9 }, (_, i) => `DISK=/mnt/d${i}|100|50|50%`)
    const raw = ['===CKUN_MON===', 'STAT=1|2|3', 'MEM_TOTAL=100', ...disks, 'GPU=0|NVIDIA A100|50|1|2|60', '===END==='].join('\n')
    const m = parseMetricsRaw(raw)
    expect(m!.disks).toHaveLength(8)
    expect(m!.gpus).toHaveLength(1)
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
