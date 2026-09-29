# S0 · 跨层契约缝与启动

锚点 `a8e588c`。范围：token/port 引导、`handleTrusted` 包络、后端进程监管、Python↔Electron 桥接线协议、`/settings/http` 权威源迁移。
本节所有行号已由我本人逐行打开核对；subagent 提出但被我证伪的子判据在「已核对但未采纳」节明示。

## 缝表（跨层契约基准，后续切片引用此表而非重新推导）

| 渲染层符号 | 通道 | 主进程/路由 | 备注 |
|---|---|---|---|
| `client.ts:23-45` `ensureApiToken` | IPC `backend:getToken` | `preload.ts:33` → `main.ts:1187-1190` | port===0 时后端返回 `''`；`apiToken` 只在非空时缓存，故可自愈 |
| `client.ts:104-126` `getBaseUrl`/`isElectron` | IPC `backend:getPort` | `preload.ts:32` → `main.ts:1182-1185` | 浏览器态退到 `:8010`；IPv6 `[::1]`→`127.0.0.1`（H13） |
| `client.ts:47-51` `withApiToken` | HTTP 头 `X-API-Token` | `backend/main.py:206-220` 中间件 | `compare_digest` bytes 化（MINOR 修复在场） |
| `client.ts:133-165` `waitForBackendReady` | HTTP `GET /api/health` | `main.py:213` 免鉴权 / `:223-229` | 并发去重在场 |
| `env.d.ts:98-100` 三个 bootstrap 成员 | — | 与 `preload.ts:25/32/33` 一一对应 | `client.ts:29` 的防御式 cast 是冗余但无害（类型已声明） |
| `netconfig._policy`（进程内缓存） | `GET/PUT /settings/http` | `routes/settings.py:498-512 / 530-577` | **a8e588c 把权威源从 DB 移到这份缓存**；`_proxy_source():515-521` 仍读 DB |
| 桥接 op：`ssh_exec`/`ssh_connect`/`ssh_disconnect` | WS `/ws/bridge`（刻意不在 `/api` 下） | `electron_bridge.py:175-205` → `bridge_relay.py:100-119` → `bridge-ws-client.ts:293-306` → `electron-bridge.ts:90-153` | `ping` op 无生产者（见 S0-11） |
| `BridgeError{code,suggestion}` | 同上 | `electron_bridge.py:25-50` | 桥接 seam 的 SSHError 分类在入口即被抹平（S0-09） |

## 发现表

| ID | 维度 | 严重度 | 标题 | 主 file:line | 可达性 | 验证方式 | 冲突 |
|---|---|---|---|---|---|---|---|
| S0-01 | a | Major | 「清除代理」不持久：空串行不算用户设定，重启后 env 代理复活 | `netconfig.py:106-113` | 需 `CKUN_HTTP_PROXY` 注入 | 静态逐行 | 无 |
| S0-04 | c | Major | 孤儿后端二次甄别的主判据永不可达，实际只有通用兜底在杀进程 | `backend-manager.ts:89-99` vs `:260-276` | 每次 `start()` | 静态逐行 | 无（09-27 轮新增甄别，未验哪条分支生效） |
| S0-05 | a | Major | res 帧丢失 → `ssh_connect` 建的会话泄漏，累计 64 条后连用户终端也连不上 | `bridge-ws-client.ts:280-282` | WS 在 req 送达与 res 发出之间断开 | 静态逐行 | 无 |
| S0-06 | c | Major | 桥接会话 id 无所有权判定 + `nextId` 每次启动归零 + `agent_jobs` 持久化 id | `electron-bridge.ts:134-141` / `ssh-manager.ts:389` / `ops_job.py:206` | 重启后恢复未终结 job | 静态逐行 + grep 零命中 | 无 |
| S0-07 | b | Major | 桥接抖动时回滚静默失败：`is_available` 只查 env token，异常吞成 warning，undo 栈照清 | `saddle.py:104-113,149-153` / `stirrups.py:250-256` | 桥接重连窗（≤15s） | 静态逐行 | 无 |
| S0-08 | b | Major | 遗留库迁移只搬 `.db` 不搬 `-wal/-shm`，且早于杀孤儿 | `backend-manager.ts:495-531` vs `:281` | 首次迁移路径 | 静态逐行 | 无 |
| S0-02 | b | Minor | 401 不经 CORS 中间件 → 表现为「网络连接失败」并对幂等 GET 白等 ≤8s | `main.py:204-220` + `client.ts:203-220` | 见 S0-03 | 静态逐行 | 无（docstring 自述为「期望行为」，但未推演后果） |
| S0-03 | a | Minor | `client.ts` 头注释的浏览器访问场景 2/3 在鉴权引入后已不可用；CORS 仍开放 LAN `:5173` | `client.ts:4-8` vs `main.py:33-43,167-173` | 恒（浏览器态） | `git log -S` 定时序 | 无 |
| S0-09 | a | Minor | 桥接 seam 抹平 SSHError 分类，`classify_bridge_error` 对该类恒失效 | `electron-bridge.ts:146-152` | 任意桥接 ssh_exec 失败 | 静态逐行 | 同 `439102d` 的错误类塌缩家族 |
| S0-10 | c | Minor | WS 单向鉴权：客户端不验证对端，`hello-ack.ok` 从不读取 | `bridge-ws-client.ts:117-133` | 本地进程抢先 bind | 静态逐行 | 无 |
| S0-11 | d | Minor | `dispatch('ping')` 是死分支，`BridgeOp` 过度声明契约 | `electron-bridge.ts:34,94-95` | 不可达（即是发现） | grep 零生产者 | 无 |

## 详情

### S0-01 [dim a][Major] 「清除代理」是一次性操作，重启后 env 代理复活
- LOCATION：`src/composables/useOpsSettings.ts:140-152`(clearProxy) → `client.ts:530` PUT `/settings/http` → `routes/settings.py:544-570` → `netconfig.set_policy:150-159` → 重启时 `netconfig.load_policy_from_db:106-113`
- CLAIM：`clearProxy()` 发 `{proxy_url:''}`，`normalize_proxy_url('')` 返回 `''`（`netconfig.py:66-68` 已核），库里落下 `http.proxy_url=''` 一行，内存策略也置空——**当场确实直连**。但 `load_policy_from_db` 只看 `if stored["proxy_url"]:`（空串为假）→ 不设 `proxy`，于是 env 的 `CKUN_HTTP_PROXY` 重新成为生效值。用户在面板上做的「清除」动作没有可持久表达的语义。
- 不对称证据：a8e588c 为 timeout/retry 正是引入了 `timeout_set`/`retry_set`（`database.get_http_policy:2142-2152`，注释写明「缺行才用 env，用户写过就以 DB 为准，显式写的 0 也算写过」），**同一套判据没有延伸到 proxy**；`_proxy_source:517` 仍用「非空才算 db 来源」。
- REACHABILITY：需部署侧注入 `CKUN_HTTP_PROXY`——这正是 `_proxy_source` docstring 自述的「运维统一注入的形态」场景。无 env 时清除正常工作（空+空）。
- 面板显示我核对过**无误**：`OpsPanel.vue:15-22` 的徽标在 `v-if="proxySet"` 内，清除后显示「直连（未配置）」；重启后显示「来自环境变量」，文案诚实——缺陷在行为不在文案。
- EVIDENCE：静态逐行，均已打开。后端 `-k` 子集未执行（见文末锁记录）。
- 候选修复方向（未实施）：给 `get_http_policy` 补 `proxy_set` 哨兵（如空串行即「显式清空」），`load_policy_from_db` 与 `_proxy_source` 同读该判据。
- 注：工作区内另一会话的注释已写明「代理口径本轮未变（旧代码在 try 之后有等价的 if not proxy 兜底）」——即这一点被看见过而刻意留下，本条按 a8e588c 判定并要求裁决。

### S0-04 [dim c][Major] 二次甄别的主判据永不可达
- LOCATION：`backend-manager.ts:260-276`(spawn) → `:89-99`(isOurBackendProcess) → `:135-146`(Win WQL 只取 ProcessId/CommandLine) / `:194`(POSIX `ps -o command=`) → `:202-215`(killPids → `taskkill /T /F`)
- CLAIM：判据 1「命令行包含本应用 backend 目录」永不成立——spawn 用的是**相对** argv `'main.py'` 且目录只进 `cwd`（:260-276 已核）；`Win32_Process.CommandLine` 与 `ps -o command=` 都不含 cwd。打包态同样不成立：argv 里是 `...\resources\python-runtime\python.exe`，而 `getBackendDir()` 是 `...\resources\backend`（:480-485,541-552 已核）。于是识别恒退化到判据 2 的通用兜底 `main.py` + `--host 127.0.0.1` + `--port`——正是 `:84-88` 注释声称「不能只匹配 main.py，误杀会破坏无关服务」要防的形态。
- REACHABILITY：每次 `start()`（首启与崩溃自动重启 :394）都会枚举并杀灭。命中条件=本机另有 python 进程 argv 同时含这三段；`--port/--host 127.0.0.1` 是本仓 CLI 习惯，教程系 FastAPI 项目与第二份 checkout 会中招。同安装的双实例已被 `requestSingleInstanceLock`（`main.ts:1341`，且 `:1335` 注释刻意不 `setPath('userData')`）挡住，故不属自我误杀。
- 全树无测试覆盖（`vitest.config.ts` 只收 `src/**`）。
- 候选方向（未实施）：argv 传绝对脚本路径，使判据 1 真能命中。

### S0-05 [dim a][Major] 丢失的 res 帧泄漏活会话，攒到 64 条后阻塞用户自己的终端
- LOCATION：`electron_bridge.py:190-198`(connect_ssh) → `bridge_relay.py:105-119` → WS 断 → `bridge-ws-client.ts:280-282`（`readyState!==OPEN` 直接 return，**无日志无错误路径**）→ `bridge_relay.py:224-228 _fail_all_pending` → Python 抛 WS_LOST → `schedule_engine.py:358-359`（工作树 in-flight，+1 行）只在成功时赋 `ssh_session_id` → `:425-426` finally 因 id 为空跳过 `disconnect_ssh`
- CLAIM：relay 的 fail-closed「绝不重发」（`bridge_relay.py:13`）保护的是请求侧，没有补偿**已在主进程发生的非幂等副作用**。`dispatch('ssh_connect')`（`electron-bridge.ts:117-132`）先把会话注册进 `sessions` 再发 res；套接字已死则 res 蒸发 → 一条健康、无人引用、界面上看不见的会话常驻。`destroySession` 只在 TCP close 时触发（`ssh-manager.ts:523-537`），而 `keepaliveInterval:10000`（:546）让它永不被远端回收。
- REACHABILITY：WS 在「req 已投递」与「res 已发出」之间死亡即产生一条。文档化触发器=后端崩溃→自动重启换端口（`main.ts:1409-1423` setTarget 主动关旧 socket）、hello-ack 超时/pong 死重连（`bridge-ws-client.ts:210-228,247-264`）。累计到 `SESSION_LIMIT`（`ssh-manager.ts:624-625`）后**所有**后续 connect 抛错，含用户交互式终端。
- 无 TTL、无按 id 认领、重连后无对账。

### S0-06 [dim c][Major] 桥接不校验会话所有权，而会话 id 每次启动重新从 1 发号且被持久化进 DB
- LOCATION：`electron-bridge.ts:134-141`(ssh_disconnect 只验非空字符串) / `:97-115`(ssh_exec 同) ← `ops_job.py:206`(从 `agent_jobs.session_id` 恢复) + `:756,834`(`_session_ctx(entry.ssh_session_id)`) → `ops_common.exec_remote:100-130` → `ssh-manager.ts:601/610`（`ssh-${nextId++}` 与 `sftp-${nextId++}` **共用同一个计数器**，`ssh-manager.ts:389 private nextId = 1`）
- CLAIM：`grep -rn "startsWith('sftp" electron src backend` → **零命中**：全仓没有任何地方限定桥接只能操作自己建的会话。`electron_bridge.py:200-205` 的 docstring 写着「关闭由 connect_ssh 建立的 SSH 会话」——这条限制只存在于散文，无机制承载。叠加 id 空间每次主进程启动归零、而 `agent_jobs` 的 `session_id` 跨重启持久（`ops_job.py:97` 注释自述「进程重启后由 _ensure_restored 恢复」），陈旧 job 携带的 `ssh-N` 在新进程里可能已经是**另一个**会话。
- REACHABILITY：应用带运行中的远程 job 退出（远端 nohup 按设计继续跑）→ 重启 → 用户开终端拿到 `ssh-1` → 恢复的 job 做 `job_output`/`job_kill` 时对 `ssh-1` 下发命令，或 `disconnect` 掉用户正在用的会话（`ssh-manager.ts:941-945` 的 disconnect 会 `cancelReconnect`+`teardownSession`，即抑制自动重连）。渲染层同名调用受 `assertTrustedSender` 保护（`main.ts:38-45`），桥接这条腿没有任何等价门。
- 备注：`ops_job.py` 在计划的切片划分里未显式归属，本条按桥接缝登记，S6 审 ops_job 时交叉引用。

### S0-07 [dim b][Major] 桥接抖动期间的回滚失败完全静默
- LOCATION：`saddle.py:104-113`(ShellUndo) / `:140-153`(FileContentUndo) → `electron_bridge.py:92-93`(`is_available` 只读 env token) → `:126-127`(`relay.connected` 不满足即抛 WS_LOST) → `stirrups.py:250-256`(undo 吞每条异常 + `undo_stack.clear()`)
- CLAIM：`is_available()` 在整个后端进程生命周期内恒为真（token 是 env 注入的），**不能**反映通道可用性；真正的通道判据在 `_ws_request` 里，抛错后被 `apply_reverse` 的 `except Exception` 收成一行 `logger.warning`。`stirrups.undo()` 再包一层 catch，`undo_all` 无论成败都 `clear()` → 既无重试也无上报。spec `2026-09-14-bridge-ws-channel-design.md:61-66` 承诺的「短等待」只实现了 `probe()` 一侧（`electron_bridge.py:159`），`_send` 全程无等待（`:139-146` 已核）。
- REACHABILITY：桥接重连窗（`bridge-ws-client.ts:22` RECONNECT_MAX_MS 上限 15s）内触发回滚——后端崩溃自动重启正是 `electron_bridge.py:78-80` 注释点名的场景。后果是远端文件未被还原而 run 显示「已回滚」。
- 定级说明：撤销本身是尽力而为，故不升 Critical；缺陷在于**不可见 + 栈被清**，属数据完整性可观测性缺口。

### S0-08 [dim b][Major] 遗留库迁移漏搬 WAL 边车，且排在杀孤儿之前
- LOCATION：`backend-manager.ts:255`(ensureDbPath) → `:495-531`(candidates 只列三个 `.db`) → `:260` spawn → `:281` killPids；`backend/database.py:65` `PRAGMA journal_mode=WAL`（该文件工作树 in-flight，hunk 在 :2202，`:65` 与 a8e588c 一致）
- CLAIM：WAL 模式下已提交数据合法地留在 `ckunagentic.db-wal` 里直到 checkpoint。迁移只 rename/copy 主文件，SQLite 在目标目录新建空 `-wal` → 旧目录 wal 中的最新事务成为孤儿，**用户无错可看**（该库存 SSH 连接记录与凭据、agent 记忆、定时任务）。且 `:506` 的 `existsSync(targetPath)` 会把这个半迁移态永久锁死。次级：迁移发生在 `killOrphanBackends`(:281) **之前**，若旧孤儿仍持有遗留库，Windows 分支 `renameSync` EPERM → 退到 `copyFileSync`(:523) 可读到事务中间图像；POSIX 分支完全无锁保护。
- REACHABILITY：新 DB 路径方案的首次启动 + 遗留位置存在带未 checkpoint 内容的库（任何在跑的 WAL 库都有）。杀孤儿与迁移这两件功能的先后顺序恰好反了。

### S0-02 / S0-03 [dim b+a][Minor] 401 的渲染层表现与已死的浏览器模式
- LOCATION：`main.py:204-220`（token 中间件经 `@app.middleware` 注册后被置于**最外层**，先于 CORSMiddleware，401 响应不带 CORS 头）→ `client.ts:190-226`（fetch 被 CORS 拦 → `TypeError` 命中 `/fetch/i` → `isNetworkError` 为真 → 幂等 GET 走 `waitForBackendReady()` ≤8s 重试一次 → 最终 `FriendlyError('网络连接失败，请检查服务是否运行')`）
- CLAIM：鉴权失败在渲染层与「后端挂了」不可区分，并且会先白等一轮健康检查——而 `/api/health` 免鉴权（`main.py:213`）必然立刻成功，于是重试必然再失败一次。`main.py:204-205` 的注释记录了该顺序并称「对未授权请求这是期望行为」，安全侧成立，但未推演可观测性后果。
- S0-03 是同一条的可达性来源：`client.ts:4-8` 把「浏览器本机/浏览器外部访问（http://IP:5173）」列为受支持场景，CORS 正则（`main.py:167-173`）也为其放行 LAN 私有段 `:5173`。但 `X-API-Token` 只能经 IPC `backend:getToken` 取得——浏览器态无 `window.electronAPI`，**没有任何 token 通道**（全仓 `CKUN_API_TOKEN|X-API-Token` grep 仅此 17 处，无 query/localStorage 兜底）。时序已核：CORS 正则在 Initial commit 即有，鉴权是后来的 `16393cc`（安全审查）加入，二者从未对齐。
- REACHABILITY：S0-03 = 浏览器态恒发（`npm run dev:web` 的 LAN 真机调试）；Electron 态基本不触发（`apiToken` 是 `BackendManager` 构造期 `readonly`，`:43` 已核，重启复用同值，不存在漂移）。
- 裁决请求（Tradeoff 性质）：浏览器模式是要（a）补 token 通道、（b）正式废弃并删掉场景 2/3 注释 + 收紧 CORS 正则、还是（c）保持现状但把 401 与网络错误分开呈现。

### S0-09 / S0-10 / S0-11
- **S0-09**：`electron-bridge.ts:146-152` 的 catch-all 把任意 `SSHError` 折成 `{status:500, error:'请求处理失败，请稍后重试'}`；`electron_bridge.py:134-136` 映射为 `BridgeError(HTTP_ERROR, 该串)` → agent 侧无法区分「会话没了（可用 ssh_connect 救回）」「命令合法超时」「会话数触顶」。同文件的渲染层对照腿 `main.ts:57-76 → :83-101 → ssh-error.toIpcTransport` 保住了 code——**同一错误两条缝两种保真度**。附带：`classify_bridge_error:30-42` 的 `"timeout" in msg` / `"未配置" in msg` 探针对该类恒不可达（`BridgeError` 在 :32-33 短路）。与 `439102d` 属同一「结构化 code 被塌缩」家族。
- **S0-10**：`ELECTRON_BRIDGE_TOKEN` 只做 client→server 断言；TS 侧 `hello-ack` 仅用于解除定时器（`bridge-ws-client.ts:130-133`，已核），`ok` 字段从不读取（relay 也只在鉴权通过时才发 ack，故恒 true——**spec 第 52 行赋予它的语义无代码承载**）。反向风险：抢先 bind 到空闲端口的本地进程可下发 `req` 让主进程执行 `ssh_exec`。窗口真实存在：`findFreePort:557-578` 在 `server.close(cb)` 后释放端口，而 Python 导入 + uvicorn 启动要 2-3s（:599-601 自测），但攻击者还需让 `GET /api/health` 回 200 才能过 `waitForReady`。定级 Minor（前置条件苛刻，本机已有恶意进程时收益边际）。
- **S0-11**：`BridgeOp` 声明的 `ping` op 无任何 Python 生产者（`electron_bridge.py` 锚点版只发 `ssh_exec`/`ssh_connect`/`ssh_disconnect`，已核）；`{type:'ping'}` 心跳帧在 `bridge_relay.py:189-190` 就地应答，永不进 `dispatch`。故 `electron-bridge.ts:94-95` 为死分支，spec:53 的 op 清单过度声明契约。

## 已核对但未采纳（防止下游切片重复提出）

1. **「清除代理后面板来源徽标自相矛盾」——我证伪了 subagent 的这一子判据**：`OpsPanel.vue:15-22` 徽标在 `v-if="proxySet"` 内，清除后走 `v-else` 显示「直连（未配置）」，显示正确。真正的缺陷只剩 S0-01 的持久化语义。
2. `installCsp`（`main.ts:223-237`）**无发现**：单次注册，`ws://127.0.0.1:*` 覆盖 dev server（`electron.vite.config.ts:31` 默认 127.0.0.1）；仅 `dev:web` 的 `VITE_HOST=0.0.0.0` 理论上可能把 HMR 排除在 CSP 外，静态无法判定，留待需要时实测。
3. 历史修复均在位、未被重复上报：空 stdout `'data'` 监听器（`backend-manager.ts:303-305` 含理由注释）、`waitForReady` settled 去重（:595-611）、second-instance `isDestroyed()`（`main.ts:1346-1352`）、`appQuitting` 前置门（:24,248-251,465）、60s 未捕获异常窗（:1381-1395）、`schedule_engine` 已无 `bridge.port` 读取。
4. `env.d.ts:98-100` 已正确声明 `getBackendToken`，`client.ts:29` 的防御式 cast 是冗余无害，不作发现。

## 闸门执行记录（诚实标注）

- `cd backend && python -m pytest -q -k "round22 or http_env or ci_matrix or model_default"` → **RC=124（280s 超时被杀），未执行完**。同 `-k` 的 `--collect-only -q` → RC=0，66/1547 collected in 1.45s → 判定为共享 SQLite 锁 hazard（另一会话活跃写入中），非改动破坏。按计划 Step E 记为 `not executed (lock hazard)`。
- `npx vue-tsc --noEmit -p tsconfig.node.json` → **RC=0**。
- S0 的 11 条发现全部为 `static-read-only`（我逐行打开验证 LOCATION），无一条伪造测试结论。

---

# S0b · SSH/IPC/HTTP 错误契约全往返（第二路）

路径纠正：渲染层回读侧实际在 **`src/ssh-error-types.ts`**（计划里写的 `src/components/ssh/ssh-error-types.ts` 不存在，59 LOC 那份在 src 根）。

## S0b 发现表

| ID | 维度 | 严重度 | 标题 | 主 file:line | 可达性 |
|---|---|---|---|---|---|
| S0-12 | b | Major | 加密私钥缺口令：ssh2 从 Promise executor 同步抛出，整条 SSHError 分类链被绕过 | `ssh-manager.ts:784` vs `:1186-1191` | 恒，无需时序 |
| S0-13 | b | Major | `/Error:\s/i` 命中 Electron IPC 信封 → 所有非 SSHError 的主进程拒绝都显示「服务暂时不可用」 | `client.ts:69` + `main.ts:100` | 恒（每条裸 Error 拒绝） |
| S0-14 | a | Major | 渲染层用 (code, detail) 重打文案并丢弃生产者的 message；detail 仅在有 original 时存在 | `client.ts:86-89` / `ssh-error.ts:116` | 恒（超时/DNS 两类） |
| S0-15 | a | Major | 传输错误的 code/retryable 以自定义属性挂裸 Error 过 IPC，与仓库自述的「自定义属性会丢」矛盾 → 自动续传分支疑死 | `ssh-manager.ts:83-88` / `sftp.ts:954-955,967` | 需一次运行时确证 |
| S0-16 | d | Minor | AUTH_PASSPHRASE 无生产者（正则对准 ssh2 从不发的措辞），口令错误被归到 AUTH_KEY | `ssh-error.ts:39,45` | 死分支（即是发现） |
| S0-17 | d | Minor | 4 个 SFTP_* 枚举成员只有枚举无生产者；真实 SFTP 失败走另一套未共享的分类 | `ssh-error-core.ts:36-39` vs `ssh-manager.ts:38-47` | 同上 |
| S0-18 | b | Minor | 重连耗尽丢弃已分类的 SSHError，UI 只能恒说「自动重连失败」 | `ssh-manager.ts:1072-1084` | 改密码后断链即触发 |
| S0-19 | b | Minor | SESSION_NOT_FOUND 的 retryable 恒 false，而主进程正为该会话跑 12 次重连 | `ssh-error-core.ts:54-66` / `DashboardView.vue:13,26` | 重连窗内轮询即触发 |
| S0-20 | a | Minor | retryable 与自家文案互相矛盾（SESSION_LIMIT 文案以「后重试」结尾却 false；CONN_REFUSED 反之） | `ssh-error-core.ts:71,117` vs `:54-66` | 恒 |
| S0-21 | a | Minor | testConnection 算出 code 又丢掉，`SSHTestResult` 无 code 字段 | `ssh-manager.ts:1178-1184` / `:303-307` | 恒（测试连接） |
| S0-22 | d | Minor | HOST_KEY_UNKNOWN 结构性不可达（TOFU 自动接受未知密钥） | `ssh-manager.ts:485-498` / `ssh-error.ts:58` | 不可达 |
| S0-23 | a | Minor | `SSHErrorTransport` 形状在两进程各抄一份，回读侧裸 cast 不校验 code 归属 | `ssh-error.ts:101-107` vs `src/ssh-error-types.ts:22-28,43` | 潜伏 |
| S0-24 | c | Minor | 标注「开发模式才显示」的 detail 与 Electron 内部信封文本无条件渲染到用户 | `ssh-error.ts:106` → `useSshNavigation.ts:259-264` | 恒 |

## 我逐行证实的部分

- **S0-12**：`ssh-manager.ts:784` 的 `client.connect(connectConfig)` 是 executor 的最后一条语句且**无 try/catch**；同文件 `:1186-1191` 的 testConnection 刻意包了 `try { client.connect() } catch { classifyRawError }` —— 两处同一动作两种防护，是「作者知道这里会同步抛」的铁证。`node_modules/ssh2/lib/client.js:259-261` 确为 `throw new Error('Cannot parse privateKey: …')` 同步抛出；executor 内同步抛 → Promise 以**裸 Error** reject → `SSHError.from`/`classifyRawError`/`toIpcTransport` 全程不经。与 S0-13 串成一条链：口令留空/输错 → toast「服务暂时不可用，请稍后重试」，而 `ssh-error-core.ts:86-87` 为这个场景专门写的「私钥保护密码（passphrase）错误或缺失」永不显示。**这是最常见的 SSH 认证用户错误之一。**
- **S0-13**：`client.ts:69` 的 `/Traceback|Exception|Error:\s/i` 作用于**未经剥壳的整条 message**，而 Electron 对 invoke 拒绝的信封恒为 `Error invoking remote method '<channel>': Error: <msg>`——`Error: ` 子串必命中。同仓 `src/stores/server-monitor-core.ts:145-147` **专门先剥这个信封再匹配**，证明团队知道该形态；`toFriendlyMessage` 这条腿没剥。后果：`ssh-manager.ts:592` 那条注释写明「P2 修复：不再落进泛化错误」的中文根因（「未检测到 SSH Agent：请先运行 ssh-agent…」），在最后一跳被替换成「服务暂时不可用，请稍后重试」。`main.ts:100` 对非 SSHError 原样透传，无任何补救。
- **S0-14**：`ssh-error.ts:116` 逐字为 `detail: err.original instanceof Error ? err.original.message : undefined`；`SSHError` 构造器只保留 `friendly`（`:70-86`），`opts.detail` **不进 transport**。于是 `ssh-manager.ts:862` 的 `new SSHError('EXEC_TIMEOUT', {detail:'30秒'})` 在主进程内是「命令执行超时（30秒）…」，跨 IPC 后 `client.ts:86-89` 用 `(code, transport.detail=undefined)` 重打 → 「命令执行超时，可增大命令超时时间」，那个数字正是这条文案存在的理由。对照组：`server-monitor-core.ts:133-135` 用 `t.message`（正确消费者），两个消费者并存本身就说明 `message` 才是设计上的载体。`DNS_FAIL` 更硬：`ssh-error-core.ts:77-78` 是全部 22 个成员里**唯一无 `detail ?` 守卫**的插值，配 `ssh-error-types.ts:52-53`（name 兜底腿硬写 `detail: undefined`）→ 打出 `无法解析主机名""`。
- **S0-24**：detail 字段注释（`ssh-error.ts:106`）自称「原始技术信息（开发模式才显示给终端用户）」，而 `useSshNavigation.ts:259-264` 与 `sftp.ts:972,1015` 都没有任何 env/dev 门（grep `import.meta.env` 零命中）→ `getaddrinfo ENOTFOUND …`、`Cannot parse privateKey: …`、`Error invoking remote method 'sftp:upload': …`（含内部通道名）直接进用户可见区。

## S0-15 —— 需要一次运行时确证（且它 gate 着 S5 的遗留 #1）

`ssh-manager.ts:83-88` 的 `toTransferError` 把 `code`/`retryable` 作为**自定义属性挂在裸 Error** 上抛出，`main.ts:100` 原样透传，`sftp.ts:954-955` 读 `err?.code`/`err?.retryable` 并据此在 `:967` 决定是否自动续传。而 `main.ts:52-55` 的注释**自己写着**「原始 SSHError 经过 IPC 后会退化为普通 Error（丢失 name/code）」——正因如此 SSH 侧才同时写 `e.name` 和 `e.__sshError` 两条腿。按仓库自己的前提，SFTP 这条腿的 `retryable` 恒 undefined ⇒ `:967`/`:1010` 的「NETWORK/STALLED 自动续传一次」与 `:977`/`:1019` 的停滞专属文案**全是死分支**，续传特性静默退化为「上传失败」。

我未采信为定论的原因：这依赖 Electron IPC 对 Error 的具体序列化实现，静态无法证伪（subagent 用 node `structuredClone` 旁证，但 Electron 的 invoke-rejection 路径与之不完全同码）。

**确证方法（一行，属主会话在跑的应用里做，不改码）**：DevTools 里对必然失败的传输 `await window.electronAPI.sftpUpload(...).catch(e => console.log(Object.keys(e), e.code, e.retryable))`。若 `code/retryable` 为 undefined ⇒ S0-15 成立、`:967` 分支确死。

**对 S5 遗留 #1 的影响（已推演，不依赖 S0-15 的结果）**：`sftp:stalled` 走的是**推送通道**（`ssh-manager.ts:1391`，JSON 载荷完好）⇒ 面板照样知道停滞并提示用户；用户**手动**点重试即进入主进程 `sftpUpload` 的 `resumeSize` 分支 ⇒ 零洞损坏路径**仍可达**。S0-15 只决定「自动」重试是否会发生，不决定 #1 是否可触发。

## 密钥泄露审计（S0b 顺带核过，判为干净，非发现）

`models_config.api_key` 只在该表（`database.py:213-219`）而不在 `app_settings`，诊断面板的 app_settings 快照带不走它；`_DIAG_SECRET_HINTS` 覆盖了唯一落在 app_settings 的密钥 `web.tavily_api_key`，且 proxy 分支排在数值白名单之前（anchor `routes/settings.py:596,622-640`）；`doConnect` 建完 ssh2 配置立刻清 `password`/`passphrase`（`ssh-manager.ts:633-634`）；`ssh:listConnections`/save/duplicate 均过 `stripCredentials`（`main.ts:1059-1061,1144-1165`）；ssh2 与 keyParser 的任何消息串都不回显密钥内容。
**唯一待裁决**：掩码规则对 >12 字符的值露出前 4 + 后 4（13 字符密钥露 8 位），而该快照经 HTTP 提供且面板用途写明「可粘进 bug 报告」——Tradeoff，留你定。

## HTTP 侧残余：无发现

后端错误体统一为 `{"detail": string}` 或 FastAPI 422 的 `[{loc,msg,type}]`，`client.ts:172-178` 两种都处理；全仓仅 `main.py:219` 一处裸 JSONResponse（`{"detail":"unauthorized"}`）。200-with-`status:"error"` 的业务形状 `settings.ts:480-488` 读对了。
