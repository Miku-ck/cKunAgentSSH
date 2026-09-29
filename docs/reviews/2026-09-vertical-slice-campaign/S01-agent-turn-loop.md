# S1 · Agent 回合循环与 SSE 流

锚点 `a8e588c`。本节每条 LOCATION 我均对锚点复核（`git show a8e588c:<path>`），涉及文件与 a8e588c 的差异已注明。
共享管道归属本切片：`driver.py`/`harness.py`/`stirrups.py`/`bridle.py`/`saddle.py`/`contracts.py`/`telemetry.py`/`llm_client.py`/`intention.py` + `reins/core|flow|defaults|clarification` + `tools/__init__.py` 派发 + `todo_write`/`spawn_subagent`。功能专属工具按分布规则留给各自切片。

## 发现表

| ID | 维度 | 严重度 | 标题 | 主 file:line | 可达性 |
|---|---|---|---|---|---|
| S1-01 | b | Major | 中止/断连的 run 一条 assistant 消息都不落，`agent_trace` 快照与后处理一起丢掉 | `routes/agent.py`(anchor):1001-1052 唯一写区 与 :851 break | 确定性（点「停止」即触发） |
| S1-02 | a | Major | 第四个终态清理点（`streamAgentRun` 的 onError）漏关澄清向导、漏释放待交付 | `client.ts:945-951` → `agent.ts:252-269` | 可达（后端重启/代理超时/断网） |
| S1-03 | c | Minor | `/agent/tool-result` 只按 `tool_id` 认领，既不绑 run 也不回查 `requires_confirm` | `agent.py:1273-1299` + `pending_delivery.py:104-141` | 需持 token 的本机调用方 |
| S1-04 | c | Minor | 不可禁用的 `Sandbox` 底线只查 `shell_exec`，漏 `job_run`——而 `core.py` 明写两者同属命令入口 | `saddle.py:55-56` vs `core.py:20` | 当前被可裁剪层兜住 |
| S1-05 | a | Minor | `plan_mode_block` 事件无投影分支亦无消费者，计划模式下的写拦截对用户完全不可见 | `plan_mode.py:413-423` → `telemetry.py:104-264` → `client.ts:698` | 可达（plan 模式） |
| S1-06 | a | Minor | `budget_extended` / `reflection_request` 每 run 上 Wire 无人读；`budget_extended.reason` 投影自一个生产者从不写的键 | `flow.py:392-409` / `lifecycle.py:143-159` → `telemetry.py:121-122,226-237` | 恒（reflection） |

## 详情

### S1-01 [dim b][Major] 中止即无痕
- LOCATION：`routes/agent.py`(anchor) `:851` `if await request.is_disconnected(): break` → 消费循环退出 → 落库区 `:1001`(`db.add_message` role=assistant `:1003`, `agent_trace=` `:1009`) 与 `:1040-1050` **同在 done 事件处理块内**（`:1053` 的 `except Exception … "done 事件处理失败"` 即其闭合证据）→ `:1023` `get_post_process_manager().submit(...)` 亦在该块内。渲染层入口 `src/stores/agent.ts:280-297 abort()` → `client.ts:970 abortController.abort()`。
- CLAIM：只有观测到 `done` 事件的 run 才会落 assistant 消息与轨迹快照。用户点「停止」、后端重启、代理断流 ⇒ `is_disconnected` 直接 break ⇒ **无 assistant 消息、无 `agent_trace`、标题/记忆/技能后处理全部跳过**；run 行由 finally 标 `failed`，`tool_calls` 行已在流式中写入。重载历史后用户只看到自己那句提问、没有半个回复，且服务端无从补救（`MessageItem.vue:35,90` 的「轨迹」按钮要求 `message.agentTrace?.lastRunId`）。
- 这条不是「设计如此」：`:1041-1046` 的注释正是为「失败早退也要落一条承载轨迹」而写的，`final_state != "completed"` 时文案为「(AI 运行失败，未产生输出)」——即作者明确要让失败run留痕，但**该分支仍在 done 块内**，早退到不了。
- REACHABILITY：确定性。`abort()` 一条路径即命中；LLM 失败子路径反而安全（`driver.py:249-253` 发 error 后仍 `_emit_done`）。
- EVIDENCE：`git show a8e588c:backend/routes/agent.py | grep -n 'role="assistant"\|agent_trace=\|is_disconnected'` → assistant 仅 1003/1042、agent_trace 仅 1009/1050、is_disconnected 仅 851。该文件工作区有另一会话的 +8 行改动，本条全部引自锚点。

### S1-02 [dim a][Major] 第四终态清理点漏两处收尾
- LOCATION：`client.ts:891-901/903-906/945-951/956-966`（四个 `onError` 调用点）→ `src/stores/agent.ts:252-269`（onError 处理器）对照 `:293`（abort 清向导）、`:347`（_reset）、`:624`（case error）、`:709`（case done）、`:764`。
- CLAIM：memory 里登记的「run 终态清理口径要三处一致」在本切片实际是**四处**，而第四处只做了 `status='failed'` + `permissionRequests=[]` + `abortFn=null`。漏项二：
  1. `clarificationWizard` 未清 ⇒ `ClarifyingWizard.vue:185` 的 watch 使 `visible=true` 常驻，`clarify-overlay` 蒙皮盖在已终止的 run 上，用户只能答题 POST 向死 run_id 或另起 run；
  2. `agentDeliver.abortRun(lastRunId)` 未调 —— 全仓 `abortRun` 仅 `agent.ts:296`（abort 内）**一个**调用点（已 grep 证实）。于是 `pending_confirm` 仍在 `AgentRunPanel.vue:389-403` 显示「命令待确认」，用户点确认 ⇒ `useAgentDeliver.deliverCommand:237-239` **把命令写进活着的 PTY/SSH 终端**，而没有任何 run 在等结果；随后的 POST 因流 `finally` 已 `clear_run` 而 404。
- 代码自己写了理由：`:290-292`「同 done/error 分支：run 已终止，继续挂着向导会让用户答一张没人收的题」——该口径在 abort/done/error 三处成立、在 onError 不成立。
- REACHABILITY：流未收到 `[DONE]` 即结束（后端重启、代理 idle-timeout、断网）→ `client.ts:948`。与 S1-01 同源不同面：S1-01 是服务端少落库，本条是前端少收尾。

### S1-03 [dim c][Minor] 交付回传不绑 run、不回查确认
- LOCATION：`client.ts:979-998`（body 仅 `{tool_id,output,success,exit_code,rejected}`，**无 run_id**）→ `routes/agent.py:1273-1299` `ToolResultRequest` → `pending_delivery.py:104-141` `resume()`/`reject()` 只 `self.get(tool_id)`。
- CLAIM：`create()` 时记了 `run_id` 与 `requires_confirm`（`:71`），但 `resume()`/`reject()` 两个字段都不看。因此「用户从未确认」与「用户确认并已执行」在服务端不可区分：`requires_confirm=true` 的破坏性命令可被直接闭合成 `success=true`，`driver.py:742` 随后把 `[已写入可见终端] {command}\n{output}` 写进模型上下文——**模型与据此生成的审计链记录了一次没有发生的执行**。run 存活侧是对的（`clear_run` 后条目消失 ⇒ 陈旧 POST 404）。
- REACHABILITY：桌面应用双调用方，且需持 `X-API-Token`（`main.py:206-220` 覆盖 `/api/*`，默认 bind 127.0.0.1）。**不是远程可 exploited 的洞**，按审批邻近路径的完整性缺口报。
- grep 佐证：`requires_confirm` 在 backend 仅出现在 `pending_delivery.py:71` 与 `shell_exec.py:71-87`，消费方只有渲染层 `useAgentDeliver.ts:276`，服务端 resume 路径从不校验。

### S1-04 [dim c][Minor] 不可禁用层与可裁剪层口径不齐
- LOCATION：`saddle.py:55-56` `if tool in ("shell_exec",): cmd = args.get("command") or args.get("shell","")` ← 对照 `reins/core.py:20` `_COMMAND_EXEC_TOOLS = {"shell_exec","job_run"}` 及其注释「job_run 同属命令入口…其 `cmd` 参数必须经 classify_command 精细判定」
- CLAIM：`Sandbox.validate` 的 docstring（`:41-47`）把自己定位成「不像 FilesystemGuard 那样可被装配方裁剪的**不可禁用**安全底线」，但它的 shell 命令检查只覆盖两个命令入口中的一个，且不读 `cmd`（`core.py:110 command_from_args` 的口径是 command/shell/cmd 三键）。经 `job_run(action=…, cmd="…")` 提交的 hardline 命令在 Saddle 层没有内容级拦截，唯一屏障恰是它声称不可依赖的那一层。`ops_job.py:27` 的自述「内部命令的审批/安全分类由 FilesystemGuard 按 cmd 参数精细判定」也印证：这一层从未打算自己兜。
- REACHABILITY：**默认装配下不可达**——`defaults.py:44-45` 恒注册 FilesystemGuard，且 `Reins.check`（`core.py:253,279-283`）对 `safety_critical` 拦截器抛异常一律 fail-close。故这是纵深防御的不对称，不是可利用绕过。按 Minor 报。

### S1-05 / S1-06 [dim a][Minor] 三类事件上 Wire 无人接
- `plan_mode_block`：生产 `reins/plan_mode.py:413-423`（payload 含 `tool`/`reason`）；`telemetry.py:104-125 _KIND_MAP` 无此项、`:150-264 to_sse_dict` 无对应分支 ⇒ 只带 `type/iteration/run_id` 上线，`tool`/`reason` 在投影阶段就丢了；`client.ts:698` 的 16 个 union 成员无此项；`agent.ts:421-721` 的 switch **无 default** ⇒ 静默丢弃。叠加 `driver.py:482-511` 的拦截分支在 `tool_call_start`(:529) **之前** continue ⇒ `run_trace_tool_calls`(:918) 与 `agent_trace.toolCalls` 都不留痕。用户感知：plan 模式下 agent「不干活了」，只有模型被告知（`driver.py:487`）。全仓 grep `plan_mode_block` 在 `src/` 零命中（后端测试 `test_plan_mode.py:242-438` 与 `docs/plan-mode.md:189` 有）。
- `budget_extended`（`flow.py:395`）与 `reflection_request`（`lifecycle.py:157`）：`_KIND_MAP:121-122` 有、投影分支 `:226-237` 有，但 `src/` 零消费者。`reflection_request` 每 run 必发必弃；`lifecycle.py:93` 写「路由层只需监听该事件即可触发异步反思任务」，而 `routes/agent.py` 从不检视它（反思实际由 done 分支 `:1090-1106` 触发）⇒ **该 docstring 描述的契约不是生效中的契约**。字段级：`telemetry.py:229 result["reason"] = payload.get("reason","")`，而 `flow.py:399-407` 的 payload 从未放 `reason` ⇒ 将来任何消费者恒得空串。
- 反向核对：union 里 16 个成员**全部**有活生产者，不存在「前端等一个没人发的事件」。

## SSE 事件对账（只列异常行，其余 13 行 ok）

| event | 生产者 | 消费者 | 状态 |
|---|---|---|---|
| tool_call_result | `driver.py:492`（拦截变体）/ `:623` / `:779` | `agent.ts:460` | **field-missing**：拦截变体不带 `tool_call_id` 且无配对 start ⇒ 落 name+iteration 匹配，并被 `routes/agent.py:931` 的 `if pending_tool_call_ids:` 丢弃；`duration_ms`（telemetry:165）从不被读 |
| warning | `flow.py:491-496`（CostBudgetRein） | `agent.ts:510` | 投影丢字段：`total_tokens/total_cost_usd/max_*` 被 `telemetry.py:168-169` 丢掉，无读者 |
| human_help_needed | `flow.py:213` | `agent.ts:521` | `telemetry.py:174-175` 投影的 `tool`/`args` **从未出现在 payload** ⇒ 恒缺 |
| done | `driver.py:859` | `agent.ts:693` | `state_history`（telemetry:213）无读者 |
| intention | `driver.py:161` | `agent.ts:629` | `reclassified`/`previous_level` 被 `telemetry.py:219-225` 丢弃 |
| budget_extended / reflection_request / plan_mode_block | 见 S1-05/06 | — | **no-consumer** |

## 工具派发门链地图（供 S2/S6/S7 引用，不重推）

执行性派发点唯一：`driver.py:539 harness.saddle.execute(...)`，只能从 `_execute_tool_calls` 的 tool_calls 循环内、经 `:481` 的 PRE_TOOL_CALL 检查后到达；`intercepted=True` 在 `:482-511` continue/break，**永不**落到 `:539`。`grep -rn "\.saddle\.execute(\|registry\.execute("` 除 `driver.py:539` 与 `saddle.py` 内部外无生产调用方。

默认装配的 PRE_TOOL_CALL 优先级（`defaults.py:39-95`）：**LoopDetector(5) → PlanModeGuard(6) → ClarificationInterceptor(7) → PermissionGate(8) → FilesystemGuard(10)**；`Reins.check`（`core.py:233-286`）首个 intercepted 即短路，`safety_critical` 拦截器抛异常 ⇒ fail-close。其下 Saddle 自带 `Sandbox.validate`(:307) 与 `UndoBuilder.build`(:312)。

已查证无逃逸：`spawn_subagent`（其 `StreamLLMClient.call` 传 `tools=[]`，不派发任何工具）、`/agent/tool-result`（只 set 一个 `asyncio.Event`）、trace 端点（只读库）、可见终端分支（`:546` 在 PRE_TOOL_CALL 之后，`_finish_visible` 于 `:770` 重发 POST_TOOL_CALL 并刻意丢弃决策——`:767-769` 有注释）。

地板**无法证明成立**的两处（各自归属切片跟进）：① `job_run` 的内容级 hardline 仅靠可裁剪层（S1-04）；② 可见终端模式的真实执行发生在渲染层（`useAgentDeliver.deliverCommand:237-239`），其门是工具侧规则 `shell_exec._compute_requires_confirm`，服务端在重交付时不复校（S1-03）。

## 闸门

- `git diff a8e588c --stat -- src/stores/agent.ts backend/agent/harness/saddle.py backend/agent/pending_delivery.py` → **空**，这三份文件与锚点逐字节一致，本切片引用其工作区行号即锚点行号。
- `routes/agent.py` 有另一会话的 +8 行未提交改动 ⇒ 该文件所有行号引自 `git show a8e588c:`。
- 后端 `-k` 子集未跑（共享库锁，见 S0 记录）；S1 全部 6 条为 `static-read-only`。
