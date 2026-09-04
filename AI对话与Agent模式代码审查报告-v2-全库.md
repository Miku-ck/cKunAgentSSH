# 代码审查报告 v2 — 全代码库（2026-09-02）

**审查范围**：全库 ~48k 行（src 22,790 行 / 72 文件，electron 4,476 行 / 9 文件，backend 21,349 行 / 68 文件），5 个分区并行逐文件全量审查 + 调用方交叉核对。

**审查分区**：
1. 前端状态与逻辑层（api/stores/composables/utils/types）
2. 前端 UI 组件层（components/views/layouts 全部 37 个 .vue）
3. 后端核心层（main/database/routes/post_processor/prompts）
4. 后端 Agent 引擎（agent/ 含 harness/ tools/ 全部）
5. Electron 主进程（electron/ 全部）

---

## 〇、P1 清单修复记录（第二轮，2026-09-02 同日续修，共 20 项全部落地）

### 后端路由（任务 #1）
| 项 | 位置 | 修复 |
|---|---|---|
| P1-1 | `routes/schedules.py` | `interval_minutes` 加 `Field(ge=1, le=525600)`（创建/更新两处） |
| P1-2 | `routes/chat.py` / `skills.py` / `agent.py` | 全部列表接口 `limit: Query(ge=1, le=500)`、`offset: Query(ge=0)` |
| P1-5 | `routes/audit.py` + `agent/audit.py` | records/export 全部 `asyncio.to_thread`；`count` 过滤集补齐 `action`/`run_id` 与 query 对齐 |
| P1-4 | `routes/chat.py` | 客户端断连分支：入库带 `…[响应中断，以上内容不完整]` 标记 + 会话置 `failed`（原实现走正常完成路径，半截回复污染下轮 LLM 历史） |

### 后端核心（任务 #2）
| 项 | 位置 | 修复 |
|---|---|---|
| P1-6 | `database.py set_default_model` | 两条非原子 UPDATE 改单事务条件 UPDATE（`SET col=0 WHERE id != ?` + `SET col=1 WHERE id = ?` 同事务），消除并发切换产生 0/2 个默认模型 |
| P1-9 | `agent/audit.py` + `reins.py` | 新增 `append_record_async`（to_thread 包装）；reins 拦截器 7 处调用点改 await（原同步 BEGIN IMMEDIATE 持锁时阻塞事件循环最长 5s） |

### Agent 引擎（任务 #3）
| 项 | 位置 | 修复 |
|---|---|---|
| P1-7 | `harness/reins.py Reins.check` | 未拦截决策的 `events`/`inject_messages` 聚合返回（原实现直接丢弃 → SyntaxVerifier 语法修复反馈、LoopDetector 一级纠偏、ConfidenceMonitor 纠偏、DynamicBudgetRein 事件全丢） |
| P1-8 | `harness/safety.py` | 命令链切分正则加 `(?<!\d)&`（覆盖单独 `&` 后台分隔符，`ls & rm -rf /` 不再绕过逐段分类；`2>&1` fd 重定向不误切） |
| P1-20 | `harness/reins.py SyntaxVerifier` | 两处同步 `open().read()` 移入 `asyncio.to_thread`（新增 `_read_text_safe` 辅助函数）；tsc 子进程原已有 to_thread |
| P1-19 | `agent/tools/builtin/grep_tool.py` | 新增 `_RE_CATASTROPHE_RE` 元模式拒绝嵌套量词（`(a+)+`、`(\d+)+`、`(x\|y)+` 类），17 组用例自测通过（7 个危险模式全拦截、10 个正常模式零误杀，`(.)*x` 单字符组线性回溯不误报） |

### Electron（任务 #4）
| 项 | 位置 | 修复 |
|---|---|---|
| P1-10 | `main.ts` | 新增 `assertTrustedSender` + `handleTrusted` 包装器，**全部 73 个 IPC handler** 统一套 sender 校验（请求必须来自主窗口 webContents，受控渲染进程不能再直调 ssh:connect/fs:*/backend:getToken） |
| P1-11 | `main.ts sftp:upload/download` | `localPath` 加 `await assertSafePath`（受控渲染进程不能再读敏感文件上传到远程/写系统任意位置） |
| P1-12 | `utils/path-guard.ts` | `ANYWHERE_SENSITIVE` 段比对补 Windows 小写化（`.SSH`/`.AWS` 任意位置绕过修复，顶层 E19 已有） |
| P1-13 | `main.ts whenReady` | 注册 `uncaughtException`（连续 3 次退出）/ `unhandledRejection`（记日志）顶层兜底 |
| P1-14 | `main.ts fs:writeBinaryFile` | 50MB base64 数据上限 |
| P1-15 | `ssh-manager.ts doConnect` | SSH/SFTP 会话总数上限 64 |
| P1-16 | `main.ts window-all-closed` | darwin 分支不再销毁 PTY/SSH/后端（原实现关窗即停后端，activate 重建窗口后半残）；资源释放统一 `before-quit` |
| P1-17 | `backend-manager.ts` | 非主动停止且 code=0 退出时推 `error` 状态（原实现静默，前端停留 ready 假象） |

### 前端（任务 #5）
| 项 | 位置 | 修复 |
|---|---|---|
| P1-3 | `createModeChatStore.ts` Agent 固化 watch | 移除 `\|\| persistence.currentConversation` 兜底 — 同 mode 双 store 实例（chat.ts/ssh-ai.ts）共享 agentStore，非发起方实例兜底会退到自家当前会话误固化/误删。发起方列表必含源会话（id/backendConvId 双键），找不到即非发起方，no-op 让发起方处理 |
| P1-18 | `chat.ts loadMessages` | 目标会话流式生成中跳过整表替换（原实现丢掉流式 temp 消息与已写入内容，UI 跳回旧消息）；流式结束后 afterStreamDone 会重载 |

### 验证
- `python -m py_compile` 全部 15 个改动/关联后端文件 ✅
- `vue-tsc --noEmit` 全量类型检查 ✅
- grep_tool 嵌套量词元模式 17 组用例自测 ✅

---

## 一-B、P2 清单修复记录（第三轮，2026-09-02 同日续修，共 15 项全部落地）

### 批次 A — 杂项
| 项 | 位置 | 修复 |
|---|---|---|
| P2-A1 | `.gitignore` | 补 `.chat-test/`、`*.db-shm`、`*.db-wal`（此前工作区 SQLite 临时文件可被误提交） |
| P2-A2 | `client.ts streamChat/streamAgentRun` | `!res.ok` 分支 `onError` 回调包 try/catch，与 H15 循环内包裹保持一致（回调异常不再变 unhandled rejection） |
| P2-A3 | `electron/pty-manager.ts` | 白名单回退后丢弃自定义 args — 原实现"校验对象 ≠ 启动对象"（Windows 下 bash -l 回退 cmd.exe，-l 按 bash 规则通过但实际启动 cmd.exe）；spawn 统一用 finalArgs |

### 批次 B — Electron 持久化与资源
| 项 | 位置 | 修复 |
|---|---|---|
| P2-B1 | `electron/ssh-store.ts` | 全部写操作（save/delete/duplicate/touchLastConnected/migrateLegacy）串入 `writeQueue` promise 链 — 原实现并发写以各自旧基线互相覆写（丢更新）；参照 known-hosts savePromise 模式，队列链吞掉单操作错误 |
| P2-B2 | `electron/ssh-store.ts` | 新增 `reencryptPlainOnDisk` — 任何写操作前把旧 `plain:`（base64 混淆）字段升级为 `enc:`（safeStorage 加密），消除明文落盘窗口；加密不可用时静默跳过 |
| P2-B3 | `electron/main.ts fs:copy` | 10 万文件 / 10GB 上限 — 原实现递归收集百万文件整棵树驻留内存可 OOM |
| P2-B4 | `electron/main.ts pty:completeCommand` | PowerShell 分支 stdout 累积 256KB 上限 — 异常环境脚本刷屏可撑爆主进程内存 |

### 批次 C — 后端 Agent 引擎
| 项 | 位置 | 修复 |
|---|---|---|
| P2-C1 | `agent/tools/builtin/web_tools.py` | 重定向 5 次上限后 status 仍 3xx 显式返回失败 — 原实现落入成功分支给 Agent 空正文 |
| P2-C2 | `agent/consciousness_tick.py` | 反思记忆按失败 run 签名去重（有界 500）— 原实现每 tick 对同一批失败重复 save_semantic，记忆表被重复条目淹没 |
| P2-C3 | `agent/schedule_engine.py stop()` | 登记 `_running_tasks`，stop 时取消并 gather 等待 — 原实现只停轮询，detached 任务继续跑 SSH，schedule_runs 滞留 'running' |
| P2-C4 | `agent/schedule_engine.py _run_task` | ① finally 中 `clear_todos(run_id)`（定时 run 不走交互清理钩子，模块级 dict 缓慢泄漏）；② 新增 `except asyncio.CancelledError` 分支把 run 标记 failed 后 re-raise（关停取消不留 'running'） |

### 批次 D — 前端组件
| 项 | 位置 | 修复 |
|---|---|---|
| P2-D1 | `LocalTerminal.vue / RemoteTerminal.vue` | onUnmounted 中 `terminal = null` — 原实现只 dispose 不清引用，卸载窗口内补全/渲染回调的守卫失效，会在已 dispose 实例上操作 |
| P2-D2 | `stores/agent.ts` | onError 增加 `status === 'completed'` 守卫，与 onDone 对称 — 原实现无条件置 failed，窄窗口竞态（done 后迟到 error）把已完成 run 翻回 failed |
| P2-D3 | `ConnectionModal.vue` | 新增 `handleOverlayClose` 统一入口（Esc/遮罩/×/取消按钮），saving/testing 期间禁用 — 原实现关闭后请求继续落库且可重复提交 |
| P2-D4 | `SideChatPanel.vue` | document mousedown 监听实现技能菜单外点关闭（onMounted 注册 / onUnmounted 移除，菜单与输入框内点击不触发） |
| P2-D5 | `SftpView.vue` | onTransfer 拆为 `doTransfer` + promise 队列串行化 — 原实现快速连续拖拽两份时确认对话框竞态，第一份确认被静默丢弃 |

### 验证
- `python -m py_compile` 13 个后端文件 ✅
- `vue-tsc --noEmit` 全量类型检查 ✅（SideChatPanel 修复过程中发现 2 个编译错误：onMounted/onUnmounted 未 import、Node 类型无 closest — 已同步修正）

---

## 一-C、P2 补充修复记录（第四轮，2026-09-02 同日续修，共 10 项全部落地）

### Electron / 前端
| 项 | 位置 | 修复 |
|---|---|---|
| P2-E1 | `ssh-manager.ts sftpMkdirRecursive` | mkdir 失败后重新 stat 判定并发已创建 — 原实现以 `err.message.includes('exists')` 判定，OpenSSH 返回 `SSH_FX_FAILURE`（无 `exists` 字样）→ 判定落空，目录上传/并发父目录场景误报失败 |
| P2-E2 | `ssh-manager.ts download` | 新增 `renameOverwrite` 辅助函数（EPERM/EACCES 先删目标再重试；EXDEV 降级 copyFile + unlink）；download 收尾用 `renameOverwrite` 替代裸 `fsPromises.rename`；`downloadComplete` 标记 + catch 分支：rename 阶段失败时保留 .part 并标 `RENAME_FAILED`（retryable），不删已下载完整数据 |
| P2-E3 | `stores/sftp.ts` | disconnect / pauseTransfer 两处 `sftpCancel` 裸 Promise 调用改 `.catch(() => {})` — 原实现同步 try/catch 捕获不到 IPC reject |
| P2-E4 | `utils/think-scrubber.ts` | `_findCloseTag` 改按最早出现位置（min indexOf）选择，与 `_findOpenTag` 对称 — 原实现按列表顺序返回首个命中，闭合标签位置交错时漏掉中间 CoT |
| P2-E5 | `LocalTerminal.vue / RemoteTerminal.vue` | 全部 `ptyWrite`/`ptyResize`/`sshWrite`/`sshResize` 裸调用加 `.catch(() => {})`（共 10 处）— 原实现卸载后 IPC reject 变 unhandled rejection |

### 后端 Agent / 数据库
| 项 | 位置 | 修复 |
|---|---|---|
| P2-E6 | `tools/__init__.py` + `saddle.py` + `spawn_subagent.py` | `ToolContext` 新增 `cost_tracker` 字段，`saddle._build_tool_context` 从 `ctx.metadata["cost_tracker"]` 透传；`spawn_subagent._run_subagent` LLM 调用后 `record_llm_call(prompt_tokens, completion_tokens)` — 原实现子 Agent token 完全丢失，面板 cost/usage 与实际不符 |
| P2-E7 | `driver.py POST_TOOL_CALL` | 拦截 + 非拦截两个分支均补 `await telemetry.emit(ev)` — 原实现只 yield 给 Harness 合并流，未写入 TelemetryBus，TraceViewer / audit 记录中 POST_TOOL_CALL 事件丢失 |
| P2-E8 | `routes/terminal.py /terminal/predict` | LLM 流式调用包 `asyncio.wait_for(15s)` 超时（LLM 挂起时不再永久阻塞）；`recent_commands` 加 `max_length=50` 上限（原实现无约束，极端输入撑爆请求体+prompt） |
| P2-E9 | `database.py` + `main.py` | 新增 `seed_models_async` 异步入口：`_probe_first_model`（同步 httpx.get）放 `asyncio.to_thread` 执行，从 lifespan `await` 调用；`init_db` 移除同步 `_seed_models` — 原实现 6s 同步阻塞阻塞整个事件循环 |
| P2-E10 | `main.ts ssh:listConnections` | 返回前 `map(({ password, passphrase, ...rest }) => rest)` 剥离明文凭据 — 原实现含解密后密码/passphrase 直接经 IPC 传输，XSS/调试面板可读 |

### 验证
- `python -m py_compile` 全部 7 个后端改动文件 ✅
- `vue-tsc --noEmit` 全量类型检查 ✅
- 全库累计改动：~46 个文件（P1 20 项 + P2 35 项）

---

## 一、已修复项（本轮，共 9 处）

### HIGH（4）

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| H1 | `backend/agent/schedule_engine.py:94` | `_tick_once` 中 `self.trigger(s)` 未 `await`（trigger 为 async def）→ 协程被静默丢弃，**后台轮询从未触发任何定时任务**，next_run_at 永不推进，自动调度整体失效，仅手动"立即执行"可用 | 改为 `await self.trigger(s)` |
| H2 | `backend/agent/file_changes.py:183` + `backend/routes/agent.py:1302` | `/agent/runs/{id}/diff` 只查内存 `_PENDING`，而 run 结束后 `clear_file_changes` 清空 → 设计使用场景（run 结束后点文件看 diff）**恒返回"未找到"**，功能完全失效 | 内存落空时经 `load_file_changes` 从 DB（C3 迁移已落 before/after_content）恢复到内存再查；注意 `_LOCK` 非可重入，恢复调用置于锁外 |
| H3 | `src/components/ssh/SshAiPanel.vue:91` | 面板 onUnmounted 走 `handleAbort`，Agent 运行中会 `agentStore.abort()` → 与 P3-4 设计契约（"Agent 运行可跨面板存活，关闭面板不中止 Agent"）直接矛盾：切页/关面板杀掉正在执行的多步运维任务并固化"(已中止)" | 卸载只调 `store.abortStream()`；Agent 结果固化在 store 层 watch（P3-4），不依赖组件生命周期 |
| H4 | `backend/agent/harness/stirrups.py:199` | 上下文超限触发压缩后重试仍复用入参 `messages` 旧快照（压缩只改写 ctx.messages）→ 重试请求体未变小，**必然再次 400**，compress_attempts 耗尽后误报"压缩后仍超限"，上下文溢出 run 无恢复路径 | 新增 `set_messages_rebuilder` 回调（Harness 注入 `bridle.assemble`），压缩后重建 messages 再重试 |

### MED（5）

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| M1 | `backend/agent/schedule_engine.py:124` | `_run_task` 开头 `create_schedule_run`/`mark_schedule_run` 在 try/finally 之外，DB 异常时 `_executing` 标记永不 discard → 该任务被永久视为执行中，后续 tick 全跳过直至重启 | DB 初始化移入 try（finally 统一清理标记） |
| M2 | `backend/agent/schedule_engine.py:232` | `environment="production"` 但 FilesystemGuard 生产强化分支只认 `"prod"` → 定时任务走普通审批流而非生产加固，审计 env 值与 "prod" 标准不一致 | 改为 `environment="prod"` |
| M3 | `backend/routes/schedules.py:30` | `interval_minutes` 无 ge/le 校验，0/负值入库后 `compute_next_run` 算出 ≤now 的时间，每 30s tick 触发无头 SSH Agent 运行，持续烧成本 | （待修，见下文 P1 清单第 1 条） |
| M4 | `backend/routes/chat.py` 等 | 列表接口 limit/offset 无边界，limit=-1 在 SQLite 语义为无限制全表返回 | （待修，见 P1 清单第 2 条） |
| M5 | `src/stores/createModeChatStore.ts` 双实例 watch | 同 mode='ssh' 两个 store 实例各自注册 agentStore.status 固化 watch，Agent 结束时 chat store 的 watch 也会执行，非发起方 store 出现误固化/会话删除风险 | （待修，见 P1 清单第 3 条） |

> 注：M3/M4/M5 修复涉及 API 层批量改动，列入 P1 待办清单，本轮先修全部 HIGH 与低耦合 MED。

---

## 二、待办清单（按优先级）

### P1 — 建议尽快修复（本轮未动）

| # | 位置 | 问题 | 建议修法 |
|---|---|---|---|
| 1 | `routes/schedules.py:30,44` | interval_minutes 无 ge/le 校验 | `Field(ge=1, le=525600)` |
| 2 | `routes/chat.py:422,435`、`routes/skills.py:45`、`routes/agent.py:1152` | limit/offset 无边界（limit=-1 = 全表） | `Query(ge=1, le=500)` |
| 3 | `stores/createModeChatStore.ts:473-525` | 双实例固化 watch 跨 store 误执行 | watch 加"本 store 为发起方"守卫 |
| 4 | `routes/chat.py:301-349` | 客户端断连后走正常完成路径，半截回复无中断标记入库，污染下轮 LLM 历史 | 断连分支带标记入库且置 failed |
| 5 | `routes/audit.py:58-70` | /audit/export 同步全表拉取跑在事件循环上 | `asyncio.to_thread` + 行数上限 |
| 6 | `database.py:1514-1529` | set_default_model 两条非原子 UPDATE，并发可致 0 或 2 个默认模型 | 单事务条件 UPDATE |
| 7 | `harness/reins.py:110-134` | check 丢弃 intercepted=False 拦截器的 events/inject_messages（语法错误反馈/纠偏全丢） | 聚合未拦截决策的 events 后返回 |
| 8 | `harness/safety.py:245,308` | 命令链切分符不含单独 `&`，`ls & <未知命令>` 绕过默认审批 | 切分正则加单 `&`（先匹配 `&&`） |
| 9 | `agent/audit.py:163-198` | append_record 同步 sqlite 在事件循环执行，持锁时阻塞 5s | `await asyncio.to_thread(append_record, ...)` |
| 10 | `electron/main.ts` 全部 ipcMain.handle | 无 sender 校验，受控渲染进程可直调 ssh:connect/fs:*/backend:getToken | 统一封装 senderFrame.url 白名单校验 |
| 11 | `electron/main.ts:1226-1231` | sftp:upload/download 的 localPath 无 assertSafePath | 传输前执行 assertSafePath |
| 12 | `electron/path-guard.ts:59-63` | Windows 敏感段比对未小写化，`C:\Users\other\.SSH` 可绕过 | 段循环大小写不敏感比对 |
| 13 | `electron/main.ts:1291+` | 无 uncaughtException/unhandledRejection 兜底 | 注册顶层处理器：日志+清理 |
| 14 | `electron/main.ts:652,1238` | fs:writeBinaryFile/sftp:writeFile 无大小上限 | 统一 50MB 钳制 |
| 15 | `electron/ssh-manager.ts:446` | SSH/SFTP 会话数无上限 | doConnect 检查 sessions.size ≤ 64 |
| 16 | `electron/main.ts:1343` | darwin window-all-closed 销毁 PTY/SSH 且 stop 后端，activate 重建窗口后后端不重启 | 延迟到 before-quit 销毁 |
| 17 | `electron/backend-manager.ts:128` | 后端 code=0 异常退出时不自动重启不推 error，前端停留 ready | 非 intentionalStop 时 tryAutoRestart 或推 error |
| 18 | `stores/chat.ts:59-73,184-200` | loadMessages 对"流进行中"无守卫，整表替换可能丢弃进行中的消息 | streaming 时跳过/延迟到流结束后 |
| 19 | `tools/builtin/grep_tool.py:107` | 未防正则灾难性回溯 | 拒绝嵌套量词或带超时匹配 |
| 20 | `harness/reins.py` 多处 async 路径 | 同步 IO 未 to_thread（SyntaxVerifier/LoadSkill/record_file_write/_emit_done） | 统一 `asyncio.to_thread` |

### P2 — 低优先级 / 可接受风险

- `electron/main.ts:88` prod 下 will-navigate 放行任意 file://（应限 renderer 目录前缀）
- `electron/main.ts:43` sandbox:false（preload 标准 contextBridge，可改 true 缩小暴露面）
- `electron/main.ts:1053` ssh:listConnections 返回明文密码（列表只返非敏感字段）
- `electron/ssh-manager.ts:884` sftpMkdirRecursive 以 message includes('exists') 判已存在（OpenSSH 返回 SSH_FX_FAILURE 无 exists 字样）
- `electron/ssh-manager.ts:1016,1219` .part.size===total 分支跳过前 4KB 一致性校验
- `electron/main.ts:275; backend-manager.ts:238` execSync(reg query/taskkill) 同步阻塞主进程
- `electron/ssh-store.ts:232-251` save/delete 读-改-写无串行化（参照 known-hosts 的 savePromise 模式）
- `electron/ssh-store.ts:79-85` 'plain:' 旧存储为 base64 混淆，建议加载时一次性重加密
- `electron/ssh-manager.ts:360-375` TOFU 换钥后无 UI 重置路径（需手改 known-hosts.json）
- `electron/ssh-manager.ts:1329` 下载 EPERM rename 失败即删 .part（保留 + 标 retryable 更优）
- `electron/pty-manager.ts:79-124` shell 白名单替换在 args 校验之后（校验对象与执行对象不一致）
- `electron/main.ts:979-997` pty:completeCommand stdout 无上限（加 256KB 钳制）
- `electron/main.ts:584-642` fs:copy 整棵树先入内存（百万文件 OOM 风险）
- `electron/ssh-manager.ts:1487,1548` 远程 /tmp 打包脚本全局可读（放 0700 私有目录）
- `electron/backend-manager.ts:16,128` intentionalStop 闩锁的旧进程 exit 晚到竞态（epoch 计数治理）
- `stores/agent.ts:196-206` onError 无条件置 failed，与 onDone 守卫不对称（窄窗口完成态被翻转）
- `stores/sftp.ts:193,993` sftpCancel Promise 同步 try/catch 捕获不到 reject
- `stores/settings.ts:194` 等 localStorage.setItem 无 try/catch（配额耗尽中断发送流程）
- `stores/ssh-ai.ts uploadFiles` dual 模式传本地 conv id 而非 backendConvId（附件关联错位）
- `utils/think-scrubber.ts:136-144` _findCloseTag 按列表序而非最早出现位置（CoT 泄漏窄窗口）
- `LocalTerminal.vue:95 / RemoteTerminal.vue:96` 卸载后 terminal 未置 null，补全竞态守卫失效
- `LocalTerminal.vue:155 / RemoteTerminal.vue:152` ptyWrite/sshWrite 裸调用无 .catch
- `SftpView.vue:613-645` onTransfer 并发拖拽第二份确认静默丢弃第一份
- `ConnectionModal.vue:246` saving/testing 时 Esc/遮罩仍可关闭但请求继续落库
- `SideChatPanel.vue:132-159` 技能菜单无点击外部关闭
- `agent.ts spawn_subagent.py:137` 子 Agent LLM token 不进 CostTracker
- `harness/driver.py:197,238` 等 ReflectionRein 事件路由死代码
- `harness/driver.py:540` POST_TOOL_CALL 拦截分支漏 `await telemetry.emit`
- `tools/web_tools.py:401-427` 重定向 5 次上限后落入成功分支返回空正文
- `consciousness_tick.py:333-343` _reflect_on_failures 对同一批失败 run 每 tick 重复写反思记忆
- `tools/todo_write.py:15` + `schedule_engine._run_agent_once` 定时 run 结束不清 clear_todos（模块级 dict 缓慢泄漏）
- `schedule_engine.stop()` 不取消进行中 _run_task，关停时 schedule_runs 滞留 'running'
- `routes/audit.py:33-47` total count 与 items 过滤集不一致
- `routes/terminal.py:73-145` /terminal/predict 无整体超时，recent_commands 无上限
- `database.py:582` _probe_first_model 同步 httpx 在 lifespan 事件循环内直调
- `stores/useChatPersistence` backend 刷新丢失每会话 agentMode 记忆
- `client.ts:542-551,776-785` streamChat/streamAgentRun 的 !res.ok 分支 onError 未包 try/catch

---

## 三、无发现维度（已核查通过）

- **SQL 注入**：全部参数化，f-string 仅拼常量列名/白名单键
- **API key 泄漏**：/models、/settings/models 均经 `_mask_api_key` 脱敏，mask_key=False 仅内部调用
- **XSS**：全仓唯一 v-html 在 MarkdownStream，经 marked + DOMPurify.sanitize（含 rel=noopener 补丁）
- **路径穿越（后端）**：上传文件名取最后段 + 扩展名白名单 + conv id 正则消毒；rmtree 有 parents 校验
- **命令注入（Electron）**：bash 补全 shellQuote 闭合、远程 zip/unzip shellQuoteRemote 闭合、PowerShell -Command 单参数数组
- **凭据存储**：password/passphrase 走 safeStorage 加密，不可用时拒存明文不降级；私钥读取白名单
- **webPreferences**：contextIsolation:true + nodeIntegration:false；setWindowOpenHandler 非 http(s) 拒绝；will-navigate 有守卫
- **资源清理（前端）**：ResizeObserver/xterm/EventSource/IPC 订阅均配对清理
- **交互死锁**：所有遮罩均有按钮 + 遮罩点击 + Esc 关闭路径
- **时区**：全链路 UTC 存储 + 本地换算，无混用比较
- **llm_adapter**：LRU 客户端 in-flight 计数 + aclose

---

## 四、验证

- `python -m py_compile`：schedule_engine / file_changes / stirrups / harness / schedules / database / main / electron_bridge ✅
- `vue-tsc --noEmit`：前端全量类型检查 ✅
