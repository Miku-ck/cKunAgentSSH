/**
 * alert-trigger-core — 告警判断纯逻辑守卫。
 *
 * 重点钉死四条设计契约（docs/p0-ops-agent-enhancements.md §1.2/§1.5）：
 * ①连续 N 次越限计数升/降级与重置；②全 0 CPU 差分样本不计入（对齐
 * computeCpuDiff 首轮/计数器未前进返回全 0 的契约，server-monitor-core.ts:603-604）；
 * ③冷却期内不重复触发、冷却以触发时刻起算（run 失败照常计时）；
 * ④task 文本包含主机名/规则/越限指标且 ≤4000 字符；⑤AlertMonitorTask 校验与模板。
 */
import { describe, expect, it } from 'vitest'
import type { ServerMetrics } from '@/stores/server-monitor-core'
import {
  ALERT_HISTORY_TAIL,
  ALERT_TASK_MAX_CHARS,
  alertMonitorTemplate,
  buildDiagnosisTaskText,
  createAlertRuntime,
  describeRule,
  evaluateRules,
  markTriggered,
  processSample,
  resetAlertRuntime,
  sanitizeAlertMonitorTask,
  sanitizeAlertMonitorTasks,
  setInFlight,
  type AlertRule,
} from './alert-trigger-core'

function makeMetrics(over: Partial<ServerMetrics> = {}): ServerMetrics {
  return {
    ts: 1_700_000_000_000,
    host: 'web-01',
    sshId: 'ssh-1',
    cpu: { used: 10, iowait: 1 },
    load: [1, 1, 1],
    mem: { total: 8000, used: 4000, percent: 50 },
    swap: { total: 0, used: 0 },
    disks: [{ mount: '/', total: 51200, used: 10240, percent: 20 }],
    ports: [],
    gpus: [],
    gpuError: '',
    gpuDriver: '',
    gpuCuda: '',
    ...over,
  }
}

function rule(over: Partial<AlertRule> = {}): AlertRule {
  return { id: 'r', metric: 'cpu', op: '>', threshold: 90, consecutiveN: 3, enabled: true, diskScope: 'any', ...over }
}

/** 用单规则喂样本的便捷封装：返回是否触发 */
function feed(rt: ReturnType<typeof createAlertRuntime>, r: AlertRule, host: string, m: ServerMetrics, now: number): boolean {
  const evals = evaluateRules([r], m)
  const fired = processSample(rt, host, evals, now)
  return fired.includes(r)
}

describe('evaluateRules 取值域（对齐 ServerMetrics 形态）', () => {
  it('cpu/iowait/mem/load1 按字段直取', () => {
    const m = makeMetrics({ cpu: { used: 88, iowait: 7 }, mem: { total: 8000, used: 7200, percent: 90 }, load: [4.2, 3, 2] })
    const [cpu, io, mem, load1] = evaluateRules([rule({ metric: 'cpu' }), rule({ metric: 'iowait' }), rule({ metric: 'mem' }), rule({ metric: 'load1' })], m)
    expect(cpu.value).toBe(88)
    expect(io.value).toBe(7)
    expect(mem.value).toBe(90)
    expect(load1.value).toBe(4.2)
  })

  it('disk：any 取全部挂载点最大值；mount: 指定挂载点；缺失 → null（fail-closed 不触发）', () => {
    const m = makeMetrics({
      disks: [
        { mount: '/', total: 51200, used: 48640, percent: 95 },
        { mount: '/data', total: 102400, used: 30720, percent: 30 },
      ],
    })
    const [any, mount, missing] = evaluateRules(
      [rule({ metric: 'disk' }), rule({ metric: 'disk', diskScope: 'mount:/data' }), rule({ metric: 'disk', diskScope: 'mount:/gone' })],
      m,
    )
    expect(any.value).toBe(95)
    expect(any.breached).toBe(true)
    expect(mount.value).toBe(30)
    expect(missing.value).toBeNull()
    expect(missing.breached).toBe(false)
  })

  it('disk：无磁盘数据 → null，不触发', () => {
    const [ev] = evaluateRules([rule({ metric: 'disk' })], makeMetrics({ disks: [] }))
    expect(ev.value).toBeNull()
    expect(ev.breached).toBe(false)
  })

  it('gpu_mem：取各卡显存最大值；无卡 → null', () => {
    const gpus = [
      { memPercent: 60, index: 0, name: 'A', utilization: 10, memUsed: 1, memTotal: 2, temperature: 30, powerDraw: null, powerLimit: null, fanSpeed: null, smClock: null, memClock: null, uuid: '', busId: '', perfState: '', persistence: '', computeMode: '', eccUncorr: null },
      { memPercent: 97, index: 1, name: 'B', utilization: 99, memUsed: 1, memTotal: 2, temperature: 30, powerDraw: null, powerLimit: null, fanSpeed: null, smClock: null, memClock: null, uuid: '', busId: '', perfState: '', persistence: '', computeMode: '', eccUncorr: null },
    ]
    const [has, none] = [evaluateRules([rule({ metric: 'gpu_mem' })], makeMetrics({ gpus })), evaluateRules([rule({ metric: 'gpu_mem' })], makeMetrics())]
      .map(evals => evals[0])
    expect(has.value).toBe(97)
    expect(has.breached).toBe(true)
    expect(none.value).toBeNull()
  })

  it('swap：total=0（无交换分区）→ null 规则跳过；有 swap 按占比', () => {
    const [noSwap, withSwap] = [evaluateRules([rule({ metric: 'swap' })], makeMetrics({ swap: { total: 0, used: 0 } })), evaluateRules([rule({ metric: 'swap' })], makeMetrics({ swap: { total: 2048, used: 1024 } }))]
      .map(evals => evals[0])
    expect(noSwap.value).toBeNull()
    expect(noSwap.breached).toBe(false)
    expect(withSwap.value).toBeCloseTo(50)
  })

  it('op 四向：< 与 <= 服务低阈值规则', () => {
    const m = makeMetrics({ mem: { total: 8000, used: 400, percent: 5 } })
    const [lt, lte, gt] = evaluateRules(
      [rule({ metric: 'mem', op: '<', threshold: 10 }), rule({ metric: 'mem', op: '<=', threshold: 5 }), rule({ metric: 'mem', op: '>', threshold: 90 })],
      m,
    )
    expect(lt.breached).toBe(true)
    expect(lte.breached).toBe(true)
    expect(gt.breached).toBe(false)
  })

  it('全 0 CPU 差分（computeCpuDiff 首轮/计数器未前进）→ cpu/iowait 无效样本不参与计数', () => {
    // 低阈值规则是误计重灾区：全 0 会被当成 <10% 的「越限」
    const m = makeMetrics({ cpu: { used: 0, iowait: 0 }, mem: { total: 8000, used: 400, percent: 5 } })
    const [cpu, io, mem] = evaluateRules(
      [rule({ metric: 'cpu', op: '<', threshold: 10 }), rule({ metric: 'iowait', op: '<', threshold: 10 }), rule({ metric: 'mem', op: '<', threshold: 10 })],
      m,
    )
    expect(cpu.validSample).toBe(false)
    expect(cpu.breached).toBe(false)
    expect(io.validSample).toBe(false)
    expect(io.breached).toBe(false)
    // mem 与 CPU 差分无关，照常有效
    expect(mem.validSample).toBe(true)
    expect(mem.breached).toBe(true)
  })
})

describe('processSample 连续计数（升降级与重置）', () => {
  it('连续越限达 N 次才触发；恰好第 N 次触发一次', () => {
    const rt = createAlertRuntime()
    const r = rule({ consecutiveN: 3 })
    expect(feed(rt, r, 'h', makeMetrics({ cpu: { used: 95, iowait: 1 } }), 1000)).toBe(false)
    expect(feed(rt, r, 'h', makeMetrics({ cpu: { used: 95, iowait: 1 } }), 2000)).toBe(false)
    expect(feed(rt, r, 'h', makeMetrics({ cpu: { used: 95, iowait: 1 } }), 3000)).toBe(true)
  })

  it('未越限样本把计数归零，连续链重新累积', () => {
    const rt = createAlertRuntime()
    const r = rule({ consecutiveN: 3 })
    const high = makeMetrics({ cpu: { used: 95, iowait: 1 } })
    const low = makeMetrics({ cpu: { used: 20, iowait: 1 } })
    expect(feed(rt, r, 'h', high, 1000)).toBe(false)
    expect(feed(rt, r, 'h', high, 2000)).toBe(false)
    expect(feed(rt, r, 'h', low, 3000)).toBe(false)
    expect(feed(rt, r, 'h', high, 4000)).toBe(false)
    expect(feed(rt, r, 'h', high, 5000)).toBe(false)
    // 归零后第 3 个连续越限样本才触发
    expect(feed(rt, r, 'h', high, 6000)).toBe(true)
  })

  it('规则不适用（swap 无）计数归零：不给陈旧计数触发机会', () => {
    const rt = createAlertRuntime()
    const r = rule({ metric: 'swap', threshold: 80, consecutiveN: 2 })
    // 前两个样本有 swap 且越限
    expect(feed(rt, r, 'h', makeMetrics({ swap: { total: 2048, used: 2000 } }), 1000)).toBe(false)
    expect(feed(rt, r, 'h', makeMetrics({ swap: { total: 2048, used: 2000 } }), 2000)).toBe(true)
    markTriggered(rt, 'h', r.id, 2000, 30)
    // swap 消失（total=0）→ 计数归零；swap 恢复后需重新连续 2 次
    expect(feed(rt, r, 'h', makeMetrics({ swap: { total: 0, used: 0 } }), 2000 + 31 * 60_000)).toBe(false)
    expect(feed(rt, r, 'h', makeMetrics({ swap: { total: 2048, used: 2000 } }), 2000 + 32 * 60_000)).toBe(false)
    expect(feed(rt, r, 'h', makeMetrics({ swap: { total: 2048, used: 2000 } }), 2000 + 33 * 60_000)).toBe(true)
  })

  it('全 0 CPU 差分样本跳过：计数冻结（不升不降），不吞掉低阈值规则的连续链', () => {
    const rt = createAlertRuntime()
    const r = rule({ metric: 'cpu', op: '<', threshold: 10, consecutiveN: 2 })
    const low = makeMetrics({ cpu: { used: 5, iowait: 0.4 } })
    const invalid = makeMetrics({ cpu: { used: 0, iowait: 0 } })
    expect(feed(rt, r, 'h', low, 1000)).toBe(false)          // count=1
    expect(feed(rt, r, 'h', invalid, 2000)).toBe(false)      // 跳过，count 仍 1
    expect(feed(rt, r, 'h', low, 3000)).toBe(true)           // count=2 → 触发
  })

  it('高阈值规则下全 0 样本同样冻结计数（既不误触发也不清零）', () => {
    const rt = createAlertRuntime()
    const r = rule({ metric: 'cpu', op: '>', threshold: 90, consecutiveN: 3 })
    const high = makeMetrics({ cpu: { used: 95, iowait: 1 } })
    const invalid = makeMetrics({ cpu: { used: 0, iowait: 0 } })
    expect(feed(rt, r, 'h', high, 1000)).toBe(false)
    expect(feed(rt, r, 'h', high, 2000)).toBe(false)
    expect(feed(rt, r, 'h', invalid, 3000)).toBe(false)      // 冻结
    expect(feed(rt, r, 'h', high, 4000)).toBe(true)          // 第 3 个有效样本触发
  })

  it('enabled:false 的规则视同不适用：不评估、不触发，且清空已累积的陈旧计数', () => {
    const rt = createAlertRuntime()
    const on = rule({ id: 'r', consecutiveN: 2 })
    const off = rule({ id: 'r', consecutiveN: 2, enabled: false })
    const high = makeMetrics({ cpu: { used: 95, iowait: 1 } })
    expect(feed(rt, on, 'h', high, 1000)).toBe(false)        // 启用态累积 1 次
    const [ev] = evaluateRules([off], high)
    expect(ev!.value).toBeNull()                             // 禁用 → 不参与取值评估
    expect(ev!.breached).toBe(false)
    expect(feed(rt, off, 'h', high, 2000)).toBe(false)       // 禁用期间不触发
    expect(rt.counts.get('h\nr')).toBe(0)                    // 且陈旧计数被清零
    expect(feed(rt, on, 'h', high, 3000)).toBe(false)        // 重新启用：重新累积，1 次不够
    expect(feed(rt, on, 'h', high, 4000)).toBe(true)
  })
})

describe('冷却与在飞（以触发时刻起算）', () => {
  it('触发后冷却期内 count≥N 也不再触发；冷却过期后重新累积满 N 才再触发', () => {
    const rt = createAlertRuntime()
    const r = rule({ consecutiveN: 2, threshold: 90 })
    const high = makeMetrics({ cpu: { used: 95, iowait: 1 } })
    const T0 = 1_700_000_000_000
    expect(feed(rt, r, 'h', high, T0)).toBe(false)
    expect(feed(rt, r, 'h', high, T0 + 5000)).toBe(true)
    markTriggered(rt, 'h', r.id, T0 + 5000, 30)
    // 冷却内：计数重新累积到 N 也不触发
    expect(feed(rt, r, 'h', high, T0 + 10_000)).toBe(false)
    expect(feed(rt, r, 'h', high, T0 + 15_000)).toBe(false)
    expect(feed(rt, r, 'h', high, T0 + 29 * 60_000)).toBe(false)
    // 冷却刚过期（30min 后）计数已 ≥N：立即再触发 —— 持续越限 30 分钟后再告警是预期
    expect(feed(rt, r, 'h', high, T0 + 5000 + 30 * 60_000)).toBe(true)
  })

  it('markTriggered 的冷却截止 = 触发时刻 + cooldownMinutes（run 失败照常计时，不重置风暴窗口）', () => {
    const rt = createAlertRuntime()
    const T0 = 1_700_000_000_000
    markTriggered(rt, 'h', 'x', T0, 30)
    expect(rt.cooldownUntil.get('h\nx')).toBe(T0 + 30 * 60_000)
    expect(rt.counts.get('h\nx')).toBe(0)
  })

  it('in-flight 标志阻止同 tick 重复触发；释放后恢复', () => {
    const rt = createAlertRuntime()
    const r = rule({ consecutiveN: 1 })
    const high = makeMetrics({ cpu: { used: 95, iowait: 1 } })
    setInFlight(rt, 'h', r.id, true)
    expect(feed(rt, r, 'h', high, 1000)).toBe(false)
    setInFlight(rt, 'h', r.id, false)
    expect(feed(rt, r, 'h', high, 2000)).toBe(true)
  })
})

describe('resetAlertRuntime 与 AlertMonitorTask 校验', () => {
  it('reset 清空计数/冷却/在飞：切换主机或 stop 后需重新累积', () => {
    const rt = createAlertRuntime()
    const r = rule({ consecutiveN: 2 })
    const high = makeMetrics({ cpu: { used: 95, iowait: 1 } })
    expect(feed(rt, r, 'h', high, 1000)).toBe(false)
    markTriggered(rt, 'h', r.id, 2000, 60)
    setInFlight(rt, 'h', r.id, true)
    resetAlertRuntime(rt)
    expect(rt.counts.size).toBe(0)
    expect(rt.cooldownUntil.size).toBe(0)
    expect(rt.inFlight.size).toBe(0)
    // 冷却被清：重新累积 2 次即可再触发（不再受 60min 冷却压制）
    expect(feed(rt, r, 'h', high, 3000)).toBe(false)
    expect(feed(rt, r, 'h', high, 4000)).toBe(true)
  })

  it('alertMonitorTemplate：默认 3 规则（cpu/mem/disk）+ 冷却 30 + 启用', () => {
    const t = alertMonitorTemplate('192.168.1.10', '生产机')
    expect(t.id).toBeTruthy()
    expect(t.name).toBe('生产机')
    expect(t.host).toBe('192.168.1.10')
    expect(t.enabled).toBe(true)
    expect(t.cooldownMinutes).toBe(30)
    expect(t.rules.map(r => r.id)).toEqual(['cpu_high', 'mem_high', 'disk_high'])
    expect(t.rules.every(r => r.enabled)).toBe(true)
  })

  it('sanitizeAlertMonitorTask：id/host 缺失 → null；rules 空 → null', () => {
    expect(sanitizeAlertMonitorTask(null)).toBeNull()
    expect(sanitizeAlertMonitorTask({})).toBeNull()
    expect(sanitizeAlertMonitorTask({ id: 'x' })).toBeNull()                 // 无 host
    expect(sanitizeAlertMonitorTask({ id: 'x', host: 'h' })).toBeNull()     // 无 rules
    // name 缺省按 host 生成
    const t = sanitizeAlertMonitorTask({
      id: 'x', host: 'h',
      rules: [{ id: 'r', metric: 'cpu', op: '>', threshold: 1, consecutiveN: 2, enabled: true, diskScope: 'any' }],
    })
    expect(t?.name).toBe('实时监控 h')
    expect(t?.enabled).toBe(true)
    expect(t?.cooldownMinutes).toBe(30)
  })

  it('sanitizeAlertMonitorTask：非法 rules 丢弃、合法保留；enabled/cooldown 兜底', () => {
    const raw = {
      id: 't1', host: 'h1', name: 'n', enabled: false, cooldownMinutes: 0,
      rules: [
        { id: 'ok', metric: 'mem', op: '>', threshold: 85, consecutiveN: 5, enabled: true, diskScope: 'any' },
        { id: '', metric: 'mem', op: '>', threshold: 85, consecutiveN: 5, diskScope: 'any' },      // 空 id → 丢
        { id: 'bad-metric', metric: 'temp', op: '>', threshold: 1, consecutiveN: 5, diskScope: 'any' }, // 取值域外 → 丢
        { id: 'bad-op', metric: 'cpu', op: '!=', threshold: 1, consecutiveN: 5, diskScope: 'any' },     // 非法比较符 → 丢
        { id: 'bad-th', metric: 'cpu', op: '>', threshold: 'x', consecutiveN: 5, diskScope: 'any' },    // 非数值阈值 → 丢
        { id: 'bad-n', metric: 'cpu', op: '>', threshold: 1, consecutiveN: 0, diskScope: 'any' },       // N<1 → 丢
      ],
    }
    const t = sanitizeAlertMonitorTask(raw)
    expect(t).not.toBeNull()
    expect(t!.rules.map(r => r.id)).toEqual(['ok'])
    expect(t!.enabled).toBe(false)
    expect(t!.cooldownMinutes).toBe(1) // 0 → 下限夹取
  })

  it('sanitizeAlertMonitorTasks：非数组/损坏 → 空数组（零监控）；合法条目保留，非法不连坐', () => {
    expect(sanitizeAlertMonitorTasks(null)).toEqual([])
    expect(sanitizeAlertMonitorTasks('x')).toEqual([])
    const ok = { id: 'a', host: 'h', rules: [{ id: 'r', metric: 'cpu', op: '>', threshold: 1, consecutiveN: 2, enabled: true, diskScope: 'any' }], cooldownMinutes: 30 }
    expect(sanitizeAlertMonitorTasks([ok])).toHaveLength(1)
    // 一条非法（无 host）不应拖垮整条数组
    expect(sanitizeAlertMonitorTasks([ok, { id: 'bad' } as unknown])).toHaveLength(1)
  })
})

describe('buildDiagnosisTaskText 与 describeRule', () => {
  it('包含主机名/规则/越限值/mem/swap/磁盘前3/GPU 摘要，且确定可复现', () => {
    const m = makeMetrics({
      cpu: { used: 96.3, iowait: 2.1 },
      load: [8.42, 6.1, 4],
      mem: { total: 8000, used: 7500, percent: 93.5 },
      swap: { total: 2048, used: 1200 },
      disks: [
        { mount: '/data', total: 102400, used: 90000, percent: 87.9 },
        { mount: '/', total: 51200, used: 48640, percent: 95 },
        { mount: '/boot', total: 500, used: 225, percent: 45 },
        { mount: '/tmp', total: 1000, used: 930, percent: 93 },
      ],
      gpus: [{
        index: 0, name: 'NVIDIA A100', utilization: 98, memUsed: 76, memTotal: 80,
        memPercent: 95, temperature: 70, powerDraw: 250, powerLimit: 300, fanSpeed: null,
        smClock: null, memClock: null, uuid: 'GPU-x', busId: '', perfState: 'P0',
        persistence: 'Enabled', computeMode: 'Default', eccUncorr: null,
      }],
    })
    const r = rule({ id: 'cpu_high', metric: 'cpu', op: '>', threshold: 90, consecutiveN: 6 })
    const text = buildDiagnosisTaskText({
      host: 'web-01', rule: r, metrics: m,
      cpuHistory: [80, 90, 95, 96], memHistory: [91, 92, 93], now: 1_700_000_000_000,
    })
    expect(text).toContain('web-01')
    expect(text).toContain('CPU 使用率 > 90%')
    expect(text).toContain('连续 6 个采样')
    expect(text).toContain('96.3')
    expect(text).toContain('93.5%')
    expect(text).toContain('Swap：已用 1200/2048 MB')
    // 磁盘按使用率降序前 3，/boot（45%）被挤出
    expect(text).toContain('/ 95%')
    expect(text).toContain('/tmp 93%')
    expect(text).toContain('/data 87.9%')
    expect(text).not.toContain('/boot')
    expect(text).toContain('NVIDIA A100')
    expect(text).toContain('80, 90, 95, 96')
    expect(text).toContain('91, 92, 93')
    // 确定 now → 确定输出
    const again = buildDiagnosisTaskText({ host: 'web-01', rule: r, metrics: m, cpuHistory: [80, 90, 95, 96], memHistory: [91, 92, 93], now: 1_700_000_000_000 })
    expect(again).toBe(text)
  })

  it('历史只取尾部 12 点；无 swap / 无 GPU 行省略或标注异常', () => {
    const cpu = Array.from({ length: 60 }, (_, i) => i)
    const text = buildDiagnosisTaskText({
      host: 'h', rule: rule({ consecutiveN: 6 }), metrics: makeMetrics({ gpuError: 'nvidia-smi not found' }),
      cpuHistory: cpu, memHistory: [], now: 0,
    })
    expect(text).toContain(`CPU 使用率近 ${ALERT_HISTORY_TAIL} 点`)
    expect(text).toContain('48, 49, 50')        // 尾 12 点是 48..59
    expect(text).not.toContain('46, 47')        // 中间点不得混入
    expect(text).not.toContain('Swap')
    expect(text).not.toContain('GPU 0')
    expect(text).toContain('GPU 采集异常：nvidia-smi not found')
    expect(text).not.toContain('内存使用率近')   // memHistory 空则整行省略
  })

  it('load1 规则描述不带百分号；disk 挂载点作用域体现在描述里', () => {
    expect(describeRule(rule({ metric: 'load1', op: '>', threshold: 5 }))).toBe('负载(1分钟) > 5')
    expect(describeRule(rule({ metric: 'disk', op: '>', threshold: 92, diskScope: 'mount:/data' }))).toBe('磁盘使用率 > 92%（挂载点 /data）')
    expect(describeRule(rule({ metric: 'disk', op: '>', threshold: 92 }))).toBe('磁盘使用率 > 92%（任一挂载点）')
  })

  it('对抗性大输入（8 卡长名 GPU + 8 磁盘 + 长 gpuError）仍 ≤4000 字符并截断标注', () => {
    const longName = 'X'.repeat(500)
    const m = makeMetrics({
      gpuError: 'E'.repeat(200),
      gpus: Array.from({ length: 8 }, (_, i) => ({
        index: i, name: longName, utilization: 90, memUsed: 1, memTotal: 2, memPercent: 90,
        temperature: 1, powerDraw: null, powerLimit: null, fanSpeed: null, smClock: null,
        memClock: null, uuid: '', busId: '', perfState: '', persistence: '', computeMode: '', eccUncorr: null,
      })),
      disks: Array.from({ length: 8 }, (_, i) => ({ mount: `/d${i}`, total: 1, used: 1, percent: 90 })),
    })
    const text = buildDiagnosisTaskText({
      host: 'h', rule: rule({ metric: 'gpu_mem', consecutiveN: 6 }), metrics: m,
      cpuHistory: [], memHistory: [], now: 0,
    })
    expect(text.length).toBeLessThanOrEqual(ALERT_TASK_MAX_CHARS)
    expect(text.endsWith('…（指标已截断）')).toBe(true)
  })
})
