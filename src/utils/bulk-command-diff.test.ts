/**
 * 多节点批量下发 — 结果对比 + 执行前分支决策测试
 */
import { describe, expect, it } from 'vitest'
import {
  diffResults,
  resultsConsistent,
  decideBulkDispatch,
  type BulkResult,
  type BulkSafetyVerdict,
} from './bulk-command-diff'

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

describe('decideBulkDispatch', () => {
  const v = (level: BulkSafetyVerdict['level'], reason = 'r', explain = ''): BulkSafetyVerdict =>
    ({ level, reason, explain })

  it('判定不可达时拒绝执行（fail-closed）', () => {
    const g = decideBulkDispatch('df -h', null, [])
    expect(g.action).toBe('block')
  })

  it('hardline 直接中止，且不存在任何放行分支', () => {
    const g = decideBulkDispatch('mkfs.ext4 /dev/sdb1', v('hardline', 'format filesystem (mkfs)', '会清空磁盘'), [])
    expect(g.action).toBe('block')
    if (g.action === 'block') {
      expect(g.title).toContain('阻止')
      expect(g.lines.join('\n')).toContain('format filesystem (mkfs)')
    }
    // 只有 run/confirm 才能走到 sshExec —— block 必须拿不到它们
    expect(g.action).not.toBe('run')
    expect(g.action).not.toBe('confirm')
  })

  it('dangerous 需要确认，警告文案用后端中文后果', () => {
    const g = decideBulkDispatch('systemctl stop nginx', v('dangerous', 'systemctl stop', '会停止 nginx 服务'), [])
    expect(g.action).toBe('confirm')
    if (g.action === 'confirm') expect(g.warnings.join('\n')).toContain('会停止 nginx 服务')
  })

  it('后端没给中文后果时回退到 reason，不留空', () => {
    const g = decideBulkDispatch('yum install foo', v('dangerous', 'unknown command, default approval (yum)'), [])
    if (g.action !== 'confirm') throw new Error('expected confirm')
    expect(g.warnings.join('\n')).toContain('unknown command, default approval (yum)')
  })

  it('prod 目标即使命令 safe 也要确认', () => {
    const g = decideBulkDispatch('df -h', v('safe'), ['prod-web-01'])
    expect(g.action).toBe('confirm')
    if (g.action === 'confirm') expect(g.warnings[0]).toContain('prod-web-01')
  })

  it('非 prod 的只读命令直接放行', () => {
    expect(decideBulkDispatch('uptime', v('safe'), []).action).toBe('run')
  })
})
