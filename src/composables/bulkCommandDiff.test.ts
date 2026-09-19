/**
 * 多节点批量下发 — 结果对比测试（F 阶段）
 */
import { describe, expect, it } from 'vitest'
import {
  diffResults,
  resultsConsistent,
  detectDangerousCommand,
  type BulkResult,
} from './bulkCommandDiff'

function mk(label: string, lines: string[], exitCode = 0, error?: string): BulkResult {
  return { label, lines, exitCode, durationMs: 100, error }
}

describe('diffResults', () => {
  it('全部一致时没有差异行', () => {
    const d = diffResults([
      mk('a', ['x', 'y']),
      mk('b', ['x', 'y']),
    ])
    expect(d.every(x => !x.inconsistent)).toBe(true)
    expect(d).toHaveLength(2)
  })

  it('部分节点独有的行标记为差异行', () => {
    const d = diffResults([
      mk('a', ['common', 'onlyA']),
      mk('b', ['common', 'onlyB']),
    ])
    const byLine = Object.fromEntries(d.map(x => [x.line, x]))
    expect(byLine['common'].inconsistent).toBe(false)
    expect(byLine['onlyA'].inconsistent).toBe(true)
    expect(byLine['onlyB'].inconsistent).toBe(true)
    expect(byLine['onlyA'].count).toBe(1)
    expect(byLine['onlyA'].total).toBe(2)
  })

  it('错误节点不参与对比', () => {
    const d = diffResults([
      mk('a', ['x'], 0),
      mk('b', ['x'], 0, 'boom'),
    ])
    expect(d[0].total).toBe(1)
    expect(d[0].inconsistent).toBe(false)
  })

  it('全部失败时返回空', () => {
    expect(diffResults([mk('a', [], 0, 'err')])).toEqual([])
  })

  it('三节点中两节点一致一行 → 该行为差异行', () => {
    const d = diffResults([
      mk('a', ['same', 'a-only']),
      mk('b', ['same', 'a-only']),
      mk('c', ['same', 'c-only']),
    ])
    const aOnly = d.find(x => x.line === 'a-only')
    expect(aOnly?.inconsistent).toBe(true)
    expect(aOnly?.count).toBe(2)
    expect(aOnly?.total).toBe(3)
  })
})

describe('resultsConsistent', () => {
  it('单节点视为一致', () => {
    expect(resultsConsistent([mk('a', ['x'])])).toBe(true)
  })

  it('输出相同且 exit 相同 → 一致', () => {
    expect(resultsConsistent([mk('a', ['x']), mk('b', ['x'])])).toBe(true)
  })

  it('输出不同 → 不一致', () => {
    expect(resultsConsistent([mk('a', ['x']), mk('b', ['y'])])).toBe(false)
  })

  it('exit code 不同 → 不一致', () => {
    expect(resultsConsistent([mk('a', ['x'], 0), mk('b', ['x'], 1)])).toBe(false)
  })
})

describe('detectDangerousCommand', () => {
  it('只读命令不命中', () => {
    expect(detectDangerousCommand('df -h /')).toEqual([])
    expect(detectDangerousCommand('uptime && free -m')).toEqual([])
    expect(detectDangerousCommand('cat /etc/os-release | head -3')).toEqual([])
    expect(detectDangerousCommand('ps aux | head')).toEqual([])
    expect(detectDangerousCommand('systemctl status nginx')).toEqual([])
    expect(detectDangerousCommand('')).toEqual([])
  })

  it('破坏性命令命中', () => {
    expect(detectDangerousCommand('rm -rf /tmp/x')).toContain('rm')
    expect(detectDangerousCommand('reboot')).toContain('reboot')
    expect(detectDangerousCommand('dd if=/dev/zero of=/dev/sda bs=1M')).toContain('dd')
    expect(detectDangerousCommand('kill -9 1234')).toContain('kill')
    expect(detectDangerousCommand('mkfs.ext4 /dev/sdb1')).toContain('mkfs')
    expect(detectDangerousCommand('DROP TABLE users')).toContain('drop')
    expect(detectDangerousCommand('systemctl stop nginx')).toContain('systemctl 停/禁/重启')
    expect(detectDangerousCommand('echo hacked > /etc/crontab')).toContain('重定向覆盖(>)')
  })

  it('变更写操作命中（cp / tee / 包安装）', () => {
    expect(detectDangerousCommand('cp /etc/nginx.conf /tmp/backup')).toContain('cp')
    expect(detectDangerousCommand('echo data | tee /var/log/x')).toContain('tee(写文件)')
    expect(detectDangerousCommand('apt-get install nginx')).toContain('包安装')
    expect(detectDangerousCommand('yum install foo')).toContain('包安装')
    expect(detectDangerousCommand('apk add curl')).toContain('包安装')
  })

  it('cp / tee / 包安装 词边界防误伤（scp / telemetry / apt update 不命中）', () => {
    // scp 不命中 \bcp\b（前有字母 s）；telemetry 不命中 \btee\b；apt update 非 install
    expect(detectDangerousCommand('scp user@host a b')).not.toContain('cp')
    expect(detectDangerousCommand('cat telemetry.log')).not.toContain('tee(写文件)')
    expect(detectDangerousCommand('apt-get update')).not.toContain('包安装')
  })

  it('串联命令（; && |）内的危险操作也命中', () => {
    expect(detectDangerousCommand('uptime; rm -rf /data')).toContain('rm')
    expect(detectDangerousCommand('df -h && reboot')).toContain('reboot')
    expect(detectDangerousCommand('ps aux | pkill -f worker')).toContain('pkill')
  })

  it('词边界防误伤', () => {
    // 含 rm/mv 子串的普通词不应命中
    expect(detectDangerousCommand('echo firmware')).toEqual([])
    expect(detectDangerousCommand('cat /etc/firmware.conf')).toEqual([])
    expect(detectDangerousCommand('echo rmdirme')).toEqual([])
  })

  it('2>&1 不算重定向覆盖', () => {
    expect(detectDangerousCommand('df -h 2>&1')).toEqual([])
    expect(detectDangerousCommand('cat a 2>&1 | head')).toEqual([])
  })

  it('命中多项时全部返回（去重）', () => {
    const hits = detectDangerousCommand('rm -rf /data; reboot')
    expect(hits).toContain('rm')
    expect(hits).toContain('reboot')
    expect(hits).toHaveLength(2)
  })
})
