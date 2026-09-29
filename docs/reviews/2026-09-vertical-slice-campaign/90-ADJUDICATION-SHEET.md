# 裁决总表 · 功能纵向切片审查战役（S0→S13）

锚点 `a8e588c` · 生成时间 2026-09-29 · 模式：**只报告，代码零改动**

总计 **133** 条：Critical **2** · Major **54** · Minor **71** · Tradeoff **4** · Rejected **0** · 登记/交叉引用 **2**。
本表由脚本从各切片报告的发现表机械汇总（列名按表头识别，未做人工重排）。

**核验列含义**：✅ = 我本人打开原文逐行证实；◐ = 采信审查路引用、我未二次打开（裁决前建议自行复核该行）；— = 该切片表未单列。

**决定列**：请逐行填 `采纳 / 驳回 / 延后`。留空 = 未裁决。本表不含任何已实施的修复。

## 五条历史遗留的重判（本轮任务书要求并入对应切片）

| 遗留 | 重判结论 | 所在切片 | 关联条目 |
|---|---|---|---|
| #1 SFTP 停滞续传零洞静默损坏 | **成立，Critical**；并新增第二条触发路径（主进程被杀/通道硬断 ⇒ 截断代码不可达 ⇒ 跨重启续传同样踩洞） | S5 | S5-01 |
| #2 plan/spec/goal_write 在 plan 模式永拒 | **成立**（端到端：注册序、无 bound_command 豁免、UI 可达、拒绝文案自带矛盾指令、无测试钉结局） | S2 | LEGACY #2 节 |
| #3 reconcile 部分失败重跑重复入库 | **成立**，且当年的硬修方向被**证伪**（mstore 写经 `asyncio.to_thread` 落在不同线程本地连接上，无法罩进一个事务）；另找到 3 条更窄的 seam | S3 | LEGACY #3 节 |
| #4 `resolvePermission` 恒 true ⇒「已拒绝」分支可达性 | **成立**：生产调用点唯一且传 true，DB/trace 回放侧亦无 `granted=false` 生产者 ⇒ 「已拒绝」是死 UI | S2 | LEGACY #4 节 |
| #5 `skill_registry` 前导三行重复 | **成立**，实测为逐字相同的 **7 行**（另含重复的 DB 禁用清单读取） | S8 | S8-01 |

## Critical — 2 条

| ID | 切片 | 维度 | 标题 | 位置 | 核验 | 决定 |
|---|---|---|---|---|---|---|
| S2-01 | S2 | c | 自动审批可推翻用户的显式「拒绝」：`PermissionGate` 调裁决器前不查 `is_denied`，而 `FilesystemGuard` 查 | ``gate.py:707-711` vs `guards.py:181-188`` | ✅ | ＿＿ |
| S5-01 | S5 | a | LEGACY #1 确认成立：上传是 8 路并发绝对偏移写，`.part.size` 必然越过连续边界，而停滞路径故意跳过截断 → 续传跳过零填充洞 → rename 出静默损坏文件 | ``ssh-manager.ts:1525` + `:1549` + `:1466`` | — | ＿＿ |

## Major — 54 条

| ID | 切片 | 维度 | 标题 | 位置 | 核验 | 决定 |
|---|---|---|---|---|---|---|
| S0-01 | S0 | a | 「清除代理」不持久：空串行不算用户设定，重启后 env 代理复活 | ``netconfig.py:106-113`` | — | ＿＿ |
| S0-04 | S0 | c | 孤儿后端二次甄别的主判据永不可达，实际只有通用兜底在杀进程 | ``backend-manager.ts:89-99` vs `:260-276`` | — | ＿＿ |
| S0-05 | S0 | a | res 帧丢失 → `ssh_connect` 建的会话泄漏，累计 64 条后连用户终端也连不上 | ``bridge-ws-client.ts:280-282`` | — | ＿＿ |
| S0-06 | S0 | c | 桥接会话 id 无所有权判定 + `nextId` 每次启动归零 + `agent_jobs` 持久化 id | ``electron-bridge.ts:134-141` / `ssh-manager.ts:389` / `ops_job.py:206`` | — | ＿＿ |
| S0-07 | S0 | b | 桥接抖动时回滚静默失败：`is_available` 只查 env token，异常吞成 warning，undo 栈照清 | ``saddle.py:104-113,149-153` / `stirrups.py:250-256`` | — | ＿＿ |
| S0-08 | S0 | b | 遗留库迁移只搬 `.db` 不搬 `-wal/-shm`，且早于杀孤儿 | ``backend-manager.ts:495-531` vs `:281`` | — | ＿＿ |
| S0-12 | S0 | b | 加密私钥缺口令：ssh2 从 Promise executor 同步抛出，整条 SSHError 分类链被绕过 | ``ssh-manager.ts:784` vs `:1186-1191`` | — | ＿＿ |
| S0-13 | S0 | b | `/Error:\s/i` 命中 Electron IPC 信封 → 所有非 SSHError 的主进程拒绝都显示「服务暂时不可用」 | ``client.ts:69` + `main.ts:100`` | — | ＿＿ |
| S0-14 | S0 | a | 渲染层用 (code, detail) 重打文案并丢弃生产者的 message；detail 仅在有 original 时存在 | ``client.ts:86-89` / `ssh-error.ts:116`` | — | ＿＿ |
| S0-15 | S0 | a | 传输错误的 code/retryable 以自定义属性挂裸 Error 过 IPC，与仓库自述的「自定义属性会丢」矛盾 → 自动续传分支疑死 | ``ssh-manager.ts:83-88` / `sftp.ts:954-955,967`` | — | ＿＿ |
| S1-01 | S1 | b | 中止/断连的 run 一条 assistant 消息都不落，`agent_trace` 快照与后处理一起丢掉 | ``routes/agent.py`(anchor):1001-1052 唯一写区 与 :851 break` | — | ＿＿ |
| S1-02 | S1 | a | 第四个终态清理点（`streamAgentRun` 的 onError）漏关澄清向导、漏释放待交付 | ``client.ts:945-951` → `agent.ts:252-269`` | — | ＿＿ |
| S2-02 | S2 | b | 可见终端分支 `continue` 跳过 `permission_pending.pop` ⇒ 同一命令本 run 内永不再出卡且永久豁免循环检测 | ``driver.py:546-551` vs `:557-570`` | ✅ | ＿＿ |
| S2-03 | S2 | b | `PUT /settings/permission-policy` 是同步 `def` ⇒ 在线程池里改全局 `PermissionGate` 状态，正是同族端点当年为除竞态而改 `async def` 的那一类 | ``settings.py:324`(anchor) → `gate.py:44-58,231-285`` | ✅ | ＿＿ |
| S2-04 | S2 | c | 每一次审批决策落审计时既无 run_id 也无 environment，与 `security-boundary.md` §6 的声明相反 | ``routes/agent.py:1395-1401` 等四处` | ✅ | ＿＿ |
| S3-01 | S3 | b | 对账器无条件信任 LLM 给的 `target_id`，不校验它属于本候选的对照面 ⇒ 作废一条无关记忆，审计 `old=NULL` | ``reconcile.py:370` `:415-419` + `mstore.py:193-197`` | ✅ | ＿＿ |
| S3-02 | S3 | b | `decay_memories` 的第二条 UPDATE 是无年龄门槛的地板清扫：新写入且 `importance<0.2` 的行在同一次 tick 里被作废，且不落审计 | ``mstore.py:671-690`（`c2` 在 `:682-686`）` | ✅ | ＿＿ |
| S3-03 | S3 | b | 主机维度用的是进程级临时会话 id `ssh-N`（每次主进程启动 `nextId` 归零），而不是 spec 明令的连接档案 id ⇒ A 机记忆下次启动被当成 B 机事实注入 | ``retriever.py:30-37` + `ssh-manager.ts:389` vs spec `:100`` | ✅ | ＿＿ |
| S3-04 | S3 | b | `pinned` 不保护对账路径：`retire`/`supersede` 无 pinned 守卫，`_similar_existing` 只按 `status='active'` 取对照面 ⇒ 用户钉住的手写事实可被 UPDATE/DELETE 掉 | ``mstore.py:165-168`（retire WHERE）、`:193-197`（supersede WHERE `id=?` 裸）、`reconcile.py:115-127`` | ✅ | ＿＿ |
| S4-01 | S4 | c | PTY shell 白名单按 basename 后缀放行，任意目录下的同名可执行文件都能起 | ``pty-manager.ts:113-115`（表 `:28-35`）` | ✅ | ＿＿ |
| S4-02 | S4 | c | `pty:completeCommand` 对渲染层给的目录做枚举/`cd`，是唯一不调 `assertSafePath` 的文件原语 | ``main.ts:918-1019` + `listFilesByPrefix:369-382`` | ✅ | ＿＿ |
| S4-03 | S4 | c | safeStorage 不可用的机器上密码静默存不进，`ssh-store.ts:49-50` 写明的「调用方须先检查并提示改用私钥」契约无人实现 | ``ssh-store.ts:46-54` + `:246-248`，`main.ts:1151-1154`` | ✅ | ＿＿ |
| S4-04 | S4 | a | 自动重连对确定性不可重试错误照跑 12 次/≈4.7 分钟：12 次认证尝试撞 sshd MaxAuthTries，主机密钥变更则堆 12 条永不消失的「中间人攻击」toast | ``ssh-manager.ts:1072-1084`（catch 不判错误）↔ `shared/ssh-error-core.ts:54-66`` | ✅ | ＿＿ |
| S4-05 | S4 | a | `connectRemote` 无在途互斥 + debounce 路径的 `releaseConnectLock` 解除自己那笔仍在跑的锁 → 同一主机两条并发会话 | ``useSshNavigation.ts:213-233`，入口 `SshView.vue:129-141`、`RemoteManager.vue:104→:379→:265`` | ✅ | ＿＿ |
| S4-06 | S4 | a | 重连窗口内点快捷命令静默丢失（不敲键、不报错、重连成功后也不补发），而那条错误 toast 是不可达死码 | ``QuickCommandBar.vue:110-114` ↔ `main.ts:1108-1110`、`ssh-manager.ts:786-791`、`pty-manager.ts:217-223`` | ✅ | ＿＿ |
| S4-07 | S4 | b | 批量下发走 Electron 侧 `ssh:exec`，零审计记录 —— 与 `security-boundary.md:26,138-139` 宣称的「批量通道在边界内、靠事后审计兜底」直接矛盾（09-21 已列待裁决，非已驳回） | ``BulkCommandModal.vue:279` → `main.ts:1133-1136`；`grep -rni audit electron/` = 0 命中；`/agent/safety/classify`（`routes/agent.py:1499-1515`）自述「只读无状态」` | ✅ | ＿＿ |
| S5-02 | S5 | b | 兜底注释宣称的「顺序写」在上传侧不存在，M-5 的顺序流水线只用于下载；注释把两侧都写成「同形」 | ``:1506` `:1548` vs `:1757`` | — | ＿＿ |
| S5-03 | S5 | a | 主进程被杀/通道硬断时截断代码根本不执行，跨重启续传同样落进洞里 | ``:1549`（只在正常收尾可达）` | — | ＿＿ |
| S5-07 | S5 | c | OS 原生拖拽上传在 Electron 35 上是死功能：`File.path` 已于 Electron 32 移除，全仓无 `webUtils.getPathForFile` 桥 | ``SftpPane.vue:505` vs `package.json:38`` | — | ＿＿ |
| S5-08 | S5 | c | `fs:copy` 无源/目标重叠判定：把文件夹粘贴进自身 → 递归到路径长度上限，留下删不掉的长路径残骸 | ``main.ts:700-731`` | — | ＿＿ |
| S5-09 | S5 | a | 承接 S5-08：10 万文件/10GB 上限在 `collect` 之后才判，而 P2 修复注释写的正是「递归收集全部驻留内存可 OOM 崩主进程」——守卫放错了位置 | ``main.ts:732-745`` | — | ＿＿ |
| S5-10 | S5 | c | `sftp:writeFile`（编辑器保存）原地 O_TRUNC 覆写远程文件：无 `.part`+rename、无字节上限（本地侧 `fs:writeFile` 有 50MB 上限） | ``ssh-manager.ts:1891-1904` vs `:1387,1576` / `main.ts:489-491`` | — | ＿＿ |
| S5-11 | S5 | c | 「取消」对进行中的同侧复制是静默空操作：`fs:copy`/`cp -r` 从不注册取消句柄，行永远停在「复制中」 | ``sftp.ts:1027-1039` + `ssh-manager.ts:1907-1910`` | — | ＿＿ |
| S5-12 | S5 | c | `expandRemoteDir` 读实时 `sshSessionId` 而非批次钉住的 sid，且宽 catch 把「会话已断」报成「目录不可读」 | ``sftp.ts:568-572` vs `:666`` | — | ＿＿ |
| S5-13 | S5 | a | 服务器提供的目录项名未净化即拼进本地写路径：`relPath` 来自 `e.name`，`fs:pathJoin` 会归一化掉 `..`，`assertSafePath` 是敏感目录黑名单而非 jail | ``sftp.ts:574,695` → `main.ts:778-780,1293`` | — | ＿＿ |
| S7-01 | S7 | c | 被成本熔断/迭代上限/循环检测/求助终止的 run 一律记 `completed` —— done 载荷里的 `state` 字段被整段丢弃 | `anchor `schedule_engine.py:246` + `:415-420` ↔ `driver.py:379-382,398-426,503-510` + `driver.py:868`` | ✅ | ＿＿ |
| S7-02 | S7 | a | 未指定目标主机的任务每个远程工具都 fail-closed，run 却记 `completed`，且 system prompt 从不告诉模型「这次没有会话」 | `anchor `schedule_engine.py:320-329,398,545-559` + `ops_common.py:172-173`` | ✅ | ＿＿ |
| S7-03 | S7 | b | `trigger()` 吞掉 next_run 推进失败仍继续起跑 ⇒ 同一任务每个 tick 反复触发，无上限、无自愈 | `anchor `schedule_engine.py:157-162` + `:124-131`` | ✅ | ＿＿ |
| S7-04 | S7 | a | 后端重启把 run 永久卡在 `running`：编辑/立即执行按钮被置灰，且启动序列里没有跨表 stale 对账 | `anchor `schedule_engine.py:239-242,253-279` + anchor `main.py:100-126` + `SchedulesView.vue:31-32,407-409`` | ✅ | ＿＿ |
| S7-05 | S7 | a | 开机首 tick 把所有到期任务无限并发扇出（无全局并发上限），叠 S0-05 时连用户自己的终端都连不上 | `anchor `schedule_engine.py:117-135,156,168`` | ✅ | ＿＿ |
| S7-06 | S7 | c | 一次 `schedule_op` 的「始终允许」= 永久放行该工具全部 action（授权键是裸工具名），含删除用户手工配的任务；headless run 同样可用 | ``gate.py:305-311,327,343-350,696-700` + `ops_schedule.py:19,187-194,298-310` + anchor `schedule_engine.py:308-309`` | ✅ | ＿＿ |
| S7-07 | S7 | a | 「立即执行」谎报：`POST /schedules/{id}/run` 恒回 `{ok:true}`，桥完全断开时也一样 | ``routes/schedules.py:141-153` + anchor `:158-175`` | ✅ | ＿＿ |
| S7-08 | S7 | d | 后台终态通知实际从不发出：去重只看状态字符串（第 2 次及以后一律静默）+ 轮询只在页面 active 时跑 | ``SchedulesView.vue:393-405,413-424,429-437` + `client.ts:1104`` | ✅ | ＿＿ |
| S7-09 | S7 | a | `ops_schedule._create` 硬编码 `target_host: ""` 且 schema 无该参数 ⇒ AI 排的巡检天生跑不到任何机器上（沉默降级，非报错） | ``ops_schedule.py:251` + `:138-180` ↔ anchor `schedule_engine.py:320`` | ✅ | ＿＿ |
| S10-01 | S10 | a | anchor `schedule_engine._resolve_model()` 只读全局 ⊕，而面板 ★ 文案明写「SSH 默认 = 对话/终端/定时任务读的那档」 | `anchor `schedule_engine.py:439` + `:291-293` ↔ anchor `ModelsPanel.vue:7-9`` | ✅ | ＿＿ |
| S10-02 | S10 | a | `GET /settings/models/default` 的兜底链跳过全局档直落 `list[0]` ⇒ 两个面板对「默认模型是哪条」给出不同答案 | `anchor `routes/settings.py:87-92` ↔ `ssh-ai.ts:78-81`、`ChatPanel.vue:132`` | ✅ | ＿＿ |
| S10-03 | S10 | b | `base_url` 无 scheme/凭据校验、不脱敏回传渲染端并原样进日志 —— 而同仓对 proxy URL 三件全做 | `anchor `routes/settings.py:24`、`database.py:1594`、`routes/settings.py:193-197` ↔ `netconfig.py:61-88`、`routes/settings.py:595-627`` | ✅ | ＿＿ |
| S10-04 | S10 | c | `models_config.top_p` 在 anchor 只有终端预测一个读者，Agent 交互/定时/子 Agent 三条主链恒 0.9 | `anchor `llm_client.py:45-54,75-85` ↔ `llm_adapter.py:192,232`；唯一读点 anchor `routes/terminal.py:129,144`` | ✅ | ＿＿ |
| S10-05 | S10 | c | `reasoning_tokens`：类型面声明、后端可写、运行时真消费，但 anchor 面板从不提交 ⇒ 所有模型钉在默认 8192 | `anchor `client.ts:270,292` ↔ `ModelsPanel.vue:367-380,491-502`（form 无此字段）↔ 消费点 anchor `routes/agent.py:826`、`schedule_engine.py:381-386`、`safe_tokens.py:53`` | ✅ | ＿＿ |
| S10-06 | S10 | d | `reasoning_tokens=0` 塌成「思考不设上限」，与其自身 Field 说明相反（0-塌陷族第三例，前次只修了 `None` 那一半） | `anchor `llm_adapter.py:264` ↔ `routes/settings.py:37` 说明、`safe_tokens.py:51-53` 反义、`schedule_engine.py:382-385` 注释` | ✅ | ＿＿ |
| S10-07 | S10 | d | 空 `api_key` 被当成「未配置模型」⇒ 面板默认 provider（自定义/vLLM）建的模型永久拿不到 LLM 自动审批与命令解释 | `anchor `auto_approve.py:106`、`command_explain.py:391` ↔ anchor `ModelsPanel.vue:182,335,372,511`、`routes/settings.py:25`、`llm_adapter.py:82,90`` | ✅ | ＿＿ |
| S10-08 | S10 | d | `_fix_model_name_and_id` 每次 `init_db` 无条件执行，任何含 `/` 的显示名都会与 model_id 互换，且用户改回来下次启动再翻 | `anchor `database.py:519-520`（调用点无 once 闸门）+ `:811-828`` | ✅ | ＿＿ |
| S10-09 | S10 | d | 种子探测的 3s 预算被用户 HTTP 超时替换（read 另有 60s 下限），启动关键路径最长可挂 20 分钟，违反 spec 的「总预算 ≤6s、绝不阻塞启动」 | `anchor `database.py:843` → anchor `netconfig.py:173-184,225-232` + anchor `main.py:74` ↔ spec `:54`` | ✅ | ＿＿ |
| S10-10 | S10 | d | `_probe_first_model` 不发 `Authorization`，而它服务的种子恰恰用得上 `settings.vllm_api_key` ⇒ 有鉴权的端点自动种子静默永久失效 | `anchor `database.py:831-855`（签名无 key、`_c.get(url)` 无 header）↔ `:876,888,903` ↔ `config.py:64`` | ✅ | ＿＿ |

## Minor — 71 条

| ID | 切片 | 维度 | 标题 | 位置 | 核验 | 决定 |
|---|---|---|---|---|---|---|
| S0-02 | S0 | b | 401 不经 CORS 中间件 → 表现为「网络连接失败」并对幂等 GET 白等 ≤8s | ``main.py:204-220` + `client.ts:203-220`` | — | ＿＿ |
| S0-03 | S0 | a | `client.ts` 头注释的浏览器访问场景 2/3 在鉴权引入后已不可用；CORS 仍开放 LAN `:5173` | ``client.ts:4-8` vs `main.py:33-43,167-173`` | — | ＿＿ |
| S0-09 | S0 | a | 桥接 seam 抹平 SSHError 分类，`classify_bridge_error` 对该类恒失效 | ``electron-bridge.ts:146-152`` | — | ＿＿ |
| S0-10 | S0 | c | WS 单向鉴权：客户端不验证对端，`hello-ack.ok` 从不读取 | ``bridge-ws-client.ts:117-133`` | — | ＿＿ |
| S0-11 | S0 | d | `dispatch('ping')` 是死分支，`BridgeOp` 过度声明契约 | ``electron-bridge.ts:34,94-95`` | — | ＿＿ |
| S0-16 | S0 | d | AUTH_PASSPHRASE 无生产者（正则对准 ssh2 从不发的措辞），口令错误被归到 AUTH_KEY | ``ssh-error.ts:39,45`` | — | ＿＿ |
| S0-17 | S0 | d | 4 个 SFTP_ 枚举成员只有枚举无生产者；真实 SFTP 失败走另一套未共享的分类 | ``ssh-error-core.ts:36-39` vs `ssh-manager.ts:38-47`` | — | ＿＿ |
| S0-18 | S0 | b | 重连耗尽丢弃已分类的 SSHError，UI 只能恒说「自动重连失败」 | ``ssh-manager.ts:1072-1084`` | — | ＿＿ |
| S0-19 | S0 | b | SESSION_NOT_FOUND 的 retryable 恒 false，而主进程正为该会话跑 12 次重连 | ``ssh-error-core.ts:54-66` / `DashboardView.vue:13,26`` | — | ＿＿ |
| S0-20 | S0 | a | retryable 与自家文案互相矛盾（SESSION_LIMIT 文案以「后重试」结尾却 false；CONN_REFUSED 反之） | ``ssh-error-core.ts:71,117` vs `:54-66`` | — | ＿＿ |
| S0-21 | S0 | a | testConnection 算出 code 又丢掉，`SSHTestResult` 无 code 字段 | ``ssh-manager.ts:1178-1184` / `:303-307`` | — | ＿＿ |
| S0-22 | S0 | d | HOST_KEY_UNKNOWN 结构性不可达（TOFU 自动接受未知密钥） | ``ssh-manager.ts:485-498` / `ssh-error.ts:58`` | — | ＿＿ |
| S0-23 | S0 | a | `SSHErrorTransport` 形状在两进程各抄一份，回读侧裸 cast 不校验 code 归属 | ``ssh-error.ts:101-107` vs `src/ssh-error-types.ts:22-28,43`` | — | ＿＿ |
| S0-24 | S0 | c | 标注「开发模式才显示」的 detail 与 Electron 内部信封文本无条件渲染到用户 | ``ssh-error.ts:106` → `useSshNavigation.ts:259-264`` | — | ＿＿ |
| S1-03 | S1 | c | `/agent/tool-result` 只按 `tool_id` 认领，既不绑 run 也不回查 `requires_confirm` | ``agent.py:1273-1299` + `pending_delivery.py:104-141`` | — | ＿＿ |
| S1-04 | S1 | c | 不可禁用的 `Sandbox` 底线只查 `shell_exec`，漏 `job_run`——而 `core.py` 明写两者同属命令入口 | ``saddle.py:55-56` vs `core.py:20`` | — | ＿＿ |
| S1-05 | S1 | a | `plan_mode_block` 事件无投影分支亦无消费者，计划模式下的写拦截对用户完全不可见 | ``plan_mode.py:413-423` → `telemetry.py:104-264` → `client.ts:698`` | — | ＿＿ |
| S1-06 | S1 | a | `budget_extended` / `reflection_request` 每 run 上 Wire 无人读；`budget_extended.reason` 投影自一个生产者从不写的键 | ``flow.py:392-409` / `lifecycle.py:143-159` → `telemetry.py:121-122,226-237`` | — | ＿＿ |
| S2-05 | S2 | d | 工具级审批过期只发事件不落审计（命令级落），同一生命周期两种记录口径 | ``gate.py:716-756` vs `guards.py:204-216`` | ◐ | ＿＿ |
| S2-06 | S2 | c | `sudo -S` 口令猜测试探的防线只在串首/分隔符后生效，任何 wrapper 前缀绕过 | ``safety.py:278-292`` | ◐ | ＿＿ |
| S2-07 | S2 | c | 多目标 `tee` 只判第一个文件 ⇒ `tee /tmp/x /root/.ssh/authorized_keys` 从 hardline 降为可批的 dangerous | ``safety.py:955-969`（`:969` 的 break）` | ◐ | ＿＿ |
| S2-08 | S2 | d | `allowlist.txt` 重写会丢掉运维手写的注释与不合约定行 | ``gate.py:154-159` vs `:276-278`` | ◐ | ＿＿ |
| S3-05 | S3 | d | `block_update` 的「仅当前主机」守卫在 run 未绑定主机时整体短路（`and sid`） | ``memory_tools.py:242-252`` | ◐ | ＿＿ |
| S3-06 | S3 | c | `POST /agent/tick/run` 响应里没有 `summary` 键，渲染层与 client JSDoc 都按有它写 ⇒「立即整理」恒 toast「已执行」 | ``routes/agent.py:1865-1868`(anchor) vs `AgentMemoryView.vue:472-480`` | ◐ | ＿＿ |
| S3-07 | S3 | c | 三处同形契约缺口：`MemoryAuditRow.old_content/new_content` 与 `MemoryRow.superseded_by` 已声明已写入但 `src/` 零消费者；`PUT /agent/blocks/{key}` 允许把画像块整块清空（agent 侧的拒空只在 `replace_in_block`） | ``mstore.py:100-101` `:527-555` / `routes/agent.py:1833-1844`(anchor)` | ◐ | ＿＿ |
| S3-08 | S3 | c | `POST /agent/memories` 的 `content` 无长度上限，而 agent 工具侧拒 500+ 并注释「schema 描述与实现必须同源」；超长条目 `estimate_tokens` 超过整份注入额度 ⇒ 被 `retrieve_for_injection` 永久跳过却仍计入 stats 与面板 | ``routes/agent.py:1727-1731`(anchor) vs `memory_tools.py:28-31,139-141`` | ◐ | ＿＿ |
| S4-08 | S4 | a | 补全会话清理跑在 `willReconnect` 提前返回之前，把刻意保留的会话的状态删了 | ``RemoteTerminal.vue:340` vs `:342-347`；`useCompletion.ts:512-518`` | ✅ | ＿＿ |
| S4-09 | S4 | a | `doConnect` 先无条件写会话池、代数校验在其后 ≈一次握手之外（当前 UI 无触发路径） | ``ssh-manager.ts:640-671` vs `:1090-1100`` | ✅ | ＿＿ |
| S4-10 | S4 | d | `qcHeightForAlign` 的不变量测试只用整数基线，亚像素情形只断言返回值不断言不变量 | ``qc-align.ts:33-34,42-53` ↔ `qc-align.test.ts:26-34,42-45`` | ◐ | ＿＿ |
| S5-04 | S5 | a | 停滞时 `resumeSize` 只有 4KB 头部校验，洞在 4KB 之后即完全不可见；`VERIFY_SIZE` 的语义被注释当作全量保证来引用 | ``:1380` `:1455-1466`` | — | ＿＿ |
| S5-05 | S5 | b | 停滞回调注释「渲染层按 STALLED 自动续传」的前提未证（S0-15：`retryable` 自定义属性疑不过 IPC）——注释描述了一条可能是死的腿，而用户手动重试走的是同一条易损路径 | ``:1404-1420` + `sftp.ts:967`` | — | ＿＿ |
| S5-06 | S5 | a | `partStat.size === total` 即判「已完成」直接 rename，未校验内容——与 D12 只防「源缩小」的口径一致，但洞恰好补到同长度时（零填充把 size 顶到 total）会被当作完成 | ``:1441-1443`` | — | ＿＿ |
| S5-14 | S5 | b | 地址栏提交路径时把刚存下的真因丢掉：`toFriendlyMessage(store.localError)` 传的是字符串，`client.ts:84-101` 无 string 分支 ⇒ 恒落 `fallback`。「拒绝访问敏感路径」被说成「路径不存在」 | ``SftpPane.vue:361-368` + `sftp.ts:75-76,253`` | — | ＿＿ |
| S5-15 | S5 | a | `fs:copy` 只对源侧防符号链接，目标侧盲目跟随：目标里已存在的链接可被写入敏感文件（`copyFile(f.src, f.dest)`，`assertSafePath` 只查过两个根） | ``main.ts:706-731` vs `:762-767`` | — | ＿＿ |
| S5-16 | S5 | c | 编辑器保存用实时 props 重推 (path, side, session)，而 `loadFile` 钉的是载入时的三元组；断开/切会话也不关编辑器 | ``SftpFileEditor.vue:174-176` vs `:287-312`` | — | ＿＿ |
| S5-17 | S5 | d | `resumeOffset`/`retryCount` 是 M-18 IPC 契约收缩后的死状态（全仓 6 命中：声明 1 + 写 1 + 注释 4，无任何读方） | ``sftp.ts:28-29,1052`` | — | ＿＿ |
| S5-19 | S5 | a | `zipRemote`/`unzipRemote` 把 `exitCode===null` 当失败并执行删除——归档已生成但通道未回 exit-status 时，用户要的产物被 unlink | ``ssh-manager.ts:1955-1959,1997-2001`（null 可达性见 `:882,901-914`）` | — | ＿＿ |
| S7-10 | S7 | a | anchor 的输出上限把 `max_tokens` 一列当两件事用，`max_output_tokens` 在定时链路全程无人读 | `anchor `schedule_engine.py:296,379-386` ↔ `routes/agent.py:717-722`、`safe_tokens.py:29-31`` | ✅ | ＿＿ |
| S7-11 | S7 | d | PATCH 绕开 POST 的 name 上下限；rationale 注释保护的大字段（`task_prompt`）三处都无上限 | ``routes/schedules.py:26-28` vs `:42-54,63-70` + `ops_schedule.py:237`` | ✅ | ＿＿ |
| S7-12 | S7 | d | 前端每次保存都带全部调度键 ⇒ 只改个错别字也把 interval 任务的下次执行重锚到「现在+一个周期」 | ``SchedulesView.vue:465-482,488-492` + `routes/schedules.py:110,123-125`` | ✅ | ＿＿ |
| S7-13 | S7 | b | interval 以「上次实际触发时刻」为锚、不贴墙钟，且逐周期单向累积漂移永不回收 | `anchor `schedule_engine.py:498-502,115-116`` | ✅ | ＿＿ |
| S7-14 | S7 | c | 同一个 `BridgeError`，只有 probe 那一条带得出 suggestion，其余三条 raise 都因 detail 非空把它顶掉 | ``electron_bridge.py:58` ↔ anchor `schedule_engine.py:332-335,360-363,367-374`` | ✅ | ＿＿ |
| S7-15 | S7 | c | DELETE 前那跳「落定执行历史」必定被 `ON DELETE CASCADE` 抹掉：删掉运行中的任务，界面不留任何「曾有一次被中断」的痕迹 | `anchor `schedule_engine.py:197-200` ↔ `database.py:388,64`` | ✅ | ＿＿ |
| S7-16 | S7 | c | `stop()` 不清 `_executing`/`_deleted_sids`，与 `remove_schedule` 的 done-callback 补偿不对称（当前进程内无第二次 start，不可达） | `anchor `schedule_engine.py:89-94` vs `:186-196,121-122`` | ✅ | ＿＿ |
| S7-17 | S7 | a | cron 报错文案承诺 `` 可用、实际拒绝：模型收到自相矛盾指引会原样重试 | ``ops_schedule.py:37-38,75,80-86,134-137`` | ✅ | ＿＿ |
| S7-18 | S7 | a | 空 `task_prompt` 也能建成任务，无头那次自动换成内置默认巡检词——用户要的和他拿到的不是同一件事 | ``ops_schedule.py:233-254` + anchor `schedule_engine.py:317-319`` | ✅ | ＿＿ |
| S7-19 | S7 | a | anchor 启动注释写「懒启动，仅在存在定时任务时 begin」，实现是无条件 `start()` ⇒ 零任务用户也每 30s 空转一次全表查询 | `anchor `main.py:117-120`` | ✅ | ＿＿ |
| S8-01 | S8 | d | LEGACY #5 确认成立（实测重复块为 6+1 行，不是当年记的「前导三行」） | ``skill_registry.py:169-174 + :176` vs `:201-206 + :208`` | — | ＿＿ |
| S8-02 | S8 | a | `build_skill_catalog` 的 `json.loads` 无兜底，而同文件 `match_skills` 有 → 一行脏 `trigger_words` 令技能目录静默消失 | ``skill_registry.py:185` vs `:116-119`` | — | ＿＿ |
| S8-03 | S8 | b | `ReflectionRein` docstring 声称「路由层只需监听该事件即可触发」，而路由从不监听 `reflection_request` | ``lifecycle.py:93` vs S1-06` | — | ＿＿ |
| S10-11 | S10 | c | `update_model_config` 支持写 `is_default`，`UpdateModelRequest` 却没这字段 ⇒ 死分支 + `PATCH {"is_default":true}` 得 200 且什么都没改 | `anchor `database.py:1701,1735-1738` vs anchor `routes/settings.py:42-57`` | ✅ | ＿＿ |
| S10-12 | S10 | c | `sort_order` 后端可写且列表按它排序，前端类型面与面板都没有它 ⇒ 顺序用户不可控，而 `GET /default` 的 `list[0]` 兜底正按这个不可控顺序选人 | `anchor `routes/settings.py:56`、`database.py:1590,1662-1663` ↔ anchor `client.ts:280-295`` | ✅ | ＿＿ |
| S10-13 | S10 | b | 测试连接端点把上游响应体前 200 字节原样回传渲染端并写日志 —— 同一顾虑在 `available` 列表上堵了，在 body 上没堵 | `anchor `routes/settings.py:211`（注释）vs `:242,246,251` ↔ anchor `stores/settings.ts:485` → `ModelsPanel.vue:608`` | ✅ | ＿＿ |
| S10-14 | S10 | b | `api_key` 没有任何「清除」途径：后端跳过空串、面板不发空串，两者互指对方 | `anchor `database.py:1708` ↔ anchor `ModelsPanel.vue:391,509-515`` | ✅ | ＿＿ |
| S10-15 | S10 | b | 掩码识别靠 `""` 子串哨兵：真实密钥含该子串则永不可写回（静默 no-op + 成功 toast） | `anchor `database.py:1712` ↔ `_mask_api_key:1542-1544`` | ✅ | ＿＿ |
| S10-16 | S10 | a | `_DEFAULT_COLUMNS` 不是单一来源：tier→列映射另有 4 处独立实现，其中 anchor 读侧对 `"global"` 反向（落进 SSH 列） | `anchor `database.py:1812` vs `:1768`（表）+ `:1735-1738`、`:1669/1681`、`routes/settings.py:77`` | ✅ | ＿＿ |
| S10-17 | S10 | a | `get_default_model` 用 `LIMIT 1` 无 `ORDER BY`，双默认时读数不确定，而 `list_models_config` 的排序键是 `sort_order, created_at` | `anchor `database.py:1814` vs `:1590`；对照 `:1576`、`:1619` 两处都写了 `ORDER BY rowid LIMIT 1`` | ✅ | ＿＿ |
| S10-18 | S10 | a | 后处理头图兜底只读全局档，且三条分支都以字面量 `"Qwen3-VL"` 收尾 —— 那是本 spec 明确删除的旧硬编码种子名 | `anchor `post_processor.py:921-929` ↔ spec `:11-13`` | ✅ | ＿＿ |
| S10-19 | S10 | d | `create_model_config` 路由把任何异常折成 400 + `str(e)`：「database is locked」这类服务端故障显示为输入错误 | `anchor `routes/settings.py:104-125` ↔ 同文件 `:294-297`、`:562-564`（都是 500 + 中文说明）` | ✅ | ＿＿ |
| S10-20 | S10 | d | `netconfig.TIMEOUT_MIN = 5` 后端零消费者（写入区间一律 low=0），而前端有个同名常量是 0 且真在驱动 UI | `anchor `netconfig.py:34` ↔ anchor `database.py:2142-2143`、`routes/settings.py:488` ↔ anchor `useOpsSettings.ts:22,69,90,161` + `OpsPanel.vue:53,71`` | ✅ | ＿＿ |
| S10-21 | S10 | d | 测试连接报错「连接超时 (Ns)」里的 N 不是真正生效的上限（read 被抬到 ≥60s） | `anchor `routes/settings.py:228-237` ↔ anchor `netconfig.py:173-184`` | ✅ | ＿＿ |
| S10-22 | S10 | d | `_resolve_model_pk` 的双键回退在 `get_model_config` 又抄一遍；`model_id` 无唯一约束，重名时静默命中 rowid 最小者 | `anchor `database.py:1558-1581` vs `:1613-1621`、schema `:213-233`` | ✅ | ＿＿ |
| S10-23 | S10 | b | spec 组件图写 `_seed_models(conn)`/`_insert_model`，代码是 `seed_models_async`/`_insert_seed_model`；spec 未随 P2-E9 更新 | ``specs/2026-09-02-…:44-47` ↔ anchor `database.py:858,920`` | ✅ | ＿＿ |
| S11-01 | S11 | a | `query`/`count` 把 DB 异常吞成空列表/0 ⇒ 审计页在故障时显示「没有记录」而非报错，与轮转后清空观感不可区分 | ``audit.py:268-271,288-291`` | ✅ | ＿＿ |
| S11-02 | S11 | a | `_canonical` 以裸 ` | `` 连接 12 字段且不转义字段内的 `` | `，`hash` 上的 UNIQUE 约束使相邻字段跨界移位可致 INSERT 冲突 ⇒ 该条审计被静默丢弃 | ＿＿ |
| S11-04 | S11 | d | 封缄记录自身落 `environment="test"`（`_DEFAULT_ENV`），对生产主机做轮转后，按 prod 过滤审计看不到这条「谁清的库」 | ``audit.py:398-404` vs `:30`` | ✅ | ＿＿ |
| S11-05 | S11 | b | `routes/audit.py` 四个端点无任何路由级测试（仅 `test_audit_coverage`/`test_audit_rotate` 测 `agent.audit` 本体），`/audit/export` 的 CSV 构造（空表时 `fieldnames=["id"]` 只出一个表头）无钉 | ``routes/audit.py:25,66-84`` | ✅ | ＿＿ |
| S12-02 | S12 | b | 「监控目标会话不可用」在主进程正在为该会话重连时仍被标为不可重试，UI 于是隐去「连接恢复后自动重连」的预期 | ``ssh-error-core.ts:54-66`（`SESSION_NOT_FOUND` 不在表内）→ `server-monitor.ts:121` → `DashboardView.vue:13,26`` | ◐（同 S0 的 S0b-7，此处为唯一条目，勿重复计） | ＿＿ |
| S12-03 | S12 | d | 编排层与展示层零测试：`GpuNvtopPanel.vue`(662) 与 `DashboardView.vue`(582) 无任何用例，纯核心（`server-monitor-core.ts` 382，31 例）覆盖了解析却覆盖不到轮询编排/取消/失败停机三件套 | ``src/stores/server-monitor.ts`(247，31 例经 core 间接)` | ✅ | ＿＿ |
| S13-01 | S13 | d | `stores/settings.ts` 的 PUT 发送点只有 3 处（post-process / updateModelConfig / setDefaultModel），其余设置全走「本地 localStorage + 由具体面板各自显式 PUT」——双轨写侧没有单一收敛点，新增一个后端设置项时没有任何机制提醒要接上 | `anchor `src/stores/settings.ts:328,430,462`` | ✅ | ＿＿ |
| S13-02 | S13 | b | `MemoryPanel.vue:59` 注释仍写「PUT 是全字段覆盖」，与 a8e588c 统一后的「不传 = 不改」相反（S3 亦独立记为文档漂移，此处为唯一条目）。无行为后果：store 恒发全六键 | ``src/components/settings/panels/MemoryPanel.vue:59`` | ✅（S3 交叉） | ＿＿ |

## Tradeoff — 4 条

| ID | 切片 | 维度 | 标题 | 位置 | 核验 | 决定 |
|---|---|---|---|---|---|---|
| S2-09 | S2 | c | 「始终允许」的授权键无主机维度：A 机上的一次「始终允许」永久命中 B 机同一命令 | ``gate.py:288-311,96,349,387`` | ◐（键构造与查找点 subagent 全列，我核了 TTL 与键形状同源描述） | ＿＿ |
| S5-18 | S5 | d | 四条解压路径（`fs:unzipLocal`、`sftpUnzipRemote` 的 unzip 与 python 两支）无自带 zip-slip 约束，安全性外包给外部工具；而同组 `fs:zipLocal` 自带 fail-close | ``main.ts:853-868` / `ssh-manager.ts:2011-2062` vs `main.ts:811-832`` | — | ＿＿ |
| S10-24 | S10 | c | `routes/agent.py:34 from routes.terminal import _get_model_config` 的跨 router 私有依赖：低危但零收益，且本仓已有 `safe_tokens.py` 专门为此类耦合建过解 | `anchor `routes/agent.py:34` ↔ anchor `routes/terminal.py:29-31` ↔ `safe_tokens.py:1-5` docstring` | ✅ | ＿＿ |
| S11-03 | S11 | c | `command_raw` 明文入库并经 `/audit/export` 整表 CSV 落盘：命令行内联口令（`mysql -u root -pSECRET`、`curl -u`）随审计证据一同外带 | ``audit.py:63,154-155` + `routes/audit.py:66-84`` | ✅ | ＿＿ |

## Register — 2 条

| ID | 切片 | 维度 | 标题 | 位置 | 核验 | 决定 |
|---|---|---|---|---|---|---|
| S11-06 | S11 | b | 规格轴：审计覆盖声明与 reality 的差，归口 S2 的 D4（批量 `ssh:exec` 零审计）与 S2-06（审批记录缺 run_id/environment），本切片不重复计条 | ``docs/security-boundary.md:26,138-139,150-156`` | ✅（引用侧我看过） | ＿＿ |
| S12-01 | S12 | — | 记忆条目过时：`NvGauge` 的「66/90 红测试欠账（工作区已修未提交）」已结清并入库——阈值实现与用例现同为 60/80，28 条全绿 | ``NvGauge.ts:5,36` vs `NvGauge.test.ts:111-116`` | ✅ | ＿＿ |

## 尚未落地的切片

S6、S9 —— 审查路仍在进行；落地后重跑本表生成脚本即可，本表计数会随之更新。
