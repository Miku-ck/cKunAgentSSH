/**
 * docker-core 解析与错误归类测试
 * fixture 里的字段分隔用真实制表符（docker --format 的 printf 输出的正是 \t）
 */
import { describe, expect, it } from 'vitest'
import {
  parsePs, parseImages, parseVolumes, parseNetworks, parseLogs, parseLabels,
  parseVolumeInspect, parseNetworkInspect, shortId, labelHint,
  imageLabel, imageHaystack,
  stateFromStatus, sq, dockerErrorFromResult, describeThrownDockerError,
  volumeRmCmd, networkRmCmd, volumePruneCmd, networkPruneCmd,
  volumeInspectCmd, networkInspectCmd,
  VOLUMES_CMD, NETWORKS_CMD,
  type ImageRow,
} from './docker-core'

const T = '\t'

describe('stateFromStatus', () => {
  it('Up → running', () => {
    expect(stateFromStatus('Up 7 days')).toBe('running')
    expect(stateFromStatus('Up 13 seconds (health: starting)')).toBe('running')
  })
  it('Exited (0) / paused → stopped（可逆语义）', () => {
    expect(stateFromStatus('Exited (0) 2 hours ago')).toBe('stopped')
    expect(stateFromStatus('paused')).toBe('stopped')
  })
  it('异常退出 → exited', () => {
    expect(stateFromStatus('Exited (137) 5 minutes ago')).toBe('exited')
    expect(stateFromStatus('Created')).toBe('exited')
  })
  it('Restarting（崩溃重启循环）→ exited（异常、非运行绿）', () => {
    expect(stateFromStatus('Restarting (1) 5 seconds ago')).toBe('exited')
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
  it('volumes json 行无 Size/LinkCount（旧 Docker）时字段不注入', () => {
    const raw = JSON.stringify({ Driver: 'local', Name: 'old-vol', Labels: {} })
    const [v] = parseVolumes(raw)
    expect(v).toEqual({ driver: 'local', name: 'old-vol' })
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
  it('空 / <no value> / 坏 JSON → {}', () => {
    expect(parseLabels('')).toEqual({})
    expect(parseLabels('<no value>')).toEqual({})
    expect(parseLabels('map[]')).toEqual({})
    expect(parseLabels('{bad')).toEqual({})
  })
})
describe('inspect 解析', () => {
  it('volume inspect: 数组取首项，Usage 可选', () => {
    const raw = JSON.stringify([{
      Name: 'pgdata', Driver: 'local', Mountpoint: '/var/lib/docker/volumes/pgdata/_data',
      CreatedAt: '2026-01-01T00:00:00Z', Labels: { env: 'prod' }, Options: { type: 'none' },
      Usage: { Size: 1073741824, RefCount: 1 },
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
