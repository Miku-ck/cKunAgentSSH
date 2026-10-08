# 自研 TLS 数据面设计（帧协议 + 网关 + mTLS 信任模型）

日期：2026-10-08  
状态：待评审  
范围：把隧道数据面从「借 SSH 通道」扩展出「连自家 TLS 网关」这一条路径，交付 Hillstone SSL VPN 的
**端口代理 / 应用代理**那一档（搬 TCP 字节流、纯用户态、零驱动）。
明确不在本版：L3 虚拟网卡与路由下发、UDP、DTLS/QUIC、国密 SM2/SM4、按进程归属授权。

## 0. 现状与决策

- 现有 `electron/tunnel-manager.ts` 三条路径全部硬绑 ssh2 API：`forwardOut`（:212 :327）、
  `forwardIn`（:255）、`'tcp connection'` 事件（:238）、`unforwardIn`（:375）。本机 listener、
  SOCKS5 握手解析（:272-350）、`conns` 生命周期、状态机与 IPC **都与 ssh2 无关，可原样复用**。
- **决策 1：不替换 SSH 数据面，抽象成 `TunnelTransport` 接口，两条实现并列。**
  理由：这不是「新旧模式切换」，而是两个语义不同的目标端——SSH transport 连的是用户已有的任意
  SSH 服务器（零部署），gateway transport 连的是自己运维的 TLS 网关（需部署网关）。删掉任一条
  都少一个功能。接口收口后 `TunnelManager` 只依赖 4 个方法（§3.1），listener / SOCKS 代码零改动。
  落地物：`electron/transports/ssh-transport.ts`（把现有 forwardOut/forwardIn 包进接口，行为不变）
  + `electron/transports/gateway-transport.ts`（新）。
- **决策 2：帧协议自研，8 字节定长头，单条 TLS 连接多路复用。**
  备选是嵌 HTTP/2 或 WSS-over-TLS（省掉半关闭/RST 语义的工作量），否决理由：本项目已有同构实践
  （`handleSocksConn` 的手写定长头状态机），且复用层是产品的核心资产，不该外包给协议栈。
- **决策 3：V1 只做 TLS over TCP，不做 DTLS。** 理由见 §2.6 心跳一节——TCP 单链路 + PING/PONG
  已能覆盖运维场景；UDP 会引入分片、MTU、NAT 映射老化三类新问题。
- **决策 4：信任模型 = 自建 CA + mTLS，TOFU 指纹骨架复用 ssh-manager 现成代码。**
  不在 TLS 里塞口令；长期凭据是客户端证书，私钥落 `safeStorage`。
- **决策 5：网关是独立单文件 Node 进程，不进 Electron 打包链。** 理由：客户端已有签名/更新链路，
  把网关塞进去会拖累它；网关是运维侧部署物，`node gateway/hillgw.mjs` + systemd 即可。
- **决策 6：会话层自动重连，通道层不复活。** 这是对 SSH 隧道「V1 不自动重连」（上一版 spec :82
  记录的已知限制）的**有意变更**：长驻接入没有自动重连不可用。重连后在途通道一律 RST，不做通道
  状态迁移——chan id 空间随会话重置，映射过来必然错配。

## 1. 帧协议（gateway-mux）

`ver=1`。头 8 字节，字段全大端：

```
 byte:   0      1      2 3 4 5    6 7      8 ...
        +------+------+----------+------+----------------+
        | ver  | type | chan     | len  | payload        |
        | u8   | u8   | u32 BE   | u16  | ≤ 65535 B      |
        +------+------+----------+------+----------------+
```

- **方向不占字节**：`chan` 由发起方分配，**客户端用奇数、网关用偶数**（与 SSH channel id 同法）。
  于是 OPEN 天然双向，`-R` 不需要第二套帧。评审前口头提过 `dir` 字节，此处改为奇偶制：省 1 字节，
  且非法方向（收到自己奇偶性的 OPEN）当场可判为协议错误。
- `len` ≤ 65535，即 DATA 单帧上限 64 KiB；`len` 超限或 `ver≠1` → 立即销毁整条 TLS 会话（帧已失同步，
  继续解只会把别人的数据当帧）。
- **控制帧 payload 用 JSON，DATA 用裸字节。** 权衡：控制帧小而稀少，可观测性/可调试性优先；
  DATA 每帧省下的头开销比 JSON 编码更重要。

| type | 名称 | chan | payload |
| --- | --- | --- | --- |
| 0x01 | HELLO | 0 | `{clientVersion, deviceName, selfReportedUser}` |
| 0x02 | AUTH | 0 | `{token}`（仅首次注册用，换证书后即不再发） |
| 0x03 | HELLO_OK | 0 | `{sessionId, heartbeatSec, maxChannels, role, policyVersion}` |
| 0x04 | OPEN | 新 chan | `{host, port, proto:"tcp"}` |
| 0x05 | OPEN_OK | 既有 | — |
| 0x06 | OPEN_ERR | 即将废弃 | `{code, message}` |
| 0x07 | DATA | 既有 | 裸字节 |
| 0x08 | FIN | 既有 | — 单向 EOF，对端仍可继续写 |
| 0x09 | RST | 既有 | — 双向立即废弃，丢弃在途缓冲 |
| 0x0A | PING / 0x0B PONG | 0 | `{seq}` |
| 0x0C | GOAWAY | 0 | `{reason, lastChan}` |

`OPEN_ERR.code` 枚举：`denied-by-policy` / `denied-by-dns` / `target-unreachable` / `quota`
/ `malformed-request` / `draining`。UI 直接展示 `message`，文案在网关侧生成（运维改文案不用发客户端版）。

### 1.1 三条必须写进实现的坑

1. **迟到帧**：`FIN`/`RST` 之后对端仍可能有在途 `DATA`。chan 已不存在时**丢帧并继续**，不得当协议
   错误杀会话。反过来，只有「奇偶性非法」「len 超限」「ver 不符」才允许杀会话。
2. **半关闭不能塌成双向关闭**：现有 `sock.pipe(stream)` + `stream.on('close') → sock.destroy()`
   （tunnel-manager.ts:223-225）在 SOCKS 上传大文件这类「单向写完等响应」的场景会提前断开。帧层必须
   把 `FIN`（单向）和 `RST`（双向）分开建模，`PeerChannel.end()` 只发 FIN，`destroy()` 才发 RST。
3. **背压没人兜了**：pipe 会自动暂停源读，帧复用后必须手写——`peer.write(chunk)` 返回 `false` 时
   `srcSocket.pause()`，`'drain'` 恢复。漏掉这一条的表现是慢客户端下内存无界增长，而不是报错。
   每通道 `highWaterMark = 256 KiB`。

### 1.2 心跳与保活

`heartbeatSec`（默认 30，网关下发）周期 PING；连续 3 次无 PONG → 判死 → 走 §3.3 重连退避。
PING 兼做 NAT/防火墙空闲映射保活（这是 Hillstone 那类产品在 4433 上做长连心跳的同一动机）。

## 2. 信任模型与认证

### 2.1 服务端鉴权（TOFU + pinning）

- 网关持 CA 签发的服务端证书。客户端 `tls.connect({ ca: [caPem], servername, checkServerIdentity })`。
- 首次连接：算服务端证书 **SPKI sha256**，本地无记录 → 推 `vpn:host-key-new` 事件，UI 确认后落盘；
  已有记录且指纹不符 → 硬失败（不降级为警告）。
- 存储结构同构照抄 `ssh-manager.ts:392-493` 的 known_hosts：`"host:port" → "sha256/…"`，
  独立文件 `gateway-known-hosts.json`，复用其 single-flight 加载（:431-446）与串行化写（:455-467）。
  这两处是上一轮修过的并发 bug，不要在新代码里重新引入。

### 2.2 客户端身份（mTLS）

- 管理员在网关侧生成**一次性注册令牌**（绑定将签发的角色，TTL 24h，用后即废）。
- 客户端首连走 `AUTH{token}` → 网关签发长期客户端证书+私钥 → 私钥经 `safeStorage` 落盘。
  此后纯 mTLS 建连，不再出现令牌。
- **策略只认证书 CN 映射出的 user，`HELLO.selfReportedUser` 仅用于展示与审计**——自报身份不可信。
- 吊销 V1 用「CA 轮换 + 客户端证书短有效期（默认 90 天）+ 到期重注册」，不维护 CRL/OCSP。

### 2.3 授权策略（这是 ZTNA 的正文，不是加密）

网关配置里的 `roles.<r>.allow[]` 条目形如 `{host, ports:[5432, "8000-8100"], note}`。
V1 只做**字面 host + 端口/端口段**，不做 CIDR——理由：CIDR 会牵出「一个域名解析到多个 IP、
其中部分落在网段外」的判定歧义，得先定义 DNS 语义再谈网段。

- **DNS 校验必须做**（多数自研实现漏掉的一条）：host 是域名时，网关 `dns.lookup(all:true)`
  后逐个 IP 校验是否落在允许集，任一不合法即 `OPEN_ERR denied-by-dns`。否则策略里的
  `internal-db.example.com` 可被一次 DNS 变更指向 `169.254.169.254`。
- **默认拒绝清单**（网关本机不可作目标，硬编码）：`169.254.0.0/16`、`127.0.0.0/8`、
  网关自身 ssh 端口、`::1`、`fe80::/10`。防止网关被当开放中继。
- 每会话 `maxChannels`（默认 256）超限 → `quota`。

### 2.4 明确不做

- **国密**：Node 内置 `tls` 不支持 SM2/SM3/SM4。要做只有两条路——链 Tongsuo/BabaSSL，或把网关
  改写成 Go + `emmansun/gmssl`。两者都改变交付形态，列为待裁决。
- **按进程归属**：纯本机端口/SOCKS 模式下网关看不到发起进程，只能授权到「身份 + 目标」。
  `OPEN` payload 预留可选 `app` 字段（V1 恒缺省），将来加驱动不改帧格式。

## 3. 客户端实现

### 3.1 Transport 接口（`electron/tunnel-transport.ts`）

```ts
interface PeerChannel extends NodeJS.ReadWriteStream {
  end(): void                          // FIN：只关自己这一向
  destroy(): void                      // RST：双向废弃
  readonly bytesUp: number
  readonly bytesDown: number
}

interface TunnelTransport {
  /** 主动开到 dst 的通道（-L / -D 用）。拒绝即 throw，reason 透传给 TunnelStatus.message */
  dial(dstHost: string, dstPort: number, originAddr: string): Promise<PeerChannel>
  /** 让对端 bind 端点，此后入站连接经 onInbound 回来（-R 用） */
  bind(bindHost: string, bindPort: number): Promise<void>
  unbind(bindHost: string, bindPort: number): Promise<void>
  onInbound(cb: (ch: PeerChannel, dst: { host: string; port: number }) => void): void
  onDead(cb: (reason: string) => void): void
  close(): void
  readonly kind: 'ssh' | 'gateway'
}
```

`TunnelManager` 现有 `clientFactory: (cfg) => Promise<Client>`（:49）改为
`transportFactory: (rule) => Promise<TunnelTransport>`，四处 ssh2 调用点各自映射：

| 现调用 | Transport 方法 |
| --- | --- |
| `client.forwardOut(...)`（:212, :327） | `transport.dial(host, port, originAddr)` |
| `client.forwardIn(...)`（:255） | `transport.bind(bindHost, bindPort)` |
| `client.on('tcp connection')`（:238） | `transport.onInbound(cb)` |
| `client.unforwardIn(...)`（:375） | `transport.unbind(...)` |

`startLocal` / `startDynamic` / `handleSocksConn` 的 listener、SOCKS 解析、`conns` 记账逻辑不动；
`startRemote` 里 `net.connect(dstPort, dstHost)` 的回连方向也不动。

### 3.2 文件划分（可测性优先）

- `electron/gateway-mux.ts`：**纯函数**帧编解码状态机，`feed(buf) → Frame[]` / `encode(frame) → buf`。
  不碰 socket。照 `handleSocksConn` 的既有惯例，握手逻辑拆成纯函数单测。
- `electron/transports/gateway-transport.ts`：socket + mux + chan 表（奇偶分配、FIN/RST 状态、
  背压、心跳），只做编排。
- `electron/gateway-store.ts`：网关记录持久化（`gateways.json`）。骨架照抄 `tunnel-store.ts`
  的写队列串行化 / tmp+rename / 损坏文件隔离，私钥字段走 `safeStorage`（此处与 tunnel-store 不同，
  后者无凭据故明文）。

### 3.3 会话状态机

```
idle → tcp → tls → authed → ready ⇄ reconnecting → closed
                              │
                       GOAWAY ┘  (网关优雅下线，带 lastChan)
```

- `reconnecting`：指数退避 1s→2s→…→30s + ±20% jitter，成功后 chan 表清空、`conns` 逐个 destroy、
  状态回 `active` 但 message 记一次「已重连，N 条在途通道被丢弃」。
- TLS 证书指纹不符、`denied-by-policy` → **不重连**，直接 `error`。这两类是配置错误，退避重试只会
  把网关的连接日志刷满。
- 与上一版一致的约束保留：`start()` 期间任何 await 都可能因连接中途死亡而永不 settle，
  每个等待点都要 `once('error', reject)` 桥接（tunnel-manager.ts:196-199 记录的坑）。

## 4. 网关实现（`gateway/hillgw.mjs`）

单文件 ESM，只用 `net`/`tls`/`fs`/`crypto`/`dns`，零依赖，`node gateway/hillgw.mjs --config gw.json`。

- 配置：`listen{host,port}`（默认 port **4433**，对齐山石文档实证值；生产建议置 443 伪装 HTTPS）、
  `ca/server/client` 证书路径、`tokens[]`、`users[{cn, role}]`、`roles{r:{allow[]}}`。
- 每客户端会话：`tls.createServer` + mTLS（`requestCert:true, rejectUnauthorized:true`）→ 解 mux 帧
  → `OPEN` 走 §2.3 授权（字面 host + 端口段 + DNS 逐 IP 校验 + 默认拒绝清单）→ `net.connect` 到目标
  → 双向搬 DATA。
- `-R`：收到 `BIND` → 本机 `net.createServer(bindHost, bindPort)` → accept 时分配**偶数** chan、
  发 `OPEN{proto:"tcp"}`（方向由奇偶隐含），后续同一 chan 双向搬。`UNBIND` 关 server。
- 日志：会话建立/OPEN 允许与拒绝/重连/GOAWAY，一行一事件，含 `sessionId`。这是 V1 唯一的审计面
  （客户端隧道不写审计，沿用上一版口径）。

## 5. 数据结构与 IPC

`TunnelRule`（tunnel-store.ts:17）新增判别字段，**老记录零迁移**：

```ts
via?: 'ssh' | 'gateway'   // 缺省视为 'ssh'，存量规则不动
gatewayId?: string        // via==='gateway' 必填，弱引用 GatewayRecord.id
```

校验补两条（`validateRule`，tunnel-store.ts:59）：`via==='gateway'` 时要求 `gatewayId`、且
`type==='remote'` 的 `remoteBindHost` 语义变成「网关侧绑定地址」；`connectionId` 与 `gatewayId`
不得同时缺省。

新 IPC（全部走 `handleTrusted`，与 main.ts:1212-1243 同款）：

| channel | 签名 | 说明 |
| --- | --- | --- |
| `gateway:list` | `() → GatewayRecord[]` | 不含私钥字段 |
| `gateway:save` | `(rec) → GatewayRecord` | host/port/ca 变更即清 pinning 记录 |
| `gateway:delete` | `(id) → void` | 运行中会话先 GOAWAY 再删 |
| `gateway:enroll` | `(id, token) → {ok, message?}` | 一次性换证书，失败只回文案不回证书 |
| `vpn:statusAll` | `() → VpnSessionStatus[]` | 每条会话的 state/chan 数/字节数/最近错误 |
| `vpn:hostKeyTrust` | `(id, spki) → void` | UI 确认 TOFU 指纹 |

推送：`vpn:status`、`vpn:host-key-new`；preload/env.d.ts 按 `onTunnelStatus` 的订阅模式补
`onVpnStatus` / `onVpnHostKeyNew`（返回取消订阅函数）。

## 6. UI

范围克制，只加三处（沿用上一版的 TitleBar + useSshNavigation 模式，不引新路由框架）：

1. `GatewayView.vue`（新，`src/components/ssh/`）：网关列表 + 每条的 mTLS 状态/会话状态/字节数。
   注册令牌在此输入，一次性的那个输入框用完即清。
2. `TunnelModal.vue`：加「数据面」单切（SSH 连接 / TLS 网关），下方目标字段按 `via` 换下拉源；
   选网关且 type=remote 时 `remoteBindHost` 标签改成「网关侧绑定地址」。
3. `vpn:host-key-new` 用一次性确认弹窗（显示 SPKI 全文 + 复制按钮），与 SSH 首次连接的
   `ssh:host-key-new` 交互一致——两个 TOFU 面必须长一样，否则用户会以为其中一个是假的。

色值/字族继续走 `variables.css` 令牌，终端页目录禁止裸 hex（既有守卫覆盖）。

## 7. 分期

- **M1**：`gateway-mux.ts` 纯函数 + §1.1 三条坑的单测（含迟到帧、半关闭、非法 len）+ §8 测试夹具里的
  本地自签 CA。无网络 IO，先证明帧层正确。
- **M2**：网关 `OPEN/DATA/FIN/RST` + 客户端 `gateway-transport.ts` + `-L` 端到端跑通（策略先全放行）。
- **M3**：mTLS 注册/签发 + SPKI TOFU + §2.3 授权表与 DNS 校验 + 心跳重连。
- **M4**：`-D`（预期近零改动，SOCKS 代码不动）+ `-R`（`BIND/UNBIND` 双向 open）。
- **M5**：`GatewayView.vue` + `TunnelModal.vue` 的 `via` 字段 + `ssh-transport.ts` 抽取（把现有 ssh2
  代码包进接口，行为不变，这一步要有回归测试兜着）。

## 8. 测试

- 纯函数：`gateway-mux` round-trip / 逐字节喂（粘包与分片）/ 未知 chan 丢帧不杀会话 / FIN 与 RST 语义。
- 端到端（vitest，主进程侧）：`node:crypto` 现生成测试 CA → 起 `hillgw.mjs` 于 127.0.0.1 随机口 →
  `GatewayTransport` 连上 → `-L` 打一个 echo server → 断言字节数、断言半关闭不提前断、
  断言网关 kill 后客户端进入 reconnecting 且 chan 清空。
- 表驱动：`validateRule` 的 `via` 组合；授权策略匹配（含 DNS 多 IP、默认拒绝清单）。
- 基线：不新增运行时依赖（`tls/net/crypto/dns` 全内置）；`typecheck` 0 错；vitest 全绿。

## 9. 待裁决

1. 国密支持要不要进路线图——决定网关是否改写为 Go（§2.4）。
2. `-R` 在网关侧 bind 的默认地址：`127.0.0.1`（同 SSH 现口径）还是允许 `0.0.0.0`（对外发布服务，
   这是网关配置项而非客户端配置项，权限模型不同）。
3. 会话并发数上限与用户级配额（多客户端同 CN 并发）——V1 是否直接限 1。
4. 网关是否要「策略热加载」（改 gw.json 后 SIGHUP 生效，不踢已建会话）。

## 10. 改动文件清单

新增：`electron/gateway-mux.ts`、`electron/tunnel-transport.ts`、`electron/transports/ssh-transport.ts`、
`electron/transports/gateway-transport.ts`、`electron/gateway-store.ts`、`gateway/hillgw.mjs`、
`src/components/ssh/GatewayView.vue`、对应 `*.test.ts`  
修改：`electron/tunnel-manager.ts`（依赖 Transport 而非 ssh2 Client）、`electron/tunnel-store.ts`
（`via`/`gatewayId` + 校验）、`electron/main.ts`（`transportFactory` 注入 + 新 IPC + will-quit 清理）、
`electron/preload.ts`、`src/env.d.ts`、`src/stores/tunnel.ts`、`src/components/ssh/TunnelModal.vue`、
`src/composables/useSshNavigation.ts`、`src/views/ssh/SshView.vue`、`src/layouts/TitleBar.vue`、`src/App.vue`

## 11. 实施回填（2026-10-08 当日，M1–M4 落地后）

本节**覆盖**上文与之冲突的表述。评审后动手时撞出五处偏离，逐条记下，不留文档与代码各说一套。

1. **§2.2 的认证模型换了主从**。原稿以「一次性令牌换长期客户端证书（mTLS）」为主、口令不提。
   对照 Hillstone Secure Connect 的实际表单（用户名 + 密码 + 记住密码）后改为：
   **口令是底座，证书是可选第二因子**。`AuthPayload` 因此是 `{user, password, token?}`，
   `token` 字段留着但本版无签发方。连带结论：**Node 的 crypto 只能解析 X.509、不能签发**，
   所以网关不内置 CA，服务端证书与私钥是部署前提（openssl 或既有 PKI），
   「一次性令牌换证书」那条路要等接进 PKI 才谈得上。
2. **§1.1 的「len 超限致命」是不可达检查**。`len` 取 u16，上限就是它自己。致命判定收敛成四类：
   `ver≠1` / 未知 type / OPEN 的 chan 奇偶非法或撞号 / 控制帧 JSON 解析失败。
3. **帧表补了三个类型**：`bind 0x0d` / `unbind 0x0e` / `bindOk 0x0f`。原稿 §3.1 要求
   `TunnelTransport.bind()`，而 SSH 侧有 `forwardIn` 对位、帧表里却没有表达 —— `-R` 无处落地。
   失败复用 `openErr` + `chan=0`，不新增错误帧。
4. **网关侧一条会话服务多条隧道**（§3.1 未预见）。SSH 数据面是「每条隧道各开一条连接」，
   因为对端是用户自己的 SSH 服务器、没有会话概念；而网关的身份是**登录出来的**，
   界面上「连接/断开」的对象是网关而非某条规则。新增 `electron/gateway-manager.ts` 持
   `gatewayId → GatewayTransport` 一张表，重连退避也收在这层（每条隧道各自退避会打出 N 倍风暴）。
   连带给 `TunnelTransport` 加了 `readonly shared: boolean`：`tunnel-manager.teardown()` 原先无条件
   `close()`，共享会话会被单条隧道的停止关掉 —— 这是实施时才暴露出来的真 bug。
5. **§6 的 UI 范围收窄**。接入页只放「连接 / 通道」两个页签：HSC 的 日志/设置 两页在本版
   没有对应数据面，不放空标签（本仓先例：侧栏那枚永灰的「插件」行是被拆掉的）。
   「最优通道」「网关探测」两个开关同理不进 UI —— UDP 承载是二期，画一个不接线的开关等于
   造一个失效设置项。
6. **组件名与皮都在验收时被改了**。§6 写的 `GatewayView.vue` 落地为
   `AccessView.vue` + `AccessModal.vue`（标题栏入口 `安全接入（TLS）`）。用户在实页上圈了四条：
   - 按钮不再由组件自带 scoped `.btn` 定义，一律走 `src/styles/button.css` 的全局系统
     （`.btn` / `.btn-primary` / `.btn-danger` / `.btn-ghost` / `.btn-sm`，24px 高 12px 字）；
     「断开」按该文件的语义表归 danger。页签条右端的「＋添加连接」原是裸文本链接钮，
     与全页其他按钮不同族，改为同一颗 `.btn.btn-primary`。稿子同步把按钮块换成 button.css 逐字规则。
   - 空态只留一行「还没有接入网关」并在页面体内居中，不再重复一颗添加按钮（入口已在页签条上）。
   - 删掉所有解释性灰字段落（空态说明、通道页的 chan 奇偶说明、弹窗里的口令传输说明、
     TOFU 弹窗的 pinning 说理）。**口径：这类 prose 属冗余，此后不写** —— 见记忆
     [[feedback-ui-no-redundant-helper-prose]]。字段级的一次性提示（如「留空 = 保留已存口令」）
     不算这类，它是输入语义而不是讲课。

**测试口径的变更**：`vitest.config.ts` 的 include 加了 `electron/**/*.test.ts`。会话层依赖
`node:stream`，搬不进 `shared/`（那边同时被 `tsconfig.web` 收，而 web 侧没有 node 类型），
放在代码旁边是唯一能被测到的位置。假对端用自写的内存 Duplex —— `stream.Duplex.pair()`
在本机 Node 24 上不存在（`typeof === 'undefined'`）。

**验收结果**：`npm run typecheck` 两侧 0 错；`npx vitest run` 434 绿（新增 43 帧层 + 14 会话层），
其中 `src/styles/variables.test.ts` 的 22 条按目录前缀扫，`src/components/ssh/` 在网内，
两个新组件因此被自动纳入「禁 hex / 禁字面字族 / 禁令牌回落值 / 强调色底禁写死白字 /
:root 令牌须有消费方」五条守卫。`node gateway/hillgw.mjs --selftest` 16 项全过，
其中 6 条 golden 字节与 TS 测试里的字面量逐字相同 —— 两份实现的防漂移锚点。
改完四条批注后同一对命令复跑，仍是 0 错 / 434 绿。
渲染这一层本机拿不到截图（浏览器视口 0×0），改用量测核对：空态盒在页面体内的中心点
(416, 433) 对容器中心 (415.5, 433)，页签条按钮 computed 为 24px 高 / 12px 字 / 4px 圆角 /
透明底 + `color-mix(accent 50%)` 描边 —— 与 button.css 逐字一致；组件内已无 scoped `.btn` 声明。

## 12. 双轴审查后的修复回填（同日，M1–M5 之后）

对 §1–§11 的产物做了一次标准轴 / 规格轴分离审查，逐条验码后修掉下面这些。本节的写法仍是
**覆盖上文冲突表述**，上文保留原句以便看出改了什么。

### 12.1 运行期缺陷（四条，都是"看着接上了、其实没通"）

1. **网关的下行方向根本没发 DATA**（`hillgw.mjs` 的 `Channel.writeOut` 写的是 `this.sock`，
   而那正是目标连接 —— 目标的回包被倒回目标自己，客户端一个字节都收不到；`-L` 与 `-R` 两条
   路径都中招）。现在拆成 `writeSock`（本端 socket ← 客户端）与 `toClient`（DATA 帧 → 客户端），
   两个方向各有一套背压：写侧满就压对应那一侧的读，且用 `slowTarget/slowClient` 记水位，
   避免重复 pause 在同一 socket 上叠 drain 监听器。
2. **未认证即可 BIND**：`onFrame` 原先不检查 `this.user`，未登录的 TLS 对端能让我在任意
   `bindHost:bindPort` 上 listen（`OPEN` 只因 `role=null → allow:[]` 侥幸 fail-closed）。
   现在认证前只放行 `hello/auth/ping/pong/goaway`，其余一律作废会话。
3. **chan 登记晚于 `await dns.lookup`**：解析窗口内同 chan 的第二个 OPEN 能通过撞号检查并覆盖
   表项（两条目标连接共用一个号 = 数据串台），`maxChannels` 也数不准。改为策略判定通过后
   同步占位、失败路径各自摘掉。
4. **一条共享会话上多条 `-R` 互相串台**：`onInbound` 原先是广播数组，一条入站连接会被每条规则
   各自回连、同一条通道被 `pipe` 进两个本机目标；且停掉的规则永不摘订阅。
   改为按 **bind 身份**键控派发：`BIND` 帧带上 `targetHost/targetPort`（网关据此回连，也据此
   回指），网关发起的 `OPEN` 回带 `bindHost/bindPort`，客户端按这个键找订阅者，`teardown`
   必须 `removeInbound` + `unbind`。`BIND_OK` 不回带身份，所以 bind 在客户端侧串行发起。
   连带把 `TunnelTransport.onDead` 改成返回取消函数（不摘就是在共享会话上漏整个 session 对象），
   并在 `TunnelManager.teardown` 里调用。

### 12.2 信任面（§2.1 此前被反着实现）

- 原实现是"首见指纹 → 立刻写库 → 继续发 AUTH → 再推 UI 确认"。后果有两条：用户点"取消"撤销不了
  任何事（库里已经写好了），而口令在确认之前就发给了未验证的对端 —— 那个确认没有重量。
- 现在 `hostKeyProbe` 只判定不落盘；判定为 `new` 时 `connect()` 在发出 AUTH **之前**拆掉 socket
  并抛 `GatewayHostKeyPendingError(spki)`，manager 记下待确认指纹并推事件；
  `gateway:trustHostKey` 校验提交值等于本次握手实收值才落库，另加 `gateway:hostKeyReject`
  让"取消"真的清掉待确认项。`mismatch` 仍是硬失败、不降级为警告（未变）。
- 因此渲染层再也拿不到指纹的写权：`gateway-store.save()` 逐字段挑，不再 `...input`，
  `trustedSpki` 只由主进程 TOFU 路径写入，`setSpki` 校验 `sha256/<base64>` 形状。

### 12.3 凭据与状态诚实性

- **编辑留空口令会把密文再加密一层**（`encrypt(all[idx].password)`，解出来是 `enc:…` 这串文本，
  被当口令送上门 → 改一次就永久认证失败，且每保存一次叠一层）。改为照 `ssh-store` 的口径：
  只加密本次提交的新明文，没有新口令就原样保留库里的密文。
- `decrypt()` 不再把非 `enc:` 前缀的内容当明文口令用（与"绝不落明文"的自述一致）。
- `gatewayStore.list()` 读失败不再降级成空列表；`access.ts` 的 `init/refresh` 返回是否读到，
  失败时页面说"没读到网关列表"并弹一条 toast，而不是印成"还没有接入网关"。
- `statusAll` 的快照只补没被推送过的 id（此前整表替换会把 `Promise.all` 在途时到达的
  "连接中/在线"抹回 idle，且不会再补推）。
- 主动 `close()` 必须 settle 在途的 `ready/dial/bind`（`kill()` 早就这么做，主动关闭漏了 →
  点"断开"时 `gateway:connect` / `tunnel:start` 的 invoke 永不返回）。两个 transport 同一口径。
- `resumeSocketIfIdle` 的门槛从写侧 `writableNeedDrain` 改回读侧 `readableLength`：拿写侧水位
  判读侧暂停，会在写侧 drain 完成后没人再解，整条共享 socket 永久停读。
- 网关被主动断开/删除时联动停掉挂在它上的隧道（`setOnGatewayDown` → `stopByGateway`），
  §11.4 说的"一条会话服务多条隧道"至此才补齐后半句。

### 12.4 帧表与命名（文档此前与实现各说一套）

- §5 的 IPC 通道名以实现为准：`vpn:statusAll/vpn:hostKeyTrust/vpn:host-key-new` →
  **`gateway:statusAll` / `gateway:trustHostKey` / `gateway:host-key-new`**，另加
  `gateway:list/save/delete/connect/disconnect/status/channels/hostKeyReject`。
- `FRAME_TYPE.bind 0x0d / unbind 0x0e / bindOk 0x0f` 的 payload 形状按 12.1(4) 变更：
  `BindPayload { bindHost, bindPort, targetHost, targetPort }`，`OpenPayload` 增可选
  `bindHost/bindPort`。两侧 golden 字节同步更新（TS 用例与 `--selftest` 逐字相同那条）。
- 心跳 `ping/pong` 的 chan 归一在两份实现里都做了（JS 此前只在调用点传 0，没在 `encodeFrame`
  里归一），并补一条"外部传非零 chan 也必须归零"的 golden —— 这条归一此前没有锚点。
- 删除无消费方的 `GatewayRecord.autoConnect`（§11.5 自己定的"不接线的开关不进 UI"，字段却留着），
  `GatewayEndpoint.trustedSpki` 同理（校验全在 probe 里，这个字段声明后没人读）。
- 网关启动时校验 `roles[].allow`：`ports` 写成裸数字（漏了数组）会在 `matchAllow` 里
  `Number.some` 抛 TypeError，从 async `doOpen` 逸出成未处理拒绝直接打死进程 —— 现在开机就报错。
- `ipDenied` 的 IPv6 判定改成字节展开后按前缀位比对：`fe80::/10` 段内此前只认 `fe80` 前缀
  （漏 fe9x–febf），`0:0:0:0:0:0:0:1` 未压缩写法漏判，而 `::ffff:127.0.0.1` 直接绕开 127/8 ——
  拒绝清单被绕一次就是开放中继。读不懂的 v6 字面量按拒处理。

### 12.5 §8 端到端的实际处置

`§8` 要求的"起真网关 + 真 TLS"没有做：Node 不能签发证书，夹具要外部 openssl，成本与收益不匹配。
替代做法是两条能自动跑的字节级往返：
- 客户端侧继续用内存 Duplex 假对端（新增用例覆盖大块写切帧、主动 close 落回在途、
  按 bind 身份派发、无人认领回 RST、取消订阅）；
- 网关侧在 `--selftest` 里补了 **loopback 三 socket 全链路**（AUTH → BIND → 服务连进来 →
  两个方向的 DATA 都要到得了对端）与"未认证即 BIND 必须硬停且不建监听"两条。
  下行方向那个 bug 正是靠这两条先跑红、修完再跑绿才被抓到的 —— 原先 16 项全绿也带着它。

**仍未做**（不粉饰）：主进程装配层（`gateway-manager` / `tunnel-manager` 的 via 分支 /
`gateway-store`）没有自动化测试，12.1–12.3 里那几条并发与时序修正是靠读码 + 类型检查 +
selftest/会话层用例兜住的；`-R` 默认绑定地址仍待 §9 裁决（现在至少认证门槛在了）；
每条网关会话的累计字节数没有展示位（只有通道表逐行）。
