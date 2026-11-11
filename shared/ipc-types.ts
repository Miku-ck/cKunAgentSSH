/**
 * IPC 数据形状单一来源。
 *
 * 以下 9 组类型原先在 electron/ 主进程与 src/env.d.ts 各写一份（两个 tsconfig 程序
 * 互不包含，副本无法被类型系统校验）。现在统一由此文件定义：主进程保留原导出名做
 * re-export，渲染层在 env.d.ts 里用 inline import 派生，避免 ambient 全局声明失效。
 *
 * 本文件是纯类型，禁止 import node/electron —— shared/ 同时被 tsconfig.node 与
 * tsconfig.web 收编，web 侧没有 node 类型。
 */

/** SFTP 远程目录条目 */
export interface SftpEntry {
  name: string
  path: string
  isDirectory: boolean
  /**
   * 是否为符号链接（S5-20：mode 的类型位 0o120000）。
   *
   * 与 `isDirectory` 互斥但**不是**它的补集：远端条目还可能是 socket/fifo/设备
   * 节点。这里显式给出链接标记，是因为链接在下载/复制两条链路上的语义与「普通
   * 文件」完全不同 —— 跟随它会把链接目标的内容拉成文件副本（本地侧 fs:copy
   * 与 zip 早已用 lstat fail-closed 拒绝，远端侧此前无防护，见 sftpList）。
   * 权限列展示用的 `mode` 字符串已含 `l` 类型位，本字段是它的机器可读版本。
   */
  isSymlink: boolean
  size: number
  modified: number
  mode: string
}

/**
 * SSH 连接配置（跨 IPC 的 wire 形状）。
 * hasPassword / hasPassphrase 只由主进程 list 接口附带，用于编辑弹窗提示「留空则保持原值」；
 * 存储侧从不写这两个字段，故声明为可选，不改变任何运行时行为。
 */
export interface SSHConnectionRecord {
  id: string
  name: string
  host: string
  port: number
  username: string
  authType: 'password' | 'privateKey' | 'agent'
  password?: string
  privateKeyPath?: string
  passphrase?: string
  /** 主进程列表接口附带的布尔标志（不含凭据内容） */
  hasPassword?: boolean
  hasPassphrase?: boolean
  /** 备注(单行文字,最多 10 字). 旧数据可能没有此字段,但 tags 数组可作为兜底 */
  note?: string
  /** 目标环境标记('test' | 'prod'). 驱动生产环境高风险命令的审计与审批强化. 旧数据默认 'test' */
  environment?: 'test' | 'prod'
  /** S4-03：系统密钥环不可用时，本次提交的密码/passphrase 没有落盘 */
  credentialNotice?: 'encryption-unavailable'
  tags: string[]
  readyTimeout?: number
  keepaliveInterval?: number
  lastConnectedAt?: number
  createdAt: number
  updatedAt: number
}

export interface SSHTestResult {
  ok: boolean
  message: string
  latencyMs: number
}

/** SSH 重连状态变化事件（主进程 → 渲染层，channel: ssh:reconnect） */
export interface SshReconnectStatus {
  id: string
  /** reconnecting=首轮排程 | retry=失败后的重试排程 | success=重连成功 | gave-up=放弃 */
  phase: 'reconnecting' | 'retry' | 'success' | 'gave-up'
  /** 当前或最后一次尝试序号，1 起；gave-up + cancelled 时为 0（哨兵值） */
  attempt: number
  maxAttempts: number
  /** reconnecting/retry 阶段：下一次尝试前的退避等待（ms） */
  delayMs?: number
  /** gave-up 阶段：exhausted | config-missing | cancelled | not-retryable */
  reason?: 'exhausted' | 'config-missing' | 'cancelled' | 'not-retryable'
  /** gave-up 阶段带上最后一次失败的分类结果，UI 得以说清失败原因 */
  errorCode?: string
  errorMessage?: string
}

/** SSH 隧道规则（connectionId 弱引用连接配置，连接被删时启动报 config-missing） */
export interface TunnelRule {
  id: string
  /** 展示名，可空（UI 显示自动名「L 8080 → db:5432」式） */
  name: string
  /** 引用 SSHConnectionRecord.id */
  connectionId: string
  /** 数据面：缺省视为 'ssh'（存量规则零迁移） */
  via?: 'ssh' | 'gateway'
  /** 引用 GatewayRecord.id（仅 via='gateway'） */
  gatewayId?: string
  type: 'local' | 'remote' | 'dynamic'
  /** 默认 127.0.0.1，仅 local 可自定义；dynamic 强制 127.0.0.1 */
  listenHost?: string
  listenPort?: number
  /** 默认 127.0.0.1，仅 remote；0.0.0.0 需 UI 显式确认风险 */
  remoteBindHost?: string
  bindPort?: number
  dstHost?: string
  dstPort?: number
  autoStart: boolean
  createdAt: number
  updatedAt: number
}

/** 隧道运行状态（主进程 → 渲染层推送，channel: tunnel:status；不持久化） */
export interface TunnelStatus {
  id: string
  state: 'starting' | 'active' | 'error' | 'stopped'
  /** error 时给人看的失败原因 */
  message?: string
  connections?: number
  startedAt?: number
}

/** TLS 接入网关记录（口令不出主进程；list 返回前由 strip() 抹掉） */
export interface GatewayRecord {
  id: string
  name: string
  host: string
  port: number
  /** CA（PEM），缺省时信任判定完全交给 trustedSpki */
  caPem?: string
  /** 已确认的服务端 SPKI 指纹（sha256/…），缺省 = 首次连接走 TOFU */
  trustedSpki?: string
  user: string
  /** 仅保存时上行；下行副本里永远没有这个字段 */
  password?: string
  createdAt: number
  updatedAt: number
}

/** 网关会话状态（主进程 → 渲染层推送，channel: gateway:status；不持久化） */
export interface GatewayStatus {
  id: string
  name: string
  host: string
  port: number
  state: 'idle' | 'connecting' | 'online' | 'reconnecting' | 'error'
  message?: string
  /** 当前活动转发通道数 */
  channels?: number
  /** 网关协商的心跳间隔（秒），仅在会话就绪后有值 */
  heartbeatSec?: number
  role?: string
  sessionId?: string
  spki?: string
  sinceAt?: number
  /** 第几次重连（0 = 未断过） */
  attempt?: number
}

/** 首次见到网关证书（TOFU），UI 确认后调 gatewayTrustHostKey 落库 */
export interface GatewayHostKeyEvent {
  id: string
  name: string
  host: string
  port: number
  spki: string
}
