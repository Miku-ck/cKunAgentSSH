/**
 * 恢复映射的验收。
 *
 * 传输中心的行是「同一个对象被原地改状态」的模型（transferById 索引），恢复时
 * 必须产出字段齐全的 TransferItem，而不是只填成功能就行的半条。这里钉住四件事：
 * 断点进度原样带出、终态带 endTime、中断标记只落在可继续的行上、入队顺序稳定。
 */
import { describe, expect, it } from 'vitest'
import type { UploadTaskRecord } from '../../shared/upload-task-core'
import { sortForRestore, toTransferItem } from './upload-task-restore'

const rec = (over: Partial<UploadTaskRecord> = {}): UploadTaskRecord => ({
  id: 't7',
  connectionId: 'c1',
  localPath: 'D:\\work\\big.bin',
  remotePath: '/srv/big.bin',
  name: 'big.bin',
  total: 1000,
  transferred: 400,
  status: 'paused',
  createdAt: 100,
  updatedAt: 200,
  ...over,
})

describe('toTransferItem', () => {
  it('字段逐一映射，断点进度不带偏移', () => {
    const item = toTransferItem(rec({ groupId: 'g2', groupLabel: 'docs/' }))
    expect(item).toMatchObject({
      id: 't7',
      direction: 'up',
      kind: 'upload',
      name: 'big.bin',
      from: 'D:\\work\\big.bin',
      to: '/srv/big.bin',
      transferred: 400,
      total: 1000,
      startTime: 100,
      size: 1000,
      groupId: 'g2',
      groupLabel: 'docs/',
    })
  })

  it('上次未完成 / 暂停的行恢复为 paused + interrupted', () => {
    for (const status of ['pending', 'running', 'paused'] as const) {
      const item = toTransferItem(rec({ status }))
      expect(item.status).toBe('paused')
      expect(item.interrupted).toBe(true)
    }
  })

  it('终态保留状态、带 endTime、不标中断', () => {
    for (const status of ['done', 'error', 'cancelled'] as const) {
      const item = toTransferItem(rec({ status, error: status === 'error' ? '网络中断' : undefined }))
      expect(item.status).toBe(status)
      expect(item.interrupted).toBeUndefined()
      expect(item.endTime).toBe(200)
    }
    expect(toTransferItem(rec({ status: 'error', error: '网络中断' })).error).toBe('网络中断')
  })
})

describe('sortForRestore', () => {
  it('按 createdAt 升序，且不改动入参', () => {
    const input = [
      rec({ id: 't3', createdAt: 300 }),
      rec({ id: 't1', createdAt: 100 }),
      rec({ id: 't2', createdAt: 200 }),
    ]
    expect(sortForRestore(input).map(r => r.id)).toEqual(['t1', 't2', 't3'])
    expect(input.map(r => r.id)).toEqual(['t3', 't1', 't2'])
  })

  it('同毫秒保持原序（一组目录的连续入队顺序）', () => {
    const input = [
      rec({ id: 't1', createdAt: 100, groupId: 'g1' }),
      rec({ id: 't2', createdAt: 100, groupId: 'g1' }),
      rec({ id: 't3', createdAt: 100, groupId: 'g1' }),
    ]
    expect(sortForRestore(input).map(r => r.id)).toEqual(['t1', 't2', 't3'])
  })
})
