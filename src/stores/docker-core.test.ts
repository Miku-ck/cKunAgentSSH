/**
 * docker-core 解析与错误归类测试
 * fixture 里的字段分隔用真实制表符（docker --format 的 printf 输出的正是 \t）
 */
import { describe, expect, it } from 'vitest'
import {
  parsePs, parseImages, parseVolumes, parseNetworks, parseLogs, parseLabels,
  parseVolumeInspect, parseNetworkInspect, shortId, labelHint,
  imageLabel, imageHaystack,
  stateFromStatus, healthFromStatus, sq, dockerErrorFromResult, describeThrownDockerError,
  volumeRmCmd, networkRmCmd, volumePruneCmd, networkPruneCmd,
  volumeInspectCmd, networkInspectCmd, rmiCmd,
  containerInspectCmd, containerStatsCmd, pullCmd, imagePruneCmd, imagePruneAllCmd,
  dockerSaveCmd, dockerLoadCmd, saveTempPath, suggestTarName, validateSaveName, ensureTarSuffix,
  validateRemoteDir, joinRemotePath,
  parseContainerInspect, parseContainerStats, formatNsDuration,
  countPrunedLines, isBuiltinNetwork,
  parseVolumeUsage, mergeVolumeUsage, volumeUsageSummary,
  imageRefMatches, imageUsageCount,
  VOLUMES_CMD, NETWORKS_CMD,
  type ContainerRow, type ImageRow, type VolumeRow,
} from './docker-core'

const T = '\t'

describe('stateFromStatus', () => {
  it('Up → running', () => {
    expect(stateFromStatus('Up 7 days')).toBe('running')
    expect(stateFromStatus('Up 13 seconds (health: starting)')).toBe('running')
  })
  it('Exited (0) / paused → stopped（可逆语义）', () => {
    expect(stateFromStatus('Exited (0) 2 hours ago')).toBe('stopped')
    // docker ps 对暂停容器实际输出的是 "Up About an hour (Paused)"，不是裸 paused；
    // 它先命中 /^up/ 就会被误判成 running。
    expect(stateFromStatus('Up About an hour (Paused)')).toBe('stopped')
    expect(stateFromStatus('Up 2 minutes (Paused)')).toBe('stopped')
  })
  it('异常退出 → exited', () => {
    expect(stateFromStatus('Exited (137) 5 minutes ago')).toBe('exited')
    expect(stateFromStatus('Created')).toBe('exited')
  })
  it('Restarting（崩溃重启循环）→ exited（异常、非运行绿）', () => {
    expect(stateFromStatus('Restarting (1) 5 seconds ago')).toBe('exited')
  })
})

describe('healthFromStatus', () => {
  it('从 Up 状态里解析健康标记', () => {
    expect(healthFromStatus('Up 2 hours (healthy)')).toBe('healthy')
    expect(healthFromStatus('Up 2 hours (unhealthy)')).toBe('unhealthy')
    expect(healthFromStatus('Up 3 seconds (health: starting)')).toBe('starting')
  })
  it('无健康标记 / paused / exited → undefined', () => {
    expect(healthFromStatus('Up 7 days')).toBeUndefined()
    expect(healthFromStatus('Up About an hour (Paused)')).toBeUndefined()
    expect(healthFromStatus('Exited (0) 2 hours ago')).toBeUndefined()
  })
  it('parsePs 带出 health，不改 state', () => {
    const raw = ['abcd1234efgh', 'nginx:1.27', 'nginx', '1 day ago', 'Up 2 hours (unhealthy)', '80/tcp->80', 'web'].join(T)
    const [c] = parsePs(raw)
    expect(c.state).toBe('running')
    expect(c.health).toBe('unhealthy')
  })
})

describe('sq shell 引用', () => {
  it('包单引号', () => {
    expect(sq('nginx')).toBe("'nginx'")
  })
  it("内部单引号用 '\\'' 脱出", () => {
    expect(sq("a'b")).toBe(`'a'\\''b'`)
  })
})

describe('parsePs', () => {
  // 字段序对齐 docker ps：ID · IMAGE · COMMAND · CREATED · STATUS · PORTS · NAMES
  const raw = [
    ['8f3c2a1d9e4b', 'nginx:1.27-alpine', 'nginx -g daemon off\;', '7 days ago', 'Up 7 days', '80/tcp->80', 'nginx-proxy'].join(T),
    ['9i0j1k2l3m4n', 'harbor.ckun.io/migrate:v2.4.0', './migrate up', '2 hours ago', 'Exited (137) 2 hours ago', '', 'migrate-job'].join(T),
    ['3m4n5o6p7q8r', 'redis', 'redis-server', '5 days ago', 'Exited (0) 5 days ago', '6379/tcp', 'redis-cache'].join(T),
  ].join('\n')

  it('拆字段 + repo:tag + 状态映射 + created/name', () => {
    const rows = parsePs(raw)
    expect(rows).toHaveLength(3)
    expect(rows[0]).toMatchObject({ id: '8f3c2a1d9e4b', name: 'nginx-proxy', repo: 'nginx', tag: '1.27-alpine', state: 'running', created: '7 days ago', cmd: 'nginx -g daemon off;' })
    expect(rows[1]).toMatchObject({ repo: 'harbor.ckun.io/migrate', tag: 'v2.4.0', state: 'exited', ports: '', name: 'migrate-job' })
    expect(rows[2]).toMatchObject({ state: 'stopped', name: 'redis-cache' })
  })
  it('无 tag 的镜像回落 latest', () => {
    expect(parsePs(raw)[2].tag).toBe('latest')
  })
  it('带端口 registry 且无 tag（localhost:5000/nginx）不当作 tag 误切', () => {
    const r = ['abcd1234efgh', 'localhost:5000/nginx', 'nginx', '1 day ago', 'Up 1 day', '', 'p'].join(T)
    expect(parsePs(r)[0]).toMatchObject({ repo: 'localhost:5000/nginx', tag: 'latest' })
  })
  it('带端口 registry 且显式带 tag（host:5000/img:v1）正常拆分', () => {
    const r = ['abcd1234efgh', 'localhost:5000/nginx:v1', 'nginx', '1 day ago', 'Up 1 day', '', 'p'].join(T)
    expect(parsePs(r)[0]).toMatchObject({ repo: 'localhost:5000/nginx', tag: 'v1' })
  })
})

describe('parseImages / parseVolumes / parseNetworks', () => {
  it('images 的 ID 归一为 12 位短 ID（sha256: 前缀不能被算进长度）', () => {
    const raw = ['sha256:8a1f2c3d4e5f6a7b', 'nginx', '1.27-alpine', '62.1MB', '2 weeks ago'].join(T)
    const [im] = parseImages(raw)
    // 旧实现是裸 slice(0,12) → 'sha256:8a1f2'：既不是合法引用也没有可读性，
    // 删悬空镜像时 `docker rmi sha256:8a1f2` 必然报 No such image。
    expect(im.id).toBe('8a1f2c3d4e5f')
    expect(im).toMatchObject({ repo: 'nginx', tag: '1.27-alpine', size: '62.1MB' })
  })
  it('已是短 ID / 裸 64 位十六进制都归到同一形状', () => {
    const short = ['8a1f2c3d4e5f', 'nginx', 'latest', '1MB', 'now'].join(T)
    expect(parseImages(short)[0].id).toBe('8a1f2c3d4e5f')
    const bare = ['8a1f2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b', 'x', 'y', '1MB', 'now'].join(T)
    expect(parseImages(bare)[0].id).toBe('8a1f2c3d4e5f')
  })
  it('volumes 命令禁用 --verbose（老版本 Docker 不识别该 flag 直接报 unknown flag）', () => {
    // 老版本 (<23) docker volume ls 没有 --verbose，参数解析阶段就失败，
    // 走不到 {{json .}} 模板；Size/LinkCount 缺失由解析层可选字段降级（显示「—」）。
    expect(VOLUMES_CMD).not.toMatch(/--verbose/)
    expect(VOLUMES_CMD).toBe(`docker volume ls --format '{{json .}}'`)
    expect(NETWORKS_CMD).not.toMatch(/--verbose/)
  })
  // 降级旧行入口仍兼容（json 函数不可用时的手写 printf 回测）
  it('volumes 降级旧行入拆分driver + name', () => {
    expect(parseVolumes(['local', 'pgdata'].join(T))[0]).toEqual({ driver: 'local', name: 'pgdata' })
  })
  it('networks 降级旧行四字段', () => {
    const raw = ['a1b2c3d4e5f6a7b8', 'bridge', 'app-net', 'local'].join(T)
    expect(parseNetworks(raw)[0]).toMatchObject({ driver: 'bridge', name: 'app-net', scope: 'local' })
    expect(parseNetworks(raw)[0].id).toHaveLength(12)
  })
  it('volumes json 行优先解析，包含 size/linkCount/labels', () => {
    const raw = [
      JSON.stringify({ Driver: 'local', Name: 'pgdata', Size: 1073741824, LinkCount: 1, Labels: { 'com.docker.compose.project': 'app' } }),
      JSON.stringify({ Driver: 'local', Name: 'orphan', Size: 0, LinkCount: 0, Labels: {} }),
    ].join('\n')
    const rows = parseVolumes(raw)
    expect(rows).toHaveLength(2)
    expect(rows[0]).toEqual({
      driver: 'local', name: 'pgdata', size: '1.0GB', linkCount: 1,
      labels: { 'com.docker.compose.project': 'app' },
    })
    // 真实列表命令不发 --verbose，Size 缺失时 formatBytesCompact(undefined)=undefined 故不写入（显示「—」）；
    // 此处合成的 Size:0 会得到 '0B'（空卷确为 0 字节），非字段缺失场景。
    expect(rows[1].name).toBe('orphan')
    expect(rows[1].linkCount).toBe(0)
    expect(rows[1].labels).toBeUndefined()
  })
  it('volumes json 行无 Size/Links（旧 Docker）时字段不注入', () => {
    const raw = JSON.stringify({ Driver: 'local', Name: 'old-vol', Labels: {} })
    const [v] = parseVolumes(raw)
    expect(v).toEqual({ driver: 'local', name: 'old-vol' })
  })
  it('volumes json 行按 Docker 实际形状解析：Labels 逗号串、Links/Size 字符串', () => {
    // docker volume ls --format '{{json .}}' 的真实输出：Labels 是 "k=v,k2=v2"，
    // Links/Size 无 UsageData 时是字符串 "N/A"（不是 number，也不是 map）。
    const raw = JSON.stringify({
      Driver: 'local', Name: 'pgdata', Size: 'N/A', Links: 'N/A',
      Labels: 'com.docker.compose.project=app,env=prod',
    })
    const [v] = parseVolumes(raw)
    expect(v.size).toBeUndefined()
    expect(v.linkCount).toBeUndefined()
    expect(v.labels).toEqual({ 'com.docker.compose.project': 'app', env: 'prod' })
    expect(labelHint(v.labels)).toBe('compose:app')
  })
  it('volumes json 行 Links 数字字符串 → linkCount；Size 人读串直通', () => {
    const raw = JSON.stringify({ Driver: 'local', Name: 'v', Size: '1.0GB', Links: '2' })
    expect(parseVolumes(raw)[0]).toMatchObject({ size: '1.0GB', linkCount: 2 })
  })
  it('networks json 行 Labels 为逗号串时仍能解析', () => {
    const raw = JSON.stringify({
      ID: 'a1b2c3d4e5f6a7b8c9d0', Driver: 'bridge', Name: 'app-net', Scope: 'local',
      Labels: 'tier=backend,com.docker.compose.project=app',
    })
    const [n] = parseNetworks(raw)
    expect(n.labels).toEqual({ tier: 'backend', 'com.docker.compose.project': 'app' })
    expect(labelHint(n.labels)).toBe('compose:app')
  })
  it('networks json 行优先解析，labels 非空才注入', () => {
    const raw = [
      JSON.stringify({ ID: 'a1b2c3d4e5f6a7b8c9d0', Driver: 'bridge', Name: 'app-net', Scope: 'local', Labels: { tier: 'backend' } }),
      JSON.stringify({ ID: 'deadbeefcafe1234', Driver: 'host', Name: 'host', Scope: 'local', Labels: {} }),
    ].join('\n')
    const rows = parseNetworks(raw)
    expect(rows[0].id).toHaveLength(12)
    expect(rows[0].labels).toEqual({ tier: 'backend' })
    expect(rows[1].labels).toBeUndefined()
  })
})

describe('parseLabels', () => {
  it('JSON 对象', () => {
    expect(parseLabels('{"a":"1","b":"2"}')).toEqual({ a: '1', b: '2' })
  })
  it('Go map 降级', () => {
    expect(parseLabels('map[a:1 b:2]')).toEqual({ a: '1', b: '2' })
  })
  it('Docker formatter 的逗号拼接标签', () => {
    expect(parseLabels('a=1,b=2')).toEqual({ a: '1', b: '2' })
    expect(parseLabels('com.docker.compose.project=app')).toEqual({ 'com.docker.compose.project': 'app' })
    // 值内部可含逗号：只在「逗号后是下一个 key=」的位置切
    expect(parseLabels('note=a,b,c=2')).toEqual({ note: 'a,b', c: '2' })
  })
  it('空 / <no value> / 坏 JSON → {}', () => {
    expect(parseLabels('')).toEqual({})
    expect(parseLabels('<no value>')).toEqual({})
    expect(parseLabels('map[]')).toEqual({})
    expect(parseLabels('{bad')).toEqual({})
  })
})
describe('inspect 解析', () => {
  it('volume inspect: 数组取首项，UsageData 字段可选', () => {
    const raw = JSON.stringify([{
      Name: 'pgdata', Driver: 'local', Mountpoint: '/var/lib/docker/volumes/pgdata/_data',
      CreatedAt: '2026-01-01T00:00:00Z', Labels: { env: 'prod' }, Options: { type: 'none' },
      UsageData: { Size: 1073741824, RefCount: 1 },
    }])
    const v = parseVolumeInspect(raw)
    expect(v).toMatchObject({
      name: 'pgdata', driver: 'local', mountpoint: '/var/lib/docker/volumes/pgdata/_data',
      createdAt: '2026-01-01T00:00:00Z', labels: { env: 'prod' },
      usage: { size: 1073741824, refCount: 1 },
    })
  })
  it('volume inspect: 空数组 / 非法 JSON → null', () => {
    expect(parseVolumeInspect('[]')).toBeNull()
    expect(parseVolumeInspect('boom')).toBeNull()
  })
  it('network inspect: IPAM + 连接容器剥掩码', () => {
    const raw = JSON.stringify([{
      Name: 'app-net', Id: 'abcdef1234567890', Driver: 'bridge', Scope: 'local',
      Internal: false, Attachable: true, Ingress: false, EnableIPv6: false,
      IPAM: { Config: [{ Subnet: '172.18.0.0/16', Gateway: '172.18.0.1', IPRange: '' }] },
      Containers: {
        'cid1': { Name: 'api', IPv4Address: '172.18.0.5/16', MacAddress: '02:42:0a:12:00:05' },
        'cid2': { Name: 'db', IPv4Address: '172.18.0.6/16', MacAddress: '02:42:0a:12:00:06' },
      },
      Labels: {}, Options: {},
    }])
    const n = parseNetworkInspect(raw)
    expect(n?.id).toHaveLength(12)
    expect(n?.attachable).toBe(true)
    expect(n?.ipam[0].subnet).toBe('172.18.0.0/16')
    expect(n?.containers.map(c => c.name)).toEqual(['api', 'db'])
    expect(n?.containers[0].ipv4).toBe('172.18.0.5')
  })
})

describe('卷用量富集（docker system df -v）', () => {
  it('解析 Name/Links/Size 三列；N/A 与非法计数视为缺失', () => {
    const raw = [
      ['pgdata', '1', '1.0GB'].join(T),
      ['orphan', '0', 'N/A'].join(T),
      ['old', 'N/A', 'N/A'].join(T),
      ['bad', '-1', '2MB'].join(T),
    ].join('\n')
    const usage = parseVolumeUsage(raw)
    expect(usage.get('pgdata')).toEqual({ size: '1.0GB', linkCount: 1 })
    expect(usage.get('orphan')).toEqual({ linkCount: 0 })
    expect(usage.has('old')).toBe(false)
    // 非法 Links 只丢计数，仍有效的 Size 照常保留
    expect(usage.get('bad')).toEqual({ size: '2MB' })
  })
  it('按卷名合并，未命中的行保持原样；0 引用不能被当成缺失', () => {
    const list: VolumeRow[] = [
      { driver: 'local', name: 'a' },
      { driver: 'local', name: 'b', size: '2GB', linkCount: 5 },
    ]
    const usage = new Map([
      ['a', { size: '1GB', linkCount: 0 }],
      ['missing', { size: '3GB' }],
    ])
    const merged = mergeVolumeUsage(list, usage)
    expect(merged[0]).toMatchObject({ size: '1GB', linkCount: 0 })
    expect(merged[1]).toEqual(list[1])
  })
  it('详情用量优先 inspect，缺失时回落到列表行', () => {
    const row: VolumeRow = { driver: 'local', name: 'v', size: '1.0GB', linkCount: 3 }
    expect(volumeUsageSummary(row, { usage: { size: 2048, refCount: 4 } })).toEqual({
      refCount: 4, size: '2.0KB',
    })
    expect(volumeUsageSummary(row, null)).toEqual({ refCount: 3, size: '1.0GB' })
    expect(volumeUsageSummary({ driver: 'local', name: 'v' }, null)).toEqual({
      refCount: undefined, size: undefined,
    })
  })
})

describe('卷/网络 动作命令构造', () => {
  it('shell 引用容忍含空格名', () => {
    expect(volumeRmCmd('my vol')).toBe(`docker volume rm 'my vol'`)
    expect(networkRmCmd("a'b")).toBe(`docker network rm 'a'\\''b'`)
  })
  it('prune 带 -f（确认在 UI 层，SSH 非 tty 无法交互）', () => {
    expect(volumePruneCmd()).toBe('docker volume prune -f')
    expect(networkPruneCmd()).toBe('docker network prune -f')
  })
  it('inspect 命令带名称', () => {
    expect(volumeInspectCmd('pgdata')).toBe(`docker volume inspect 'pgdata'`)
    expect(networkInspectCmd('app-net')).toBe(`docker network inspect 'app-net'`)
  })
})

describe('countPrunedLines', () => {
  it('卷：表头与回收空间汇总不计入个数', () => {
    const out = 'Deleted Volumes:\nvol-a\nvol-b\n\nTotal reclaimed space: 12.3MB\n'
    expect(countPrunedLines(out)).toBe(2)
  })
  it('网络：表头不计入个数', () => {
    expect(countPrunedLines('Deleted Networks:\napp-net\n')).toBe(1)
  })
  it('没有删除项时返回 0', () => {
    expect(countPrunedLines('')).toBe(0)
    expect(countPrunedLines('Total reclaimed space: 0B\n')).toBe(0)
  })
})

describe('isBuiltinNetwork', () => {
  it('Docker 预置的 bridge/host/none 不可删除', () => {
    expect(isBuiltinNetwork('bridge')).toBe(true)
    expect(isBuiltinNetwork('host')).toBe(true)
    expect(isBuiltinNetwork('none')).toBe(true)
  })
  it('自定义网络可删除（名字包含关键字也不算内置）', () => {
    expect(isBuiltinNetwork('app-net')).toBe(false)
    expect(isBuiltinNetwork('bridge-net')).toBe(false)
  })
})

describe('镜像删除命令', () => {
  it('默认不加强制位：有容器引用时由 docker 拒绝（与确认框承诺一致）', () => {
    expect(rmiCmd('nginx:1.27')).toBe(`docker rmi 'nginx:1.27'`)
  })
  it('显式 force 才带 -f', () => {
    expect(rmiCmd('nginx:1.27', true)).toBe(`docker rmi -f 'nginx:1.27'`)
  })
})

describe('DOCKER_IN_USE 归类', () => {
  it('volume rm 时占用', () => {
    const d = dockerErrorFromResult(
      'Error response from daemon: remove pgdata: volume is in use by some container', 1)
    expect(d?.code).toBe('DOCKER_IN_USE')
    expect(d?.retryable).toBe(false)
  })
  it('network rm 时 active endpoints', () => {
    const d = dockerErrorFromResult(
      'Error response from daemon: network app-net has active endpoints', 1)
    expect(d?.code).toBe('DOCKER_IN_USE')
  })
})

describe('parseLogs', () => {
  it('去 CR、剥尾部空行、保留内部空行', () => {
    expect(parseLogs('a\r\n\r\nb\r\n')).toEqual(['a', '', 'b'])
  })
  it('全空返回空数组', () => {
    expect(parseLogs('')).toEqual([])
  })
})

describe('dockerErrorFromResult', () => {
  it('退出码 0 → null（成功，即便 stderr 有提示）', () => {
    expect(dockerErrorFromResult('Some warning on stderr', 0)).toBeNull()
  })
  it('127 / command not found → DOCKER_NOT_FOUND 且不可重试', () => {
    const d = dockerErrorFromResult('bash: docker: command not found', 127)
    expect(d?.code).toBe('DOCKER_NOT_FOUND')
    expect(d?.retryable).toBe(false)
  })
  it('permission denied 连 socket → DOCKER_PERM', () => {
    const d = dockerErrorFromResult('permission denied while trying to connect to the Docker daemon socket', 1)
    expect(d?.code).toBe('DOCKER_PERM')
  })
  it('Cannot connect to the Docker daemon → DOCKER_PERM', () => {
    expect(dockerErrorFromResult('Cannot connect to the Docker daemon at unix:///var/run/docker.sock', 1)?.code).toBe('DOCKER_PERM')
  })
  it('No such container → 目标不存在、可重试', () => {
    const d = dockerErrorFromResult('Error response from daemon: No such container: foo', 1)
    expect(d?.code).toBe('DOCKER_NOT_FOUND_OBJ')
    expect(d?.retryable).toBe(true)
  })
  it('其它非零退出 → DOCKER_EXEC_FAILED 透首行', () => {
    const d = dockerErrorFromResult('Error: something\nsecond line', 1)
    expect(d?.code).toBe('DOCKER_EXEC_FAILED')
    expect(d?.message).toBe('Error: something')
  })
  it('exit-status 缺失（null）且有 stderr → DOCKER_UNKNOWN', () => {
    expect(dockerErrorFromResult('boom', null)?.code).toBe('DOCKER_UNKNOWN')
  })
  it('docker logs（2>&1）：stderr 空但合并 stdout 含错 → 正确归类不降级', () => {
    const d = dockerErrorFromResult('', 1, 'Error response from daemon: No such container: foo')
    expect(d?.code).toBe('DOCKER_NOT_FOUND_OBJ')
    expect(d?.retryable).toBe(true)
    const p = dockerErrorFromResult('', 1, 'Cannot connect to the Docker daemon at unix:///var/run/docker.sock')
    expect(p?.code).toBe('DOCKER_PERM')
  })
  it('stderr 优先于合并 stdout（两者都有时取 stderr）', () => {
    expect(dockerErrorFromResult('permission denied while trying to connect to the Docker daemon socket', 1, 'irrelevant log')?.code).toBe('DOCKER_PERM')
  })
})

describe('shortId / imageLabel / imageHaystack', () => {
  it('shortId：带算法前缀、裸长十六进制、已截短三种形态都落到 12 位', () => {
    expect(shortId('sha256:8a1f2c3d4e5f6a7b8c9d')).toBe('8a1f2c3d4e5f')
    expect(shortId('8a1f2c3d4e5f6a7b8c9d')).toBe('8a1f2c3d4e5f')
    expect(shortId('8a1f2c3d4e5f')).toBe('8a1f2c3d4e5f')
  })
  it('shortId：非十六进制的值原样透传，不当摘要剥（<none> 不许被裁）', () => {
    expect(shortId('<none>')).toBe('<none>')
    expect(shortId(null)).toBe('')
    expect(shortId(undefined)).toBe('')
  })
  it('imageLabel：repo 与 tag 任一为 <none> 都回落到可删的短 ID', () => {
    const mk = (repo: string, tag: string): ImageRow =>
      ({ id: '8a1f2c3d4e5f', repo, tag, size: '', created: '' })
    expect(imageLabel(mk('<none>', '<none>'))).toBe('8a1f2c3d4e5f')
    // 按摘要拉取的行：repo 有名、Tag 是 <none>。原实现只判 repo，于是
    // imageLabel 交出 'nginx:<none>' —— 那不是合法引用，docker rmi 必失败。
    expect(imageLabel(mk('nginx', '<none>'))).toBe('8a1f2c3d4e5f')
    expect(imageLabel(mk('harbor.ckun.io/migrate', 'v2.4.0'))).toBe('harbor.ckun.io/migrate:v2.4.0')
  })
  it('imageHaystack：回落到短 ID 的行仍按仓库名搜得到', () => {
    const im: ImageRow = { id: '8a1f2c3d4e5f', repo: 'nginx', tag: '<none>', size: '', created: '' }
    expect(imageHaystack(im)).toContain('nginx')
    expect(imageHaystack(im)).toContain('8a1f2c3d4e5f')
    expect(imageHaystack(im)).toContain('nginx:<none>')
  })
})

describe('labelHint', () => {
  it('compose 项目名优先于条数', () => {
    expect(labelHint({ 'com.docker.compose.project': 'app', other: '1' })).toBe('compose:app')
  })
  it('无 compose 键给条数，空/缺失给「—」', () => {
    expect(labelHint({ a: '1', b: '2' })).toBe('2 项')
    expect(labelHint({})).toBe('—')
    expect(labelHint(undefined)).toBe('—')
  })
})

describe('describeThrownDockerError', () => {
  it('普通 Error → EXEC_THROWN 带消息', () => {
    const d = describeThrownDockerError(new Error('channel closed'))
    expect(d.code).toBe('EXEC_THROWN')
    expect(d.message).toBe('channel closed')
  })
})

describe('容器 inspect / stats 命令', () => {
  it('容器名一律 shell 引用', () => {
    expect(containerInspectCmd('my web')).toBe(`docker container inspect 'my web'`)
    expect(containerStatsCmd("a'b")).toBe(`docker stats --no-stream --format '{{json .}}' 'a'\\''b'`)
  })
})

describe('formatNsDuration', () => {
  it('ns → 人读时长', () => {
    expect(formatNsDuration(30e9)).toBe('30s')
    expect(formatNsDuration(90e9)).toBe('1m30s')
    expect(formatNsDuration(500e6)).toBe('500ms')
  })
  it('0 / 缺失 / 非法 → undefined', () => {
    expect(formatNsDuration(0)).toBeUndefined()
    expect(formatNsDuration(undefined)).toBeUndefined()
    expect(formatNsDuration(-1)).toBeUndefined()
  })
})

describe('parseContainerInspect', () => {
  const raw = JSON.stringify([{
    Id: 'sha256:1111222233334444',
    Name: '/api',
    Image: 'sha256:aaaabbbbccccdddd',
    Created: '2026-10-01T00:00:00Z',
    Platform: 'linux',
    Path: '/docker-entrypoint.sh',
    Args: ['nginx', '-g', 'daemon off;'],
    State: {
      Status: 'running', Running: true, Paused: false, Restarting: false,
      OOMKilled: false, ExitCode: 0,
      StartedAt: '2026-10-20T08:00:00Z', FinishedAt: '0001-01-01T00:00:00Z',
      Health: {
        Status: 'unhealthy', FailingStreak: 3,
        Log: [{
          Start: '2026-10-20T09:00:00Z', End: '2026-10-20T09:00:01Z',
          ExitCode: 1, Output: 'curl: (7) refused\n',
        }],
      },
    },
    RestartCount: 2,
    Config: {
      Image: 'nginx:1.27',
      Entrypoint: ['/docker-entrypoint.sh'],
      Cmd: ['nginx', '-g', 'daemon off;'],
      WorkingDir: '/', User: 'root',
      Env: ['PATH=/usr/bin', 'TOKEN=a=b'],
      Labels: {
        'com.docker.compose.project': 'app',
        'com.docker.compose.service': 'web',
        'com.docker.compose.project.config_files': '/srv/app/docker-compose.yml',
      },
      Healthcheck: {
        Test: ['CMD-SHELL', 'curl -f http://localhost'],
        Interval: 30e9, Timeout: 5e9, StartPeriod: 10e9, Retries: 3,
      },
    },
    HostConfig: {
      RestartPolicy: { Name: 'on-failure', MaximumRetryCount: 3 },
      Memory: 536870912, NanoCpus: 500000000, PidsLimit: 256,
    },
    NetworkSettings: {
      Ports: {
        '80/tcp': [{ HostIp: '0.0.0.0', HostPort: '8080' }],
        '443/tcp': null,
      },
      Networks: {
        'app-net': {
          IPAddress: '172.18.0.5', Gateway: '172.18.0.1',
          MacAddress: '02:42:ac:12:00:05',
        },
      },
    },
    Mounts: [
      {
        Type: 'volume', Name: 'appdata',
        Source: '/var/lib/docker/volumes/appdata/_data',
        Destination: '/data', RW: true,
      },
      {
        Type: 'bind', Name: '',
        Source: '/srv/app/conf', Destination: '/etc/nginx/conf.d', RW: false,
      },
    ],
    RepoDigests: ['nginx@sha256:deadbeef'],
  }])

  it('投影状态 / 健康 / 重启 / 网络 / 端口 / 挂载 / env / compose', () => {
    const v = parseContainerInspect(raw)
    expect(v).not.toBeNull()
    expect(v).toMatchObject({
      id: '111122223333',
      name: 'api',
      image: 'nginx:1.27',
      imageId: 'aaaabbbbcccc',
      imageDigest: 'nginx@sha256:deadbeef',
      created: '2026-10-01T00:00:00Z',
      platform: 'linux',
      restartCount: 2,
      restartPolicy: 'on-failure:3',
      workingDir: '/',
      user: 'root',
      entrypoint: ['/docker-entrypoint.sh'],
      cmd: ['nginx', '-g', 'daemon off;'],
      compose: {
        project: 'app', service: 'web',
        configFiles: '/srv/app/docker-compose.yml',
      },
    })
    expect(v!.state).toMatchObject({
      status: 'running',
      running: true,
      health: 'unhealthy',
      failingStreak: 3,
      startedAt: '2026-10-20T08:00:00Z',
      finishedAt: '',
      lastHealth: {
        exitCode: 1, output: 'curl: (7) refused\n',
        end: '2026-10-20T09:00:01Z',
      },
    })
    expect(v!.env).toEqual([
      { key: 'PATH', value: '/usr/bin' },
      { key: 'TOKEN', value: 'a=b' },
    ])
    expect(v!.healthcheck).toEqual({
      test: 'CMD-SHELL curl -f http://localhost',
      interval: '30s', timeout: '5s', startPeriod: '10s', retries: 3,
    })
    expect(v!.ports).toEqual([
      { container: '80/tcp', host: '0.0.0.0:8080' },
      { container: '443/tcp', host: '' },
    ])
    expect(v!.networks).toEqual([
      {
        name: 'app-net', ip: '172.18.0.5', gateway: '172.18.0.1',
        mac: '02:42:ac:12:00:05',
      },
    ])
    expect(v!.mounts).toEqual([
      {
        type: 'volume', name: 'appdata',
        source: '/var/lib/docker/volumes/appdata/_data',
        destination: '/data', rw: true,
      },
      {
        type: 'bind', name: '',
        source: '/srv/app/conf',
        destination: '/etc/nginx/conf.d', rw: false,
      },
    ])
    expect(v!.resources).toEqual({ memory: '512.0MB', cpus: '0.5', pidsLimit: 256 })
  })

  it('缺字段时回落默认，不抛', () => {
    const v = parseContainerInspect(JSON.stringify([{ Id: 'abc', Name: '/x' }]))
    expect(v?.id).toBe('abc')
    expect(v?.name).toBe('x')
    expect(v?.state.status).toBe('')
    expect(v?.state.health).toBeUndefined()
    expect(v?.env).toEqual([])
    expect(v?.ports).toEqual([])
    expect(v?.networks).toEqual([])
    expect(v?.mounts).toEqual([])
    expect(v?.healthcheck).toBeUndefined()
    expect(v?.restartPolicy).toBe('')
    expect(v?.compose).toBeUndefined()
  })

  it('空数组 / 非法 JSON → null', () => {
    expect(parseContainerInspect('[]')).toBeNull()
    expect(parseContainerInspect('boom')).toBeNull()
  })
})

describe('parseContainerStats', () => {
  it('解析 docker stats --no-stream 的 JSON 行', () => {
    const raw = JSON.stringify({
      CPUPerc: '0.12%', MemUsage: '12.5MiB / 1GiB', MemPerc: '1.22%',
      NetIO: '1.2kB / 3.4kB', BlockIO: '0B / 0B', PIDs: '5',
    })
    expect(parseContainerStats(raw)).toEqual({
      cpuPerc: '0.12%', memUsage: '12.5MiB / 1GiB', memPerc: '1.22%',
      netIO: '1.2kB / 3.4kB', blockIO: '0B / 0B', pids: '5',
    })
  })
  it('空输出 / 无 JSON → null', () => {
    expect(parseContainerStats('')).toBeNull()
    expect(parseContainerStats('not json')).toBeNull()
  })
  it('字段缺失给空串，不注入 undefined', () => {
    expect(parseContainerStats('{"CPUPerc":"1%"}')).toEqual({
      cpuPerc: '1%', memUsage: '', memPerc: '', netIO: '', blockIO: '', pids: '',
    })
  })
})

describe('镜像拉取 / 悬空清理', () => {
  it('pull 用 --quiet，引用按单引号转义', () => {
    expect(pullCmd('nginx:1.27')).toBe(`docker pull --quiet 'nginx:1.27'`)
  })
  it('悬空镜像清理只清 dangling（不带 -a）', () => {
    expect(imagePruneCmd()).toBe('docker image prune -f')
  })
  it('未使用镜像清理带 -a', () => {
    expect(imagePruneAllCmd()).toBe('docker image prune -a -f')
  })
  it('image prune 的 Deleted Images 表头不计入个数', () => {
    const out = 'Deleted Images:\ndeleted: sha256:aaa\ndeleted: sha256:bbb\n\nTotal reclaimed space: 1.2GB\n'
    expect(countPrunedLines(out)).toBe(2)
  })
})

describe('镜像引用匹配', () => {
  const img = (over: Partial<ImageRow> = {}): ImageRow => ({
    id: '8a1f2c3d4e5f', repo: 'nginx', tag: '1.27-alpine', size: '62.1MB', created: 'now', ...over,
  })
  const container = (id: string, imageRef: string): ContainerRow => ({
    id, name: 'c' + id, repo: 'nginx', tag: 'latest', imageRef,
    state: 'stopped', stateText: 'Exited (0)', ports: '', cmd: '', created: '',
  })

  it('parsePs 保留 IMAGE 原始串（repo/tag 拆分不再丢形态）', () => {
    const raw = ['abcd1234efgh', 'harbor.ckun.io/app@sha256:abc', 'x', '1 day ago', 'Up 1 day', '', 'p'].join(T)
    expect(parsePs(raw)[0].imageRef).toBe('harbor.ckun.io/app@sha256:abc')
  })
  it('repo:tag 命中；latest 的裸 repo 也命中', () => {
    expect(imageRefMatches(img(), 'nginx:1.27-alpine')).toBe(true)
    expect(imageRefMatches(img({ tag: 'latest' }), 'nginx')).toBe(true)
    expect(imageRefMatches(img({ tag: 'latest' }), 'nginx:latest')).toBe(true)
  })
  it('短 ID / 完整 sha256 / 裸 64 位 hex 命中', () => {
    expect(imageRefMatches(img(), '8a1f2c3d4e5f')).toBe(true)
    expect(imageRefMatches(img(), 'sha256:8a1f2c3d4e5f' + 'a'.repeat(52))).toBe(true)
    expect(imageRefMatches(img(), '8a1f2c3d4e5f' + 'b'.repeat(52))).toBe(true)
  })
  it('按摘要创建：repo@sha256:… 命中 tag=<none> 的同仓镜像', () => {
    expect(imageRefMatches(img({ tag: '<none>' }), 'nginx@sha256:abc')).toBe(true)
  })
  it('别的 tag / 别的仓库 / 悬空镜像不误命中', () => {
    expect(imageRefMatches(img(), 'nginx:1.29')).toBe(false)
    expect(imageRefMatches(img(), 'redis:1.27-alpine')).toBe(false)
    expect(imageRefMatches(img({ repo: '<none>', tag: '<none>' }), 'nginx:1.27-alpine')).toBe(false)
  })
  it('imageUsageCount 按容器去重，含已停止容器', () => {
    const list = [
      container('c1', 'nginx:1.27-alpine'),
      container('c2', '8a1f2c3d4e5f'),
      container('c3', 'redis:7'),
    ]
    expect(imageUsageCount(img(), list)).toBe(2)
    expect(imageUsageCount(img(), [])).toBe(0)
  })
})

describe('镜像拉取错误归类', () => {
  it('unauthorized / denied → DOCKER_AUTH，不可重试，提示 docker login', () => {
    const d = dockerErrorFromResult('Error response from daemon: unauthorized: authentication required', 1)
    expect(d?.code).toBe('DOCKER_AUTH')
    expect(d?.retryable).toBe(false)
    expect(d?.hint).toContain('docker login')
    expect(dockerErrorFromResult('denied: requested access to the resource is denied', 1)?.code).toBe('DOCKER_AUTH')
  })
  it('toomanyrequests → DOCKER_RATE_LIMIT，可重试', () => {
    const d = dockerErrorFromResult('toomanyrequests: You have reached your pull rate limit', 1)
    expect(d?.code).toBe('DOCKER_RATE_LIMIT')
    expect(d?.retryable).toBe(true)
  })
  it('permission denied 仍归 DOCKER_PERM，不被鉴权分支抢走', () => {
    expect(
      dockerErrorFromResult('permission denied while trying to connect to the Docker daemon socket', 1)?.code,
    ).toBe('DOCKER_PERM')
  })
})

describe('镜像导出 / 加载命令（纯远端）', () => {
  it('save / load 路径与引用都过单引号转义', () => {
    expect(dockerSaveCmd('nginx:1.27', '/tmp/a.tar')).toBe(`docker save -o '/tmp/a.tar' 'nginx:1.27'`)
    expect(dockerLoadCmd('/tmp/a.tar')).toBe(`docker load -i '/tmp/a.tar'`)
  })
  it('gz 先落同目录临时文件再压：退出码能反映 docker save，失败也不碰旧备份', () => {
    expect(dockerSaveCmd('nginx:1.27', '/tmp/a.tar.gz', true)).toBe(
      `docker save -o '/tmp/.a.tar.gz.part' 'nginx:1.27' && gzip -1 -c '/tmp/.a.tar.gz.part' > '/tmp/a.tar.gz'; rc=$?; rm -f '/tmp/.a.tar.gz.part'; exit $rc`,
    )
  })
  it('不压缩时不给 save 加任何管道，退出码直取 docker', () => {
    expect(dockerSaveCmd('nginx', '/tmp/a.tar', false)).toBe(`docker save -o '/tmp/a.tar' 'nginx'`)
    expect(dockerSaveCmd('nginx', '/tmp/a.tar')).not.toContain('gzip')
  })
  it('路径里的单引号被脱出，不会把命令截断', () => {
    expect(dockerSaveCmd('nginx', "/tmp/it's.tar")).toBe(`docker save -o '/tmp/it'\\''s.tar' 'nginx'`)
  })
  it('带空格的路径整体包住', () => {
    expect(dockerLoadCmd('/var/lib/my images/a.tar')).toBe(`docker load -i '/var/lib/my images/a.tar'`)
  })
})

describe('saveTempPath', () => {
  it('与目标同目录的点前缀临时文件（/tmp 常是 tmpfs，多 GB 镜像放不下）', () => {
    expect(saveTempPath('/data/img/a.tar.gz')).toBe('/data/img/.a.tar.gz.part')
    expect(saveTempPath('/a.tar.gz')).toBe('/.a.tar.gz.part')
    expect(saveTempPath('a.tar.gz')).toBe('.a.tar.gz.part')
  })
})

describe('suggestTarName', () => {
  it('去掉 namespace，只留仓库末段 + 标签', () => {
    expect(suggestTarName('harbor.ckun.io/app', 'v3.8.2')).toBe('app_v3.8.2.tar')
    expect(suggestTarName('nginx', '1.27-alpine')).toBe('nginx_1.27-alpine.tar')
    expect(suggestTarName('a/b/c/deep', 'v1')).toBe('deep_v1.tar')
  })
  it('标签里的 + 换成 _（文件系统不欢迎）', () => {
    expect(suggestTarName('app', 'v1.0+build.7')).toBe('app_v1.0_build.7.tar')
  })
  it('悬空镜像（repo 或 tag 为 <none>）用短 ID', () => {
    expect(suggestTarName('<none>', '<none>', '9f8e7d6c5b4a')).toBe('9f8e7d6c5b4a.tar')
    expect(suggestTarName('nginx', '<none>', '9f8e7d6c5b4a')).toBe('9f8e7d6c5b4a.tar')
  })
  it('既无 repo 也无 id 时给一个可用的兼底名，不产出空文件名', () => {
    expect(suggestTarName('', '')).toBe('image.tar')
  })
  it('gz 只换后缀，其余部分不变', () => {
    expect(suggestTarName('harbor.ckun.io/app', 'v3.8.2', '', true)).toBe('app_v3.8.2.tar.gz')
    expect(suggestTarName('<none>', '<none>', '9f8e7d6c5b4a', true)).toBe('9f8e7d6c5b4a.tar.gz')
  })
})

describe('validateSaveName', () => {
  it('合法名放行', () => {
    expect(validateSaveName('app_v3.8.2.tar')).toBeNull()
    expect(validateSaveName('a b.tar')).toBeNull()
  })
  it('拒绝路径分隔符 / .. / 前导 - / 控制字符 / 过长', () => {
    expect(validateSaveName('a/b.tar')).toContain('/')
    expect(validateSaveName('a..b.tar')).toContain('..')
    expect(validateSaveName('-rf.tar')).toContain('-')
    expect(validateSaveName('a\u0000b.tar')).toContain('控制字符')
    expect(validateSaveName('a'.repeat(201))).toContain('过长')
    expect(validateSaveName('   ')).toContain('不能为空')
  })
  it('归档后缀不参与校验：格式由开关决定，名字带 .gz 不再是错', () => {
    expect(validateSaveName('app.tar.gz')).toBeNull()
    expect(validateSaveName('app.tgz')).toBeNull()
    expect(validateSaveName('app.gz')).toBeNull()
  })
  it('只剩后缀的名字等于空名', () => {
    expect(validateSaveName('.tar')).toContain('不能为空')
    expect(validateSaveName('..')).toContain('不能为空')
  })
})

describe('ensureTarSuffix', () => {
  it('缺 .tar 时补上，已带不重复', () => {
    expect(ensureTarSuffix('app_v1')).toBe('app_v1.tar')
    expect(ensureTarSuffix('app_v1.tar')).toBe('app_v1.tar')
    expect(ensureTarSuffix('APP.TAR')).toBe('APP.tar')
  })
  it('按选中的格式归一：.tgz / .gz 不会被叠成 .gz.tar 这种四不像', () => {
    expect(ensureTarSuffix('app_v1.tar', true)).toBe('app_v1.tar.gz')
    expect(ensureTarSuffix('app_v1.tar.gz', true)).toBe('app_v1.tar.gz')
    expect(ensureTarSuffix('app_v1.tar.gz', false)).toBe('app_v1.tar')
    expect(ensureTarSuffix('app_v1.tgz', true)).toBe('app_v1.tar.gz')
    expect(ensureTarSuffix('app_v1.gz')).toBe('app_v1.tar')
    expect(ensureTarSuffix('app_v1.gz.tar')).toBe('app_v1.gz.tar')
  })
  it('空串原样返回（不造出 .tar 这种隐形文件名）', () => {
    expect(ensureTarSuffix('')).toBe('')
    expect(ensureTarSuffix('  ')).toBe('')
  })
  it('尾点先削掉：backup. 不能拼成 backup..tar（校验器明令禁止 ..）', () => {
    expect(ensureTarSuffix('backup.')).toBe('backup.tar')
    expect(ensureTarSuffix('backup.', true)).toBe('backup.tar.gz')
    expect(validateSaveName(ensureTarSuffix('backup.'))).toBeNull()
  })
})

describe('validateRemoteDir / joinRemotePath', () => {
  it('目录必须是绝对路径', () => {
    expect(validateRemoteDir('/tmp')).toBeNull()
    expect(validateRemoteDir('tmp')).toContain('绝对路径')
    expect(validateRemoteDir('')).toContain('不能为空')
    expect(validateRemoteDir('/tmp\u0000x')).toContain('控制字符')
  })
  it('拼路径去重尾斜杠，根目录不多加一道', () => {
    expect(joinRemotePath('/tmp', 'a.tar')).toBe('/tmp/a.tar')
    expect(joinRemotePath('/tmp/', 'a.tar')).toBe('/tmp/a.tar')
    expect(joinRemotePath('/tmp///', 'a.tar')).toBe('/tmp/a.tar')
    expect(joinRemotePath('/', 'a.tar')).toBe('/a.tar')
  })
  it('文件名为空时只回收目录本身（不产出以 / 结尾的半截路径）', () => {
    expect(joinRemotePath('/tmp/', '')).toBe('/tmp')
    expect(joinRemotePath('/', ' ')).toBe('/')
  })
})
