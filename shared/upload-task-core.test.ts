/**
 * 上传记录共享契约的验收。
 *
 * 这里的每条判据都会被主进程（读盘校验、启动归一化、id 起点）与渲染层（恢复展示）
 * 各用一遍：两侧对 `running` 或 id 解析的理解一旦分叉，重启后会出现「显示可继续
 * 但没有续传点」或「新任务覆盖旧记录」。纯函数直接钉住，不依赖任何一端。
 */
import { describe, expect, it } from 'vitest'
import {
  basenameAny,
  isTerminalUploadStatus,
  isValidUploadTaskRecord,
  nextTransferSeqFrom,
  restoredStatusOf,
  type UploadTaskRecord,
  type UploadTaskStatus,
} from './upload-task-core'

const rec = (over: Partial<UploadTaskRecord> = {}): UploadTaskRecord => ({
  id: 't1',
  connectionId: 'c1',
  localPath: '/local/a.bin',
  remotePath: '/remote/a.bin',
  name: 'a.bin',
  total: 100,
  transferred: 0,
  status: 'pending',
  createdAt: 1,
  updatedAt: 1,
  ...over,
})

describe('isTerminalUploadStatus', () => {
  it('done/error/cancelled 是终态', () => {
    expect(isTerminalUploadStatus('done')).toBe(true)
    expect(isTerminalUploadStatus('error')).toBe(true)
    expect(isTerminalUploadStatus('cancelled')).toBe(true)
  })

  it('pending/running/paused 仍可被生命周期改写', () => {
    expect(isTerminalUploadStatus('pending')).toBe(false)
    expect(isTerminalUploadStatus('running')).toBe(false)
    expect(isTerminalUploadStatus('paused')).toBe(false)
  })
})

describe('restoredStatusOf —— 启动归一化全表', () => {
  const cases: Array<[UploadTaskStatus, UploadTaskStatus, boolean]> = [
    ['pending', 'paused', true],
    ['running', 'paused', true],
    // 上次是用户暂停还是进程被杀，对「点继续」没有区别
    ['paused', 'paused', true],
    ['done', 'done', false],
    ['error', 'error', false],
    ['cancelled', 'cancelled', false],
  ]
  for (const [from, status, interrupted] of cases) {
    it(`${from} → ${status}${interrupted ? ' + interrupted' : ''}`, () => {
      expect(restoredStatusOf({ status: from })).toEqual({ status, interrupted })
    })
  }

  it('磁盘上残留的 interrupted 不改变终态', () => {
    expect(restoredStatusOf({ status: 'done', interrupted: true }))
      .toEqual({ status: 'done', interrupted: false })
  })
})

describe('nextTransferSeqFrom', () => {
  it('同时解析 t/g 前缀取最大后 +1', () => {
    expect(nextTransferSeqFrom([{ id: 't3' }, { id: 'g41' }, { id: 't12' }])).toBe(42)
  })

  it('跳过畸形 id 与空表', () => {
    expect(nextTransferSeqFrom([])).toBe(1)
    expect(nextTransferSeqFrom([{ id: 'tX' }, { id: 'job-7' }, { id: '' }])).toBe(1)
  })

  it('负号/小数不算数字段', () => {
    expect(nextTransferSeqFrom([{ id: 't-1' }, { id: 't1.5' }, { id: 't9' }])).toBe(10)
  })

  it('不解析前缀以外的数字（t12x 不是合法 id）', () => {
    expect(nextTransferSeqFrom([{ id: 't12x' }, { id: 't2' }])).toBe(3)
  })
})

describe('isValidUploadTaskRecord', () => {
  it('完整记录通过', () => {
    expect(isValidUploadTaskRecord(rec())).toBe(true)
  })

  it('缺连接归属或路径的记录被丢弃（续传不能拿 undefined 去 stat）', () => {
    expect(isValidUploadTaskRecord({ ...rec(), connectionId: '' })).toBe(false)
    expect(isValidUploadTaskRecord({ ...rec(), localPath: '' })).toBe(false)
    expect(isValidUploadTaskRecord({ ...rec(), remotePath: undefined })).toBe(false)
  })

  it('未知状态与非法数字被拒', () => {
    expect(isValidUploadTaskRecord({ ...rec(), status: 'weird' })).toBe(false)
    expect(isValidUploadTaskRecord({ ...rec(), total: Number.NaN })).toBe(false)
    expect(isValidUploadTaskRecord({ ...rec(), createdAt: Infinity })).toBe(false)
    expect(isValidUploadTaskRecord({ ...rec(), transferred: '10' })).toBe(false)
  })

  it('可选字段只接受 string/boolean', () => {
    expect(isValidUploadTaskRecord({ ...rec(), error: 1 })).toBe(false)
    expect(isValidUploadTaskRecord({ ...rec(), interrupted: 'yes' })).toBe(false)
    expect(isValidUploadTaskRecord({ ...rec(), interrupted: true, groupLabel: 'docs/' })).toBe(true)
  })

  it('非对象一律拒绝', () => {
    for (const v of [null, undefined, 't1', 7, []]) {
      expect(isValidUploadTaskRecord(v)).toBe(false)
    }
  })
})

describe('basenameAny', () => {
  it('两种分隔符都取末段', () => {
    expect(basenameAny('D:\\a\\b\\c.bin')).toBe('c.bin')
    expect(basenameAny('/a/b/c.bin')).toBe('c.bin')
  })

  it('尾随分隔符不产生空名', () => {
    expect(basenameAny('/a/b/')).toBe('b')
  })

  it('空串原样返回', () => {
    expect(basenameAny('')).toBe('')
  })
})
