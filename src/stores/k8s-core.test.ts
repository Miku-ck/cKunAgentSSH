/**
 * k8s-core 命令构造 / 解析 / 错误归类测试
 *
 * fixture 按真实 kubectl `-o json` 的字段形状写（List.items + 每对象 kind/metadata/spec/status），
 * 因为这套解析层的全部价值就在于「按 kubectl 自己的口径读数」：READY 含 init 容器、
 * waiting.reason 压过 phase、Gi 与 G 差 7.4%。这些口径一旦漂开，界面就会把
 * 「正在崩溃重启」读成「正常运行」。
 */
import { describe, expect, it, vi } from 'vitest'
import {
  NODES_CMD, WORKLOADS_CMD, PODS_CMD, SERVICES_CMD, EVENTS_CMD, EVENTS_CMD_WITH_NORMAL,
  TOP_NODES_CMD, getOneCmd, getOneYamlCmd, objectEventsCmd, logsCmd,
  rolloutRestartCmd, scaleCmd, deletePodCmd, deleteJobCmd, SCALABLE_KINDS,
  validateObjectName, validateReplicas, assertConcreteNs, shortKind,
  quantityToBase, cpuText, formatK8sBytes, ageText, timeText,
  requireParsableJson, parseNodes, parseTopNodes, mergeNodeUsage,
  parseWorkloads, parsePods, parseServices, parseEvents, parseResourceDetail, podContainerNames,
  podReadyCounts, podDisplayStatus, podRestarts, isAbnormalPod, podTone, nodeRoles,
  type PodRow, type K8sError,
  k8sErrorFromResult, describeThrownK8sError, k8sErrText, isHostLevelError, isNotDetectedError, isNotDetectedState,
  hostErrorCopy, parseEnvProbe, envProbeNote, withExecDeadline,
} from './k8s-core'

const NOW = Date.parse('2026-10-30T12:00:00Z')
const list = (...items: unknown[]): string => JSON.stringify({ apiVersion: 'v1', kind: 'List', items })
const one = (o: unknown): string => JSON.stringify(o)

const podObj = (over: Record<string, any> = {}): any => ({
  kind: 'Pod',
  apiVersion: 'v1',
  metadata: { name: 'web-1', namespace: 'default', creationTimestamp: '2026-10-30T11:00:00Z', ...over.metadata },
  spec: {
    nodeName: 'k1',
    containers: [{ name: 'app', image: 'nginx:1.25' }],
    ...over.spec,
  },
  status: { phase: 'Running', podIP: '10.42.0.7', containerStatuses: [{ name: 'app', ready: true, restartCount: 0 }], ...over.status },
})

describe('列表命令：一律 -A', () => {
  it('四条列表命令都带 -A，没有 ns 选择器就没有第二种取数范围', () => {
    for (const cmd of [WORKLOADS_CMD, PODS_CMD, SERVICES_CMD, EVENTS_CMD]) {
      expect(cmd, cmd).toContain(' -A')
      expect(cmd, cmd).not.toContain(' -n ')
    }
  })
  it('每条取数都带 --request-timeout，比渲染层 15s 预算短', () => {
    for (const cmd of [PODS_CMD, WORKLOADS_CMD, SERVICES_CMD, EVENTS_CMD, NODES_CMD]) {
      expect(cmd).toContain('--request-timeout=12s')
    }
  })
  it('节点是 cluster-scoped，既不带 -n 也不带 -A', () => {
    expect(NODES_CMD).not.toContain('-n ')
    expect(NODES_CMD).not.toContain('-A')
  })
  it('事件默认只取 Warning，且按 lastTimestamp 服务端排序', () => {
    expect(EVENTS_CMD).toContain('--field-selector type=Warning')
    expect(EVENTS_CMD).toContain('--sort-by=.lastTimestamp')
    expect(EVENTS_CMD_WITH_NORMAL).not.toContain('type=Warning')
    expect(EVENTS_CMD_WITH_NORMAL).toContain('--sort-by=.lastTimestamp')
  })
  it('top 走 --no-headers 文本表，不用 json（json 里 cpu 是 nanocores 串）', () => {
    expect(TOP_NODES_CMD).toContain('--no-headers')
    expect(TOP_NODES_CMD).not.toContain('-o json')
  })
})

describe('名称白名单（而不是 shell 转义）', () => {
  it('合法 DNS-1123 通过', () => {
    expect(validateObjectName('web-1')).toBeNull()
    expect(validateObjectName('app.kubernetes.io')).toBeNull()
    expect(validateObjectName('a')).toBeNull()
  })
  it('带空格 / 引号 / 大写 / 空 一律拒绝', () => {
    expect(validateObjectName('my web')).toContain('不是合法的 Kubernetes 名称')
    expect(validateObjectName("a'; rm -rf /")).toContain('不是合法的')
    expect(validateObjectName('Web')).toContain('不是合法的')
    expect(validateObjectName('')).toContain('不能为空')
    expect(validateObjectName('x'.repeat(300))).toContain('过长')
  })
  it('注入串过不了校验，也就进不了命令', () => {
    expect(() => deletePodCmd('p; touch /tmp/x', 'default')).toThrow()
    expect(() => logsCmd('$(id)', 'default')).toThrow()
  })
})

describe('动作的下发边界', () => {
  it('动作的目标 ns 必须是行上那个具体名字：通配与空值都拒绝', () => {
    expect(assertConcreteNs('*')).toContain('不是合法的 Kubernetes 名称')
    expect(assertConcreteNs('')).toContain('不能为空')
    expect(assertConcreteNs('default')).toBeNull()
    expect(() => rolloutRestartCmd('Deployment', 'web', '*')).toThrow('不是合法的')
    expect(() => deleteJobCmd('backup', '')).toThrow('不能为空')
  })
  it('四个动作各自拼出单条命令，ns 显式带上', () => {
    expect(rolloutRestartCmd('Deployment', 'web', 'prod')).toBe('kubectl rollout restart deployments/web -n prod --request-timeout=12s')
    expect(scaleCmd('deployments', 'web', 'prod', 3)).toBe('kubectl scale deployments/web -n prod --replicas=3')
    expect(deletePodCmd('web-1', 'prod')).toBe('kubectl delete pods web-1 -n prod')
    expect(deleteJobCmd('nightly', 'prod')).toBe('kubectl delete jobs nightly -n prod')
  })
  it('只有 Deployment/StatefulSet 有副本数可改', () => {
    expect(SCALABLE_KINDS).toEqual(['deployments', 'statefulsets'])
    expect(() => scaleCmd('daemonsets', 'ds-x', 'prod', 2)).toThrow('没有副本数可改')
  })
  it('副本数只能是非负整数，且有上限', () => {
    expect(validateReplicas('0')).toBeNull()
    expect(validateReplicas('12')).toBeNull()
    expect(validateReplicas('-1')).toContain('非负整数')
    expect(validateReplicas('1.5')).toContain('非负整数')
    expect(validateReplicas('abc')).toContain('非负整数')
    expect(validateReplicas('1000')).toContain('上限')
    expect(() => scaleCmd('deployments', 'web', 'prod', '-2')).toThrow('非负整数')
  })
  it('kind 原名与短名都能落到同一个复数键', () => {
    expect(shortKind('Deployment')).toBe('deployments')
    expect(shortKind('deployments')).toBe('deployments')
    expect(shortKind('Pod')).toBe('pods')
    expect(shortKind('CronJob')).toBe('cronjobs')
  })
})

describe('日志与关联事件命令', () => {
  it('多容器选容器、崩溃看上一次、tail 有下限', () => {
    expect(logsCmd('web-1', 'prod')).toBe('kubectl logs web-1 -n prod --tail=200 2>&1')
    expect(logsCmd('web-1', 'prod', { container: 'sidecar' })).toContain('-c sidecar')
    expect(logsCmd('web-1', 'prod', { previous: true })).toContain('--previous')
    expect(logsCmd('web-1', 'prod', { tail: 0 })).toContain('--tail=1')
    expect(logsCmd('web-1', 'prod', { tail: 50 })).toContain('--tail=50')
  })
  it('详情与对象事件都按 kind/name/ns 定位', () => {
    expect(getOneCmd('Deployment', 'web', 'prod')).toContain('kubectl get deployments web -n prod')
    expect(objectEventsCmd('web-1', 'Pod', 'prod')).toContain('involvedObject.name=web-1,involvedObject.kind=Pod')
  })
  it('YAML 页签取同一对象的原文，集群级对象不带 -n', () => {
    expect(getOneYamlCmd('Deployment', 'web', 'prod')).toBe('kubectl get deployments web -n prod --request-timeout=12s -o yaml')
    expect(getOneYamlCmd('Node', 'k1', '')).toBe('kubectl get nodes k1 --request-timeout=12s -o yaml')
    expect(() => getOneYamlCmd('Pod', '$(id)', 'default')).toThrow()
  })
})

describe('quantity 与文本口径', () => {
  it('1024 进制与 10 进制后缀分开算（Gi 与 G 差 7.4%）', () => {
    expect(quantityToBase('4')).toBe(4)
    expect(quantityToBase('500m')).toBeCloseTo(0.5, 10)
    expect(quantityToBase('1Gi')).toBe(1024 ** 3)
    expect(quantityToBase('1G')).toBe(1e9)
    expect(quantityToBase('1024Ki')).toBe(1024 * 1024)
    expect(quantityToBase('3910276Ki')).toBe(3910276 * 1024)
    expect(quantityToBase('1e3')).toBe(1000)
    expect(quantityToBase('1234567n')).toBeCloseTo(1234567e-9, 12)
  })
  it('认不出的后缀返回 undefined，不猜一个数', () => {
    expect(quantityToBase('12Q')).toBeUndefined()
    expect(quantityToBase('')).toBeUndefined()
    expect(quantityToBase(undefined)).toBeUndefined()
  })
  it('CPU 文本与 kubectl 的读数一致', () => {
    expect(cpuText('500m')).toBe('0.5')
    expect(cpuText('4')).toBe('4')
    // 2,000,000 微核 = 0.002 核（=2m）：nanocores 是 top 的实际单位，别当成毫核
    expect(cpuText('2000000n')).toBe('0.002')
  })
  it('内存按二进制单位收口', () => {
    expect(formatK8sBytes(1024 ** 3)).toBe('1.0Gi')
    expect(formatK8sBytes(12 * 1024 ** 3)).toBe('12Gi')
    expect(formatK8sBytes(512 * 1024 ** 2)).toBe('512Mi')
    expect(formatK8sBytes(4096)).toBe('4Ki')
  })
  it('AGE 只保留 kubectl 那四档', () => {
    expect(ageText('2026-10-30T11:59:30Z', NOW)).toBe('30s')
    expect(ageText('2026-10-30T11:15:00Z', NOW)).toBe('45m')
    expect(ageText('2026-10-30T11:00:00Z', NOW)).toBe('1h')
    expect(ageText('2026-10-27T12:00:00Z', NOW)).toBe('3d')
    expect(ageText('2025-10-30T12:00:00Z', NOW)).toBe('1y')
    expect(ageText('')).toBe('')
  })
  it('时刻读不出时原样显示，不伪装成空', () => {
    expect(timeText('2026-10-30T11:20:30Z')).toBe(new Date('2026-10-30T11:20:30Z').toLocaleTimeString())
    expect(timeText('<unknown>')).toBe('<unknown>')
    expect(timeText('')).toBe('')
  })
})

describe('requireParsableJson：截断与坏输出如实抛出', () => {
  it('命中主进程的截断标记 → K8S_TRUNCATED，而不是空列表', () => {
    const raw = '{"kind":"List","items":[' + '{"a":1}'.padEnd(200, '.')
    try {
      requireParsableJson(raw + '\n[输出已截断，超过 1M 字符上限]')
      expect.unreachable('应当抛出')
    } catch (e) {
      expect((e as { code: string }).code).toBe('K8S_TRUNCATED')
      expect((e as { hint?: string }).hint).toContain('搜索框')
    }
  })
  it('不是 JSON → K8S_BAD_JSON（不冒充「这里没有资源」）', () => {
    try {
      requireParsableJson('bash: kubectl: command not found')
      expect.unreachable('应当抛出')
    } catch (e) {
      expect((e as { code: string }).code).toBe('K8S_BAD_JSON')
    }
  })
  it('空输出是空集；单对象 get 没有 items 也要收成一条', () => {
    expect(requireParsableJson('')).toEqual([])
    expect(requireParsableJson('   ')).toEqual([])
    expect(requireParsableJson(one(podObj()))).toHaveLength(1)
    expect(requireParsableJson(list())).toEqual([])
  })
})

describe('parsePods：与 kubectl 同口径的就绪与状态', () => {
  it('运行中且全部就绪 → 1/1 Running，不算异常', () => {
    const rows = parsePods(list(podObj()))
    expect(rows[0]).toMatchObject({ name: 'web-1', namespace: 'default', ready: '1/1', status: 'Running', phase: 'Running', ip: '10.42.0.7', node: 'k1', image: 'nginx:1.25' })
    expect(isAbnormalPod(rows[0])).toBe(false)
  })
  it('两容器一个没过就绪探针 → 1/2，Running 也算异常', () => {
    const p = podObj({
      spec: { containers: [{ name: 'app', image: 'a' }, { name: 'sidecar', image: 'b' }] },
      status: { phase: 'Running', containerStatuses: [{ name: 'app', ready: true }, { name: 'sidecar', ready: false }] },
    })
    const rows = parsePods(list(p))
    expect(rows[0].ready).toBe('1/2')
    expect(rows[0].status).toBe('Running')
    expect(isAbnormalPod(rows[0])).toBe(true)
  })
  it('init 容器计入分母，成功的计入分子', () => {
    const p = podObj({
      spec: { containers: [{ name: 'app', image: 'a' }], initContainers: [{ name: 'migrate', image: 'm' }] },
      status: {
        phase: 'Running',
        containerStatuses: [{ name: 'app', ready: true }],
        initContainerStatuses: [{ name: 'migrate', state: { terminated: { exitCode: 0 } } }],
      },
    })
    expect(podReadyCounts(p as never)).toEqual({ ready: 2, total: 2 })
    expect(parsePods(list(p))[0].ready).toBe('2/2')
  })
  it('init 正在跑 → 0/2 且状态取 waiting.reason', () => {
    const p = podObj({
      spec: { containers: [{ name: 'app', image: 'a' }], initContainers: [{ name: 'migrate', image: 'm' }] },
      status: {
        phase: 'Pending',
        containerStatuses: [],
        initContainerStatuses: [{ name: 'migrate', state: { waiting: { reason: 'PodInitializing' } } }],
      },
    })
    const row = parsePods(list(p))[0]
    expect(row.ready).toBe('0/2')
    expect(row.status).toBe('PodInitializing')
    expect(isAbnormalPod(row)).toBe(true)
  })
  it('崩溃循环：phase 只是 Pending，显示必须是 CrashLoopBackOff', () => {
    const p = podObj({
      status: {
        phase: 'Pending',
        containerStatuses: [{ name: 'app', ready: false, restartCount: 5, state: { waiting: { reason: 'CrashLoopBackOff' } } }],
      },
    })
    const ds = podDisplayStatus(p as never)
    expect(ds.status).toBe('CrashLoopBackOff')
    expect(parsePods(list(p))[0].restarts).toBe(5)
  })
  it('RESTARTS 取各容器最大值（与 kubectl 一致，不求和）', () => {
    const p = podObj({
      status: {
        phase: 'Running',
        containerStatuses: [{ name: 'a', ready: true, restartCount: 3 }, { name: 'b', ready: true, restartCount: 7 }],
        initContainerStatuses: [{ name: 'i', restartCount: 1, state: { terminated: { exitCode: 0 } } }],
      },
    })
    expect(podRestarts(p as never)).toBe(7)
  })
  it('删除中带 deletionTimestamp → Terminating 压过一切', () => {
    const p = podObj({ metadata: { name: 'web-1', namespace: 'default', deletionTimestamp: '2026-10-30T11:59:00Z' } })
    expect(podDisplayStatus(p as never).status).toBe('Terminating')
    expect(isAbnormalPod(parsePods(list(p))[0])).toBe(true)
  })
  it('跑完的 Job Pod 是 Completed，不算异常', () => {
    const p = podObj({
      status: { phase: 'Succeeded', containerStatuses: [{ name: 'app', ready: false, state: { terminated: { exitCode: 0, reason: 'Completed' } } }] },
    })
    const row = parsePods(list(p))[0]
    expect(row.status).toBe('Completed')
    expect(isAbnormalPod(row)).toBe(false)
  })
  it('被驱逐的 Pod 保留 Evicted 这个词（只报 Failed 会丢掉原因）', () => {
    const p = podObj({ status: { phase: 'Failed', reason: 'Evicted', containerStatuses: [] } })
    expect(parsePods(list(p))[0].status).toBe('Evicted')
  })
  it('非 0 退出的 terminated.reason 也当作状态读', () => {    const p = podObj({
      status: { phase: 'Running', containerStatuses: [{ name: 'app', ready: false, state: { terminated: { exitCode: 137, reason: 'OOMKilled' } } }] },
    })
    expect(parsePods(list(p))[0].status).toBe('OOMKilled')
  })
  it('缺字段的老集群读数不崩：无 containerStatuses 时 0/1 而不是抛异常', () => {
    const p = { kind: 'Pod', metadata: { name: 'x', namespace: 'default' }, spec: { containers: [{ name: 'app' }] }, status: { phase: 'Unknown' } }
    const row = parsePods(list(p))[0]
    expect(row.ready).toBe('0/1')
    expect(row.status).toBe('Unknown')
    expect(row.age).toBe('')
  })
})

describe('podTone：行颜色与异常计数共用一份判据', () => {
  type Row = Pick<PodRow, 'phase' | 'status' | 'restarts' | 'readyCount' | 'totalCount'>
  const row = (over: Partial<Row>): Row => ({
    phase: 'Running', status: 'Running', restarts: 0, readyCount: 1, totalCount: 1, ...over,
  })
  it('全就绪是 ok；探针没过就是 bad（哪怕 phase 仍是 Running）', () => {
    expect(podTone(row({}))).toBe('ok')
    expect(podTone(row({ readyCount: 1, totalCount: 2 }))).toBe('bad')
  })
  it('跑完的是 ok，正在删除的是 warn，崩溃/被驱逐是 bad', () => {
    expect(podTone(row({ phase: 'Succeeded', status: 'Completed', readyCount: 0, totalCount: 1 }))).toBe('ok')
    expect(podTone(row({ status: 'Terminating' }))).toBe('warn')
    expect(podTone(row({ restarts: 3 }))).toBe('bad')
    expect(podTone(row({ phase: 'Failed', status: 'Evicted' }))).toBe('bad')
    expect(podTone(row({ phase: 'Pending', status: 'PodInitializing' }))).toBe('warn')
  })
  it('isAbnormalPod 就是「不是 ok」——两者不可能各说一套', () => {
    for (const r of [row({}), row({ readyCount: 0 }), row({ status: 'Terminating' }), row({ restarts: 2 })]) {
      expect(isAbnormalPod(r)).toBe(podTone(r) !== 'ok')
    }
  })
})

describe('parseNodes', () => {
  const node = (over: Record<string, any> = {}): any => ({
    kind: 'Node',
    metadata: {
      name: 'k1',
      creationTimestamp: '2026-10-20T12:00:00Z',
      labels: { 'kubernetes.io/hostname': 'k1', ...over.labels },
    },
    spec: over.spec || {},
    status: {
      addresses: [{ type: 'InternalIP', address: '192.168.1.10' }, { type: 'Hostname', address: 'k1' }],
      allocatable: { cpu: '4', memory: '7941156Ki', pods: '110' },
      nodeInfo: { kubeletVersion: 'v1.31.2+k3s1', operatingSystem: 'linux', architecture: 'amd64' },
      conditions: [{ type: 'Ready', status: 'True', lastHeartbeatTime: '2026-10-30T11:59:00Z' }],
      ...over.status,
    },
  })

  it('Ready 条件按 type 挑元素，不看数组顺序', () => {
    const rows = parseNodes(list(node({ status: { conditions: [{ type: 'MemoryPressure', status: 'False' }, { type: 'Ready', status: 'True' }] } })))
    expect(rows[0].ready).toBe(true)
    expect(rows[0].status).toBe('Ready')
  })
  it('NotReady 与 SchedulingDisabled 各自如实', () => {
    expect(parseNodes(list(node({ status: { conditions: [{ type: 'Ready', status: 'False' }] } })))[0].status).toBe('NotReady')
    const cordoned = node({ spec: { unschedulable: true }, status: { conditions: [{ type: 'Ready', status: 'True' }] } })
    const row = parseNodes(list(cordoned))[0]
    expect(row.status).toBe('Ready,SchedulingDisabled')
    expect(row.unschedulable).toBe(true)
  })
  it('角色从 node-role 标签的键取，worker 排最后', () => {
    expect(nodeRoles({ 'node-role.kubernetes.io/control-plane': '', 'kubernetes.io/hostname': 'k1' })).toBe('control-plane')
    expect(nodeRoles({ 'node-role.kubernetes.io/worker': '', 'node-role.kubernetes.io/edge': '' })).toBe('edge,worker')
    expect(nodeRoles({ 'kubernetes.io/hostname': 'k1' })).toBe('')
    const row = parseNodes(list(node({ labels: { 'node-role.kubernetes.io/master': '' } })))[0]
    expect(row.roles).toBe('master')
  })
  it('展示 allocatable 而不是 capacity；单位换算按二进制', () => {
    const row = parseNodes(list(node()))[0]
    expect(row.cpu).toBe('4')
    // 7941156Ki = 7.57Gi → 一位小数收口
    expect(row.memory).toBe('7.6Gi')
    expect(row.pods).toBe('110')
    expect(row.version).toBe('v1.31.2+k3s1')
    expect(row.internalIp).toBe('192.168.1.10')
    // AGE 由 Date.now() 推出，不断言具体天数（跨分钟就会从 9d 跳成 10d）
    expect(row.age).toMatch(/^\d+d$/)
  })
})

describe('parseTopNodes / 富集', () => {
  it('默认 5 列（NAME CPU CPU% MEM MEM%），表头与空行跳过', () => {
    const usage = parseTopNodes('k1   312m   8%   2.1Gi   27%\nk2   90m   2%   810Mi   10%\n\n')
    expect(usage.get('k1')).toEqual({ cpu: '312m', cpuPerc: '8%', mem: '2.1Gi', memPerc: '27%' })
    expect(usage.size).toBe(2)
  })
  it('-v=0 退化成 3 列时也认，百分比留空', () => {
    const usage = parseTopNodes('k1   312m   2.1Gi')
    expect(usage.get('k1')).toEqual({ cpu: '312m', cpuPerc: '', mem: '2.1Gi', memPerc: '' })
  })
  it('列数不足或不是数量的行不收（否则错误文本会变成假数据）', () => {
    expect(parseTopNodes('error: Metrics API not available').size).toBe(0)
    expect(parseTopNodes('k1  Metrics  API  not  available').size).toBe(0)
  })
  it('命中才填，没命中保持缺省（不是 0）', () => {
    const rows = parseNodes(list({ kind: 'Node', metadata: { name: 'k1' }, status: { conditions: [{ type: 'Ready', status: 'True' }] } }))
    const merged = mergeNodeUsage(rows, new Map([['k1', { cpu: '312m', cpuPerc: '8%', mem: '2.1Gi', memPerc: '27%' }]]))
    expect(merged[0].cpuUsage).toBe('312m')
    expect(merged[0].cpuPerc).toBe('8%')
    expect(merged[0].memPerc).toBe('27%')
    expect(mergeNodeUsage(rows, new Map())[0].cpuUsage).toBeUndefined()
  })
})

describe('parseWorkloads：五类合并一张表，字段按 kind 分派', () => {
  const deploy = (over: Record<string, any> = {}): any => ({
    kind: 'Deployment',
    metadata: { name: 'web', namespace: 'default', creationTimestamp: '2026-10-29T12:00:00Z' },
    spec: { replicas: over.replicas ?? 3, template: { spec: { containers: [{ name: 'app', image: 'nginx:1.25' }] } } },
    status: { readyReplicas: over.ready ?? 3, availableReplicas: over.ready ?? 3, conditions: over.conditions || [] },
  })

  it('期望与就绪对齐是 OK，缺一个就是 Degraded', () => {
    expect(parseWorkloads(list(deploy()))[0]).toMatchObject({ kind: 'Deployment', ready: '3/3', status: 'OK', image: 'nginx:1.25' })
    expect(parseWorkloads(list(deploy({ ready: 2 })))[0].status).toBe('Degraded')
    expect(parseWorkloads(list(deploy({ ready: 2 })))[0].ready).toBe('2/3')
  })
  it('缩到 0 不当异常看：明确写 Scaled to 0', () => {
    const row = parseWorkloads(list(deploy({ replicas: 0, ready: 0 })))[0]
    expect(row.status).toBe('Scaled to 0')
    expect(row.ready).toBe('0/0')
  })
  it('ReplicaFailure 优先于其他判读（镜像写错时 readyReplicas 恒 0，只说 Degraded 会看不出原因）', () => {
    const row = parseWorkloads(list(deploy({ ready: 0, conditions: [{ type: 'ReplicaFailure', status: 'True', reason: 'FailedCreate' }] })))[0]
    expect(row.status).toBe('Failure')
  })
  it('StatefulSet 与 Deployment 同字段；没写 replicas 视为 0', () => {
    const sts = { kind: 'StatefulSet', metadata: { name: 'db', namespace: 'default' }, spec: {}, status: {} }
    expect(parseWorkloads(list(sts))[0].ready).toBe('0/0')
  })
  it('DaemonSet 的期望/就绪都在 status 里', () => {
    const ds = { kind: 'DaemonSet', metadata: { name: 'agent', namespace: 'kube-system' }, spec: {}, status: { desiredNumberScheduled: 2, numberReady: 1 } }
    expect(parseWorkloads(list(ds))[0]).toMatchObject({ ready: '1/2', status: 'Degraded' })
  })
  it('Job 用 completions/succeeded，条件决定 Completed/Failed/Running', () => {
    const job = (status: Record<string, unknown>) => ({ kind: 'Job', metadata: { name: 'backup', namespace: 'default' }, spec: { completions: 2, template: { spec: { containers: [{ image: 'busybox' }] } } }, status })
    expect(parseWorkloads(list(job({ succeeded: 2, conditions: [{ type: 'Complete', status: 'True' }] })))[0]).toMatchObject({ ready: '2/2', status: 'Completed', image: 'busybox' })
    expect(parseWorkloads(list(job({ failed: 1, conditions: [{ type: 'Failed', status: 'True' }] })))[0].status).toBe('Failed')
    expect(parseWorkloads(list(job({})))).toHaveLength(1)
    expect(parseWorkloads(list(job({})))[0]).toMatchObject({ ready: '0/2', status: 'Running' })
  })
  it('CronJob 没有就绪计数：ready 留空，状态是 schedule + 上次实际触发', () => {
    const cj = { kind: 'CronJob', metadata: { name: 'nightly', namespace: 'default' }, spec: { schedule: '0 3 * * *' }, status: { lastScheduleTime: '2026-10-30T03:00:00Z' } }
    const row = parseWorkloads(list(cj))[0]
    expect(row.ready).toBe('')
    expect(row.status).toContain('0 3 * * *')
    expect(row.status).toContain('上次')
    const suspended = { ...cj, spec: { schedule: '0 3 * * *', suspend: true } }
    expect(parseWorkloads(list(suspended))[0].status).toBe('Suspended')
    const never = { kind: 'CronJob', metadata: { name: 'n' }, spec: { schedule: '@hourly' }, status: {} }
    expect(parseWorkloads(list(never))[0].status).toContain('尚未触发')
  })
  it('五类一次调用取回：kind 顺序即行顺序', () => {
    const rows = parseWorkloads(list(deploy(), { kind: 'StatefulSet', metadata: { name: 'db' }, spec: { replicas: 1 }, status: { readyReplicas: 1 } }, { kind: 'DaemonSet', metadata: { name: 'a' }, spec: {}, status: { desiredNumberScheduled: 1, numberReady: 1 } }))
    expect(rows.map(r => r.kind)).toEqual(['Deployment', 'StatefulSet', 'DaemonSet'])
    expect(rows[1].status).toBe('OK')
    expect(rows[2].status).toBe('OK')
  })
  it('工作负载名/命名空间缺字段也不抛（老版本或部分响应）', () => {
    expect(parseWorkloads(list({ kind: 'Deployment' }))[0]).toMatchObject({ name: '', ready: '0/0' })
  })
})

describe('parseServices：Service 与 Ingress 同表', () => {
  it('ClusterIP 的端口列带 NodePort 与协议', () => {
    const svc = {
      kind: 'Service',
      metadata: { name: 'web', namespace: 'default', creationTimestamp: '2026-10-29T12:00:00Z' },
      spec: { type: 'NodePort', clusterIP: '10.43.0.11', ports: [{ port: 80, targetPort: 8080, nodePort: 30080, protocol: 'TCP' }, { port: 443 }] },
      status: {},
    }
    expect(parseServices(list(svc))[0]).toMatchObject({ kind: 'Service', type: 'NodePort', clusterIp: '10.43.0.11', ports: '80:30080/TCP,443/TCP' })
  })
  it('headless 的 None 原样显示，端口号为 0 也出字', () => {
    const svc = { kind: 'Service', metadata: { name: 'etcd' }, spec: { clusterIP: 'None', ports: [{ port: 0 }] }, status: {} }
    const row = parseServices(list(svc))[0]
    expect(row.clusterIp).toBe('None')
    expect(row.ports).toBe('0/TCP')
  })
  it('LoadBalancer 的外部地址取 status.ingress 的 ip 或 hostname', () => {
    const svc = { kind: 'Service', metadata: { name: 'gw' }, spec: { type: 'LoadBalancer', ports: [] }, status: { loadBalancer: { ingress: [{ ip: '203.0.113.9' }, { hostname: 'lb.example' }] } } }
    expect(parseServices(list(svc))[0].externalIp).toBe('203.0.113.9,lb.example')
  })
  it('Ingress 折叠成 host 路径→服务:端口', () => {
    const ing = {
      kind: 'Ingress',
      metadata: { name: 'web-ing', namespace: 'default' },
      spec: {
        ingressClassName: 'traefik',
        rules: [
          {
            host: 'a.example',
            http: { paths: [{ path: '/', backend: { service: { name: 'web' }, port: { number: 80 } } }] },
          },
          {
            http: { paths: [{ path: '/api', backend: { service: { name: 'api' }, port: { name: 'http' } } }] },
          },
        ],
      },
      status: { loadBalancer: { ingress: [{ ip: '192.168.1.20' }] } },
    }
    const row = parseServices(list(ing))[0]
    expect(row).toMatchObject({ kind: 'Ingress', type: 'traefik', externalIp: '192.168.1.20' })
    expect(row.rules).toBe('a.example /→web:80 / * /api→api:http')
  })
})

describe('parseEvents', () => {
  const ev = (over: Record<string, any>): any => ({
    kind: 'Event', type: 'Warning', reason: 'FailedScheduling',
    message: '0/2 nodes are available', count: 3,
    involvedObject: { kind: 'Pod', name: 'web-1' },
    metadata: { name: 'web-1.18abc', namespace: 'default', creationTimestamp: '2026-10-30T11:00:00Z' },
    source: { component: 'default-scheduler' },
    firstTimestamp: '2026-10-30T11:00:00Z', lastTimestamp: '2026-10-30T11:05:00Z',
    ...over,
  })
  it('服务端已升序，这里翻成最近的在上面（不在本地重排）', () => {
    const rows = parseEvents(list(ev({ lastTimestamp: '2026-10-30T11:01:00Z' }), ev({ lastTimestamp: '2026-10-30T11:09:00Z' })))
    expect(rows[0].lastSeen).toBe(timeText('2026-10-30T11:09:00Z'))
    expect(rows[1].lastSeen).toBe(timeText('2026-10-30T11:01:00Z'))
  })
  it('对象折叠成 Kind/name，来源取 component', () => {
    const row = parseEvents(list(ev({})))[0]
    expect(row).toMatchObject({ object: 'Pod/web-1', type: 'Warning', reason: 'FailedScheduling', count: 3, source: 'default-scheduler', namespace: 'default' })
    expect(row.message).toBe('0/2 nodes are available')
  })
  it('count 缺失按 1，type 缺失按 Normal，reportingInstance 兜底来源', () => {
    const row = parseEvents(list({ kind: 'Event', metadata: { name: 'e' }, involvedObject: { kind: 'Node', name: 'k1' }, reportingInstance: { name: 'kubelet.k1' } }))[0]
    expect(row.count).toBe(1)
    expect(row.type).toBe('Normal')
    expect(row.source).toBe('kubelet.k1')
  })
  it('v1 Event 没有 message/count 也不抛', () => {
    expect(parseEvents(list({ kind: 'Event' }))[0]).toMatchObject({ object: '', type: 'Normal', message: '' })
  })
})

describe('parseResourceDetail', () => {
  const pod = {
    kind: 'Pod',
    metadata: { name: 'web-1', namespace: 'default', labels: { app: 'web', 'pod-template-hash': '7d9' }, ownerReferences: [{ kind: 'ReplicaSet', name: 'web-7d9' }] },
    spec: {
      containers: [{ name: 'app', image: 'nginx:1.25', resources: { limits: { cpu: '500m', memory: '128Mi' }, requests: { cpu: '10m' } } }],
      initContainers: [{ name: 'migrate', image: 'busybox' }],
    },
    status: {
      phase: 'Running',
      conditions: [{ type: 'Ready', status: 'True' }, { type: 'PodScheduled', status: 'True', reason: 'Scheduled' }],
      containerStatuses: [{ name: 'app', ready: true, restartCount: 2, state: { running: { startedAt: '2026-10-30T11:00:00Z' } } }],
      initContainerStatuses: [{ name: 'migrate', ready: false, state: { terminated: { exitCode: 0 } } }],
    },
  }

  it('容器带 limits/requests 文本、init 标记、状态词来自 state 的键', () => {
    const d = parseResourceDetail(one(pod))
    expect(d.kind).toBe('Pod')
    expect(d.owner).toBe('ReplicaSet/web-7d9')
    expect(d.labels.app).toBe('web')
    expect(d.conditions).toHaveLength(2)
    expect(d.containers[0]).toMatchObject({ name: 'app', ready: 'ready', restarts: 2, state: 'running', limits: 'cpu=500m,memory=128Mi', requests: 'cpu=10m' })
    expect(d.containers[1]).toMatchObject({ name: 'migrate', init: true, ready: 'done' })
  })
  it('事件按传入的关联事件解析，不传就是空数组', () => {
    expect(parseResourceDetail(one(pod)).events).toEqual([])
    const withEvents = parseResourceDetail(one(pod), list({ kind: 'Event', type: 'Warning', reason: 'OOMKilling', involvedObject: { kind: 'Pod', name: 'web-1' } }))
    expect(withEvents.events[0]).toMatchObject({ reason: 'OOMKilling', type: 'Warning' })
  })
  it('原始 JSON 一并留下，供「完整 JSON」兜底视图', () => {
    expect(parseResourceDetail(one(pod)).raw).toContain('"phase":"Running"')
  })
  it('容器名列表：普通在前、init 在后（logsCmd 选容器用）', () => {
    expect(podContainerNames(one(pod))).toEqual([{ name: 'app', init: false }, { name: 'migrate', init: true }])
  })
})

describe('k8sErrorFromResult', () => {
  it('exit 0 即成功：kubectl 把 warning 打到 stderr 不算失败', () => {
    expect(k8sErrorFromResult('Warning: resource is deprecated', 0)).toBeNull()
  })
  it('127 / command not found → KUBECTL_NOT_FOUND', () => {
    expect(k8sErrorFromResult('bash: kubectl: command not found', 127)?.code).toBe('KUBECTL_NOT_FOUND')
    expect(k8sErrorFromResult('', 127)?.code).toBe('KUBECTL_NOT_FOUND')
  })
  it('kubeconfig 读不到（kubeadm 上非 root 最常见）单独成类', () => {
    const e = k8sErrorFromResult('error loading config file "/home/app/.kube/config": open /home/app/.kube/config: permission denied', 1)
    expect(e?.code).toBe('KUBECONFIG_UNREADABLE')
    expect(e?.message).toBe('未检测到 k8s')
    expect(e?.hint).toContain('~/.kube/config')
  })
  it('配置里的证书指向别的账号目录：也算读不到 kubeconfig，不透裸的 Error in configuration', () => {
    const e = k8sErrorFromResult(
      'error: Error in configuration: \n* unable to read client-cert /root/.minikube/profiles/minikube/client.crt for minikube due to open /root/.minikube/profiles/minikube/client.crt: permission denied', 1)
    expect(e?.code).toBe('KUBECONFIG_UNREADABLE')
    expect(e?.message).toBe('未检测到 k8s')
    expect(e?.message).not.toContain('Error in configuration')
    expect(e?.hint).toContain('证书')
  })
  it('没有 current-context 与连不上 API server 是两回事', () => {
    expect(k8sErrorFromResult('error: current-context is not set', 1)?.code).toBe('K8S_CONTEXT_MISSING')
    expect(k8sErrorFromResult('The connection to the server 10.0.0.5:6443 was refused - did you specify the right host or port?', 1)?.code).toBe('K8S_NO_CLUSTER')
    expect(k8sErrorFromResult('Unable to connect to the server: dial tcp 10.0.0.5:6443: connect: connection refused', 1)?.code).toBe('K8S_NO_CLUSTER')
  })
  it('RBAC 无权 / 证书与鉴权 / 缺 API 各自成类', () => {
    expect(k8sErrorFromResult('Error from server (Forbidden): pods is forbidden: User "app" cannot list resource "pods"', 1)?.code).toBe('K8S_FORBIDDEN')
    expect(k8sErrorFromResult('error: x509: certificate has expired or is not yet valid', 1)?.code).toBe('K8S_AUTH')
    expect(k8sErrorFromResult('error: Unauthorized', 1)?.code).toBe('K8S_AUTH')
    expect(k8sErrorFromResult('error: the server doesn\'t have a resource type "ingresses"', 1)?.code).toBe('K8S_MISSING_KIND')
  })
  it('没装 metrics-server 的失败单独成类（富集静默降级，不该和列表失败混）', () => {
    expect(k8sErrorFromResult('error: Metrics API not available', 1)?.code).toBe('K8S_NO_METRICS')
  })
  it('NotFound 是「刷新一下」，可重试', () => {
    expect(k8sErrorFromResult('Error from server (NotFound): pods "web-9" not found', 1)?.code).toBe('K8S_NOT_FOUND_OBJ')
    expect(k8sErrorFromResult('Error from server (NotFound): x', 1)?.retryable).toBe(true)
  })
  it('其它非零退出透首行；exit-code 缺失且不安静才报 unknown', () => {
    const other = k8sErrorFromResult('error: unknown shorthand flag: q', 1)
    expect(other?.code).toBe('K8S_EXEC_FAILED')
    expect(other?.message).toContain('unknown shorthand flag')
    // 没回 exit-status 且不安静才报 unknown；安静收尾就什么都不报，别凭空造一条错误
    expect(k8sErrorFromResult('通道被远端关闭', null)?.code).toBe('K8S_UNKNOWN')
    expect(k8sErrorFromResult('', null)).toBeNull()
  })
  it('stderr 空时回落合并 stdout（kubectl 的错误有时只出现在 stdout）', () => {
    expect(k8sErrorFromResult('', 1, 'error: current-context is not set')?.code).toBe('K8S_CONTEXT_MISSING')
  })
  it('判据顺序敏感：未安装 kubectl 不能被后面的分类抢走', () => {
    expect(k8sErrorFromResult('bash: kubectl: command not found', 127)?.code).toBe('KUBECTL_NOT_FOUND')
  })
  it('回落到默认 8080 是「这台主机没有集群配置」，不是「集群连不上」', () => {
    // kubectl 找不到 kubeconfig 时不报「没配置」，而是连默认的 http://localhost:8080。
    // 把它当连不上（retryable）会让用户在一台没有 k8s 的主机上反复点重试。
    const e = k8sErrorFromResult(
      'The connection to the server localhost:8080 was refused - did you specify the right host or port?', 1)
    expect(e?.code).toBe('K8S_NOT_DETECTED')
    expect(e?.message).toBe('未检测到 k8s')
    expect(e?.retryable).toBe(false)
    expect(k8sErrorFromResult(
      'Unable to connect to the server: dial tcp 127.0.0.1:8080: connect: connection refused', 1)?.code,
    ).toBe('K8S_NOT_DETECTED')
    // 真地址上的同一句话仍然是「连不上」
    expect(k8sErrorFromResult('Unable to connect to the server: dial tcp 10.0.0.5:6443: connect: connection refused', 1)?.code).toBe('K8S_NO_CLUSTER')
  })
  it('回包是网页不是 JSON：单独成类，不把 kubectl 的日志行原样吐给用户', () => {
    const e = k8sErrorFromResult(
      'E1030 16:17:17.042406 1116243 memcache.go:265] "Unhandled Error" err="couldn\'t get current server API group list: invalid character \'<\' looking for beginning of value"', 1)
    expect(e?.code).toBe('K8S_API_NOT_JSON')
    expect(e?.message).not.toContain('memcache.go')
    expect(e?.message).toBe('集群地址配置有误')
    expect(e?.hint).toContain('server 地址')
    expect(e?.retryable).toBe(false)
  })
})

describe('用户看到的措辞：什么问题就说什么问题', () => {
  const cases: Array<[string, number, string]> = [
    ['bash: kubectl: command not found', 127, 'KUBECTL_NOT_FOUND'],
    ['error loading config file "/home/app/.kube/config": open /home/app/.kube/config: permission denied', 1, 'KUBECONFIG_UNREADABLE'],
    ['error: current-context is not set', 1, 'K8S_CONTEXT_MISSING'],
    ['The connection to the server localhost:8080 was refused - did you specify the right host or port?', 1, 'K8S_NOT_DETECTED'],
    ['E1030 16:17:17.042406 1116243 memcache.go:265] "Unhandled Error" err="couldn\'t get current server API group list: invalid character \'<\' looking for beginning of value"', 1, 'K8S_API_NOT_JSON'],
    ['Unable to connect to the server: dial tcp 10.0.0.5:6443: connect: connection refused', 1, 'K8S_NO_CLUSTER'],
    ['error: x509: certificate has expired or is not yet valid', 1, 'K8S_AUTH'],
    ['Error from server (Forbidden): pods is forbidden: User "app" cannot list resource "pods"', 1, 'K8S_FORBIDDEN'],
    ['error: the server doesn\'t have a resource type "ingresses"', 1, 'K8S_MISSING_KIND'],
    ['error: Metrics API not available', 1, 'K8S_NO_METRICS'],
    ['Error from server (NotFound): pods "web-9" not found', 1, 'K8S_NOT_FOUND_OBJ'],
  ]
  it('不夹 kubectl 日志、调试开关、RBAC / 证书术语', () => {
    for (const [stderr, exitCode, code] of cases) {
      const e = k8sErrorFromResult(stderr, exitCode)
      expect(e?.code).toBe(code)
      const copy = `${e?.message} ${e?.hint ?? ''}`
      expect(copy).not.toMatch(/memcache\.go|-v=6|admin\.conf|kubelet\.conf|sshd|RBAC|ClusterRole|x509/i)
    }
  })
  it('没检测到 k8s 的几种原因，标题统一「未检测到 k8s」，细节各说各的', () => {
    const titles = new Set<string>()
    for (const [stderr, exitCode, code] of cases.filter(c => isNotDetectedError(c[2]))) {
      const e = k8sErrorFromResult(stderr, exitCode)
      expect(e?.message, code).toBe('未检测到 k8s')
      titles.add(e?.hint || '')
    }
    // 细节不能退化成同一句：读不到配置和没装 kubectl 要做的事不一样
    expect(titles.size).toBe(4)
  })
  it('说「当前账号」而不是「这台主机没有 k8s」：同一台机器换个账号就有配置', () => {
    const e = k8sErrorFromResult(
      'The connection to the server localhost:8080 was refused - did you specify the right host or port?', 1)
    expect(e?.hint).toContain('当前 SSH 账号')
    expect(e?.hint).not.toContain('这台主机没有集群配置')
  })
  it('网络问题直说是网络问题', () => {
    const e = k8sErrorFromResult('Unable to connect to the server: dial tcp 10.0.0.5:6443: connect: connection refused', 1)
    expect(e?.message).toBe('连不上集群')
    expect(e?.hint).toContain('网络')
  })
  it('没权限 / 资源不存在都不把 kubectl 原文透给用户', () => {
    const forbidden = k8sErrorFromResult('Error from server (Forbidden): pods is forbidden: User "app" cannot list resource "pods"', 1)
    expect(forbidden?.message).toBe('当前账号没有读取权限')
    const gone = k8sErrorFromResult('Error from server (NotFound): pods "web-9" not found', 1)
    expect(gone?.message).toBe('这个资源已经不在了')
  })
  it('认不出的失败保留原始首行：如实说「不知道这是什么」，不编一句像样的解释', () => {
    const e = k8sErrorFromResult('error: unknown shorthand flag: q', 1)
    expect(e?.code).toBe('K8S_EXEC_FAILED')
    expect(e?.message).toContain('unknown shorthand flag')
  })
})

describe('环境自检：为什么读不到 k8s', () => {
  it('解析 exec 现场的 key=value；缺项留空，不拿本地猜', () => {
    const p = parseEnvProbe('user=root\nhome=/root\nkubeconfig=\nkubectl=/usr/local/bin/kubectl\nconfig_file=missing\n')
    expect(p).toEqual({
      user: 'root', home: '/root', kubeconfigEnv: '', kubectl: '/usr/local/bin/kubectl', configReadable: false,
    })
    expect(parseEnvProbe('')).toEqual({
      user: '', home: '', kubeconfigEnv: '', kubectl: '', configReadable: false,
    })
  })
  it('说清是哪个账号、断在哪一步', () => {
    const base = {
      user: 'root', home: '/root', kubeconfigEnv: '', kubectl: '/usr/local/bin/kubectl', configReadable: false,
    }
    expect(envProbeNote(null)).toBe('')
    expect(envProbeNote(base)).toContain('root（HOME=/root）')
    expect(envProbeNote(base)).toContain('没有可读的 ~/.kube/config')
    expect(envProbeNote({ ...base, kubeconfigEnv: '/tmp/k' })).toContain('KUBECONFIG 指向 /tmp/k')
    expect(envProbeNote({ ...base, kubectl: '' })).toContain('PATH 里找不到 kubectl')
    expect(envProbeNote({ ...base, configReadable: true })).toContain('能读到 ~/.kube/config')
  })
})

describe('hostErrorCopy：页顶错误带最终显示的那两行', () => {
  const err = (code: string, message: string, hint = ''): K8sError => ({ code, message, hint, retryable: false })
  it('分类层已说「未检测到 k8s」的直接透出', () => {
    const copy = hostErrorCopy(err('K8S_NOT_DETECTED', '未检测到 k8s', '把集群的 kubeconfig 放到 ~/.kube/config'), '')
    expect(copy).toEqual({ title: '未检测到 k8s', hint: '把集群的 kubeconfig 放到 ~/.kube/config' })
  })
  it('回包是网页时看 context：说不出 context 并进「未检测到 k8s」，说得出才报地址配错', () => {
    const e = err('K8S_API_NOT_JSON', '集群地址配置有误', '核对 kubeconfig 里的 server 地址和端口')
    expect(hostErrorCopy(e, '')?.title).toBe('未检测到 k8s')
    expect(hostErrorCopy(e, 'prod-master')).toEqual({ title: '集群地址配置有误', hint: '核对 kubeconfig 里的 server 地址和端口' })
  })
  it('其余错误原样透出分类层措辞；没有错误就没有错误带', () => {
    expect(hostErrorCopy(err('K8S_NO_CLUSTER', '连不上集群', '网络不通'), 'prod-master'))
      .toEqual({ title: '连不上集群', hint: '网络不通' })
    expect(hostErrorCopy(undefined, '')).toBeNull()
  })
  it('hint 缺失时给空串，页面不必再判 undefined', () => {
    expect(hostErrorCopy(err('K8S_AUTH', '集群认证失败'), 'prod')).toEqual({ title: '集群认证失败', hint: '' })
  })
})

describe('isNotDetectedError：哪些失败等于「这台主机没有 k8s」', () => {
  it('未装 / 读不到 kubeconfig / 没有 context / 回落到 8080 都算没检测到', () => {
    for (const code of ['KUBECTL_NOT_FOUND', 'KUBECONFIG_UNREADABLE', 'K8S_CONTEXT_MISSING', 'K8S_NOT_DETECTED']) {
      expect(isNotDetectedError(code)).toBe(true)
    }
  })
  it('集群在但读不了（连不上 / 鉴权 / RBAC / 地址回网页）不算没检测到', () => {
    for (const code of ['K8S_NO_CLUSTER', 'K8S_AUTH', 'K8S_FORBIDDEN', 'K8S_API_NOT_JSON', 'K8S_NO_METRICS']) {
      expect(isNotDetectedError(code)).toBe(false)
    }
    expect(isNotDetectedError(undefined)).toBe(false)
  })
  it('这两类是整机级的：五个页签报同一句，只在页顶说一次', () => {
    expect(isHostLevelError('K8S_NOT_DETECTED')).toBe(true)
    expect(isHostLevelError('K8S_API_NOT_JSON')).toBe(true)
  })
})

describe('isNotDetectedState：页顶那句是「未检测到 k8s」还是「集群出错」', () => {  const err = (code: string): K8sError => ({ code, message: 'x', retryable: false })
  it('没装 / 读不到 kubeconfig / 没 context / 连回落地址：报未检测到', () => {
    for (const code of ['KUBECTL_NOT_FOUND', 'KUBECONFIG_UNREADABLE', 'K8S_CONTEXT_MISSING', 'K8S_NOT_DETECTED']) {
      expect(isNotDetectedState(err(code), 'prod-master')).toBe(true)
    }
  })
  it('回包是网页时看 context 定夺：说不出 context 就是没配 k8s，说得出就是地址配错', () => {
    expect(isNotDetectedState(err('K8S_API_NOT_JSON'), '')).toBe(true)
    expect(isNotDetectedState(err('K8S_API_NOT_JSON'), 'prod-master')).toBe(false)
  })
  it('集群在、只是这台主机读不了（连不上 / 鉴权 / RBAC）不报未检测到', () => {
    for (const code of ['K8S_NO_CLUSTER', 'K8S_AUTH', 'K8S_FORBIDDEN', 'K8S_EXEC_FAILED']) {
      expect(isNotDetectedState(err(code), 'prod-master')).toBe(false)
    }
    expect(isNotDetectedState(undefined, '')).toBe(false)
  })
})

describe('describeThrownK8sError / k8sErrText', () => {
  it('普通 Error → EXEC_THROWN 带消息', () => {
    const e = describeThrownK8sError(new Error('channel closed'))
    expect(e.code).toBe('EXEC_THROWN')
    expect(e.message).toBe('channel closed')
  })
  it('取数抛出的 K8sError 直接给文本，不落成 [object Object]', () => {
    const err = { code: 'K8S_FORBIDDEN', message: '当前身份无权读取该类资源', retryable: false }
    expect(k8sErrText(err)).toBe('当前身份无权读取该类资源')
    expect(k8sErrText(new Error('boom'))).toBe('boom')
  })
})

describe('withExecDeadline：IPC 回包丢了也不能永远转圈', () => {
  it('过了预算就拒绝，并说清等了多少秒', async () => {
    vi.useFakeTimers()
    try {
      const p = withExecDeadline(new Promise<string>(() => { /* 永不结算 */ }), 15)
      const rejected = expect(p).rejects.toThrow('18 秒未返回')
      await vi.advanceTimersByTimeAsync(18_000)
      await rejected
    } finally {
      vi.useRealTimers()
    }
  })
  it('正常回包原样透传，且把定时器清掉（不在后台留一颗哑弹）', async () => {
    vi.useFakeTimers()
    try {
      await expect(withExecDeadline(Promise.resolve('ok'), 15)).resolves.toBe('ok')
      expect(vi.getTimerCount()).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })
  it('原错误原样抛，不被超时吞成成功', async () => {
    vi.useFakeTimers()
    try {
      await expect(withExecDeadline(Promise.reject(new Error('channel closed')), 15)).rejects.toThrow('channel closed')
      expect(vi.getTimerCount()).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })
})
