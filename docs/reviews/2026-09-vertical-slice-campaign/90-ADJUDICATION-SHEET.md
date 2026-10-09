# 裁决总表 · 功能纵向切片审查战役（S0→S13）

锚点 `a8e588c` · 生成时间 2026-09-29 · 模式：**只报告，代码零改动**

总计 **193** 条：Critical **3** · Major **68** · Minor **115** · Tradeoff **5** · Rejected **0** · 登记/交叉引用 **2**。
本表由脚本从各切片报告的发现表机械汇总（列名按表头识别，未做人工重排）。

**核验列含义**：✅ = 我本人打开原文逐行证实；◐ = 采信审查路引用、我未二次打开（裁决前建议自行复核该行）；— = 该切片表未单列。

**决定列**：请逐行填 `采纳 / 驳回 / 延后`。留空 = 未裁决。**本表生成时不含已实施的修复，但役中及役后已批量落库 —— 当前落库状态与真正待裁决的 16 条见下方「落库状态」节，勿按本表逐行再开工单。**

**并行提交的影响（务必先读这一段）**：役中并行会话提交了 `9634dee fix(settings): 打通模型采样参数链路并修正设置项语义`。表内标题带 **【9634dee 已修】** 的 7 行（S9-04 / S9-14 / S9-15 / S10-01 / S10-04 / S10-05 / S10-20）已被那次提交顺带解决 —— 裁决时只需确认要不要补回归测试，不必再开工单。反过来，**S10-06 因那次提交从「需要 curl」升级为「面板一键可踩」**（它把 `reasoning_tokens` 做成 `min="0"` 的可改项，并把 0 注释成「跟随输出预算」，而运行时把 0 当「未设」⇒ 无上限），建议优先处理。三条 Critical（S5-01 / S2-01 / S6-01）与其余全部 Major 均**未被该提交触及**。

**收尾闸门实测（`9634dee` 之后，工作树干净）**：`npm run typecheck` RC=0（两个 config 零错误）· `npx vitest run` RC=0（18 文件 / 231 用例）· `pytest -k "round22 or round23 or model_default or http_env"` RC=0（93 passed）· `pytest -k "schedule or audit or seed or clarify or plan_mode"` RC=0（212 passed）。役中那些 `lock hazard, not executed` 标注的原因是并发会话持锁，用例本身是绿的。

## 落库状态（2026-10-09 补，取代本文件开头「本表不含任何已实施的修复」）

战役里的 Major **全部已修并推送**（3 条 Critical 在 `9e104bb`；其余按切片批量落库，最后五连是
`d7e9efb` S11-01/05、`210afbe` S1-01、`d2abb83` S11-04、`a3f3510` S1-03、`7719086` S7-18+S5-16）。
Minor 中可单侧修的也已一并落库。**下面这张短表才是真正等你的东西** —— 每条都是一个只有你能定的
产品/契约取向，不是待查的 bug；填「采纳」我再开工单，填「驳回/延后」我把它登记进
`docs/security-boundary.md` 或就地注释。

| 待裁决 | 要决定的是什么（一句话） | 关联 |
|---|---|---|
| S0-01 | 「清除代理」后重启该回落到 env 默认，还是用户的否决应当压过 env | `test_proxy_env_precedence_s001_disputed.py` 钉着现状 |
| S2-04 environment 半条 | 给 `agent_runs` 加 environment 列（新能力），还是接受审批端点只按 run_id 求值 | run_id 半条已做完 |
| S1-01 后半 | 断连/中止的 run 要不要也跑后处理（标题/记忆/技能）—— 现在刻意不跑，半截答案喂记忆库是污染 | `persist_interrupted_run` |
| S1-03 强度 | `confirmed` 只是调用方主张；要不要改成服务端一次性 token（真证明，代价是交付协议加一跳） | `confirmed_by_user` |
| S11-02 | 审计 canonical 用裸 `\|` 连接 12 字段：接受极低概率的边界移位，还是做编码版本化 + 历史链迁移 | 改编码 = 重算全部历史 hash |
| S11-03 | `command_raw` 明文随导出/归档外带：出口加掩码，还是在文档里登记为已接受取舍 | 等保证据链要求明文 |
| S7-15 | 删除运行中的定时任务时 `ON DELETE CASCADE` 抹掉那次执行历史：界面不留「曾有一次被中断」的痕迹 | 改 FK 或留 tombstone |
| S7-19 | 启动注释写「懒启动」而实现是无条件 `start()`：改注释，还是真做懒启动（给启动加一次查询判定） | 并行会话正在改 `backend/main.py` |
| S9-20 | 关掉 AI 面板即丢 posture/审批模式/草稿/附件/已选技能五项局部态：提升到 store（新能力）还是接受 | 后端 `workflowPhase` 仍在跑，两根轴会不一致 |
| S0-07 | 回滚失败被两层吞掉：要让部分回滚失败可见，得先改 `apply_reverse` 的返回契约（现在分不清「合法跳过」与「没做成」） | 涉及 contracts.py + saddle/stirrups/driver |
| S3-03 | 记忆的主机维度用进程级临时 `ssh-N`：换成连接档案 id = 跨层身份变更 + 存量 `host:{id}` 行迁移 | 不是改一行 retriever |
| S3-09 | 换 embedding 模型时现在 DROP `memories_vec`：改成全量重嵌入是功能，不是修复 | 数据侧成本 |
| S13-01 | 设置写侧「localStorage + 面板各自 PUT」双轨无收敛点：要不要加单一收敛层 | 组织问题，无用户可见错误 |
| S0-08 后半 | 遗留库迁移要不要排到杀孤儿之前（会给启动加一段真实阻塞，并改掉 S0-04 已落地的时序契约） | 整族搬迁已落库 |
| S9-16 | 上传 `accept` 与 Python 侧 `ALLOWED_UPLOAD_EXTS` 同源：跨语言单一来源要新增端点或常量表 | 我没单方面定 |
| S7-06 所有权 | AI 能否取消用户手工配的定时任务：现在只收紧了授权键粒度与 prod 升级，没加所有权判断 | 产品政策 |
| S1-06 后半 | `budget_extended` / `reflection_request` 两类事件在渲染层**零消费者**：投影已改成只搬生产者真写的键（本轮已修，`test_trace_projection_keys_s106.py` 钉住），剩下要定的是**删掉这族事件**，还是接上 UI 让用户看得见「预算被延长」「反思已排队」 | producer `flow.py:393` / `lifecycle.py:157`，`src`+`shared` grep 零命中 |

已在 HEAD 复核确认**被并行会话顺带解决、无需裁决**的：S9-01/S9-02/S10-02、S5-13、S9-21、
S13-02（MemoryPanel 注释已改对）、S9-22 的 `ADD_ATTR: ['class']` 空操作半条。

## 五条历史遗留的重判（本轮任务书要求并入对应切片）

| 遗留 | 重判结论 | 所在切片 | 关联条目 |
|---|---|---|---|
| #1 SFTP 停滞续传零洞静默损坏 | **成立，Critical**；并新增第二条触发路径（主进程被杀/通道硬断 ⇒ 截断代码不可达 ⇒ 跨重启续传同样踩洞） | S5 | S5-01 |
| #2 plan/spec/goal_write 在 plan 模式永拒 | **成立**（端到端：注册序、无 bound_command 豁免、UI 可达、拒绝文案自带矛盾指令、无测试钉结局） | S2 | LEGACY #2 节 |
| #3 reconcile 部分失败重跑重复入库 | **成立**，且当年的硬修方向被**证伪**（mstore 写经 `asyncio.to_thread` 落在不同线程本地连接上，无法罩进一个事务）；另找到 3 条更窄的 seam | S3 | LEGACY #3 节 |
| #4 `resolvePermission` 恒 true ⇒「已拒绝」分支可达性 | **成立**：生产调用点唯一且传 true，DB/trace 回放侧亦无 `granted=false` 生产者 ⇒ 「已拒绝」是死 UI | S2 | LEGACY #4 节 |
| #5 `skill_registry` 前导三行重复 | **成立**，实测为逐字相同的 **7 行**（另含重复的 DB 禁用清单读取） | S8 | S8-01 |

## Critical — 3 条

| ID | 切片 | 维度 | 标题 | 位置 | 核验 | 决定 |
|---|---|---|---|---|---|---|
| S2-01 | S2 | c | 自动审批可推翻用户的显式「拒绝」：`PermissionGate` 调裁决器前不查 `is_denied`，而 `FilesystemGuard` 查 | ``gate.py:707-711` vs `guards.py:181-188`` | ✅ | ＿＿ |
| S5-01 | S5 | a | LEGACY #1 确认成立：上传是 8 路并发绝对偏移写，`.part.size` 必然越过连续边界，而停滞路径故意跳过截断 → 续传跳过零填充洞 → rename 出静默损坏文件 | ``ssh-manager.ts:1525` + `:1549` + `:1466`` | — | ＿＿ |
| S6-01 | S6 | a | `readCurrentLine` 用视口相对行号去索引绝对行存储 ⇒ 补全读到的是游标上方 `ybase` 行之前的历史输出，并把候选直接写进活动 shell | ``useCompletion.ts:168-176`（消费点 `useCompletionPopup.ts:59,70,133,149`）` | ✅ | ＿＿ |

## Major — 68 条

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
| S3-09 | S3 | b | spec §4.2:112/§5:178 要求「维度变更触发全量重嵌入」，实现是直接 DROP `memories_vec` ⇒ 换 embedding 模型后只有新记忆有可用向量，且不可恢复 | ``vec.ensure_vec_table` ↔ `mstore.py:97`（全仓 grep 重嵌入仅命中这句注释）` | ◐ | ＿＿ |
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
| S6-02 | S6 | c | `job_run` 在 await 之前预留 job_id、之后才登记 ⇒ 两个并发 run 撞同一 id 与同一远程目录（`>` 截断前一份 out.log、覆盖 run.sh/rc、丢 pid 句柄） | ``ops_job.py:563` → `:583-587`（`_next_id:118-128`、`add:130-132`）` | ✅ | ＿＿ |
| S6-03 | S6 | c | `job_kill` 用 `kill -0 <leader pid>` 验证终止而非进程组 ⇒ 自我 daemon 化的命令被报「已终止」而实际仍在跑，SIGKILL 连发都没发 | ``ops_job.py:297-305` + `:418-431` + `:836-841`` | ✅ | ＿＿ |
| S6-04 | S6 | c | `out_offset` 按 UTF-16/有损解码后的字符串长度推进而非远程字节数 ⇒ 非 UTF-8 或跨包多字节字符的日志永久丢字节，且漂移被落盘 | ``ops_job.py:410` ↔ 该文件自述不变量 `:377-384` ↔ `ssh-manager.ts:890-899`` | ✅ | ＿＿ |
| S6-05 | S6 | c | 复用 id 的新作业继承上一份 `rc` 文件 ⇒ 刚启动的作业被立刻判「已完成」并拿到死作业的退出码，随后 `job_kill` 拒止 | ``ops_job.py:270-283`（从不删 rc）+ `:118-128` + anchor `database.py:2786-2799`` | ✅ | ＿＿ |
| S6-06 | S6 | b | `/terminal/predict` 没有任何可达调用者：AI 预测闸门要求「空前缀」，而 `parseInput` 在到达该状态前就返回 null ⇒ 那条 15s 后端 LLM 腿与「AI 预测」开关都是空转 | ``useCompletion.ts:324` + `:230` ↔ `useCompletionPopup.ts:70-76`` | ✅ | ＿＿ |
| S6-07 | S6 | d | `network_probe` 的 `[本地监听]` 段落永远填不上：判据整串不含 `[exit_code=`，而 `run_headless` 恒追加该哨兵 | ``ops_obs.py:238-246` ↔ `ops_common.py:143-144` ↔ `electron-bridge.ts:113`；对照同文件 `:161` 的逐行正确写法` | ✅ | ＿＿ |
| S6-08 | S6 | d | `web_fetch` 没有总时限，`meta.timeout` 无人执行：httpx 的标量 timeout 是每次套接字操作，慢滴流服务端可把一个 ≤10MB 响应拖成分钟级并钉住整个 run | ``web_tools.py:395` + `:463-496` + `:359`（meta 无 timeout）↔ anchor `driver.py:539`（`saddle.execute` 外无 `wait_for`）` | ✅ | ＿＿ |
| S7-01 | S7 | c | 被成本熔断/迭代上限/循环检测/求助终止的 run 一律记 `completed` —— done 载荷里的 `state` 字段被整段丢弃 | `anchor `schedule_engine.py:246` + `:415-420` ↔ `driver.py:379-382,398-426,503-510` + `driver.py:868`` | ✅ | ＿＿ |
| S7-02 | S7 | a | 未指定目标主机的任务每个远程工具都 fail-closed，run 却记 `completed`，且 system prompt 从不告诉模型「这次没有会话」 | `anchor `schedule_engine.py:320-329,398,545-559` + `ops_common.py:172-173`` | ✅ | ＿＿ |
| S7-03 | S7 | b | `trigger()` 吞掉 next_run 推进失败仍继续起跑 ⇒ 同一任务每个 tick 反复触发，无上限、无自愈 | `anchor `schedule_engine.py:157-162` + `:124-131`` | ✅ | ＿＿ |
| S7-04 | S7 | a | 后端重启把 run 永久卡在 `running`：编辑/立即执行按钮被置灰，且启动序列里没有跨表 stale 对账 | `anchor `schedule_engine.py:239-242,253-279` + anchor `main.py:100-126` + `SchedulesView.vue:31-32,407-409`` | ✅ | ＿＿ |
| S7-05 | S7 | a | 开机首 tick 把所有到期任务无限并发扇出（无全局并发上限），叠 S0-05 时连用户自己的终端都连不上 | `anchor `schedule_engine.py:117-135,156,168`` | ✅ | ＿＿ |
| S7-06 | S7 | c | 一次 `schedule_op` 的「始终允许」= 永久放行该工具全部 action（授权键是裸工具名），含删除用户手工配的任务；headless run 同样可用 | ``gate.py:305-311,327,343-350,696-700` + `ops_schedule.py:19,187-194,298-310` + anchor `schedule_engine.py:308-309`` | ✅ | ＿＿ |
| S7-07 | S7 | a | 「立即执行」谎报：`POST /schedules/{id}/run` 恒回 `{ok:true}`，桥完全断开时也一样 | ``routes/schedules.py:141-153` + anchor `:158-175`` | ✅ | ＿＿ |
| S7-08 | S7 | d | 后台终态通知实际从不发出：去重只看状态字符串（第 2 次及以后一律静默）+ 轮询只在页面 active 时跑 | ``SchedulesView.vue:393-405,413-424,429-437` + `client.ts:1104`` | ✅ | ＿＿ |
| S7-09 | S7 | a | `ops_schedule._create` 硬编码 `target_host: ""` 且 schema 无该参数 ⇒ AI 排的巡检天生跑不到任何机器上（沉默降级，非报错） | ``ops_schedule.py:251` + `:138-180` ↔ anchor `schedule_engine.py:320`` | ✅ | ＿＿ |
| S9-01 | S9 | a | DOMPurify 只加了 `ADD_ATTR`、没禁任何标签/属性 ⇒ `<style>` 与 `style=` 默认放行，远端输出可控的 markdown 拿到全文档级 CSS | ``src/utils/markdown.ts:75-77` → 落点 `MarkdownStream.vue:2`(anchor `v-html`)` | ✅ | ＿＿ |
| S9-02 | S9 | a | markdown 链接无人拦截，而 `will-navigate` 在生产放行任意 `file:` URL，`assertTrustedSender` 又是身份判定而非源判定 ⇒ 本地文档接管整套主进程 IPC 面 | ``MarkdownStream.vue:97-110`(anchor) + `electron/main.ts:191-206` + `:38-45` + `preload.ts:8`` | ✅ | ＿＿ |
| S9-03 | S9 | c | 消息历史静默截断到 50 条，且截断后的窗口就此成为本地权威 —— 后端分页能力完好却无人用 | `anchor `client.ts:410-411` ↔ 唯一调用点 anchor `ssh-ai.ts:120-125` ↔ `useChatPersistence.ts:104,118-124`` | ✅ | ＿＿ |
| S9-04 | S9 | b | 【9634dee 已修】anchor 启动时从库里读出的后处理配置被丢弃：四个开关在重启后全部失效，直到有人 PUT 一次设置端点 | `anchor `post_processor.py:1097-1101` ↔ `:992` ↔ `:916` ↔ anchor `main.py:90-93`` | ✅ | ＿＿ |
| S9-05 | S9 | b | 「第一条消息隐式创建的会话」其标题生成在结构上不可能运行（LLM 与兜底两条路都被门挡住） | `anchor `ssh-ai.ts:523-531` + `:557-559` ↔ `useChatPersistence.ts:256-266` ↔ 门在 anchor `post_processor.py:499-505` + `:36`` | ✅ | ＿＿ |
| S9-06 | S9 | d | 会话删除是不可逆的（消息 + 附件目录 `rmtree`），却是全应用唯一没有二次确认的用户数据删除入口 | ``HistoryPanel.vue:27-34` → anchor `ssh-ai.ts:139-141` → `useChatPersistence.ts:235-254` → `routes/chat.py:145-163`` | ✅ | ＿＿ |
| S10-01 | S10 | a | 【9634dee 已修】anchor `schedule_engine._resolve_model()` 只读全局 ⊕，而面板 ★ 文案明写「SSH 默认 = 对话/终端/定时任务读的那档」 | `anchor `schedule_engine.py:439` + `:291-293` ↔ anchor `ModelsPanel.vue:7-9`` | ✅ | ＿＿ |
| S10-02 | S10 | a | `GET /settings/models/default` 的兜底链跳过全局档直落 `list[0]` ⇒ 两个面板对「默认模型是哪条」给出不同答案 | `anchor `routes/settings.py:87-92` ↔ `ssh-ai.ts:78-81`、`ChatPanel.vue:132`` | ✅ | ＿＿ |
| S10-03 | S10 | b | `base_url` 无 scheme/凭据校验、不脱敏回传渲染端并原样进日志 —— 而同仓对 proxy URL 三件全做 | `anchor `routes/settings.py:24`、`database.py:1594`、`routes/settings.py:193-197` ↔ `netconfig.py:61-88`、`routes/settings.py:595-627`` | ✅ | ＿＿ |
| S10-04 | S10 | c | 【9634dee 已修】`models_config.top_p` 在 anchor 只有终端预测一个读者，Agent 交互/定时/子 Agent 三条主链恒 0.9 | `anchor `llm_client.py:45-54,75-85` ↔ `llm_adapter.py:192,232`；唯一读点 anchor `routes/terminal.py:129,144`` | ✅ | ＿＿ |
| S10-05 | S10 | c | 【9634dee 已修】`reasoning_tokens`：类型面声明、后端可写、运行时真消费，但 anchor 面板从不提交 ⇒ 所有模型钉在默认 8192 | `anchor `client.ts:270,292` ↔ `ModelsPanel.vue:367-380,491-502`（form 无此字段）↔ 消费点 anchor `routes/agent.py:826`、`schedule_engine.py:381-386`、`safe_tokens.py:53`` | ✅ | ＿＿ |
| S10-06 | S10 | d | 【⚠️ 9634dee 把本条从「需 curl」升级为「面板一键可达」】`reasoning_tokens=0` 塌成「思考不设上限」，与其自身 Field 说明相反（0-塌陷族第三例，前次只修了 `None` 那一半） | `anchor `llm_adapter.py:264` ↔ `routes/settings.py:37` 说明、`safe_tokens.py:51-53` 反义、`schedule_engine.py:382-385` 注释` | ✅ | ＿＿ |
| S10-07 | S10 | d | 空 `api_key` 被当成「未配置模型」⇒ 面板默认 provider（自定义/vLLM）建的模型永久拿不到 LLM 自动审批与命令解释 | `anchor `auto_approve.py:106`、`command_explain.py:391` ↔ anchor `ModelsPanel.vue:182,335,372,511`、`routes/settings.py:25`、`llm_adapter.py:82,90`` | ✅ | ＿＿ |
| S10-08 | S10 | d | `_fix_model_name_and_id` 每次 `init_db` 无条件执行，任何含 `/` 的显示名都会与 model_id 互换，且用户改回来下次启动再翻 | `anchor `database.py:519-520`（调用点无 once 闸门）+ `:811-828`` | ✅ | ＿＿ |
| S10-09 | S10 | d | 种子探测的 3s 预算被用户 HTTP 超时替换（read 另有 60s 下限），启动关键路径最长可挂 20 分钟，违反 spec 的「总预算 ≤6s、绝不阻塞启动」 | `anchor `database.py:843` → anchor `netconfig.py:173-184,225-232` + anchor `main.py:74` ↔ spec `:54`` | ✅ | ＿＿ |
| S10-10 | S10 | d | `_probe_first_model` 不发 `Authorization`，而它服务的种子恰恰用得上 `settings.vllm_api_key` ⇒ 有鉴权的端点自动种子静默永久失效 | `anchor `database.py:831-855`（签名无 key、`_c.get(url)` 无 header）↔ `:876,888,903` ↔ `config.py:64`` | ✅ | ＿＿ |

## Minor — 115 条

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
| S3-10 | S3 | b | scope 体系缺写源：`host:` 无任何产出方（episodic 硬编 `global`、抽取丢弃 LLM 返回的 scope 字段而 prompt 仍在要它），`run:` 可被 UI 写入却永不被 `context_scopes` 取回，spec 列的 `project` 全仓不存在 | ``reconcile.py`（episodic 写入）+ `mstore.context_scopes` ↔ spec §4.1:101` | ◐ | ＿＿ |
| S3-11 | S3 | b | spec §11:272 的非法 JSON 四项兜底（重试 1 次、`importance×0.8`、`malformed-fallback` 审计、可见降级）全部未实现 ⇒ `_call_llm` 解析失败直接返回 None | ``reconcile._call_llm` ↔ spec §11:272` | ◐ | ＿＿ |
| S3-12 | S3 | b | FTS 建表口径与实现不同：spec §4.2:106 写的是 SQL 侧分词，实现是应用侧 2-gram | ``mstore.py` FTS 建表 ↔ spec §4.2:106` | ◐ | ＿＿ |
| S3-13 | S3 | b | spec §9:230 的「列表按 source 筛选 + 取代链展开」与 §9:239 的「embedding 探活」在 UI 均无 | ``AgentMemoryView.vue` ↔ spec §9:230/§9:239` | ◐ | ＿＿ |
| S3-14 | S3 | b | 审计动词表（spec §4.4:150）与渲染器的 `PIN/EDIT` 不匹配，且 `set_pinned` 根本不落审计 | ``mstore.set_pinned` ↔ 审计动词表 ↔ `AgentMemoryView.vue` 渲染分支` | ◐ | ＿＿ |
| S3-15 | S3 | b | `rejected` 状态后端从不写入，UI 却有该筛选项 ⇒ 恒空列表（一个用户永远不会命中，因而永远不会被报告的筛选项） | ``mstore` 状态写入集 ↔ `AgentMemoryView.vue` 筛选下拉 ↔ spec §4.1:80` | ✅ | ＿＿ |
| S3-16 | S3 | b | `MemoryPanel.vue:59` 注释仍写「PUT 是全字段覆盖」，与 `a8e588c` 统一后的「不传=不改」相反（store 恒发全键 ⇒ 无行为后果，纯文档） | ``MemoryPanel.vue:59` ↔ `client.ts` 的 PUT 形状` | ◐ | ＿＿ |
| S3-17 | S3 | b | 反向缺口（代码有、文档无）：第三档 `_overlap_hits` 词面重叠召回、按连接缓存 sqlite-vec 可用性 —— 两处均未在 spec 登记 | ``mstore.py:312-327`、`vec.py:37-55`` | ✅ | ＿＿ |
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
| S6-09 | S6 | b | predict 请求恒传 `cwd: ''`，而端点与它的 system prompt 都在消费该字段 | ``useCompletion.ts:336-342` ↔ anchor `routes/terminal.py:48,101-102`、prompt 规则 6（`:71`）` | ✅ | ＿＿ |
| S6-10 | S6 | b | predict 的 `platform` 由应用 UA 推导 ⇒ 远端 Linux 会话被声明成 "windows"，且忽略 `queryCompletion` 早已知道的 `side` | ``useCompletion.ts:341` ↔ `:381,397,408` ↔ anchor `routes/terminal.py:50,104`` | ✅ | ＿＿ |
| S6-11 | S6 | b | SSH 补全腿没有专属超时（30s 默认），而本地两条腿都在 5s kill 子进程 | ``main.ts:1320-1330` → `ssh-manager.ts:929-932` → `:844-864`（`timeout=30`）↔ `main.ts:990-993`、`:1014`` | ✅ | ＿＿ |
| S6-12 | S6 | b | 两个 shell 生产者都在发 `cmd:` 标签，而唯一消费者把它整个丢弃 ⇒ CMD 的命令位补全恒 0 候选、PowerShell 的命令名匹配算完就扔 | ``main.ts:929-930`、`:967-968` ↔ `useCompletion.ts:435,445-446`（`hiddenTags` 含 `func/alias/cmd`）` | ✅ | ＿＿ |
| S6-13 | S6 | b | PowerShell 补全把用户 token 里每个 `@` 都改写：单引号 here-string 不做转义处理，`'@'→'@{'` 改的是正文 | ``main.ts:953-954` ↔ `:947`（真正需要转义的 `'` 已处理）` | ✅ | ＿＿ |
| S6-14 | S6 | b | `stripPrompt` 的 prompt 边界可被用户自己正在输入的内容移动 | ``useCompletion.ts:95,207-217` ↔ `main.ts:281-296`（`complete -p` 那条 `arg:` 候选来源被跳过）` | ✅ | ＿＿ |
| S6-15 | S6 | d | `stripPrompt` 的 OSC 6973 精确通道双重不可达：全仓无人发出该序列，且 xterm 把 OSC 消费进解析器状态、从不落进单元格文本 | ``useCompletion.ts:92-93,198-205` ↔ `BufferLine.translateToString:524-538`；`grep -rn 6973 src electron backend scripts` 只命中这个消费者` | ✅ | ＿＿ |
| S6-16 | S6 | a | 三处远程命令拼接「今天安全」纯靠调用方约束，无任何断言：b64 裸嵌单引号内、pid 无 `int()` 强制、`log_dir` 未加引号 | ``ops_common.py:207`、`ops_job.py:300-304`（对照启动路径的 `_parse_remote_pid:255-263`，恢复路径 `:207` 无校验）、`ops_job.py:279-282/291-293/321`` | ✅ | ＿＿ |
| S6-17 | S6 | c | `exited` 对 Manager 是终态、对 DB 不是 ⇒ 僵尸行每次重启都被恢复，且永久占号 | ``ops_job.py:153` ↔ anchor `database.py:2756`、`:2793-2795`；`grep DELETE FROM agent_jobs` anchor 零命中` | ✅ | ＿＿ |
| S6-18 | S6 | c | 恢复的 `start_ts` 用 `time.mktime` 解一个 UTC 串 ⇒ `job_list` 报出数小时幻影运行时长，并让淘汰顺序永远先踢恢复项 | ``ops_job.py:194-198`（消费 `:523-524`、`:672`、淘汰 `:167`）↔ anchor `database.py:411` `DEFAULT (datetime('now'))`` | ✅ | ＿＿ |
| S6-19 | S6 | c | 作业路径上的同步 sqlite 跑在事件循环线程里，违背本仓既有的 M-13/S11 约定 | ``ops_job.py:183`、`:219/:236/:245`（均由 async `execute():562,656,749,822` 与 `_drain:493-499` 抵达）↔ anchor `database.py:77-88` 线程本地连接、`:67` `busy_timeout=5000`` | ✅ | ＿＿ |
| S6-20 | S6 | c | S0-06 的加重：恢复的作业在从未校验的持久化 session id 上执行命令并收 SIGTERM/SIGKILL —— 且部分路径完全没有用户审批 | ``ops_job.py:206,250-252,690-692,449-453`；`kill_remote_jobs_for_run` 由 anchor `routes/agent.py:1215-1216` 的 `asyncio.shield(...)` 自动触发` | ✅ | ＿＿ |
| S6-21 | S6 | c | 目标机重启后的 PID 复用，使 `job_kill`/`job_list` 变成「对陌生进程组动手」 | ``ops_job.py:207`（pid 直取 DB，无 `int()` 也无存活证明）、`:297-305`、`:308-314`` | ✅ | ＿＿ |
| S6-22 | S6 | c | `job_run` 的入库是远程启动成功之后的 best-effort：DB 失败 ⇒ 目标机上留下永远无法 kill 的孤儿守护进程 | ``ops_job.py:583-588` + `:216-230`、`:233-238`（`except Exception: pass`，且无日志）` | ✅ | ＿＿ |
| S6-23 | S6 | c | `keep_full` 挡不住主进程侧的 1MB 头部保留截断，于是截断提示语的字节数被计进 offset | ``ops_job.py:391,396-410` ↔ `ssh-manager.ts:884-893,908-913`（`TRUNCATED_NOTE`）` | ✅ | ＿＿ |
| S6-24 | S6 | c | `[stderr]` 段落被计进日志段 ⇒ offset 多推进那些从未存在于 out.log 的字节 | ``ops_job.py:396-410` ↔ `ops_common.py:141-144`（stderr 拼在哨兵之前）；`:382-383` 的「有 stderr 即 exit≠0」假设此处不成立` | ✅ | ＿＿ |
| S6-25 | S6 | c | 对账走的是位置对齐协议却没开 `keep_full` ⇒ 中间行被中文标记替换后，退出码错位归属到别的作业 | ``ops_job.py:692,697-700,707-719` ↔ `truncate_output`（阈值 4000，`truncation_limits.py:8-12`）↔ `ops_common.py:109-113` 自己写的规则` | ✅ | ＿＿ |
| S6-26 | S6 | b | `_split_cmd` 在 Windows 宿主上做 POSIX 词法，反斜杠路径被吃掉 —— 而 docstring 承诺相反 | ``ops_job.py:464-474`（声明 `:467-468`）↔ `tests/test_ops_file_job_img.py:430` 已承认该行为` | ✅ | ＿＿ |
| S6-27 | S6 | a | 选区按钮定位的 x/y 同时差一：`IBufferCellPosition.x/.y` 都文档化为 1-based | ``useTerminalContext.ts:75,77` ↔ `@xterm/xterm/typings/xterm.d.ts:1488-1498` ↔ `BufferApiView.ts:24`（`viewportY` 0-based）` | ⚠️ | ＿＿ |
| S6-28 | S6 | d | 三条搜索腿没有响应体上限，Tavily 结果也不按 `max_results` 截断，而 fetch 路径是专门加了上限的 | ``web_tools.py:164`、`:197`、`:266` ↔ 刻意的 `:21,463-496`；`_format_results:321-328` 无总输出上限` | ✅ | ＿＿ |
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
| S9-07 | S9 | a | 远端 `https:` 图片在渲染时自动拉取 ⇒ 会话被打开过的探针（view oracle），带应用 IP/UA | ``markdown.ts:75` ↔ `dompurify/dist/purify.cjs.js:680`(`img` ∈ DATA_URI_TAGS) ↔ CSP `main.ts:232`` | ✅ | ＿＿ |
| S9-08 | S9 | c | 后处理没有任何结果通道：`submit()` 的 task_id 被丢掉，所有失败只落日志 | `anchor `routes/agent.py:1023` ↔ anchor `post_processor.py:604,639,672-674,726-730,950-957`` | ✅ | ＿＿ |
| S9-09 | S9 | b | 无正文的 run 整体跳过后处理 —— 恰恰是 else 分支想让它们可见的那批（失败/早退） | `anchor `routes/agent.py:999`、`:1034-1051`` | ✅ | ＿＿ |
| S9-10 | S9 | b | 记忆/技能抽取用的是运维 system prompt，而任务要求严格 JSON | `anchor `post_processor.py:613`、`:750` ↔ `prompts.py:10-14` ↔ 用户轮 `:568-590`、`:682-711`` | ✅ | ＿＿ |
| S9-11 | S9 | b | 反思的「safety」得分来自对工具输出做子串探测 ⇒ 远端主机内容可影响演化信号 | ``reflection.py:153-158`(`("拦截","权限","禁止")` 打在 `tc["result"]` 上) → `:161-186` → `:446-487` → `:495-512`` | ✅ | ＿＿ |
| S9-12 | S9 | c | `Conversation.status` 声明了后端从不产出的 `'failed'`、`preview` 无人读；而 `list_conversations` 每行带整条最后消息正文 | `anchor `client.ts:235,239` ↔ anchor `database.py:964`、anchor `routes/agent.py:796`、`:1238`、anchor `database.py:1932-1951` ↔ anchor `database.py:991-1000`` | ✅ | ＿＿ |
| S9-13 | S9 | c | `AgentTraceSnapshot.status` 类型只有 2 值，生产者写的是原始终态（≥4）⇒ `max_reached`/`human_help` 一律渲染成「失败」 | `anchor `client.ts:775-776` ↔ anchor `routes/agent.py:985-987,970` ↔ `database.py:1866`(`_TERMINAL_RUN_STATUSES` 4 值) ↔ `AgentFullTrace.vue:17-18`` | ✅ | ＿＿ |
| S9-14 | S9 | c | 【9634dee 已修】anchor 的 `keep_count=0` 哨兵含义在保留期 UI 里写反了 | `anchor `ChatPanel.vue:96`（可见描述无哨兵）与 `:148-149`、`:181-183`（注释断言「0 = 清空全部已结束会话」）↔ `database.prune_conversations:2690` `if keep_count is None or keep_count <= 0: return 0`` | ✅ | ＿＿ |
| S9-15 | S9 | c | 【9634dee 已修】anchor 后处理开关只在 App 挂载时读一次，而那通常正是后端还没起来的时候 | `anchor `App.vue:100-106`（唯一调用点）↔ anchor `settings.ts:374-387`（catch 吞掉）↔ anchor `:328-333`（PUT 恒发全四字段）` | ✅ | ＿＿ |
| S9-16 | S9 | c | 上传接口只收文本，但聊天文件选择器不设 `accept`，被拒的字节已经全量传到 127.0.0.1 | ``SideChatPanel.vue:314-321` ↔ `routes/chat.py:24-34,61-69`` | ✅ | ＿＿ |
| S9-17 | S9 | d | 配额淘汰下次启动就被撤销，且不修 `currentConvId`，toast 还超售 | ``useChatPersistence.ts:53-83`（淘汰只写本地）↔ `:108-141`（同步再把空壳加回来）↔ `:146`、`:155-158`、`:64`、`:67`、`:79-81`` | ✅ | ＿＿ |
| S9-18 | S9 | d | localStorage 里复原的 `temp-agent-` 占位符永不被清扫 ⇒ 一只永久空白的助手气泡 | ``useChatPersistence.ts:34-51` ↔ anchor `ssh-ai.ts:158-205`（收尾只靠会话内状态 watch）↔ `finalizeAgentMessage.ts:204-207`（只处理最新那只）` | ✅ | ＿＿ |
| S9-19 | S9 | d | 滚动到底的 watch 以 `length` 为键且非 `immediate` —— 文档记录过的「子组件晚于其数据事件创建」那一类的新实例 | ``SideChatPanel.vue:1188-1190`（`scrollToBottom` 在 `:1180-1186`），三处调用者全非 immediate ↔ `SshView.vue:156` 的 `v-if` 挂载` | ✅ | ＿＿ |
| S9-20 | S9 | d | 关掉 AI 面板即丢弃 posture / 审批模式 / 草稿 / 附件 / 已选技能五项局部态 | ``SideChatPanel.vue:646,739,579,584,969`（都在 `SshView.vue:156` 的 `v-if` 子树里）↔ anchor `ssh-ai.ts:243-249` 的承诺只覆盖 run 结果` | ✅ | ＿＿ |
| S9-21 | S9 | d | 后处理路径上的死配置与死计算 | `anchor `post_processor.py:74`(`memory_similarity_threshold` 全仓零读者)、`reflection.py:324`(`_CONFLICT_PATTERNS = []` ⇒ `:326-373` 每次工具调用都算一遍 `normpath` 序列再返回 `[]`)` | ✅ | ＿＿ |
| S9-22 | S9 | d | 消息形态有三套并行定义，其中渲染路径两套都不 import；一个净化选项是空操作 | `anchor `types/chat.ts:9-20` vs anchor `MessageItem.vue:53-62` vs `SideChatPanel.vue:514-524`；`markdown.ts:76` 的 `ADD_ATTR:['class',…]` 重复 DOMPurify 默认已含的 `class`；`SideChatPanel.vue:965-966,1063-1064` 四条 watch 守同一条「只开一个浮动菜单」不变量` | ✅ | ＿＿ |
| S10-11 | S10 | c | `update_model_config` 支持写 `is_default`，`UpdateModelRequest` 却没这字段 ⇒ 死分支 + `PATCH {"is_default":true}` 得 200 且什么都没改 | `anchor `database.py:1701,1735-1738` vs anchor `routes/settings.py:42-57`` | ✅ | ＿＿ |
| S10-12 | S10 | c | `sort_order` 后端可写且列表按它排序，前端类型面与面板都没有它 ⇒ 顺序用户不可控，而 `GET /default` 的 `list[0]` 兜底正按这个不可控顺序选人 | `anchor `routes/settings.py:56`、`database.py:1590,1662-1663` ↔ anchor `client.ts:280-295`` | ✅ | ＿＿ |
| S10-13 | S10 | b | 测试连接端点把上游响应体前 200 字节原样回传渲染端并写日志 —— 同一顾虑在 `available` 列表上堵了，在 body 上没堵 | `anchor `routes/settings.py:211`（注释）vs `:242,246,251` ↔ anchor `stores/settings.ts:485` → `ModelsPanel.vue:608`` | ✅ | ＿＿ |
| S10-14 | S10 | b | `api_key` 没有任何「清除」途径：后端跳过空串、面板不发空串，两者互指对方 | `anchor `database.py:1708` ↔ anchor `ModelsPanel.vue:391,509-515`` | ✅ | ＿＿ |
| S10-15 | S10 | b | 掩码识别靠 `""` 子串哨兵：真实密钥含该子串则永不可写回（静默 no-op + 成功 toast） | `anchor `database.py:1712` ↔ `_mask_api_key:1542-1544`` | ✅ | ＿＿ |
| S10-16 | S10 | a | `_DEFAULT_COLUMNS` 不是单一来源：tier→列映射另有 4 处独立实现，其中 anchor 读侧对 `"global"` 反向（落进 SSH 列） | `anchor `database.py:1812` vs `:1768`（表）+ `:1735-1738`、`:1669/1681`、`routes/settings.py:77`` | ✅ | ＿＿ |
| S10-17 | S10 | a | `get_default_model` 用 `LIMIT 1` 无 `ORDER BY`，双默认时读数不确定，而 `list_models_config` 的排序键是 `sort_order, created_at` | `anchor `database.py:1814` vs `:1590`；对照 `:1576`、`:1619` 两处都写了 `ORDER BY rowid LIMIT 1`` | ✅ | ＿＿ |
| S10-18 | S10 | a | 后处理头图兜底只读全局档，且三条分支都以字面量 `"Qwen3-VL"` 收尾 —— 那是本 spec 明确删除的旧硬编码种子名 | `anchor `post_processor.py:921-929` ↔ spec `:11-13`` | ✅ | ＿＿ |
| S10-19 | S10 | d | `create_model_config` 路由把任何异常折成 400 + `str(e)`：「database is locked」这类服务端故障显示为输入错误 | `anchor `routes/settings.py:104-125` ↔ 同文件 `:294-297`、`:562-564`（都是 500 + 中文说明）` | ✅ | ＿＿ |
| S10-20 | S10 | d | 【9634dee 已修】`netconfig.TIMEOUT_MIN = 5` 后端零消费者（写入区间一律 low=0），而前端有个同名常量是 0 且真在驱动 UI | `anchor `netconfig.py:34` ↔ anchor `database.py:2142-2143`、`routes/settings.py:488` ↔ anchor `useOpsSettings.ts:22,69,90,161` + `OpsPanel.vue:53,71`` | ✅ | ＿＿ |
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

## Tradeoff — 5 条

| ID | 切片 | 维度 | 标题 | 位置 | 核验 | 决定 |
|---|---|---|---|---|---|---|
| S2-09 | S2 | c | 「始终允许」的授权键无主机维度：A 机上的一次「始终允许」永久命中 B 机同一命令 | ``gate.py:288-311,96,349,387`` | ◐（键构造与查找点 subagent 全列，我核了 TTL 与键形状同源描述） | ＿＿ |
| S5-18 | S5 | d | 四条解压路径（`fs:unzipLocal`、`sftpUnzipRemote` 的 unzip 与 python 两支）无自带 zip-slip 约束，安全性外包给外部工具；而同组 `fs:zipLocal` 自带 fail-close | ``main.ts:853-868` / `ssh-manager.ts:2011-2062` vs `main.ts:811-832`` | — | ＿＿ |
| S6-29 | S6 | d | `web_fetch` 只解析 A 记录 ⇒ IPv6-only 目标一律报「DNS 解析失败」，双栈目标恒钉 A 记录 | ``web_tools.py:523` → `:529` → `:498-509`；设计意图在 `:392-394` 有文档` | ✅ | ＿＿ |
| S10-24 | S10 | c | `routes/agent.py:34 from routes.terminal import _get_model_config` 的跨 router 私有依赖：低危但零收益，且本仓已有 `safe_tokens.py` 专门为此类耦合建过解 | `anchor `routes/agent.py:34` ↔ anchor `routes/terminal.py:29-31` ↔ `safe_tokens.py:1-5` docstring` | ✅ | ＿＿ |
| S11-03 | S11 | c | `command_raw` 明文入库并经 `/audit/export` 整表 CSV 落盘：命令行内联口令（`mysql -u root -pSECRET`、`curl -u`）随审计证据一同外带 | ``audit.py:63,154-155` + `routes/audit.py:66-84`` | ✅ | ＿＿ |

## Register — 2 条

| ID | 切片 | 维度 | 标题 | 位置 | 核验 | 决定 |
|---|---|---|---|---|---|---|
| S11-06 | S11 | b | 规格轴：审计覆盖声明与 reality 的差，归口 S2 的 D4（批量 `ssh:exec` 零审计）与 S2-06（审批记录缺 run_id/environment），本切片不重复计条 | ``docs/security-boundary.md:26,138-139,150-156`` | ✅（引用侧我看过） | ＿＿ |
| S12-01 | S12 | — | 记忆条目过时：`NvGauge` 的「66/90 红测试欠账（工作区已修未提交）」已结清并入库——阈值实现与用例现同为 60/80，28 条全绿 | ``NvGauge.ts:5,36` vs `NvGauge.test.ts:111-116`` | ✅ | ＿＿ |

