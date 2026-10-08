/**
 * S5-13 回归：服务器书写的目录项名能不能拼进本地写路径。
 *
 * 下载侧把 `e.name` 当 relPath 一路拼到 `fs:pathJoin`，而它会归一化掉 `..`，
 * `assertSafePath` 又只是敏感目录黑名单 —— 越界落点不会撞到黑名单。
 * 这些形态必须在进 join 之前就被挡掉。
 */
import { describe, expect, it } from 'vitest'

import { isUnsafeRemoteName } from './remote-name'

describe('isUnsafeRemoteName', () => {
  it('常规文件名放行', () => {
    for (const name of ['app.log', 'nginx.conf', '数据-2026.csv', 'a b.txt', '.bashrc', 'v1.2+3']) {
      expect(isUnsafeRemoteName(name)).toBe(false)
    }
  })

  it('路径穿越与分隔符形态拒绝', () => {
    // 注意 `.ssh` 这类「名字本身像敏感目录」的条目不在此列：它落在用户选的目录里，
    // 敏感位置由落点上的 assertSafePath 负责拦，名字判据只管能不能安全拼接
    for (const name of ['', '..', '../evil', 'a/b', 'C:\\Windows\\system32', 'a\\b', '/etc/passwd']) {
      expect(isUnsafeRemoteName(name)).toBe(true)
    }
  })

  it('NUL 截断拒绝', () => {
    expect(isUnsafeRemoteName('app.log\u0000.txt')).toBe(true)
  })
})
