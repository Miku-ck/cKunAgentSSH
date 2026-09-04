# AI 对话与 Agent 模式代码审查报告 v1

> 范围：普通对话链路（chat 后端 + 前端）与 Agent 模式链路（路由/驱动/拦截器/沙箱/工具 + 前端）
> 方法：4 个独立分片审查（chat 后端 / Agent 路由+驱动 / 沙箱安全+工具 / 前端）逐文件通读，
> 关键论断全部由主审**独立复核**：心跳截断独立复现、13 条绕过命令实测 classify_command、
> 审计链调用点 grep 验证、undo 数据流逐函数跟踪、grant 鉴权一条被**证伪修正**。
> 只读审查，未改动任何生产代码。实证脚本在 `.chat-test/repro_heartbeat.py`、`repro_classify.py`。
> 日期：2026-09-02（代码现状）

## 0. 结论摘要

两条链路的工程素养明显高于平均（多轮编号修复注释、fail-closed 拦截、SSE 生命周期、
命令哈希绑定审批），**未发现 XSS/SQL 注入类 CRITICAL**（前端唯一 v-html 入口有完整
DOMPurify 闭环，已验证）。但存在 **4 条 CRITICAL**，全部集中在"安全承诺与实现不符"：

| # | CRITICAL | 一句话 |
|---|---|---|
| C1 | 命令分类默认放行（`safety.py`） | 未知远程命令免审批执行；**实测 13 条破坏性命令全部判 safe**（含 `rm -rf /` 变体、建 uid=0 后门账户、`kubectl delete ns`） |
| C2 | 审计链只记"拒绝"（`reins.py`/`audit.py`） | 成功执行的命令、grant 批准**全部不入哈希链**；`/audit/export` 导出的合规证据缺了最关键的"谁批了什么、执行了什么" |
| C3 | completed run 的 `/undo` 失效（`agent.py`/`file_changes.py`） | 主场景（run 正常完成后点撤销）modify/delete 无法恢复——备份被 B-8 刻意清理 + DB 不落 before/after 内容 |
| C4 | SSH 模式回滚打错机器（`saddle.py`） | `ShellUndo.apply_reverse` 用**本地** `create_subprocess_shell` 执行反向命令，而原命令在远程服务器执行；run 失败自动回滚无需点击即触发 |

另有 15 条 MAJOR，其中**心跳截断 bug 已独立复现**（LLM 流停顿 >15s → 半截回答被当正常完成入库）。

---

## 1. CRITICAL（4 条，均已独立验证）

### C1. 命令安全"默认放行" + 实测绕过命令族
`backend/agent/harness/safety.py:672-727` — `classify_command` 未命中任何模式时返回
`("safe", None)`，**未知命令免审批直接在远程服务器执行**。审批面 = 有限正则表的命中率。

主审实测（真实调用 `classify_command`，`.chat-test/repro_classify.py`）：

| 命令 | 分类 | 实际效果 |
|---|---|---|
| `r${IFS}-m${IFS}-rf${IFS}/` | **safe** | bash 展开 = `rm -rf /`（归一化不展开变量，`\brm\b` 不匹配） |
| `rm$'\x2f' -rf $'\x2f'` | **safe** | ANSI-C 引用 = `rm -rf /`；归一化把 `\x` 剥成 `x` **主动制造漏检** |
| `useradd -o -u 0 -g 0 backdoor` | **safe** | 创建 uid=0 根等价账户（useradd/usermod/passwd 完全未覆盖） |
| `chmod -R 0777 /etc` | **safe** | 正则只认字面 `777`，`0777`/setuid 变体漏过 |
| `ls /etc/cron.d \| xargs -I{} rm -rf {}` | **safe** | `{}` 占位符被当普通目标，实际删 cron |
| `echo '0 0 * * * evil' \| crontab -` | **safe** | 仅 `crontab -r` 入表，**安装/替换 crontab 漏检**（持久化通道） |
| `echo evil > /tmp/j; at -f /tmp/j` | **safe** | `at` 计划任务未覆盖 |
| `env -i /bin/bash /tmp/x` / `nice -n 10 bash /tmp/x` | **safe** | 包装器带 flag 即击穿首 token 解释器判定（`safety.py:441-448`） |
| `truncate -s 0 /etc/passwd` | **safe** | 目标提取取"第一个非 flag 参数"= `0`（size 值）而非文件 |
| `kubectl delete ns default` | **safe** | K8s 工具族完全未覆盖 |
| `apt-get remove -y --purge nginx` | **safe** | 仅 pip/npm uninstall 入表 |
| `iptables -X; iptables --flush` | **safe** | 仅 `-F`/`disable` 入表，`--flush` 同效变体漏过 |

对照组（正确拦截）：`rm -rf /`、`find / -exec rm -rf {} \;`、`python3 -c "...rm -rf /"`、
`bash -c 'rm -rf /'` → hardline。

**修复建议**：① 远程 shell 改为**默认审批**（safe 也弹 permission_needed，或至少 prod 环境），
或反转成"已知良命令白名单"；② 立即补入：useradd/usermod/passwd、`chmod` 任意八进制含 7 位模式、
crontab 安装/`at`、xargs 后接破坏性动词、包装器 flag 保守判 dangerous；③ 把本次实测表
固化为 `test_safety.py` 回归用例（当前测试未覆盖这些变体）。

### C2. 审计链只覆盖"拒绝"，不覆盖"执行"和"批准"
`reins.py:915-923`（AuditLogger.check 只 `logger.info`）+ 全库 `append_record` 仅 4 处调用
（`reins.py:177/209/240/280`，全在 FilesystemGuard 的 blocked/denied 分支）。
`/agent/permission/grant`（`agent.py:928-944`）批准动作无审计记录。

**后果**：`audit.py` 头部宣称"执行的命令、审批结论、拦截决定全部落不可篡改审计链"，
实际哈希链只有"拒绝"。等保 2.0 安全审计目标落空，事后追责无法证明"谁批了什么"。
（执行明细在 `tool_calls` 表有记录，但那是可变更表，不在哈希链、不在审计导出里。）

**修复建议**：POST_TOOL_CALL 对 shell_exec/文件变更工具 `append_record(conclusion="executed"/"failed")`；
grant/revoke 路由落 `conclusion="approved"/"revoked"` 记录。

### C3. completed run 的 `/undo` 在主场景失效
数据流验证：`agent.py:808-815`（B-8：仅 `final_state=="completed"` 时
`cleanup_run_backups`，删除磁盘备份）+ `file_changes.py:174-175`（持久化时 pop 掉
before/after_content）+ `file_changes.py:273-283`（`load_file_changes` 重建 FileChange
**不含内容字段**）→ run 结束后 `/undo`（`agent.py:1229`）对 modify/delete 只能走
`failed` 分支返回 `{"undone": false}`。

**后果**：UI 基于 done 事件 `file_changes` 向用户承诺"可撤销"，但用户看完 Agent 完成结果
后点撤销——**恰是最常用的场景**——数据丢失不可恢复。（failed/断连 run 的 undo 因 B-8
保留备份反而是好的，组合效应把主场景挖空了。）

**修复建议**：completed 时保留备份直至用户显式 keep 或 TTL 到期；`persist_file_changes`
同时落 before_content（DB 兜底）。

### C4. SSH 模式回滚反向命令在本地执行（打错机器）
`saddle.py:272-289` — `ShellUndo.apply_reverse` 用本地 `create_subprocess_shell` 执行
`reverse_command`；而 SSH 模式下原命令经 ElectronBridge 在**远程服务器**执行
（`shell_exec.py:42-43`）。`driver.py:640-643` run 失败自动 `undo_all`，无需用户点击。

**后果**：远程 `mkdir /tmp/foo` 失败回滚 → 在**后端主机（本机 Windows/Linux）**执行
`rmdir /tmp/foo`：要么无效（承诺失真），要么误删本地同名路径（`touch` 反向是
`rm <path>`）。

**修复建议**：`ShellUndo` 记录执行宿主（local/ssh + session_id），远程 reverse 走
ElectronBridge 下发；或 SSH 模式直接禁用 shell 反向命令（只留记录）。

---

## 2. MAJOR（15 条）

### 普通对话链路（5）

**M1. 心跳超时静默截断 LLM 流（已独立复现）**
`chat.py:278-291` — `asyncio.wait_for(chunk_iter.__anext__(), timeout=15)` 超时时取消
`__anext__` 任务，CancelledError 抛进 async generator 帧 → generator 永久关闭
（`llm_adapter.py` finally 随即 aclose 底层流）→ 下次 `__anext__` 立即
StopAsyncIteration → **半截回答走"正常完成"路径**：入库、状态 completed、发 `[DONE]`。
`repro_heartbeat.py` 实测复现（首 token 等待 >15s 与流中途停顿均触发）。
触发条件平凡：长 prompt prefill、GPU 忙、thinking 停顿。**L333-334"中断保护"注释对这条
路径无效**（保护只在异常分支生效，截断走的是正常 StopAsyncIteration 路径）。
**修**：`asyncio.shield(chunk_task)` 模式——超时只发心跳，chunk 任务继续存活。

**M2. 记忆功能整条链路失效（读写两张不同的表）**
`chat.py:168` 从 `memories` 表读记忆注入 system prompt（`database.py:954-967`）；
后处理提取的记忆经 `memory.py:354-363` 写 **`agent_memories`** 表。全库 grep：
`memories` 表**只有读、没有任何写入方** → "过往对话记住的关键信息"**恒为空**，
记忆提取/去重/embedding 全部白跑。
**修**：`get_relevant_memories` 改读 `agent_memories`（semantic 类型，复用 embedding 检索），
并补"提取→注入"集成测试。

**M3. context_window 无上限 + token 预算边界失效**
`chat.py:54`（pydantic 无 ge/le）+ `chat.py:166` 原样透传 → 历史条数无界；
`safe_tokens.py:65` 的 `max(256, ...)` 下限使估算 prompt 超限时返回的 256 仍越界 →
vLLM 400，且**该会话此后每条请求都 400**（历史只增不减），直到手动删会话/调小窗口。
**修**：`history_limit = min(..., 100)` 硬顶；更彻底按 token 预算反向裁剪。

**M4. 同一会话并发 stream 无互斥**
`chat.py:247-251` — 并发请求各 add_message(user)、in_progress→completed 互相覆盖，
历史交错、post_processor turn_index 漂移、后处理双份执行。双击发送即触发。
**修**：按 conversation_id 原子状态转移抢锁
（`UPDATE ... WHERE status<>'in_progress'` 按 rowcount 判定）。

**M5. 首轮失败后标题永远生成不了（+ turn_index 频率控制整体偏移）**
`post_processor.py:313-314` 只在 `is_first_turn` 生成标题，但首轮失败（user 消息已入库）
后次轮 turn_index=1 → 门槛永久关闭，会话名停在 "New Chat"；技能/记忆按 turn_index % N
的频率控制同样被失败轮偏移。
**修**：标题门槛改为"标题是否仍为默认值"，去掉首轮门槛。

### Agent 链路（7）

**M6. LoopDetector"第 3 次强制终止"是死代码 + 状态反复 + 双反思**
`driver.py:448-469` — PRE 拦截分支只发事件 + 追加结果消息 + `continue`，**从不
transition FAILED/break**，`inject_messages` 被丢弃；`reins.py:447-463` 第 3 次触发的
"强制终止"文案与 kind="error" 事件经 `agent.py:750-777` 中途把 run 标 failed + 触发
第一轮反思，随后 done 事件再改回真实终态 + 第二轮反思。
**后果**：真死循环 run 不被终止（烧满 max_iterations），状态/反思重复。
**修**：`ReinDecision` 加 `terminate: bool`，driver 据此 transition FAILED + break。

**M7. 无 WAITING_PERMISSION 挂起态 + 与循环检测结构性冲突 + 无审批超时**
审批期间 run 继续跑（每轮重发命令烧 LLM 调用）；重发签名进 LoopDetector 窗口，
连续 3 次后第 4 次重发被循环检测**抢截**（PermissionGate 不再执行、不再发
permission_needed）→ **用户批准稍慢（>2 轮重发间隔）授权卡片即成孤儿**，命令永远
执行不了。无审批超时机制。
**修**：引入挂起态（park tool call，grant 时恢复）；或"等待审批中"的签名豁免循环检测
+ 审批超时（超时按拒绝）。

**M8. once 授权时序竞态（"点了允许却仍被拦"）**
`driver.py:556-558` 每轮迭代末无条件 `clear_once()`（`reins.py:784-786` 全清）；
用户在"permission_needed 发出 → 本轮迭代结束"窗口内点"仅本次"，授权被静默清空，
下一轮重发仍被拦 → 死循环。窗口窄（本轮剩余执行时长）但用户可见症状真实。
**修**：once 授权按 `run_id+grant_key` 记录、**消费时**移除而非批量清空。

**M9. 客户端断连不终止在途命令（孤儿进程/孤儿远程任务）**
`agent.py:794-796` `engine_task.cancel()` 未 await；`shell_exec.py:116-122` 只在
`asyncio.TimeoutError` 分支 `proc.kill()`，`CancelledError`（BaseException）不被
`except Exception` 捕获 → 本地子进程孤儿继续执行；远程 SSH 命令在主机上继续跑。
**后果**："用户已停止"的 run 仍可能在生产主机上改文件。
**修**：`except asyncio.CancelledError: proc.kill()` 后 re-raise；bridge 协议加命令中止。

**M10. 无 token/成本硬熔断**
`agent.py:258` 允许 max_iterations 到 500，`reins.py:1298-1304` 动态预算可再翻倍
（cap 2.0）→ 单 run 最坏 ~1000 次 LLM 调用，CostTracker 只累计不裁决。
**修**：POST_ITERATION 预算拦截器（max_cost_usd / max_total_tokens 配置化，超限终止）。

**M11. session 授权进程级共享，跨会话泄露**
`reins.py:646-649` — `_confirmed_session` 是全局单例，会话 A"会话级"批准的命令在
会话 B 的 run 中免审。（子代理所称"grant 端点无鉴权"**已证伪**：全局 API Token
中间件覆盖所有 `/api/*` 且服务器仅绑 127.0.0.1，降级为 MINOR 记录。）
**修**：session scope 按 conversation_id 键控。

**M12. bridle "滚动再摘要" metadata 路径是死代码**
`bridle.py:188-209` — 压缩生成的摘要消息 role=user，而 metadata 写入条件要求
`sys_msgs` 非空（输入已剔除全部 system）→ 条件恒假；实测两轮压缩后历史坍缩为
"200 字符切片嵌套"（`[user] 以下是之前对话的摘要:\n[user] 以下是...`），25% cap
修复作用在不可达分支。
**修**：摘要用显式标记 + 真正写入 metadata + 按 cap 截断，接通或删除死分支。

### 沙箱/工具（2）

**M13. 同 run 内同文件二次写入静默覆盖磁盘备份**
`saddle.py:57-83` — `shutil.copy2` 同名目标；undo 反演第一次写入时恢复到**中间态**
而非 run 前原始内容。**修**：备份名加代次序号，undo 按构建顺序取对应代次。

**M14. 文件工具"split-brain"：ssh 模式注册的是本地磁盘工具**
`tools/__init__.py:37`（`ToolMeta.mode_scope` 默认 `["ssh"]`）+ `file_ops.py` 全部
`os.path` 本地 IO（文件头自称"IDE 模式专属"）→ ssh 模式下 `write_file` "改"的是
**后端所在机器**的文件，而 `shell_exec` 改的是远程服务器；模型混淆两套文件系统，
undo/备份/文件变更面板记录的全是本地变更，远端 shell 变更完全不在追踪范围。
（v1 生产化文档 D4 "work/ide 半成品"的具体化。）**修**：SSH 模式提供走 SFTP 桥的远端
文件工具，或把文件工具从 ssh 模式摘除并在 system prompt 明示。

### 前端（4）

**M15. 流代际竞态：旧流迟到回调清空新流的 abortFn**
`useChatStream.ts:91-118` — `onError`/`onDone` 闭包无条件写共享 `streaming`/`abortFn`，
无"该回调属于哪代流"校验；旧流错误回调晚于新流 startStream 落地时，UI 失去中止能力、
旧错误内容写入新消息。**修**：每代流递增 streamId，回调比对代际。

**M16. IME 组合输入 Enter 直接发送（中文用户高频路径）**
`SideChatPanel.vue:578-584` — Enter 分支无 `e.isComposing`/keyCode 229 检查，
输入法确认候选词即发送半成品。**修**：`if (e.isComposing || e.keyCode === 229) return`。

**M17. Agent 中止后消息不固化（空泡永久残留历史）**
`SshAiPanel.vue:87-93` + `createModeChatStore.ts:459-484` — 中止使状态 running→idle，
而固化 watch 只捕获 completed/failed → `temp-agent-` 占位消息（空内容）以
localStorage 持久化**永久残留**历史会话，与 agentStore 状态漂移。**修**：abort 路径
同样 finalize（带"已中止"文案），或 watch 补 running→idle 分支。

**M18. 发送失败无回滚（长文/附件直接丢失）**
`SideChatPanel.vue:600-613` — emit 前清空 input/skills/attachments，而父层
sendMessage 有仅 toast 的早退路径（后端未就绪/无模型/建会话失败）。**修**：可发送
校验前置，或 store 返回失败结果由组件回滚草稿。

**M19. 会话列表刷新抹掉非当前会话消息 + 当前会话闪烁**
`chat.ts:59-68` + `useChatPersistence.ts:64-83` — `fetchConversations` 把**所有**会话
替换为 `messages: []`，仅当前会话重载；窗口期内切会话 → 该会话消息被抹到延迟刷新才自愈。
**修**：合并时保留已有 messages，或刷新后无条件重载当前会话。

---

## 3. MINOR（12 条，摘要）

| # | 位置 | 问题 |
|---|---|---|
| 1 | `post_processor.py:319-377` | 标题生成 check-then-act：30s LLM 调用期间用户改的自定义标题被无条件覆盖 |
| 2 | `llm_adapter.py:43-67` | LRU 淘汰 aclose 进行中流的 httpx 连接池（需 8+ 模型配置 + 长流，低概率） |
| 3 | `database.py:869-872` | `get_recent_messages` 仅按秒级 created_at 排序，同秒消息可能乱序喂 LLM（加 rowid 兜底） |
| 4 | `chat.py:91-93` | 上传全量读入内存后才校验大小（先 read(MAX+1)） |
| 5 | `chat.py:100-103` | async 路由内同步磁盘 IO（mkdir/write_bytes），阻塞事件循环（其余 db 调用都已 to_thread，唯此漏） |
| 6 | `memory.py:228/250/301` | 检索路径同步 sqlite 调用漏出线程池（WAL 锁竞争时卡事件循环） |
| 7 | `chat.py:193-265` | reasoning_tokens 未传入 safe_max_tokens（thinking_budget 可超 max_tokens）+ 重复查库 |
| 8 | `tools/__init__.py:126-131` | saddle retry_with_backoff 实际不可达（registry 已吞异常），"瞬时错误自动重试"未生效 |
| 9 | `telemetry.py:38-61` | "可重放 run" 承诺不成立：500 条 FIFO 截断 + run 结束 clear + 从不落盘 |
| 10 | `web_tools.py:353-362` | web_fetch max_chars 无上限（LLM 可传 10^9，10MB 响应全量进上下文） |
| 11 | `api/client.ts` + `chat.ts:182` | 历史消息默认 limit=50 无分页，长会话静默截断无提示 |
| 12 | `agent.py:270-460` | 同会话并发 run 无防护（消息交错/授权互相消费/文件互覆）；崩溃后 run 滞留 running 无启动对账 |

## 4. NIT（摘要）

- `State.PLANNING/REFLECTING` 死枚举（driver 从不进入，state_history 消费者被误导）
- `check_permission` 的 scope 参数是死参数（行为实际由 risk 决定）
- `prompts.py:45-66` POST_PROCESS_PROMPT 死代码且长度语义与 MAX_TITLE_LEN=20 不一致
- `Sandbox._check_path_safe` 空 workspace fail-open（有上层兜底，底线组件应 fail-closed）
- 4000/1500/1000 截断常量 4 处手工复制（drift 风险）
- `chat.ts:79-88` messages computed setter 死代码；上传落盘名 file_id[:8] 小概率覆盖
- 技能菜单无障碍（无 role=menu/aria-activedescendant）；textarea 无客户端软上限
- `pickSkill` 插入的 `@技能名` 文本与 skillIds 注入双重出现、可漂移
- trace 回填按 (tool, iteration) 反向匹配，同轮同工具多次调用时张冠李戴
- agent 占位消息 reasoning 强制空串，"外层 ThinkingBlock 展示"注释与实际不符（运行中 reasoning 无展示）

---

## 5. 与《生产化评估 v1》的关系

| v1 已知项 | 本次复核结论 |
|---|---|
| A1 默认放行 | **确认**，且新增 13 条实测绕过 payload（`repro_classify.py` 可复跑） |
| A2/A3 审批闭环 | **确认**，新增细节：与 LoopDetector 抢截（M7）、once 竞态（M8）、error 事件状态反复（M6） |
| B1 审计 | audit 链已落地（v1 后新增），但**覆盖面只有"拒绝"**（C2）——需补 executed/approved |
| C4 shell undo | 确认，且比 v1 描述的更糟：回滚打错机器（C4）+ completed 主场景失效（C3） |
| D4 work/ide 半成品 | 具体化为文件工具 split-brain（M14） |
| **本次新发现** | C1 实测 payload、C3、M1（复现）、M2、M6、M9、M10、M12、M15-M19 及全部前端项 |

## 6. 写得好的地方（抽样）

1. **`reins.py:85-118` Reins.check** — priority 分组 + 首个 intercepted 短路 +
   safety_critical 异常 fail-closed，注释明确点出旧实现"异常即放行"的漏洞方向。
2. **`reins.py:677-695` _grant_key** — dangerous 命令的"始终允许"绑定归一化命令
   sha256，一次授权不能泛化到同工具其他危险命令；旧裸 allowlist 条目自动失效（收紧方向）。
3. **`chat.py:243/353-368`** — 流式生成器收尾教科书级：断连立即关 LLM 流停止烧 token，
   finally 仅在仍 in_progress 时复位 completed（不覆盖并发写入的 failed）。
4. **`web_tools.py` SSRF 防御** — 预解析 IP 直连 + SNI 保留 + 每跳重定向前重校验 +
   显式封锁 CGNAT/Teredo/6to4/NAT64 + mapped-IPv4 递归检查；实测 TLS 握手正常。
   是审过范围内最扎实的模块。
5. **`saddle.py:426-473` _shell_reverse** — 回滚命令推导拒绝含 shell 元字符的路径、
   派生命令再过一次 classify_command 非 safe 即放弃，对"回滚命令注入"防御到位。
6. **前端 `markdown.ts` XSS 闭环** — 全仓库唯一 v-html 入口经 DOMPurify，
   afterSanitizeAttributes hook 给 target=_blank 补 rel=noopener（已验证渲染链路）。

## 7. 修复优先级建议

**P0（本周，安全闭环最小集，与 v1 阶段 1 合流）**
1. C1：远程 shell 默认审批（至少 prod 环境）+ 补高危命令表 + 实测 payload 入回归测试
2. C2：审计链补 executed/approved 记录
3. C3/C4：undo 数据流修复（completed 保留备份 + 落 before 内容；ShellUndo 记录宿主）
4. M1：心跳 shield 修复（10 行改动 + 1 个单测即可）

**P1（本月）**
5. M7+M8：审批挂起态 + once 消费式管理（与 v1 A2/A3 同一工程）
6. M2/M3：记忆双表打通 + context_window 硬顶
7. M6：terminate 决策 + 迭代内终止
8. M9/M10：断连 kill 子进程 + 成本熔断
9. M15-M19 前端五项（每项均为小改动，IME 一行）

**P2**
10. M11/M12/M13/M14（授权键控、bridle 死代码、备份代次、文件工具架构决策）
11. MINOR/NIT 速清
