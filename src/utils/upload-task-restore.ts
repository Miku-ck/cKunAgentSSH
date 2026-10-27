/**
 * 上传记录 → 传输中心行的映射。记录归主进程所有，渲染端只做展示，恢复时不能
 * 复用「新建任务」的路径：那会重新分配 id、重置 startTime，并把一条上次没传完
 * 的任务显示成刚刚入队的 pending。
 */
import type { TransferItem } from '@/stores/sftp'
import {
  isTerminalUploadStatus,
  restoredStatusOf,
  type UploadTaskRecord,
} from '../../shared/upload-task-core'

/** 字段一一对上是刻意的最低映射：transferred/total 直接来自记录，续传入口据此
 *  显示断点进度；终态用 updatedAt 当 endTime，重启后「用时」不会从零重算。 */
export function toTransferItem(record: UploadTaskRecord): TransferItem {
  const restored = restoredStatusOf(record)
  const item: TransferItem = {
    id: record.id,
    direction: 'up',
    kind: 'upload',
    name: record.name,
    from: record.localPath,
    to: record.remotePath,
    transferred: record.transferred,
    total: record.total,
    status: restored.status,
    startTime: record.createdAt,
    groupId: record.groupId,
    groupLabel: record.groupLabel,
    size: record.total,
  }
  if (record.error) item.error = record.error
  if (isTerminalUploadStatus(restored.status)) item.endTime = record.updatedAt
  if (restored.interrupted) item.interrupted = true
  return item
}

/** 按入队顺序恢复：同一目录展开的 N 条共享 groupId，乱序插入会让组内子行
 *  的重建顺序与上次运行不一致。sort 在现代 V8 上是稳定的，同毫秒内保持原序。 */
export function sortForRestore(records: readonly UploadTaskRecord[]): UploadTaskRecord[] {
  return [...records].sort((a, b) => a.createdAt - b.createdAt)
}
