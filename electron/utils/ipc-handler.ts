/**
 * ipc-handler.ts — main 进程 IPC 分发器的 handler 形状（单一来源）。
 *
 * handleTrusted(channel, fn) 的 fn 是通用分发入口：event 固定，args/返回开放。
 * 每个通道在 main.ts 注册处各自收窄入参（如 ssh:connect 用 ConnectPayload），
 * 这里是唯一允许 any 的边界 —— 抽出来避免 fs-handler 等调用方手抄同一形状漂移。
 */
import type { IpcMainInvokeEvent } from 'electron'

export type IpcInvokeHandler = (event: IpcMainInvokeEvent, ...args: any[]) => any
