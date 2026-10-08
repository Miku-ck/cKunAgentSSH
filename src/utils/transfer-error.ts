/**
 * transfer-error.ts — 渲染端读取跨 IPC 传输错误的唯一入口（S0-15）
 *
 * 主进程 toTransferError 有两条腿：Error.name 前缀 `TransferError:<CODE>`
 * （本仓实证保真性最强的通道）和自定义 code/retryable 属性（可能被 IPC 结构化
 * 克隆丢弃）。原实现只读自定义属性 ⇒ 属性一丢，`runUpload`/`runDownload` 的
 * 「NETWORK/STALLED 自动续传一次」与停滞专属文案全成死分支，续传静默退化成
 * 「上传失败」。这里两条腿都读，并把 retryable 改为按 code 现推（单一事实源，
 * 与主进程 .part 取舍判据同源）。
 *
 * 顺带（S0-24）：message 先剥 Electron 的 invoke 信封再落进 t.error / toast ——
 * 否则用户看到 `Error invoking remote method 'sftp:upload': Error: …`，
 * 连内部通道名一起进了用户可见区。
 */
import {
  isTransferRetryable,
  parseTransferErrorCode,
  type TransferErrorCode,
} from '../../shared/transfer-error-core'
import { unwrapIpcErrorEnvelope } from '@/api/client'

export interface TransferErrorInfo {
  code?: TransferErrorCode
  retryable: boolean
  message: string
}

export function describeTransferError(err: unknown): TransferErrorInfo {
  const e = err as { code?: unknown; name?: unknown; message?: unknown } | null
  const code = parseTransferErrorCode(e?.code) ?? parseTransferErrorCode(e?.name)
  const raw = typeof e?.message === 'string' ? e.message : ''
  return {
    code: code ?? undefined,
    retryable: code ? isTransferRetryable(code) : false,
    message: unwrapIpcErrorEnvelope(raw),
  }
}
