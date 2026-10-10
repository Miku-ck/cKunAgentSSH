# SSH 隧道功能设计（数据结构 + IPC + UI 位置）

日期：2026-10-08  
状态：待评审  
范围：本地转发（-L）/ 远程转发（-R）/ 动态转发（-D, SOCKS5）。跳板机（ProxyJump）不在本版。

## 0. 现状与决策

- `electron/ssh-manager.ts` 基于 ssh2，现有 connect/shell/exec/SFTP，无任何 `forwardOut`/`forwardIn` 调用；`SSHConnectionConfig`（ssh-manager.ts:287）无转发字段。
- **决策 1：隧道不复用终端会话的 client。** 每条隧道在主进程持有**独立 ssh2 Client**。理由：shell 会话有自动重连（ReconnectEntry）、代数取消等复杂生命周期，隧道挂上去会互相干扰；终端 tab 关闭也不应杀隧道。凭据通过 `SSHManager.setConfigProvider` 同款机制由主进程自 SSHStore 取，渲染层不碰明文。
- **决策 2：隧道规则独立持久化**（新 `electron/tunnel-store.ts`，复用 ssh-store.ts 的 encrypt/tmp-write/写队列模式），不改 `SSHConnectionRecord`——避免存量连接记录迁移。规则以 `connectionId` 弱引用连接配置，连接被删时隧道启动报 `config-missing`。
- **决策 3：动态转发（-D）V1 钳死 `listenHost=127.0.0.1`**。SOCKS 意味着本机任意进程可经此访问远端网段，不对局域网开放。远程转发（-R）`remoteBindHost` 默认 127.0.0.1，改 0.0.0.0 时 UI 显式告警。

## 1. 数据结构

主进程定义于 `electron/tunnel-store.ts`（Record）与 `electron/tunnel-manager.ts`（Status），渲染层副本按惯例复制到 `src/env.d.ts`（跨 tsconfig 不 import）。

```ts
type TunnelType = 'local' | 'remote' | 'dynamic'

/** 持久化规则（tunnel-store.ts，明文字段仅此结构；无凭据） */
interface TunnelRule {
  id: string
  name: string                  // 空则 UI 显示自动名「L 8080 → db:5432」式
  connectionId: string          // 引用 SSHConnectionRecord.id
  type: TunnelType
  // local:   本机 listenPort → ( SSH) → dstHost:dstPort
  // remote:  远端 bindPort   → ( SSH回) → 本机 dstHost:dstPort
  // dynamic: 本机 listenPort 起 SOCKS5，目标由客户端 CONNECT 指定
  listenHost?: string           // 默认 127.0.0.1，仅 local 可自定义；dynamic 强制 127.0.0.1
  listenPort: number            // 1–65535
  remoteBindHost?: string       // 默认 127.0.0.1，仅 remote；0.0.0.0 需 UI 二次确认
  bindPort?: number             // remote 必填
  dstHost?: string              // local/remote 必填
  dstPort?: number              // local/remote 必填
  autoStart: boolean            // 应用启动后隧道自动拉起（V1 默认 false，字段先留）
  createdAt: number
  updatedAt: number
}

/** 运行状态（主进程 → 渲染层推送，不持久化） */
interface TunnelStatus {
  id: string                    // = rule.id
  state: 'starting' | 'active' | 'error' | 'stopped'
  message?: string              // error 时给人看的失败原因（EADDRINUSE、auth 失败、config-missing…）
  connections?: number          // 当前活动转发通道数
  startedAt?: number
}
```

校验规则（save 与 start 双重校验，save 层只查结构、start 层查运行时）：

- `listenPort`/`bindPort`/`dstPort` ∈ [1,65535]；同 rule 内类型决定必填字段（见上注释）。
- 同一 `listenHost:listenPort` 不允许两条 enabled 规则；`bindPort` 同理（同 connectionId 内查重即可，跨连接服务端才可能冲突，启动时由服务端报错兜底）。
- `connectionId` 必须存在（SSHStore.get 可查到）。

## 2. 主进程实现（新文件 ×2）

### electron/tunnel-manager.ts

```ts
interface TunnelSession {
  rule: TunnelRule
  client: Client                 // 独立 ssh2 连接
  server?: net.Server            // local/dynamic 用的本地 TCP listener；remote 为空
  socks?: SocksServer            // dynamic 专用句柄
  state: TunnelStatus['state']
  conns: Set<net.Socket>         // 活动通道，stop 时逐个 destroy
}
```

三条转发路径：

- **local**：`net.createServer` 监听 `listenHost:listenPort`；每来一个 socket → `client.forwardOut(srcAddr, srcPort, dstHost, dstPort)` 取回 duplex stream → 双向 pipe；错误即 destroy socket。
- **remote**：`client.forwardIn(remoteBindHost, bindPort)`；监听 client 的 `'tcp connection'` 事件 → `net.connect(dstHost, dstPort)` → pipe；stop 时 `client.unforwardIn()` 再 `client.end()`。
- **dynamic**：主进程内置**极简 SOCKS5 server**（无认证、仅 CONNECT 命令，≈100 行，不引第三方依赖），握手完成后按目标地址 `client.forwardOut(...)` pipe。V1 不支持 UDP/Bind。

生命周期：

- `start(rule)`：`starting` → 取配置（`configProvider`）→ ssh2 connect → 建监听 → `active`。任一步失败 → `error` + message，`client.end()`。
- `stop(id)`：关 server/销毁 conns/`unforwardIn`/`client.end()` → `stopped`。幂等。
- client `'error'`/`'close'` → 通道死亡 → `error`（message 带原因）。**V1 不做隧道自动重连**（终端重连逻辑不覆盖隧道），UI 提供「重启」按钮手动拉起——明确记录为已知限制。
- 应用退出（main.ts will-quit）遍历 stop，防半开连接。
- `TunnelManager` 挂到现有 `safeSend` 机制，状态每次变更推 `tunnel:status`。

### electron/tunnel-store.ts

照抄 ssh-store.ts 骨架：`encrypt`/`writeQueue`/tmp+rename 原子写/损坏文件隔离（`.corrupt-*.bak`）。存储路径同目录 `tunnels.json`。无明文凭据字段，无需 reencrypt。

## 3. IPC

### main.ts 注册（全部走现有 `handleTrusted`）

| channel            | 签名                                | 说明                                        |
| ------------------ | --------------------------------- | ----------------------------------------- |
| `tunnel:list`      | `() → TunnelRule[]`               | 全量规则                                      |
| `tunnel:save`      | `(rule: TunnelRule) → TunnelRule` | 新增/更新，落库后回传（含 id/时间戳）                     |
| `tunnel:delete`    | `(id) → void`                     | 若运行中先内部 stop                              |
| `tunnel:start`     | `(id) → TunnelStatus`             | 拉起；失败也以 status 返回而非 throw，UI 直接渲染 message |
| `tunnel:stop`      | `(id) → TunnelStatus`             | 停止                                        |
| `tunnel:statusAll` | `() → TunnelStatus[]`             | 渲染层启动时恢复视图                                |
| `tunnel:checkPort` | `(host, port) → { ok, message? }` | 保存前预检本机监听端口被占即时报（评审补充，实现已含）              |

### 推送（复用 `safeSend`，窗口重建不丢事件的机制已有）

- `tunnel:status`：`TunnelStatus`，每次状态迁移推一次。

### preload.ts / env.d.ts

`window.electronAPI` 增 `tunnelList/tunnelSave/tunnelDelete/tunnelStart/tunnelStop/tunnelStatusAll` + `onTunnelStatus(handler)`，与 `ssh:reconnect` 的订阅模式一致（返回取消订阅函数）。

## 4. UI 位置

导航入口走现有「TitleBar 图标按钮 + useSshNavigation 页」模式，不加新路由框架：

1. `src/composables/useSshNavigation.ts`：`activeNavPage` 联合类型加 `'tunnels'`，新增 `openTunnels()`（照抄 openAudit 的 toggle 逻辑）。
2. `src/layouts/TitleBar.vue`：新增按钮（图标：双向箭头 `M7 17 17 7 / M7 7h10v10` 类），activePage 联合类型加 `'tunnels'`，emit `action-tunnels`；`src/App.vue` 的 `activePage` computed 联合类型同步加。
3. `src/views/ssh/SshView.vue`：加 `<div v-show="activeNavPage === 'tunnels'">` + `pageMounted('tunnels')` 惰性挂载，与 audit/schedules 同款。
4. 新组件（`src/components/ssh/`）：
   - **TunnelView.vue**：规则列表 + 状态列。行结构：`[状态点] L 8080 → db.internal:5432 · 生产机`，类型徽标 L/R/D，操作列 启动/停止/编辑/删除。错误状态就地展示 message（toast 兜底走现有 toast store）。
   - **TunnelModal.vue**：新建/编辑弹窗。连接下拉复用 `sshStore.connections`（无凭据副本，只有连接名）；类型单切后按类型显隐字段；dynamic 时 listenHost 输入禁用并标注 127.0.0.1；remote 填 0.0.0.0 弹确认。
5. **不改** ConnectionModal/ConnectionRow：V1 不做「连接级隧道配置」，规则集中在一个页面管理，避免拉出迁移。

UI 终稿以 `designs/tunnel-ui-mockup-v3.html` 为准（评审 v1 → v2 → v3）：
组件层零硬编码色值、全量消费 variables.css 令牌（8 套主题逐套验证）；徽标底色用
`color-mix` 派生。**待补令牌**：light 主题下 `--accent-cyan/--accent-purple` 在白底
不可读（1.6:1），需在 variables.css 增补 `--tint-l/--tint-r/--tint-d` 三条语义令牌
（深色主题默认回落到 accent 值，有真实消费方，符合该文件维护约定）。

**回填（2026-10-10）**：空状态只留单行文案（「还没有隧道规则」/「没有符合当前筛选的
隧道」），「新建隧道」入口只在页头部画一处——DESIGN.md 反模式条「空状态里重复头部
已有的主操作按钮」在本页补齐。全仓其余页面（远程管理/安全接入/审计/定时任务/
Docker/SFTP/dsh-ssh 隧道页签）同日排查后本已合规，无需改动。

状态订阅：新建 `src/stores/tunnel.ts`（Pinia），持有 rules/statuses 两个 map，`onTunnelStatus` 合并状态；app 启动时 `tunnel:list` + `tunnel:statusAll` 各拉一次。

## 5. 安全边界与既有文档的关系

- 与 `docs/p3-remote-least-privilege.md` 不冲突：该文档加固的是 agent 桥接通道（authorized_keys `restrict` 禁转发），走的是桥接专用 key；用户隧道走用户自己的凭据。但若目标主机账号用的就是 restrict key，`forwardOut/forwardIn` 会被服务端拒绝——错误信息透传 `message`，属预期行为。
- dynamic 强制 127.0.0.1（决策 3）；remote 默认 127.0.0.1，放宽需确认。隧道自身不写审计，V1 接受（运维审计页只覆盖命令面）。

## 6. 分期与测试

- **M1**：tunnel-store + TunnelManager(local) + IPC + TunnelView 只读列表与启停 → 先把骨架和状态推送打通。
- **M2**：remote（forwardIn）+ dynamic（SOCKS5）+ TunnelModal 编辑。
- **M3**：autoStart、字节数统计（status 加 counters）、错误文案打磨。
- 测试：tunnel-store round-trip（照 ssh-store 测试模式）；SOCKS5 握手拆纯函数单测；save 校验规则表驱动单测。

## 7. 改动文件清单

新增：`electron/tunnel-manager.ts`、`electron/tunnel-store.ts`、`src/stores/tunnel.ts`、`src/components/ssh/TunnelView.vue`、`src/components/ssh/TunnelModal.vue`  
修改：`electron/main.ts`（IPC 注册 + will-quit 清理）、`electron/preload.ts`、`src/env.d.ts`、`src/composables/useSshNavigation.ts`、`src/views/ssh/SshView.vue`、`src/layouts/TitleBar.vue`、`src/App.vue`
