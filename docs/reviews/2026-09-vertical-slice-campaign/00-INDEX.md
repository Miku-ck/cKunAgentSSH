# 功能纵向切片审查战役 · 索引

- **锚点提交**：`a8e588c`（所有 file:line 以 `git show a8e588c:<path>` 为准）
- **模式**：只报告，不改码、不提交、不加测试 —— **已按此口径完成，全程零源码改动**
- **维度**：(a) 正确性 bug hunt · (b) 规格轴对齐 · (c) 安全与审批链路 · (d) 冗余死码
- **开始**：2026-09-29 13:37 · **14/14 片全部落盘**：2026-09-29
- **总表**：`90-ADJUDICATION-SHEET.md`（由脚本从各片发现表机械汇总，列名按表头识别；**行数=193，与本表计数一致**）

## 🚨 三条 Critical（按「连跑 + Critical 即时报」口径已在发现当刻报出）

| ID | 标题 | 主位置 | 报出方式 |
|---|---|---|---|
| **S5-01** | LEGACY #1 成立：上传是 8 路并发绝对偏移写，`.part.size` 必然越过连续边界，而停滞路径故意跳过截断 → 续传跳过零填充洞 → rename 出**静默损坏文件**；另发现第二条触发路径（主进程被杀/通道硬断 ⇒ 截断代码不可达） | `ssh-manager.ts:1525` + `:1549` + `:1466` + `:1576` | 即时报（S5 落盘时） |
| **S2-01** | 自动审批可推翻用户的**显式拒绝**：`PermissionGate` 调裁决器前不查 `is_denied`，而 `FilesystemGuard` 查；`auto_approve.py` 全文零 `is_denied` | `gate.py:707-711` vs `guards.py:181-188` | 即时报（S2 落盘时） |
| **S6-01** | `readCurrentLine` 用**视口相对**行号索引**绝对**行存储 ⇒ Tab 补全读到游标上方 `ybase` 行之前的历史输出，并把整条候选**不经确认写进活动 shell**（唯一候选自动补全路径），同时把垃圾行记进喂给预测/历史的命令记录 | `useCompletion.ts:168-176` ↔ `useCompletionPopup.ts:89-91,126-143,146-154` | 即时报（本轮） |

S6-01 的判据我逐字核到底层库：`@xterm/xterm` 的 `BufferApiView.ts:21`（`cursorY=_buffer.y`）与 `:27-32`（`getLine(y)=lines.get(y)`）、`Buffer.ts:96/375` 库里自己的换算 `absoluteY = ybase + y`、`typings:1509-1514`（cursorY 明确 0..rows-1）；同仓反证是 `useCompletionPopup.ts:112-113` 把同一个值当视口相对用（正确）与 `useTerminalContext.ts:72-73` 注释写明绝对/视口规则。

## 切片状态

| 切片 | 面 | 风险 | 状态 | 发现数 | C | M | m | T | 登记 |
|---|---|---|---|---|---|---|---|---|---|
| S0 | 跨层契约缝与启动 | 🟠 | **已完成**（两路） | 24 | 0 | 10 | 14 | 0 | 0 |
| S1 | Agent 回合循环与 SSE 流 | 🔴 | **已完成** | 6 | 0 | 2 | 4 | 0 | 0 |
| S2 | 命令审批与安全门链 | 🔴 | **已完成** | 9（+2 项遗留裁决） | 1 | 3 | 4 | 1 | 0 |
| S3 | 记忆子系统 | 🔴 | **已完成** | 17（8 发现 + 9 规格漂移行） | 0 | 5 | 12 | 0 | 0 |
| S4 | SSH 终端与连接管理 | 🟠 | **已完成** | 10 | 0 | 7 | 3 | 0 | 0 |
| S5 | SFTP 文件传输 | 🔴 | **已完成**（S5a 核心传输 + S5b 渲染/路径） | 19 | 1 | 9 | 8 | 1 | 0 |
| S6 | AI 命令行辅助 / explain / 运维工具 | 🟠 | **已完成** | 29 | 1 | 7 | 20 | 1 | 0 |
| S7 | 定时任务与桥接中继 | 🟠 | **已完成** | 19 | 0 | 9 | 10 | 0 | 0 |
| S8 | 技能系统 | 🟡 | **已完成** | 3 | 0 | 0 | 3 | 0 | 0 |
| S9 | 会话与消息及后处理 | 🟠 | **已完成** | 22 | 0 | 6 | 16 | 0 | 0 |
| S10 | 模型与 Provider 配置 | 🟠 | **已完成** | 24 | 0 | 10 | 13 | 1 | 0 |
| S11 | 审计链与运维观测 | 🟡 | **已完成** | 6 | 0 | 0 | 4 | 1 | 1 |
| S12 | GPU 仪表盘与服务监控 | ⚪ | **已完成**（含实跑 vitest 28 绿） | 3 | 0 | 0 | 2 | 0 | 1 |
| S13 | 设置外壳 / 外观 / 桌面偏好 | ⚪ | **已完成** | 2 | 0 | 0 | 2 | 0 | 0 |
| **合计** | | | | **193** | **3** | **68** | **115** | **5** | **2** |

C=Critical M=Major m=Minor T=Tradeoff；「登记」= 严重度单元不是五级词汇的行（交叉引用/正例，不计入发现数）。各片另有 **Rejected** 段（命中 `91-DO-NOT-RELITIGATE.md` 的条目就地登记，不进总表计数）。

## 遗留 5 项归属与重判结论

| 遗留 | 内容 | 所属切片 | 重判结论 |
|---|---|---|---|
| #1 | SFTP 停滞续传零洞静默损坏 | S5 | **成立，Critical**（S5-01）。新增第二条触发路径：主进程被杀/通道硬断 ⇒ `Promise.all` 之后的截断代码不可达 ⇒ 跨重启续传同样踩洞 |
| #2 | `plan/spec/goal_write` 在 plan 模式永拒 | S2 | **成立**（端到端：注册序、无 `bound_command` 豁免、UI 可达、拒绝文案自带矛盾指令、无测试钉结局）。另核实 spec 声称的 `plan_approval.py`/`PlanApprovalGuard`/`max_goal_turns` **全仓零命中** —— 计划批准门不存在，而 `workflow.py:183` 注释仍在引用它 |
| #3 | reconcile 部分失败重跑重复入库 | S3 | **成立**，且当年的「收进一个事务」硬修方向被**证伪**（`database.py:23` 线程本地连接 + mstore 每次写经 `asyncio.to_thread` ⇒ 无法罩进同一事务）；另找到 3 条更窄的 seam |
| #4 | `resolvePermission` 的 `granted` 恒 true ⇒「已拒绝」分支可达性 | S2 | **成立**：生产调用点唯一且传 true，DB/trace 回放侧亦无 `granted=false` 生产者 ⇒ 「已拒绝」是**死 UI** |
| #5 | `skill_registry` 前导三行重复 | S8 | **成立**，实测为逐字相同的 **7 行**（`skill_registry.py:169-174+176` vs `:201-206+208`），另含同一次调用里两遍 DB 禁用清单读取 |

五项均以「重判 + 候选修复方向（未实施）」记录，**无一项被本轮动过代码**。

## ⚠️ 工作树状态告警（贯穿全役的处理口径）

役始工作树**并非干净**：另一会话实时写入 21 个文件（`git status --short` 现含 `contracts.py`、`llm_client.py`、`stirrups.py`、`schedule_engine.py`、`database.py`、`main.py`、`netconfig.py`、`post_processor.py`、`routes/agent.py`、`routes/settings.py`、4 个测试文件、`client.ts`、`MarkdownStream.vue`、`MessageContentRenderer.vue`、`MessageItem.vue`、`AgentPanel.vue`、`ChatPanel.vue`、`ModelsPanel.vue`、`OpsPanel.vue`、`settings.test.ts`、`settings.ts`、`ssh-ai.ts`，并新增 `test_models_panel_payload_round23.py`、`test_settings_round23.py`）。

口径（**全程未用 stash/checkout/reset 去「清理」**）：
1. 所有行号钉 `a8e588c`；脏文件一律 `git show a8e588c:<path>` 读并标 anchor。
2. 发现若已被在写改动修掉，正文单列「**工作树已修（未提交）**」，不算本片新发现、也不重复开工单。
   **状态更新（15:03）**：并行会话已把这 21 个文件提交为 `9634dee fix(settings): 打通模型采样参数链路并修正设置项语义` —— 上面这批「未提交」现在**全部已落库**，逐条证据见 `S10-model-provider-config.md` 与 `S09-chat-postprocess.md` 各自的「落库状态更新」小节：S9-04 / S9-14 / S9-15 / S10-01 / S10-04 / S10-05 / S10-20 已修，S7 的 `_resolve_model` 面同步修好。
   ⚠️ 一处**因落库而恶化**：**S10-06**（`reasoning_tokens=0` 塌成「思考不设上限」）—— 9634dee 把它做成面板可改项且 `min="0"`，新注释还把 0 解释成「跟随输出预算」（`ModelsPanel.vue:479-480`），从「需要 curl」升级为「用户一键可踩」。裁决时建议优先看这条。
   ⚠️ 另一处副作用：`9634dee` 把本战役当时已存在的 12 份报告一并提交进了那个以 settings 修复为主题的 commit（`docs/reviews/2026-09-vertical-slice-campaign/` 的 S00–S08、S10–S13、90/91/00）。S06 与 S09 是该 commit 之后写的，仍是未跟踪文件。**这是并行会话的动作，本战役自身未做任何提交。**
3. 测试闸门跑的是**工作树**不是锚点，基线数字含义为「当前工作树」，逐片标注。

## 验证与闸门（如实记录）

- **役中未执行整文件 pytest**：`pytest -k "round22 or http_env or ci_matrix or model_default"` 当时 → **RC=124（280s 超时）**；`--collect-only -q` 1.45s 通过 ⇒ 确诊为文档化的共享 SQLite 写锁（并发会话在写）。各片 EVIDENCE 一律标注 `static-read-only, not executed (lock hazard)`，未伪造任何结果。
- **收尾复跑（工作树已随 `9634dee` 转干净之后）**，四条闸门全绿：
  | 闸门 | 结果 |
  |---|---|
  | `npm run typecheck`（`vue-tsc -p tsconfig.web.json --noEmit && vue-tsc -p tsconfig.node.json --noEmit`） | **RC=0**，零错误 |
  | `npx vitest run` | **RC=0**，18 文件 / **231** 用例全绿（战役开始时记的基线是 ~223，`9634dee` 新增了 round23 面板用例） |
  | `pytest -k "round22 or round23 or model_default or http_env"`（S0/S10 子集） | **RC=0**，93 passed / 1465 deselected / 15.85s —— 役中的 RC=124 确证是并发锁而非用例本身 |
  | `pytest -k "schedule or audit or seed or clarify or plan_mode"`（S7/S11/S2 子集） | **RC=0**，212 passed / 1346 deselected / 6.76s |
  未跑全量 `pytest -q`（1465+ 用例、既有 SQLite 锁警示）与 `bash scripts/ci-check.sh`（本地等价全量门，锁日不跑）。
- 可安全执行的动态证据：`vitest run src/components/ssh/GpuNvtopPanel.test.ts`（S12，28 绿）；`compute_next_run` 五类型边界 + tick 漂移仿真（S7，stub 掉 `database`、零触库）；`time_hour=25` 的 `ValueError` 类型确认；`python -c` 按 schema 默认值代入的输出上限计算（S7-10）。
- 依赖包内部事实（只读、零执行）：`marked` 18.0.5 无 sanitizer、`dompurify` 3.4.11 默认 ALLOWED_TAGS/ATTR 含 `style`、`@xterm/xterm` 的 cursorY/baseY/getLine 与 `IBufferCellPosition` 1-based（S9-01、S6-01、S6-27）。
- `git status --porcelain` 在本目录之外**零改动**（`git status --porcelain | grep -v docs/reviews/... | wc -l` = 0）。本役唯一写入路径是 `docs/reviews/2026-09-vertical-slice-campaign/`（14 片报告 + 总表 + 本索引 + `91-DO-NOT-RELITIGATE.md`）；**本战役自身未做任何提交**，报告被并入 `9634dee` 是并行会话的动作（见上方告警块）。

## 结构性观察（跨片共性，供裁决时成批处理）

这些不是新缺陷类，但重复出现，值得一次成口径的修复而不是 14 次单点补丁：

1. **合法空值被 `or` / 真值判断吞掉**（0-collapse 族）：`S10-06`（`reasoning_tokens=0` 被 `> 0` 当未设，且 `schedule_engine.py:382-385` 注释显示只修了一半）、`S10-07`（`api_key==""` 被 reins 层判成未配置模型）、`S7-10`（`or ctx_limit` 是同值回退的死式）、`S6-13`… 已修样本可当模板：`_num_param`（temperature）、`asNumber`、`is_int_app_setting_set`、`keep_count ?? 1000`。
2. **await 前后同一不变量两半只守一半**：`S4-05`（`releaseConnectLock` 解除自己那笔在途的锁）、`S6-02`（job_id 在 await 前预留、await 后才登记）、`S7`（`_executing.add` 位置正确，是本轮的**正例**）。
3. **信号/校验按一个口径、验证按另一个口径**：`S6-03`（发信号用进程组、验生死用组长）、`S10-16`（`"global"` 写侧=全局、读侧=SSH 列）、`S11-02`（`|` 拼接 canon 的歧义面）。
4. **同族防护写了一处漏了另一处**：`S4-02`（24 个 `assertSafePath` 调用点独缺 `pty:completeCommand`）、`S8-02`（`match_skills` 有 JSON 兜底、`build_skill_catalog` 没有）、`S6-07`（同文件 `:161` 逐行跳哨兵、`:238` 整串判）、`S9-01`（净化器按默认白名单跑）。
5. **文档/注释在承诺代码没做的事**（本役最大宗，约 30 行）：`S2` 的 plan 批准门、`S10-01` 的 ★ 文案、`S7-19` 的懒启动注释、`S3-16`、`S6-15`、`S6-26`、`S9-14`、`S10-09` 的 spec 预算、`docs/security-boundary.md` 的「批量可追」（S4-07/S11-06）。**这一类零行为风险，但会持续制造误判成本。**

## 共享缝档案位置

- S0：跨层契约基座（token/port 引导、`__sshError` 往返、`handleTrusted` 包络、http 权威源）—— `S00-contract-seams.md`
- S1：工具派发图与 SSE 事件表 —— `S01-agent-turn-loop.md`
- S4：**`SSHManager` 会话 API 档案**（供 S5/S6/S7/S12 直接引用，不重读 2089 行）—— `S04-ssh-terminal-connections.md` 末节
- S6：**全部远程/本地命令构造点清单**（逐行引号判定）—— `S06-ai-assist-ops-tools.md` 末节
- S9：**XSS 面整表**（17 个向量逐个判定）—— `S09-chat-postprocess.md` 中节
- S10：**默认模型 13 消费者 × 档位优先级表** + **数值设置 × 0 值处理表** —— `S10-model-provider-config.md` 中节
- S7：`compute_next_run` 五类型实测表 —— `S07-schedules-bridge.md` 中节

## 诚实边界（各片「未覆盖声明」的汇总口径）

S8 的 skills IPC 组与 `SkillsPanel.vue` 只做了存在性核对；S9 的 `SideChatPanel.vue` 只读完脚本主干（模板 `120-311`/`321-486`、`handleKeydown` 体内、`<style>` 整块未判）；S6 的远端白名单跨端契约（`scripts/deploy/agent-exec.py`）与 `setsid` fork 语义未证；S5 只做静态读（共享库锁 + 常驻后端风险）；各片「◐」行 = 采信审查路引用而我未二次打开原文，**裁决前建议自行复核该行**。
