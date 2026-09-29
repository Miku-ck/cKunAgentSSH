# S7 · 定时任务与桥接中继

锚点 `a8e588c`。范围：`routes/schedules.py`(6 端点) / `agent/schedule_engine.py`(558) / `agent/bridge_relay.py` / `agent/electron_bridge.py` / `tools/builtin/ops_schedule.py`；渲染层 `views/ssh/SchedulesView.vue`(786)、`client.ts:1087-1132`。跨切片：任务经 S1 的 `/agent/run` 同一条 harness 起跑，经 relay/`electron_bridge` 落到 S4 的 ssh 会话。

**行号来源分两类**：`schedule_engine.py`、`database.py`、`main.py` 属并行会话在写的脏文件 ⇒ 一律取自 `git show a8e588c:<path>`（下文标 anchor）；`routes/schedules.py`、`ops_schedule.py`、`bridge_relay.py`、`electron_bridge.py`、`SchedulesView.vue`、`src/utils/format.ts` 经 `git diff a8e588c --stat` 确认空差异 ⇒ 行号即工作树。`src/api/client.ts` 有在写 hunk（283-295/813-875），本片引用的 1088-1172 与 53-102 区间逐字节等同 anchor。

## 任务书点名的问题：复核 `439102d` 之后「桥不可达」的结构化错误是否仍完整送达

**结论：DB 记录与执行历史弹窗两层完整，实时面一层都不存在。** 逐跳（全 anchor 行号）：

1. `electron_bridge.py:148-161` `probe()` 返回 `BridgeErrorCode.WS_LOST`/`NOT_CONFIGURED`，**全程不读 `bridge.port`** ⇒ 当年的 `AttributeError` 顶掉 `BridgeError` 那条路径已死。
2. anchor `schedule_engine.py:343` `probe_error = await bridge.probe()` → `:347` → `:351-353` `logger.warning(... [%s], probe_error.value)` → `:354` `raise BridgeError(probe_error)`。`:348-350` 是墓碑注释。
3. `electron_bridge.py:53-58`：`super().__init__(f"[{code.value}] {self.suggestion}")`（detail 为空才走这一支）。**提交信息说的「拼进 detail」不精确**——什么都没进 detail，suggestion 之所以活下来恰恰**因为** detail 是空的。这一点派生出 S7-15。
4. anchor `:271-275` `except Exception as e: update_schedule_run(run_id, status="failed", error=f"{type(e).__name__}: {e}")`。
5. anchor `database.py:2552-2583` 原样写进 `schedule_runs.error`（无截断，**没有 `error_code` 列**，整棵 taxonomy 只以文本存在）。
6. `SchedulesView.vue:68` `{{ r.error }}` 渲染；`:647-657` `askAiDiagnose` 再把它喂回 AI 面板。

用户能读到的串是 `BridgeError: [BRIDGE_WS_LOST] …建议全文…`，code 与 suggestion 都在，但**只在执行历史 modal 里**；卡片本身（`:42`）只有「状态：失败」。**没有 schedule SSE**：`_run_agent_once` 本地消费 harness 流（anchor `:404-420`），不外发 ⇒ 见 S7-07。

## 缝表

| 渲染层 | 端点 | 路由处理器 | 判定 |
|---|---|---|---|
| `client.ts:1088 listSchedules` | GET `/schedules` | `routes/schedules.py:73` | ok |
| `client.ts:1092 createSchedule` | POST | `:84` | `ScheduleIn.name` 有 `min_length=1,max_length=100` |
| `client.ts:1096 updateSchedule` | PATCH | `:106` | **同一字段在 PATCH 侧零约束**（S7-12） |
| `client.ts:1100 deleteSchedule` | DELETE | `:131` | 先 `fail_running_schedule_runs` 再 `delete` ⇒ 被 CASCADE 抹掉（S7-16） |
| `client.ts:1104 runScheduleNow` | POST `/schedules/{id}/run` | `:141-153` | 恒 `{"ok":true}`，run 是 fire-and-forget（S7-07） |
| `client.ts:1108 listScheduleRuns` | GET `/schedules/{id}/runs` | `:155` | ok（clamp 1..200，UI 取 50） |
| `client.ts:1104 ScheduleRecord.last_run_id: string` | — | anchor `database.py:379 last_run_id TEXT`（可 NULL） | **类型谎报 + 全 src 零消费者**（S7-08） |
| AI 侧 `schedule_op` 工具 | — | `ops_schedule.py:196 execute` | 直接写库、绕过 REST 校验（S7-06/S7-10/S7-18/S7-19） |
| `SchedulesView.vue:413 LIST_POLL_MS=30000` | — | 与引擎 tick（anchor `:115 sleep(_poll_seconds)`=30s）同周期 | 数字一致，但注释（`:26`）仍写「每 3s 一轮」（S7-12 附带） |

## 发现表

| ID | 维度 | 严重度 | 核验 | 标题 | 主 file:line | 可达性 |
|---|---|---|---|---|---|---|
| S7-01 | c | Major | ✅ | 被成本熔断/迭代上限/循环检测/求助终止的 run 一律记 `completed` —— done 载荷里的 `state` 字段被整段丢弃 | anchor `schedule_engine.py:246` + `:415-420` ↔ `driver.py:379-382,398-426,503-510` + `driver.py:868` | 恒（无头 8 轮上限很容易撞 2） |
| S7-02 | a | Major | ✅ | 未指定目标主机的任务每个远程工具都 fail-closed，run 却记 `completed`，且 system prompt 从不告诉模型「这次没有会话」 | anchor `schedule_engine.py:320-329,398,545-559` + `ops_common.py:172-173` | 恒（UI 下拉首项就是「未指定」，`SchedulesView.vue:167`） |
| S7-03 | b | Major | ✅ | `trigger()` 吞掉 next_run 推进失败仍继续起跑 ⇒ 同一任务每个 tick 反复触发，无上限、无自愈 | anchor `schedule_engine.py:157-162` + `:124-131` | 脏行或持续 DB 锁（见正文的升级条件） |
| S7-04 | a | Major | ✅ | 后端重启把 run 永久卡在 `running`：编辑/立即执行按钮被置灰，且启动序列里没有跨表 stale 对账 | anchor `schedule_engine.py:239-242,253-279` + anchor `main.py:100-126` + `SchedulesView.vue:31-32,407-409` | 任何落在 run 窗口内的重启 |
| S7-05 | a | Major | ✅ | 开机首 tick 把所有到期任务无限并发扇出（无全局并发上限），叠 S0-05 时连用户自己的终端都连不上 | anchor `schedule_engine.py:117-135,156,168` | 关机过周末 + 若干 daily/weekly |
| S7-06 | c | Major | ✅ | 一次 `schedule_op` 的「始终允许」= 永久放行**该工具全部 action**（授权键是裸工具名），含删除用户手工配的任务；headless run 同样可用 | `gate.py:305-311,327,343-350,696-700` + `ops_schedule.py:19,187-194,298-310` + anchor `schedule_engine.py:308-309` | 需用户一次显式「始终允许」 |
| S7-07 | a | Major | ✅ | 「立即执行」谎报：`POST /schedules/{id}/run` 恒回 `{ok:true}`，桥完全断开时也一样 | `routes/schedules.py:141-153` + anchor `:158-175` | 恒 |
| S7-08 | d | Major | ✅ | 后台终态通知实际从不发出：去重只看状态字符串（第 2 次及以后一律静默）+ 轮询只在页面 active 时跑 | `SchedulesView.vue:393-405,413-424,429-437` + `client.ts:1104` | 恒（任何 recurring 任务的第 2 次终态） |
| S7-09 | a | Major | ✅ | `ops_schedule._create` 硬编码 `target_host: ""` 且 schema 无该参数 ⇒ AI 排的巡检天生跑不到任何机器上（沉默降级，非报错） | `ops_schedule.py:251` + `:138-180` ↔ anchor `schedule_engine.py:320` | 恒（任何 schedule_op create） |
| S7-10 | a | Minor | ✅ | anchor 的输出上限把 `max_tokens` 一列当两件事用，`max_output_tokens` 在定时链路全程无人读 | anchor `schedule_engine.py:296,379-386` ↔ `routes/agent.py:717-722`、`safe_tokens.py:29-31` | 恒（所有定时 run） |
| S7-11 | d | Minor | ✅ | PATCH 绕开 POST 的 name 上下限；rationale 注释保护的大字段（`task_prompt`）三处都无上限 | `routes/schedules.py:26-28` vs `:42-54,63-70` + `ops_schedule.py:237` | API 恒可（UI 有 `!form.name.trim()` 挡空名） |
| S7-12 | d | Minor | ✅ | 前端每次保存都带全部调度键 ⇒ 只改个错别字也把 interval 任务的下次执行重锚到「现在+一个周期」 | `SchedulesView.vue:465-482,488-492` + `routes/schedules.py:110,123-125` | 恒（编辑任意 interval 任务并保存） |
| S7-13 | b | Minor | ✅ | interval 以「上次实际触发时刻」为锚、不贴墙钟，且逐周期单向累积漂移永不回收 | anchor `schedule_engine.py:498-502,115-116` | 恒 |
| S7-14 | c | Minor | ✅ | 同一个 `BridgeError`，只有 probe 那一条带得出 suggestion，其余三条 raise 都因 detail 非空把它顶掉 | `electron_bridge.py:58` ↔ anchor `schedule_engine.py:332-335,360-363,367-374` | anchor `:374` 那条 = 目标主机连不上，最常见 |
| S7-15 | c | Minor | ✅ | DELETE 前那跳「落定执行历史」必定被 `ON DELETE CASCADE` 抹掉：删掉运行中的任务，界面不留任何「曾有一次被中断」的痕迹 | anchor `schedule_engine.py:197-200` ↔ `database.py:388,64` | 恒（删除运行中的任务） |
| S7-16 | c | Minor | ✅ | `stop()` 不清 `_executing`/`_deleted_sids`，与 `remove_schedule` 的 done-callback 补偿不对称（**当前进程内无第二次 start，不可达**） | anchor `schedule_engine.py:89-94` vs `:186-196,121-122` | 不可达 ⇒ 按规则降级 |
| S7-17 | a | Minor | ✅ | cron 报错文案承诺 `*` 可用、实际拒绝：模型收到自相矛盾指引会原样重试 | `ops_schedule.py:37-38,75,80-86,134-137` | 恒（模型写 cron 的常见形态） |
| S7-18 | a | Minor | ✅ | 空 `task_prompt` 也能建成任务，无头那次自动换成内置默认巡检词——用户要的和他拿到的不是同一件事 | `ops_schedule.py:233-254` + anchor `schedule_engine.py:317-319` | 恒（模型漏参） |
| S7-19 | a | Minor | ✅ | anchor 启动注释写「懒启动，仅在存在定时任务时 begin」，实现是无条件 `start()` ⇒ 零任务用户也每 30s 空转一次全表查询 | anchor `main.py:117-120` | 恒（无害但注释说谎） |

**跨切片引用（不在本片计数，避免双计）**：`_resolve_model()` 只读全局 ⊕ 而面板 ★ 文案承诺定时任务读 SSH 档 —— 见 **S10-01**（同一事实，home 在模型档位解析那侧）。本片只补充它的下游后果：`未配置默认模型` 的 RuntimeError 经 S7-01 之外的 `:271-275` 分支记成 failed，错误串里连 code 都没有。

## 详情

### S7-01 [dim c][Major] 终止路径的信息都在 done 载荷里，被丢的是消费端
- LOCATION：anchor `schedule_engine.py:244-251`
  ```python
  content, err = await self._run_agent_once(sched, run_id)
  snippet = (content or "").strip()
  status = "failed" if (not snippet and err) else "completed"
  ```
  与 anchor `:415-420`：`async for ev in harness.run(...)` 里 `if ev.kind == "error": … elif ev.kind == "done": done_content = payload.get("content")` —— **只取 content，`state` 从未读**。而 `driver.py:858-883` 的 done 事件 payload 明确带着 `"state": final_state.value`（`:868`）与 `state_history`。
- CLAIM：driver 的四条终止路径都不发 `error` 事件（`kind="error"` 在 driver 里只出现在 `:243` 的 LLM 失败分支），于是全部落进 `completed`：
  1. **成本熔断**：`reins/flow.py:481-497` `terminate=True` 且只发 `kind="warning"` → `driver.py:379-382` `transition(FAILED)` + break → 走 `_emit_done`。定时任务从不传 `max_cost_usd`，命中 `flow.py:437` 的 `default_max_cost_usd=2.0` 默认熔断。
  2. **MAX_REACHED**：`driver.py:398-426` 发 `warning`「达到最大迭代数 8」，且 `driver.py:404-407` 在无正文时**用工具日志合成一份摘要当 content** ⇒ snippet 非空 ⇒ 历史里躺着一份看起来正常的报告，任务其实被腰斩。
  3. **LoopDetector 第 3 次 force_terminated**：`driver.py:503-510` FAILED + break；正文空且无 error ⇒ `not snippet and err` 为假 ⇒ `completed` 且 `summary=""`。
  4. **human_help_needed**：`driver.py:385-388` FAILED + break ⇒ 同上。
  附带第 5 型：`driver.py:249-254` 的 LLM 失败分支**同时**发 `error` 和带既往 `full_content` 的 `done` ⇒ 早期轮次已有正文的 LLM 失败也记 completed。
- 可达性：恒。无头通道 `_SCHEDULE_MAX_ITERATIONS` 固定 8 轮且 `enable_thinking=True`（anchor `:391-397`），巡检类 prompt 撞 (1)(2) 是常态而非边角。
- 判定说明：这条的**修法几乎是免费的**（`:420` 多读一个 `payload.get("state")`），但它改变「什么叫失败」的产品定义（腰斩算不算失败、合成摘要算不算正文），故按只报告口径交裁决。

### S7-02 [dim a][Major] 「未指定目标主机」不是不执行远程命令，而是执行一次全部失败的 run
- LOCATION：anchor `:320` `host = (sched.get("target_host") or "").strip() or None` → `:329 if host:` 整段建会话逻辑被跳过 → `ssh_session_id=""` 进 `harness.run(ssh_session_id=…)`（anchor `:407`）。`ops_common.py:172-173` 的 `run_headless` 对空会话返回 `(False, "拒绝执行：缺少 SSH 会话…")`。
- CLAIM：模型收到一串工具失败，于是写一份「我无法连接」的报告 ⇒ content 非空 ⇒ S7-01 的判据给 `completed`。更根本的是 anchor `_build_schedule_system_prompt:545-559` **无条件注入 SSH_MODE_PROMPT，且从不提 target_host、也不说「这次没有会话」**——任务书让模型去巡检，却没告诉它没有可巡检的对象。
- 可达性：UI 的「目标主机」下拉首项就是 `value=""`，标签写着「未指定（不执行远程命令）」（`SchedulesView.vue:167`）。**文案与实现相反**：不是不执行，而是执行并全败。`ops_schedule` 建的行更是恒空（S7-09），所以 AI 排班 100% 走这条路。

### S7-03 [dim b][Major] next_run 推进失败被吞掉之后仍然起跑
- LOCATION：anchor `:157-162`
  ```python
  try:
      if not manual:
          next_at = compute_next_run(sched)
          await asyncio.to_thread(db.set_schedule_next_run, sid, next_at)
  except Exception as e:
      logger.warning("schedule=%s 计算 next_run 失败: %s", sid, e)
  ```
  然后**无条件继续** `:167-168` `create_task(self._run_task(...))`。
- CLAIM：行没被推进 ⇒ `_tick_once` 的 `due = _parse_ts(s["next_run_at"]) <= now`（anchor `:128`）下个 tick 仍为真 ⇒ run 结束释放 `_executing` 后再次触发，**无限循环**，每轮都是一次无头 Agent run + SSH 会话 + 模型计费。日志文案「计算 next_run 失败」还会把真失败点（DB 写那一跳）说成计算。两种触发都成立：
  1. `sqlite database is locked` —— 本仓已知共享写锁 hazard（`database.py:67` busy_timeout=5000）；
  2. `compute_next_run` 对脏行抛 `ValueError` —— `_local_to_utc` 用 `datetime(y,mo,d,h,mi)`，`h=25`/`mi=60` 直接炸；`_validate_fields` 只在 POST/PATCH 跑（`routes/schedules.py:30-31` 自述「审查修复」是**后加的**），**fire 时不校验**，所以存量旧行的脏值合法存在。
  我用 `python -c` 装载 anchor 源码 exec（stub 掉 `database`，零触库）喂 `time_hour=25` 复现了异常类型：`ValueError: hour must be in 0..23`。
- 兜底缺口（放大因子，如实登记）：全仓 grep `max_retries|retry_count|consecutive_fail` 在 schedules/引擎侧**零命中** ⇒ 没有失败计数、没有退避、没有「连续失败 N 次即停用」。这条不是缺陷本身，但它正是本条无自愈的原因。
- 裁决提示：**若你的库里确实存在脏行**（`SELECT id,time_hour,time_minute FROM schedules WHERE time_hour>23 OR time_minute>59`），本条应升 **Critical** —— 后果是无人值守反复对生产机执行远程命令并持续计费。默认按 Major 报是因为触发前提是「库里已有非法值或持续锁竞争」。

### S7-04 [dim a][Major] running 状态的唯一出口是那个已经死掉的 task
- LOCATION：anchor `:239-242` `create_schedule_run` + `mark_schedule_run(sid,"running")` → 只有 `_run_task` 自己的 `except CancelledError`（`:253-270`）/ `except Exception`（`:271-276`）/ 正常路径（`:246-251`）会落终态。启动序列 anchor `main.py:100-126` 里**没有任何跨表 stale 清理**。
- CLAIM：进程被杀或崩溃后由 BackendManager 自动重启（Electron 主进程不受影响，WS 通道重建后旧 `running` 行无人回收）⇒ `schedules.last_status` 停在 `running`，`schedule_runs` 留一行永不变动的 `running`。前端 `isRunning(s) { return s.last_status === 'running' }`（`SchedulesView.vue:407-409`）据此把「编辑」和「立即执行」置灰（`:31-32`），用户只能删了重建；自愈要等下一次到期触发（daily 任务 ≈ 最长 24 小时）。`fail_running_schedule_runs` **函数是有的**（anchor `database.py:2586-2598`），但全仓只有两处生产引用：`remove_schedule`（anchor `:201`，且见 S7-15 它被 CASCADE 废掉）和测试。
- 可达性：任何一次重启落在 run 窗口内；`interval=1min` 的任务几乎每 60 次重启必中一次。

### S7-05 [dim a][Major] 首 tick 无限并发扇出
- LOCATION：anchor `_run_loop:100-115`（注释「启动后先扫一遍」）→ `_tick_once:117-135` 对每个 `due` **`await self.trigger(s)`**，而 `trigger` 只做 `create_task`（anchor `:168`）就返回。
- CLAIM：一次 tick 内并发拉起 N 个无头 run = N 条 `connect_ssh`、N 份模型调用、N 份 $2 熔断预算。`_executing` 只是 **per-schedule** 去重（anchor `:149-156`），没有任何全局 semaphore 或串行队列。叠加 S0-05（`ssh-manager.ts:624-625` 的 `SESSION_LIMIT=64`，且 lost res frame 会让会话泄漏到上限）时，用户自己的终端也连不上。
- 与 S0-05 的分工：机制在 S0 已立案，**本条只补「schedule 侧把它放大成一次几十条」这个量化与来源**。

### S7-06 [dim c][Major] 授权键的粒度是「工具」，而 schedule_op 是多动作工具
- LOCATION：`gate.py:305-311`
  ```python
  if command and str(command).strip():
      … return f"{tool_name}:{digest}"
  return tool_name            # ← 无命令可绑定时就是裸工具名
  ```
  与 `ops_schedule.py:187-194`：`bound_command` **只在 `action=="create"` 且有 command 时**返回命令串，`action=list` / `action=cancel` / 只用 `task_prompt` 的 create 一律返回 `None`。消费点：`check_permission:343` 用它算 key、`:349 if self._permanent_active(key): return True`、`:398 grant()` 用同一个键写入永久池。
- CLAIM：用户在交互对话里对某张 `schedule_op` 卡片点「始终允许」（卡片一定会出：`meta = ToolMeta(risk_level="moderate", reversible=False)` ⇒ 走 `check()` 的 moderate 分支 `:694-700`，未授权即 `:707` 自动审批 → `:713+` 发卡），**永久授权的是该工具全部 action**，含 `cancel` 用户手工配置的任务。而 `_cancel` 只校验 id 非空（`ops_schedule.py:298-301`），不校验来源/所有权，直接调 `get_schedule_engine().remove_schedule(sid)`。同时 headless run 用同一个 `create_registry()`（anchor `:308-309`；`tools/builtin/__init__.py:166-172` 已无 mode 轴）⇒ `schedule_op` 在定时任务里可用，而永久 allowlist 优先于 session（`:327` 注释即检查顺序）⇒ **凌晨的无人值守 run 可以往 schedules 表插新任务或删掉用户配好的任务**，任务集合对自动变更不闭合。`ToolMeta` 未声明 `destructive_actions`（`:19`），所以 prod 也不会把 cancel 升成逐次 dangerous（`gate.py:544-548` 需要 destructive 非空）。
- 与已裁决项的区别：`file_write` 那一族当年就是靠 `bound_command` 把键从裸工具名收紧到目标粒度（`gate.py:300,323-325,379-381` 的注释正是这段历史）。**schedule_op 是多动作工具却只给 create 绑命令**，等于把同一道收紧做漏了一半。
- 我判 Major 而非 Critical 的理由（供裁决推翻）：仍需用户一次**显式**「始终允许」才成立；与 S2-01（无需用户动作即推翻其已给出的拒绝）不同族。若你认为「用户批的是查询、拿到的是删除」属审批绕过，则升 Critical。

### S7-07 [dim a][Major] 立即执行的 ok:true 与桥的实际状态无关
- LOCATION：`routes/schedules.py:141-153`：`started = await engine.trigger(sched, manual=True)`，只在 `started is False` 时回 409（那是**去重**命中，不是执行结果），否则 `return {"ok": True, …}`。trigger 内部是 anchor `:168` 的 `create_task`。
- CLAIM：路由不回读任何执行结果 ⇒ 桥完全断开时也回 200，用户看到 toast「已触发立即执行」，真实结局（`[BRIDGE_WS_LOST]` + 建议）要么等 30s 轮询落到卡片状态，要么主动打开历史 modal。这正是 `439102d` 提交信息里那句「不改『桥不可达时定时任务记什么状态』的语义裁决」所遗留的可见面缺口：**状态记对了，但没有任何实时面**。

### S7-08 [dim d][Major] 通知这条通道写好了却几乎不可能响
- LOCATION：`SchedulesView.vue:393-405`
  ```ts
  const body = NOTICE_BODIES[s.last_status]
  if (!body || s.last_status === before.last_status) continue
  ```
  轮询 `:413-424 LIST_POLL_MS = 30000` + `:429-437` 的 `watch(() => props.active, …)`（切走即 `stopListPoll()`）。
- CLAIM：两重失效叠加。① 去重判据**只有状态字符串、不看本次 run**：每天成功的任务只在第一次成功时通知一次，此后 completed→completed 全静默；每天失败的同理 failed→failed 静默 —— 而「定时任务失败」恰是这条通知存在的理由（`:380-382` 注释自己说「后台调度器触发的执行没有前端动作可挂靠，列表轮询是唯一能看到它进展的地方」）。② 轮询只在「停在定时任务页」时运行，而主进程刻意在窗口聚焦时不弹（`:385` 注释）⇒ 唯一能收到通知的组合是「停在该页 + 窗口未聚焦」；凌晨自动跑完的任务，用户下次打开应用早已过轮询窗口。
- 结构性原因（一并登记）：`schedules` 表没有 `last_error`/`error_code` 列（anchor `database.py:364-382`），卡片层想显示「为什么失败」必须再查 `schedule_runs`，所以通知文案只能是「执行失败，可在历史里查看」这种空话。而 `ScheduleRecord.last_run_id`（`client.ts:1104`）正是后端为此存的字段（anchor `:244` 写入），**全 src 检索只有这一行类型声明、零读取点** —— 被遗弃的对账锚点。

### S7-09 [dim a][Major] AI 排班建的定时任务永远不带目标主机
- LOCATION：`ops_schedule.py:251` `"target_host": "",` 硬编码；`parameters` schema（`:138-180`）里也没有该参数，模型无从传。
- CLAIM：后果不是报错而是**沉默降级**（与 S7-02 拼起来即为完整路径）：用户让 Agent「每天 3 点巡检 web-01」，任务建出来了、每天跑、每天「已完成」、每天什么都没查。
- 可达性：恒（任何 `schedule_op` create）。这是本片我认为最该先修的一条 —— 因为它把「AI 帮你排班」这个功能整条变成了假动作，而所有信号都显示成功。

### S7-10 … S7-19（简述，全部已按 LOCATION 打开原文核对）
- **S7-10** anchor `:296` `ctx_limit = int(model_cfg.get("max_tokens") or 16384)`（上下文窗口，与 `routes/agent.py:740` 同口径 ✓）；anchor `:379` `out_limit = int(model_cfg.get("max_tokens") or ctx_limit)` —— **读的是同一行同一列** ⇒ `out_limit == ctx_limit` 恒成立，`or ctx_limit` 是被吞的死操作数，`safe_out` 化简为 `ctx_limit // 2`。`:376-378` 的注释描述「用户把输出上限设得接近上下文窗口时钳一半」，实际是「无论用户设什么都是一半」，`max_output_tokens` 全程未参与。`python -c` 按 schema 默认代入：ctx=16384 ⇒ safe_out=8192，而 `max_output_tokens=4096` —— 定时 run 要的输出上限是用户配置值的 2 倍。与 91 清单 item 29 同族，**本条只补「表达式恒等 ⇒ 疑似列名写错（本意大概是 `max_output_tokens or ctx_limit`）」这一新事实**，不重议两通道口径是否要统一。
- **S7-11** `ScheduleIn.name = Field(..., min_length=1, max_length=100)`（`routes/schedules.py:26-28`）对 `SchedulePatch.name: Optional[str] = None`（`:42-54`）无约束，`_drop_nulls`（`:63-70`）只丢 `None` ⇒ `{"name":""}` 与超长名都能 PATCH 进（anchor `database.py:2452-2477` 原样写列）。同一理由适用于 `task_prompt`，而它在 POST/PATCH/ops_schedule 三处都无上限。附带注释失真：`:26` 说「每 3s 一轮」，实际 30s。
- **S7-12** `formPayload()`（`SchedulesView.vue:465-482`）恒含全部调度键 ⇒ anchor 的「调度参数变化」判据（`:110` 按**键是否出现**而非**值是否变化**）恒真 ⇒ 给 interval 任务改个错别字 = `next_run_at` 重算为 now+interval，本次即将到期的那次执行被推迟一整个周期。daily/weekly/monthly 无影响（墙钟幂等，已用 `python -c` 验）。`tests/test_schedule_routes.py:100/108` 只锁 toggle 两个方向，未覆盖此形。后端「不发=不改」契约本身是对的，是前端把它用成了 PUT。
- **S7-13** `compute_next_run` interval 分支 anchor `:502` `return _fmt(now + timedelta(minutes=minutes))` 以**实际触发时刻**为锚；`_run_loop` 是 `tick → sleep(30)`（anchor `:115`），tick 本身含 `to_thread` 的 DB 往返且随到期任务数线性变长 ⇒ 栅格自己也在滑。仿真（interval=60min、tick 30.05s）：1 天滑 144s，7 天滑 1008s。5 种类型里只有 interval 无墙钟锚点。Tradeoff 性质，但与其它四类的锚点语义不一致 ⇒ 留一行。
- **S7-14** `electron_bridge.py:58` `super().__init__(f"[{code}] {detail}" if detail else f"[{code}] {suggestion}")` —— detail 非空即丢 suggestion。定时任务另外三处 raise 都带 detail：anchor `:332`（NOT_CONFIGURED 自造文案，「请…再触发定时任务」的用户指引丢失）、anchor `:360`（HTTP_ERROR + "connect_ssh 返回空 sessionId"）、anchor `:374`（`BridgeError(code, str(e))`，把主进程那条被 **S0-09** 抹平的「请求处理失败，请稍后重试」塞进 detail，suggestion 里「看主进程日志」的指引再丢一次）。`439102d` 修的恰好是唯一不带 detail 的那一条 ⇒ 它的修复完整，但同族还剩三处。与 S0-09 同族（那里记的是 code 被抹平，这里是 suggestion 被抹平）。
- **S7-15** anchor `:197-200` 先 `fail_running_schedule_runs(sid,"任务已删除，执行被取消")` 再 `delete_schedule(sid)`，而 `schedule_runs.schedule_id … ON DELETE CASCADE`（`database.py:388`，且 `_create_connection` 确实 `PRAGMA foreign_keys=ON`）⇒ 刚写的那行随即被删。`routes/schedules.py:134` 的注释「先取消在途…并落定执行历史」与 `tests/test_schedule_engine_round6.py:128` 的注释都把这条顺序当成有意义的写，实际只在 delete 失败时才有意义。anchor `:260-267` 的 CancelledError 分支还会第二次写同一（已不存在的）行。净效果：删掉运行中的任务后界面不留痕。危害仅限留痕缺失，无脏数据。
- **S7-16** anchor `stop():89-94` 只 `cancel()+gather()+_running_tasks.clear()`，**没有** `_executing.clear()`；而未开跑的 task 被 cancel 时协程体（含 finally）根本不执行 —— anchor `:189-191` 的注释自己承认这点并在 `remove_schedule` 里用 done_callback 兜底，`stop()` 却没兜。同进程内若再次 `ensure_started()`，残留 sid 会被 `_tick_once`（`:121-122`）永久跳过、`trigger` 永久返回 False（立即执行恒 409）。**当前不可达**（`stop()` 只被 lifespan 关停调用，之后无第二次 start）⇒ 按规则降级。
- **S7-17** `_parse_int_field` 报错文案「必须是数字或 '*'」，但 `min hour * * *` 分支里 `_is_digits('*')` 为假 ⇒ `"* 9 * * *"` 被拒（只有五字段全 '*' 的 `:75` 特例接走 00:00 daily）。模型收到自相矛盾指引会原样重试。另：`parameters` 只暴露 create/list/cancel，**没有 update/disable/run** —— 任务书要的「UPDATE/DELETE/RUN one-hop parity」三项里只有 DELETE 可比，且它已复用 `get_schedule_engine().remove_schedule`（`:305`，与 `routes/schedules.py:135` 同一实现 ✓）；create 的 `6934f64` 一跳修复在位（`:258-263` ✓）。
- **S7-18** `task_prompt` 允许为空入库（`ops_schedule.py:233-254`），触发时 anchor `:318-319` 回落成「对当前环境执行一次例行检查，输出结构化报告。」—— 模型漏传 task/command 时任务照样建成、照样「成功」，跑的却是另一件事。REST 侧同形状，但 UI 有「留空用内置模板」的诚实文案兜底（`SchedulesView.vue:174`）。
- **S7-19** anchor `main.py:117` 注释「懒启动，仅在存在定时任务时 begin（ensure_started）」与实现不符：`:120` 无条件 `schedule_engine.ensure_started()` ⇒ 每 30s 一次 `db.list_enabled_schedules` 永久轮询，零任务用户也在空转。对照 `:113-115` 的意识 tick 才是真懒启动。

## 计算内核实测（anchor `compute_next_run`，`python -c` 装载 `git show a8e588c:` 源码 exec、stub 掉 `database`，零 DB 触达；本机固定 UTC+8）

| kind | 输入 | now=2026-09-21 01:00Z（=09:00 本地，周一） | now=2026-09-21 16:00Z | 锚点语义 | 判定 |
|---|---|---|---|---|---|
| interval | 60 min | `02:00Z` | `17:00Z` | 相对「本次触发时刻」 | ✓ `>now`；✗ 逐周期漂移（S7-13） |
| interval | 0 / NULL | `02:00Z`（=+60min） | — | `or 60` 安全回落 + 路由 `ge=1` | ✓ 不塌成每 tick 触发 |
| daily | 09:00 | `09-22 01:00Z` | 同左 | 本地日基准 + `cand>now` 严格 | ✓ 午夜塌陷已修 |
| daily | 00:00 | `09-21 16:00Z`→本地 9/22 00:00 | `09-22 16:00Z` | 相等即滚下一天 | ✓（`:504-507` 的 `is not None` 判定在位） |
| weekly | weekday=0 02:00 | `09-27 18:00Z`（=9/28 周一 02:00 本地） | 同左 | 本周目标日已过 → +7 天 | ✓ |
| monthly | dom=1 09:00 | `10-01 01:00Z` | 同左 | `min(dom, monthrange)` 月末钳制、跨年 `(y+1,1)` | ✓ |
| 未知/非法 st | 任意 | `now+1h`（anchor `:542` 兜底） | — | 无 cron 类型存在 | ✓ 不返回过去时刻；三处入口按 `_SCHEDULE_TYPES` 白名单拦 ⇒ 不可达 |
| 脏行 `time_hour=25` | daily | **`ValueError: hour must be in 0..23`** | — | fire 时不再校验 | ✗ → S7-03 无限重跑 |

所有返回串均为 naive-UTC `"%Y-%m-%d %H:%M:%S"`，无一处 `<= now`。

## 核对为干净（登记以免重复怀疑）

- **午夜塌陷族**两处修复都在位：anchor `schedule_engine.py:504-507`（`is not None` + 注释）与 anchor `database.py:2432-2434`；`interval_minutes` 保留 `or 60`（anchor `:499-502`）是注释声明的**有意安全方向**，路由另有 `ge=1`。
- `ops_schedule` create 的一跳修复（`6934f64`）在位：`ops_schedule.py:258-263`，`next_run_at` 与 INSERT 同写。
- fire-and-forget 的 `_bg_tasks` 强引用 + done-callback（`b96d77d`）在位：anchor `:64,219-227`。
- daily/weekly/monthly 一律用 `_local_now(now).date()` 作本地日基准（anchor `:514/523/533`），各分支 `while True` 保证 `> now`。
- **per-schedule 并发去重正确**：`_executing.add` 落在第一个 await 之前（anchor `:152-156`），手动/自动同刻双到达不会双开；`finally` 才释放（`:163-166`）。运行超过 tick 间隔不会被重复触发，长任务结束后只补跑一次，无追赶风暴。
- **错过的窗口只补一次**：关机一天后 daily 09:00 任务 `due=True` → 跑一次 → `compute_next_run` 已滚到明天，N 次补跑不存在。
- **naive-UTC 闭环无偏移**：`utcnow()`（`:462-463`）、`db._now()`（anchor `database.py:946-947`）、`_fmt`/`_parse_ts`（`:466-467`）三者同格式；前端 `parseBackendTs`（`src/utils/format.ts:24-31`）的结尾锚定正则对 naive 串判 `hasTz=false` 并补 `Z`，`countdownText`/`formatBackendTs`/`durationText` 全走它（`SchedulesView.vue:595-620`）。
- **runs 清理对 UI 够用**：`prune_schedule_runs` 每任务留 2000 行（anchor `database.py:2616-2642`，`PARTITION BY schedule_id`），路由 clamp 1..200，UI 取 50。
- **enable/disable 语义**：`exclude_unset` + `_drop_nulls` 使「不发=不改」成立，停用不重算、恢复启用重算（`routes/schedules.py:122-125`），有测试锁（`test_schedule_routes.py:100,108`）。
- **409/400/422 detail 到前端**：`request()`（`client.ts:197-199`）取 `detail`，`toFriendlyMessage` 的模式表（`:67-74`）不匹配「该任务正在执行中…」「不支持的周期类型…」⇒ 原文到达 toast（**注意与 S0-13 的 `client.ts:69` `/Error:\s/i` 是两回事**，那条打的是主进程 IPC 外壳，HTTP detail 不经过它）。
- **headless 审批不会挂起**：`PermissionGate` 不发阻塞等待，未授权即 `intercepted=True` + `inject_message`（`gate.py:713-789`）⇒ 定时 run 不会卡在等人。附带：`environment="prod"` 硬编码（anchor `:408`）使 dangerous 工具只认预登记 allowlist（`gate.py:651-686`）——这是安全姿态**成立**的一面。
- **S0-06 对 schedules 不成立**：schedule 路径的 `ssh_session_id` 只存活于 `_run_agent_once` 局部（anchor `:328,356-364`），从不落库（`schedule_runs` 无该列，anchor `database.py:386-396`）⇒ id 归零复用打不到定时任务。S0-05 对 schedules 的放大已并入 S7-05。
- **计划里的「cron 类型」在引擎/DB 层不存在**：`_SCHEDULE_TYPES`（`routes/schedules.py:22`）与 `compute_next_run` 只有 4 种，schema 无 CHECK；cron 仅是 `ops_schedule._parse_cron` 的入参门面（编译成 daily/weekly/monthly）⇒ 跨层契约上没有 `cron` 这个值，非缺陷，登记口径。
- anchor `:345` 注释「probe 内部已全量捕获异常」不准确：`electron_bridge.probe():148-161` 只靠 `is_available` 与 `_wait_ws`，后者→`relay.wait_ready` 仅捕 `TimeoutError`（`bridge_relay.py:77-80`）。行为上被 `:344-346` 的防御 except 兜住 ⇒ 只是注释说谎，不单列。
- `_build_schedule_system_prompt`（anchor `545-559`）的 `except Exception: pass` 会把「[系统时间] …」和整段 `SSH_MODE_PROMPT` 静默吞成两条通用文案 —— 无头 run 拿到没有时钟的任务书也不报错。已并入 S7-02 的 prompt 面，不另计。（该文件末行无换行符，`wc -l` 报 558 而实际 559 行，引用注意。）

## 闸门

- `python -c` 类动态证据两条：`compute_next_run` 五类型 × 边界实测（上表）、tick 栅格漂移算术仿真、`time_hour=25` 的 `ValueError` 类型确认 —— 均 stub 掉 `database`，**零 DB 触达**。
- **未执行 pytest**（并行会话持 SQLite 写锁，见 `00-INDEX.md` 警示块；`routes/schedules.py` 与 `schedule_engine` 的既有测试留待你本地跑 `pytest -k "schedule"`）。
- 其余全部为 static-read-only + `git grep` 引用计数表（`last_run_id` 全 src 1 命中、`fail_running_schedule_runs` 全仓 2 处生产引用、`max_retries|retry_count|consecutive_fail` schedules 侧 0 命中）。
