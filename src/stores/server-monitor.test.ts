/**
 * server-monitor 解析逻辑测试（B 阶段）
 * fixture 取自真实 Linux（WSL2 Ubuntu 26.04）上 METRICS_CMD / PROCS_CMD 的输出
 */
import { describe, expect, it, vi } from 'vitest'
import { parseMetricsRaw, parseProcsRaw, annotatePortUsers, computeCpuDiff, describeMonitorError, type PortEntry } from './server-monitor-core'

const FIXTURE = [
  '===CKUN_MON===',
  'STAT=321399|319801|288132',
  'LOAD=2.13 0.96 0.37',
  'MEM_TOTAL=15895',
  'MEM_USED=677',
  'MEM_PCT=4.3',
  'DISK=/|1031019|128627|14%',
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

  it('解析进程 TOP（10 段格式：ppid + comm + args，与 PROCS_CMD 真实输出一致）', () => {
    const raw = [
      '===CKUN_PROCS===',
      'PROC=1212|1180|7.4|0.0|root|5800|01:23:45|R|bash|bash -lc ps -eo pid,comm',
      'PROC=784|1|4.8|0.6|docker|102400|3-04:11:09|S|dockerd|/usr/bin/dockerd -H fd:// --containerd=/run/containerd/containerd.sock',
      '===END===',
    ].join('\n')
    const procs = parseProcsRaw(raw)
    expect(procs).toHaveLength(2)
    expect(procs[0]).toEqual({
      pid: 1212, ppid: 1180, cpu: 7.4, mem: 0, user: 'root', rss: 5800,
      etime: '01:23:45', stat: 'R', name: 'bash', args: 'bash -lc ps -eo pid,comm',
    })
    expect(procs[1]).toMatchObject({ pid: 784, ppid: 1, name: 'dockerd' })
    expect(procs[1].args).toContain('--containerd=/run/containerd/containerd.sock')
  })

  it('进程 TOP：args 内含 | 也能整段拼回，无命令行时为空串', () => {
    const raw = [
      '===CKUN_PROCS===',
      'PROC=22|1|1.0|0.1|root|100|00:10|Ss|python|/usr/bin/python3 -m uvicorn app:main --log-config a|b',
      'PROC=33|1|0.5|0.1|root|100|00:10|R|python|',
      '===END===',
    ].join('\n')
    const procs = parseProcsRaw(raw)
    expect(procs[0].args).toBe('/usr/bin/python3 -m uvicorn app:main --log-config a|b')
    expect(procs[0].ppid).toBe(1)
    expect(procs[1].args).toBe('')
  })

  it('进程 TOP：标记行缺失返回空数组（非本命令输出）', () => {
    expect(parseProcsRaw('PROC=1|1|1|root|1|1|R|x')).toEqual([])
  })

  it('解析监听端口（通配双栈折叠：0.0.0.0+[::] 合并为一条；精确地址独立）', () => {
    const m = parseMetricsRaw(FIXTURE)
    // FIXTURE 4 行端口：tcp 0.0.0.0:22 + tcp [::]:22 → 折叠为 1 条；udp 127.0.0.1:323 + udp [::1]:323 → 精确地址不折叠
    expect(m!.ports).toHaveLength(3)
    expect(m!.ports[0]).toMatchObject({ proto: 'tcp', addr: '0.0.0.0', port: 22, secondaryAddrs: ['[::]'] })
    expect(m!.ports[1]).toMatchObject({ proto: 'udp', addr: '127.0.0.1', port: 323 })
    expect(m!.ports[2]).toMatchObject({ proto: 'udp', addr: '[::1]', port: 323 })
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

  it('端口达上限(100)后 GPU 行仍被解析（回归：曾因 break 整循环丢 GPU）', () => {
    const ports = Array.from({ length: 101 }, (_, i) => `PORT=tcp|0.0.0.0|${1000 + i}`)
    const raw = ['===CKUN_MON===', 'STAT=1|2|3', 'MEM_TOTAL=100', ...ports, 'GPU=0|NVIDIA A100|50|1|2|60', '===END==='].join('\n')
    const m = parseMetricsRaw(raw)
    expect(m!.ports).toHaveLength(100)
    expect(m!.gpus).toHaveLength(1)
    expect(m!.gpus[0].powerDraw).toBeNull() // 短行（缺扩展段）不丢行，扩展字段 null
  })

  it('双栈通配地址（0.0.0.0 + [::]）→ 合并为一条，IPv4 主显示，IPv6 入 secondaryAddrs', () => {
    const raw = [
      '===CKUN_MON===', 'STAT=1|2|3', 'MEM_TOTAL=100',
      'PORT=tcp|0.0.0.0|80|1234|nginx',
      'PORT=tcp|[::]|80|1234|nginx',
      'PORT=tcp|0.0.0.0|443|5678|nginx',
      '===END===',
    ].join('\n')
    const m = parseMetricsRaw(raw)
    expect(m!.ports).toHaveLength(2)
    // port 80 合并为一条：IPv4 主显示，IPv6 在 secondaryAddrs
    const p80 = m!.ports.find(p => p.port === 80)!
    expect(p80.addr).toBe('0.0.0.0')
    expect(p80.secondaryAddrs).toEqual(['[::]'])
    // port 443 只有 IPv4，无 secondaryAddrs
    const p443 = m!.ports.find(p => p.port === 443)!
    expect(p443.addr).toBe('0.0.0.0')
    expect(p443.secondaryAddrs).toBeUndefined()
  })

  it('通配地址格式变异（*/*[::]）→ 统一展示为 0.0.0.0/[::]', () => {
    const raw = [
      '===CKUN_MON===', 'STAT=1|2|3', 'MEM_TOTAL=100',
      'PORT=tcp|*|22|100|sshd',     // * = IPv4 通配（busybox ss）
      'PORT=tcp|::|22|100|sshd',    // :: = IPv6 通配（无括号格式）
      '===END===',
    ].join('\n')
    const m = parseMetricsRaw(raw)
    expect(m!.ports).toHaveLength(1)
    expect(m!.ports[0]).toMatchObject({ addr: '0.0.0.0', secondaryAddrs: ['[::]'], port: 22 })
  })

  it('纯 IPv6 监听（仅 [::]）→ 主显示 [::]，无 secondaryAddrs', () => {
    const raw = [
      '===CKUN_MON===', 'STAT=1|2|3', 'MEM_TOTAL=100',
      'PORT=tcp|[::]|8080|999|node',
      '===END===',
    ].join('\n')
    const m = parseMetricsRaw(raw)
    expect(m!.ports).toHaveLength(1)
    expect(m!.ports[0].addr).toBe('[::]')
    expect(m!.ports[0].secondaryAddrs).toBeUndefined()
  })

  it('精确地址（127.0.0.1 等）不参与通配折叠，独立成行', () => {
    const raw = [
      '===CKUN_MON===', 'STAT=1|2|3', 'MEM_TOTAL=100',
      'PORT=tcp|127.0.0.1|5432|777|postgres',
      'PORT=tcp|0.0.0.0|5432|888|postgres',
      'PORT=tcp|[::1]|5432|999|postgres',
      '===END===',
    ].join('\n')
    const m = parseMetricsRaw(raw)
    expect(m!.ports).toHaveLength(3)  // 三个独立地址，不通配折叠
  })

  it('端口重复（同 proto|addr|port 多行）→ 去重保留首条，有 pid 行覆盖无 pid 行', () => {
    const raw = [
      '===CKUN_MON===', 'STAT=1|2|3', 'MEM_TOTAL=100',
      'PORT=tcp|0.0.0.0|80||',          // 无 pid（非特权采集）
      'PORT=tcp|0.0.0.0|80|1234|nginx', // 有 pid（sudo 补全）
      'PORT=tcp|0.0.0.0|443|5678|nginx',
      '===END===',
    ].join('\n')
    const m = parseMetricsRaw(raw)
    expect(m!.ports).toHaveLength(2)
    // tcp|0.0.0.0|80 合并为一条，pid 从 sudo 补全的行回填
    expect(m!.ports[0]).toMatchObject({ proto: 'tcp', addr: '0.0.0.0', port: 80, pid: 1234, proc: 'nginx' })
    expect(m!.ports[1]).toMatchObject({ proto: 'tcp', addr: '0.0.0.0', port: 443, pid: 5678 })
  })

  it('TCP + UDP 同端口 → 合并为一条，proto 偏好 TCP，UDP 入 secondaryProtos', () => {
    const raw = [
      '===CKUN_MON===', 'STAT=1|2|3', 'MEM_TOTAL=100',
      'PORT=tcp|0.0.0.0|53|100|systemd-resolve',
      'PORT=udp|0.0.0.0|53|100|systemd-resolve',
      '===END===',
    ].join('\n')
    const m = parseMetricsRaw(raw)
    expect(m!.ports).toHaveLength(1)
    expect(m!.ports[0]).toMatchObject({
      proto: 'tcp', addr: '0.0.0.0', port: 53,
      secondaryProtos: ['udp'],
      pid: 100, proc: 'systemd-resolve',
    })
  })

  it('地址范围标识（127.0.0.53%lo）→ 剥离 scope 后与其他格式的去重合并', () => {
    const raw = [
      '===CKUN_MON===', 'STAT=1|2|3', 'MEM_TOTAL=100',
      // 非特权 ss -ltn 输出不带 %scope
      'PORT=tcp|127.0.0.53|53|100|systemd-resolve',
      // 特权 sudo ss -ltnp 输出带 %scope（awk 两份输出的 key 不同）
      'PORT=tcp|127.0.0.53%lo|53|100|systemd-resolve',
      '===END===',
    ].join('\n')
    const m = parseMetricsRaw(raw)
    expect(m!.ports).toHaveLength(1)  // scope 剥离后 key 相同，合并
    expect(m!.ports[0]).toMatchObject({ proto: 'tcp', addr: '127.0.0.53', port: 53, pid: 100 })
  })

  it('TCP+UDP 同端口（scope 地址变体）→ 合并后只保留一条，addr 剥离 scope', () => {
    const raw = [
      '===CKUN_MON===', 'STAT=1|2|3', 'MEM_TOTAL=100',
      'PORT=tcp|127.0.0.53%lo|53|100|systemd-resolve',
      'PORT=udp|127.0.0.53|53||systemd-resolve',
      '===END===',
    ].join('\n')
    const m = parseMetricsRaw(raw)
    expect(m!.ports).toHaveLength(1)
    expect(m!.ports[0]).toMatchObject({
      proto: 'tcp', addr: '127.0.0.53', port: 53,
      secondaryProtos: ['udp'], pid: 100,
    })
  })

  it('新字段 recvq/sendq/iface 解析 → 端口条目附带 backlog、队列、接口信息', () => {
    const raw = [
      '===CKUN_MON===', 'STAT=1|2|3', 'MEM_TOTAL=100',
      'PORT=tcp|0.0.0.0|22|1234|sshd|0|128|*',
      'PORT=tcp|127.0.0.53%lo|53|567|systemd-resolve|3|511|lo',
      'PORT=udp|0.0.0.0|68|89|dhclient|0|64|*',
      '===END===',
    ].join('\n')
    const m = parseMetricsRaw(raw)
    expect(m!.ports).toHaveLength(3)

    const ssh = m!.ports.find(p => p.port === 22)!
    expect(ssh.recvq).toBe(0)
    expect(ssh.sendq).toBe(128)
    expect(ssh.iface).toBeUndefined()  // '*' = 全局监听，不展示接口

    const dns = m!.ports.find(p => p.port === 53)!
    expect(dns.recvq).toBe(3)
    expect(dns.sendq).toBe(511)
    expect(dns.iface).toBe('lo')

    const dhcp = m!.ports.find(p => p.port === 68)!
    expect(dhcp.recvq).toBe(0)
    expect(dhcp.sendq).toBe(64)
  })

  it('TCP+UDP 同端口分属不同进程 → 合并行登记 secondaryPids（UI 据此禁用终止）', () => {
    const raw = [
      '===CKUN_MON===', 'STAT=1|2|3', 'MEM_TOTAL=100',
      'PORT=tcp|0.0.0.0|5353|111|avahi',
      'PORT=udp|0.0.0.0|5353|222|other-daemon',
      '===END===',
    ].join('\n')
    const m = parseMetricsRaw(raw)
    expect(m!.ports).toHaveLength(1)
    expect(m!.ports[0]).toMatchObject({
      proto: 'tcp', pid: 111, secondaryProtos: ['udp'], secondaryPids: [222],
    })
  })

  it('TCP+UDP 同端口同一进程（systemd-resolved 形态）→ 不登记 secondaryPids', () => {
    const raw = [
      '===CKUN_MON===', 'STAT=1|2|3', 'MEM_TOTAL=100',
      'PORT=tcp|127.0.0.53|53|567|systemd-resolve|0|511|lo',
      'PORT=udp|127.0.0.53|53|567|systemd-resolve|0|0|lo',
      '===END===',
    ].join('\n')
    const m = parseMetricsRaw(raw)
    expect(m!.ports).toHaveLength(1)
    expect(m!.ports[0].secondaryPids).toBeUndefined()
    // recvq/sendq 取先到的 TCP 侧（UDP 的 Send-Q 是缓冲区字节数，不是 backlog）
    expect(m!.ports[0]).toMatchObject({ proto: 'tcp', recvq: 0, sendq: 511 })
  })

  it('方括号 IPv6 带 scope（[fe80::1%eth0]）→ 剥离 scope 保留右括号，与非 scope 变体去重', () => {
    const raw = [
      '===CKUN_MON===', 'STAT=1|2|3', 'MEM_TOTAL=100',
      'PORT=tcp|[fe80::1]|8080|200|app',
      // 特权 ss -ltnp 输出带 %ifname：截到 % 会丢右括号，需补回 ']'
      'PORT=tcp|[fe80::1%eth0]|8080|200|app',
      '===END===',
    ].join('\n')
    const m = parseMetricsRaw(raw)
    expect(m!.ports).toHaveLength(1)
    expect(m!.ports[0]).toMatchObject({ proto: 'tcp', addr: '[fe80::1]', port: 8080, pid: 200 })
  })

  it('进程重复（同 PID 多行）→ 去重保留首条', () => {
    const raw = [
      '===CKUN_PROCS===',
      'PROC=123|1|5.0|1.0|root|1024|01:00|R|bash|bash',
      'PROC=456|1|3.0|0.5|root|2048|02:00|S|sshd|sshd: root@pts/0',
      'PROC=123|1|9.9|9.9|root|9999|99:99|Rz|phantom|phantom', // 同 PID 重复行
      '===END===',
    ].join('\n')
    const procs = parseProcsRaw(raw)
    expect(procs).toHaveLength(2)
    expect(procs[0].pid).toBe(123)
    expect(procs[0].cpu).toBe(5.0)  // 保留首条，不覆盖为 9.9
    expect(procs[1].pid).toBe(456)
  })

  it('PORT= 行带 ss -p 进程段 → pid/proc 解析；缺省段与旧三段格式向后兼容', () => {
    const raw = [
      '===CKUN_MON===', 'STAT=1|2|3', 'MEM_TOTAL=100',
      'PORT=tcp|0.0.0.0|22|812|sshd',
      'PORT=tcp|0.0.0.0|5432||', // 进程段为空（非特权采集看不到他人进程）
      'PORT=udp|127.0.0.1|323', // 旧三段格式（升级前的采集端）
      '===END===',
    ].join('\n')
    const m = parseMetricsRaw(raw)
    expect(m!.ports[0]).toMatchObject({ proto: 'tcp', addr: '0.0.0.0', port: 22, pid: 812, proc: 'sshd' })
    expect(m!.ports[1].pid).toBeUndefined()
    expect(m!.ports[1].proc).toBeUndefined()
    expect(m!.ports[2].pid).toBeUndefined()
    expect(m!.ports[2].port).toBe(323)
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

  it('轮询硬超时（withTimeout 标签）→ 中文文案 + 可重试（回归：曾直透英文 "metrics poll timeout"）', () => {
    const r = describeMonitorError(new Error('metrics poll timeout'))
    expect(r.code).toBe('MON_TIMEOUT')
    expect(r.retryable).toBe(true)
    expect(r.message).toContain('指标采集超时')
    expect(r.message).not.toContain('metrics poll timeout')
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

/**
 * stop() 的状态清零完整性。
 *
 * 这里曾经连出两次同族缺陷：stop() 只停表不清读数（DashboardView 继续渲染上一台
 * 主机最后一次采样，空态分支不可达），补上清零后又漏了 lastError —— 而
 * DashboardView 的分支顺序是「先 v-if=lastError，再 v-else 空态」，于是关掉主机、
 * 开一个本地 shell，看到的仍是「监控已中断：…（连接恢复后自动重连）」，指向一个
 * 已经不存在的会话，且这一整个页面会话期间再也回不到空态。
 * start()/stop() 各抄一份清单就是漂移的入口，现在只有 stop() 一处。
 */
describe('stop() 必须清空全部读数（含错误态）', () => {
  it('停表后不留任何上一台主机的状态', async () => {
    const { createPinia, setActivePinia } = await import('pinia')
    setActivePinia(createPinia())
    const { useServerMonitorStore } = await import('./server-monitor')
    const mon = useServerMonitorStore()

    // 先跑一轮 start 的真实路径留下脏状态：无 electronAPI 的采样必然失败并记 lastError
    mon.current = { host: 'A', sshId: 's-1' } as never
    mon.procs.push({ pid: 1, ppid: 0, cpu: 1, mem: 0, name: 'x', args: '', user: 'root', rss: 1, etime: '0:01', stat: 'S' })
    mon.cpuHistory.push(12)
    mon.memHistory.push(30)
    mon.loadHistory.push(1.2)
    mon.gpuHist = { '0': [1, 2] } as never
    mon.lastError = { message: '监控已中断', code: 'SESSION_NOT_FOUND', retryable: true } as never
    mon.polling = true

    mon.stop()

    expect(mon.polling).toBe(false)
    expect(mon.current).toBe(null)
    // 进程 TOP 也要清零：不留上一台主机的进程表
    expect(mon.procs).toEqual([])
    expect(mon.cpuHistory).toEqual([])
    expect(mon.memHistory).toEqual([])
    expect(mon.loadHistory).toEqual([])
    expect(mon.gpuHist).toEqual({})
    // 这条是补漏的那一项：lastError 不清，空态分支永远不可达
    expect(mon.lastError).toBe(null)
  })
})

describe('annotatePortUsers', () => {
  it('端口 PID 匹配到进程 → 回填属主名', () => {
    const rawPorts: PortEntry[] = [
      { proto: 'tcp', addr: '0.0.0.0', port: 22, pid: 1234, proc: 'sshd' },
      { proto: 'tcp', addr: '0.0.0.0', port: 80 },
    ]
    annotatePortUsers(rawPorts, [{ pid: 1234, user: 'root' }])
    expect(rawPorts[0].user).toBe('root')
    expect(rawPorts[1].user).toBeUndefined()
  })
})

/**
 * GPU 进程表的展示节奏（GPU_PROCS_INTERVAL_MS）。
 *
 * GPROC/GPS 段和 CPU/内存挤在同一条 nvidia-smi 命令里，随 5s 快轮询一起回来；
 * 而「进程（GPU）」表头写的是每 60 秒刷新 —— 所以表必须真的 60s 才换一次。
 * 两条判据：到 60s 才换；换会话立刻换（否则新主机会看到上一台的进程）。
 */
describe('GPU 进程表 60s 换快照（采集仍是 5s）', () => {
  const metricsWith = (pid: number, util: number) => [
    '===CKUN_MON===',
    'STAT=100|80|20',
    'MEM_TOTAL=1024',
    'MEM_USED=512',
    'MEM_PCT=50',
    'SWAP_TOTAL=0',
    'SWAP_USED=0',
    `GPU=0|NVIDIA A100|${util}|1024|40960|60|100|300|N/A|1500|9802|GPU-abc0|580.1|x|P0|Enabled|Default|N/A`,
    `GPROC=GPU-abc0|${pid}|1008|python train.py`,
    `GPS=${pid}|root|1.0|2.0|01:00:00`,
    '===END===',
  ].join('\n')

  it('5 秒轮询不改表，60 秒才改；换会话立即改', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(0)
    const g = globalThis as unknown as Record<string, unknown>
    let stdout = metricsWith(111, 50)
    g.window = { electronAPI: { sshExec: async () => ({ stdout }) } }
    g.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} }

    const { createPinia, setActivePinia } = await import('pinia')
    setActivePinia(createPinia())
    const { useSSHStore } = await import('./ssh')
    const ssh = useSSHStore()
    ssh.connections.push({ id: 'c-1', host: 'h1' } as never)
    ssh.sessions.push({ id: 's-1', type: 'remote', connectionId: 'c-1' } as never)
    ssh.sessions.push({ id: 's-2', type: 'remote', connectionId: 'c-1' } as never)

    const { useServerMonitorStore } = await import('./server-monitor')
    const mon = useServerMonitorStore()

    mon.start('s-1')
    await vi.advanceTimersByTimeAsync(0)
    expect(mon.gpuProcs.map(p => p.pid)).toEqual([111])

    // 远端数据已变，推进一个 5s 轮次
    stdout = metricsWith(222, 70)
    await vi.advanceTimersByTimeAsync(5000)
    // 利用率这类 5s 字段照常跟新 —— 证明只节流进程表，不是整张卡停更
    expect(mon.current!.gpus[0].utilization).toBe(70)
    expect(mon.gpuProcs.map(p => p.pid)).toEqual([111])

    // 到 60s 边界才换表
    await vi.advanceTimersByTimeAsync(55000)
    expect(mon.gpuProcs.map(p => p.pid)).toEqual([222])

    // 换会话：不等 60s，立刻换成本会话的快照
    stdout = metricsWith(333, 90)
    mon.start('s-2')
    await vi.advanceTimersByTimeAsync(0)
    expect(mon.gpuProcs.map(p => p.pid)).toEqual([333])

    mon.stop()
    // stop() 也要把这张表清干净（它不在 current 里，单靠 current=null 盖不住）
    expect(mon.gpuProcs).toEqual([])
    vi.useRealTimers()
    delete g.window
    delete g.localStorage
  })
})

/**
 * Q-F5 ①：retained 快照随会话从列表移除而清理。
 *
 * 生命线判据：stop() 不得清 retained —— start() 的切会话流程是
 * retainActive() → stop() → restoreFrom(retained[sshId])，stop() 清了它
 * 就回退「切 tab 保留 sparkline 历史」。清理只由「会话 id 从列表消失」触发。
 */
describe('retained 快照随会话移除清理（Q-F5）', () => {
  it('已消失会话的条目被删；在列会话与 stop() 都不动', async () => {
    const { createPinia, setActivePinia } = await import('pinia')
    setActivePinia(createPinia())
    const { nextTick } = await import('vue')
    const g = globalThis as unknown as Record<string, unknown>
    g.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} }

    const { useSSHStore } = await import('./ssh')
    const ssh = useSSHStore()
    ssh.connections.push({ id: 'c-1', host: 'h1' } as never)
    ssh.sessions.push({ id: 's-1', type: 'remote', connectionId: 'c-1' } as never)
    ssh.sessions.push({ id: 's-2', type: 'remote', connectionId: 'c-1' } as never)
    ssh.sessions.push({ id: 's-3', type: 'remote', connectionId: 'c-1' } as never)

    const { useServerMonitorStore } = await import('./server-monitor')
    const mon = useServerMonitorStore()

    // 切两次会话：s-1、s-2 各自被 retainActive 归档；s-3 活跃
    mon.start('s-1')
    mon.start('s-2')
    mon.start('s-3')
    expect(mon.retained['s-1']).toBeTruthy()
    expect(mon.retained['s-2']).toBeTruthy()

    // 移除已归档的 s-1 → 只清它的条目，在列的 s-2 不动
    ssh.sessions.splice(ssh.sessions.findIndex(s => s.id === 's-1'), 1)
    await nextTick()
    expect(mon.retained['s-1']).toBeUndefined()
    expect(mon.retained['s-2']).toBeTruthy()

    // stop() 是快照机制生命线：不得清 retained（否则切 tab 历史（s-2）被抹）
    mon.stop()
    expect(mon.retained['s-2']).toBeTruthy()

    // 活跃会话也会被移除：重新监控 s-3 再切走（retainActive 归档它），
    // 移除后只清 s-3 自己的条目，活跃的 s-2 不误清
    mon.start('s-3')
    mon.start('s-2')
    expect(mon.retained['s-3']).toBeTruthy()
    ssh.sessions.splice(ssh.sessions.findIndex(s => s.id === 's-3'), 1)
    await nextTick()
    expect(mon.retained['s-3']).toBeUndefined()
    expect(mon.retained['s-2']).toBeTruthy()

    mon.stop()
    delete g.localStorage
  })
})

/**
 * Q-F5 ②：retry 模式（polling=false，sampleProcs 早退）期间 60s 的 procsTimer
 * 不再空转自排；采样成功恢复轮询时必然重挂（手动重连走同一条恢复路径）。
 */
describe('retry 模式进程采样停排与恢复（Q-F5）', () => {
  it('进入重试后 60s 无进程采样；探针成功恢复轮询即补采并重挂 60s', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(0)
    const g = globalThis as unknown as Record<string, unknown>
    const METRICS = '===CKUN_MON===\nSTAT=1|2|3\nMEM_TOTAL=100\n===END==='
    let fail = true
    const exec = vi.fn(async (_id: string, cmd: string) => {
      if (fail) throw new Error('connection down')
      return { stdout: cmd.includes('CKUN_PROCS') ? '===CKUN_PROCS===\n===END===' : METRICS, stderr: '', exitCode: 0 }
    })
    g.window = { electronAPI: { sshExec: exec } }
    g.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} }

    const { createPinia, setActivePinia } = await import('pinia')
    setActivePinia(createPinia())
    const { useSSHStore } = await import('./ssh')
    const ssh = useSSHStore()
    ssh.connections.push({ id: 'c-1', host: 'h1' } as never)
    ssh.sessions.push({ id: 's-1', type: 'remote', connectionId: 'c-1' } as never)

    const { useServerMonitorStore } = await import('./server-monitor')
    const mon = useServerMonitorStore()

    const procsCalls = () => exec.mock.calls.filter((c: Array<unknown>) => String(c[1]).includes('CKUN_PROCS')).length

    mon.start('s-1')
    // 连续三次采样失败（t0/t5/t10）→ 转入后台重试模式
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(5_000)
    await vi.advanceTimersByTimeAsync(5_000)
    expect(mon.polling).toBe(false)

    const procsAtRetry = procsCalls()
    // retry 期间 60s 到点也不再有进程采样（procsTimer 已停排，不再空转自排）
    await vi.advanceTimersByTimeAsync(60_000)
    expect(procsCalls()).toBe(procsAtRetry)

    // 目标会话恢复：下一次 15s 重试探针成功 → 恢复轮询 + 进程采样立即补采
    fail = false
    await vi.advanceTimersByTimeAsync(15_000)
    expect(mon.polling).toBe(true)
    const procsAtRestore = procsCalls()
    expect(procsAtRestore).toBeGreaterThan(procsAtRetry)

    // 恢复路径必然重挂 60s 定时器：再过一个周期有新的进程采样
    await vi.advanceTimersByTimeAsync(60_000)
    expect(procsCalls()).toBeGreaterThan(procsAtRestore)

    mon.stop()
    vi.useRealTimers()
    delete g.window
    delete g.localStorage
  })
})
