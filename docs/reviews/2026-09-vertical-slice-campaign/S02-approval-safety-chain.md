# S2 · 命令审批与安全门链

锚点 `a8e588c`。`gate.py`、`guards.py`、`auto_approve.py`、`driver.py`、`saddle.py`、`src/stores/agent.ts` 与锚点**逐字节一致**（`git diff a8e588c --stat` 空）。
`backend/routes/settings.py` 与 `src/stores/ssh-ai.ts` 有另一会话的未提交改动，涉及处已标 anchor。
**核验图例**：✅ = 我本人打开原文逐行证实；◐ = 采信 subagent 的引用与 grep，我未二次打开（裁决前建议复核）。

## 发现表

| ID | 维度 | 严重度 | 标题 | 主 file:line | 核验 |
|---|---|---|---|---|---|
| **S2-01** | c | **Critical** | **自动审批可推翻用户的显式「拒绝」**：`PermissionGate` 调裁决器前不查 `is_denied`，而 `FilesystemGuard` 查 | `gate.py:707-711` vs `guards.py:181-188` | ✅ |
| S2-02 | b | Major | 可见终端分支 `continue` 跳过 `permission_pending.pop` ⇒ 同一命令本 run 内**永不再出卡**且永久豁免循环检测 | `driver.py:546-551` vs `:557-570` | ✅ |
| S2-03 | b | Major | `PUT /settings/permission-policy` 是同步 `def` ⇒ 在线程池里改全局 `PermissionGate` 状态，正是同族端点当年为除竞态而改 `async def` 的那一类 | `settings.py:324`(anchor) → `gate.py:44-58,231-285` | ✅ |
| S2-04 | c | Major | 每一次审批决策落审计时**既无 run_id 也无 environment**，与 `security-boundary.md` §6 的声明相反 | `routes/agent.py:1395-1401` 等四处 | ✅ |
| S2-05 | d | Minor | 工具级审批过期只发事件不落审计（命令级落），同一生命周期两种记录口径 | `gate.py:716-756` vs `guards.py:204-216` | ◐ |
| S2-06 | c | Minor | `sudo -S` 口令猜测试探的防线只在串首/分隔符后生效，任何 wrapper 前缀绕过 | `safety.py:278-292` | ◐ |
| S2-07 | c | Minor | 多目标 `tee` 只判第一个文件 ⇒ `tee /tmp/x /root/.ssh/authorized_keys` 从 hardline 降为可批的 dangerous | `safety.py:955-969`（`:969` 的 break） | ◐ |
| S2-08 | d | Minor | `allowlist.txt` 重写会丢掉运维手写的注释与不合约定行 | `gate.py:154-159` vs `:276-278` | ◐ |
| S2-09 | c | Tradeoff | 「始终允许」的授权键**无主机维度**：A 机上的一次「始终允许」永久命中 B 机同一命令 | `gate.py:288-311,96,349,387` | ◐（键构造与查找点 subagent 全列，我核了 TTL 与键形状同源描述） |

## S2-01 · Critical：裁决器翻掉用户的显式拒绝

**这条为什么是 Critical 而不是 Major**：击穿的不是某个边角策略，而是本模块自己写在文件头的安全不变量。

- LOCATION（跨层）：`SideChatPanel.vue:742-745`/`run-modes.ts:44`（审批档=自动审批）→ 模型调工具 → `gate.py:637`（risk 判定）→ `:695-699` `check_permission(...)` **返回 False** → `:707-711` `await try_auto_approve(...)` → `auto_approve.py:168-180` ALLOW ⇒ `return ReinDecision()` ⇒ 工具执行。用户拒绝发生在更早：`AgentRunPanel.vue:78 → agent.ts:367 denyPermission → :386 dismissPermission` → `agentApi.denyCommand` → `routes/agent.py:1552 pg.deny_command(tool, command, run_id)` 写入 `_denied_commands`。
- CLAIM：`check_permission` 因 `is_denied` 而返回 False（`gate.py:344-346`，注释即「用户明确拒绝的命令不再免审……当前 run 的拒绝状态也优先」），但调用方**无法从 `False` 里区分「用户拒绝过」与「还没批过」**；`:707` 于是把两者一视同仁送进 LLM 裁决器。
- 我证实的三处对照：
  1. `auto_approve.py:11-16` 把「**用户在 run 内明确拒绝过的命令 —— 裁决器不得推翻用户显式拒绝**」列为「刻意不经过裁决器的路径」，并写明「**调用方负责保持这个顺序**」。
  2. `guards.py:181-188` 的 `FilesystemGuard` 履行了这个顺序：注释「② 在 is_denied 兜底之前先做同一检查——用户明确拒绝过的命令不能被模型翻案，用户否决永远高于裁决」，代码 `if not self.permission_gate.is_denied(tool, str(cmd)…): decision = await try_auto_approve(…)`。
  3. `gate.py:707` 前**没有任何** `is_denied` 判定（上方 `:703-706` 的注释只交代 prod 与 bound_command 两点），且 `grep -n "is_denied\|denied" auto_approve.py` → **零命中**，裁决器内部也不自查。
- REACHABILITY：需「自动审批」档 + 非 prod + 走 `gate.py` 工具级分支且无 `bound_command` 的工具。subagent 点名三类：`service_manage(stop|restart)`（`ops_service.py:85-90`，我核过该文件无 bound_command 覆写）、`schedule_op` 的非 create 动作（`ops_schedule.py:188` 仅在 create 返回绑定命令）、`job_kill`。序列：模型调 `service_manage` → 出卡 → 用户点「拒绝」→ 模型再调同一工具 → **静默执行**。prod 分支安全（`:651-686` 先 return，不问模型）。
- 与 09-24 死锁裁决的关系：`0b39829`（到期卡保留按钮）我在本报告外单独核过仍在位，**本条与它无关**，也不是它引入的。

### 候选修复方向（未实施）
把 `guards.py` 的 `is_denied` 前置照搬到 `gate.py:707` 之前（一行门），或在 `check_permission` 返回值上区分三态（放行/待授权/已拒绝）。前者是零行为面积的收敛，后者动契约。属审批链冻结区边缘，按本轮口径只报不修。

## S2-02 / LEGACY #2 · plan 模式写工件永拒 —— 判定：成立（端到端）

我此前已静态核实「`PLAN_READ_ONLY_TOOLS`(15 项，:98-109) 不含三者、非 `_COMMAND_EXEC_TOOLS`、`PLAN_DISCIPLINE:194` 却要求调用」；本轮 subagent 补齐了链上其余环，我采认并列出可复核点：

1. `PlanModeGuard` 注册于 `defaults.py:92`，PRE_TOOL_CALL、priority 6（`plan_mode.py:382`），在 Clarification(7)/Gate(8)/Guard(10) **之前**，只被 LoopDetector(5) 越前——而 LoopDetector 只对重复动作反应（`flow.py:78-98`），首次 `plan_write` 不会被它抢先。⇒ 拒绝分支确实可达。
2. 三个工具（`workflow_tools.py:78/104/130`）都是裸 `BaseTool`，`bound_command` 默认 None（`tools/__init__.py:77-89`）⇒ `extract_bound_command` 得 `(None, False)`；`meta.risk_level="safe"` 与本判定无关（`PlanModeGuard` 从不看 ToolMeta）。
3. **计划模式从 UI 可达**：`SideChatPanel.vue:560 → SshAiPanel.vue:114 → ssh-ai.ts:588 → run-modes.ts:41-47 → client.ts:886 → routes/agent.py:403 → harness.py:253-260` 写 `ctx.metadata[MODE_KEY]`，`src/api/permission-mode.test.ts` 钉住。⇒ 不是纸面风险。
4. 用户所见：拒绝文案 = `plan_mode.py:318-322` + 再附一整块 `PLAN_DISCIPLINE`（`:411-412`），而那块里第 194 行仍在喊「用 plan_write 输出有序步骤」。该文本只走 `intercept_reason` → `driver.py:487` 变成工具消息喂回模型；`plan_mode_block` 事件在 `src/` 零消费者（S1-05 独立证实）⇒ **面板上没有任何弹窗**，用户只看到内联轨迹里一步失败。
5. 后果两种，都不干净：模型听 194 行重试 ⇒ 内容变了就是新 `tool_call_signature`，LoopDetector 永不触发 ⇒ 空烧到 `max_iterations`；模型听 192 行「被拒时不要重试」⇒ 改用聊天作答 ⇒ `plan.md` 永不产出 ⇒ `get_phase()` 不为 plan（`workflow.py:106-108`）、「计划」chip 空、`[计划]` 永不注入。
6. `test_plan_mode.py:238` **断言了这个自相矛盾**（`assert "plan_write" in d.intercept_reason`），而 `plan_write` 既不在 ALLOWED_TOOLS 也不在 DENIED_TOOLS ⇒ 无测试钉住结局。

**裁决要点**：工件仍可产出——但只能在 `normal`/`auto` 档、即用户先关掉「计划」chip 之后。而 `goal-plan-mechanics` spec 设计的批准门（`plan_approval.py`/`PlanApprovalGuard`/`max_goal_turns`）**全仓零命中**，于是 `workflow.py:239-243` 会把一份**未经批准的** plan 以「已确定的执行步骤（按此推进…）」注入系统提示；`workflow.py:183` 至今仍以「否则『计划批准门』有了逃逸口」作为不给模型删除工件工具的理由——**为一道不存在的门付的代价**。两条待裁决项在此合流，建议一并定夺。

## S2-03 / LEGACY #4 · `resolvePermission` 恒 true —— 判定：成立，「已拒绝」是死 UI

我亲自跑的调用方枚举（`grep -rn "resolvePermission\|denyPermission\|dismissPermission" src/`）：

| 符号 | 生产调用点 | 传值 |
|---|---|---|
| `resolvePermission` | **仅** `AgentRunPanel.vue:96` | `true` |
| `denyPermission` | `AgentRunPanel.vue:78` | 不调 resolve，走 `dismissPermission` |
| `dismissPermission` | `AgentRunPanel.vue:272`（忽略）+ deny 内部 | 从数组**移除**元素 |
| 测试 | `agent.test.ts:206/219/228/232/239`（deny）、`:317/344/358`（resolve 恒 true） | — |

`req.granted` 的唯一写入点是 `agent.ts:395`、`req.resolved` 唯一写入点 `:394`，二者同一次调用 ⇒ `resolved===true ⇒ granted===true` 恒成立。
**回放侧也查了**（这是当年留的悬念）：`permissionRequests` 是纯内存 `ref`（`agent.ts:141`），唯一写入者 `:554`；`GET /agent/runs/{id}/trace` 只投 `run/cost/state_history/tool_calls`，**不含 permission 对象**，其唯一消费者 `AgentFullTrace.vue:77` 不碰 store。⇒ 在线与回放两条路都产不出 `granted=false`。
⇒ `AgentRunPanel.vue:380-382` 的「已拒绝」分支、`:114` 注释、`agent.ts:63-66` 描述的状态空间均为死面。用户感知：点拒绝后卡消失，历史区（N 已处理）**永远只能列出「已授权」**——尽管拒绝在后端是记了账的（`denyCommand`）。
定级 Minor/死码类，但因它属审批可观测性，按当年口径只报不删。

## S2-04 / S2-05 / S2-06（其余已证实三条）

- **S2-02（Major，✅）**：`driver.py:546-551` 的 `if getattr(result,"pending_delivery",None): … continue` 发生在 `:557-570` 的 `permission_pending.pop(sig)` **之前**。两个后果：① `flow.py:68-69` 对滞留签名直接 no-op ⇒ 同命令无限重试不受循环检测（`0b39829` 提交信息里描述的那个状态）；② 后续同命令在 `guards.py:247` 命中 `first_ts is not None` ⇒ `is_first=False` ⇒ `events=[]`（`:327`）**不再出卡**，而注入文案却写着「已在界面上向用户请求授权，请重新发起完全相同的命令（授权后会被放行）」——原卡已 `resolved` 进只读追溯区（`AgentRunPanel.vue:115,374-384`），`permission_expired` 处理又跳过已处理卡（`agent.ts:606`）⇒ 用户无从照做。可达性是**默认路径**：anchor `src/stores/ssh-ai.ts:592` 硬编 `visibleTerminal: true`（我核过 anchor），`client.ts:879` 透传。`:557-563` 的 R2 注释本身正是为「成败都该出集」写的，pending_delivery 分支把它绕过了。`grep -n permission_pending tests/` 显示三条出集路径有测试、此分支没有。
- **S2-03（Major，✅）**：anchor `settings.py:324` 为 `def update_permission_policy`（同步）→ FastAPI 丢线程池 → 里面推 TTL/prune/重写 allowlist（`gate.py:44-58,231-247,265-285`，含 `sorted(self._confirmed_permanent)` 与 `open(path,'w')` 非原子整写）。而同仓审批端点族**已就同一失败模式定过论**：`routes/agent.py:1352-1359` 的注释「sorted() 迭代中另一线程 add/discard 会抛 RuntimeError，defaultdict 复合 += 丢账……改 async def 后 gate 状态操作全部回到事件循环单线程」，并复述于 `:1402/:1427/:1455/:1535/:1594`。⇒ 这是**该已裁决修复漏掉的最后一个调用点**；两个并发 `open(path,'w')` 还可产出重复/截断的 allowlist.txt。触发面：设置→Agent 治理→授权保留时长，同时在另一个会话跑带审批的 run。
- **S2-06（Major，✅）**：`routes/agent.py:1395-1401` 的 grant 审计只传 `action/conclusion/target_type/target_id/command_raw/result`，**`run_id` 与 `environment` 都没传**（`audit.py:123-135` 的默认值 `run_id=""`、`environment=_DEFAULT_ENV="test"`），而 `req.run_id` 就在手边（`:1384` 已用于 `pg.grant`）；`result` 串里也只有 scope 与 conversation。deny-command/revoke/allow 同形。⇒ 每一次「谁批准了什么」都记成 `run_id=""、environment="test"`，**包括对生产主机的一次「始终允许」**；审计页按 prod 过滤恒零命中，授权也无法与它放行的 run 对账。与 `docs/security-boundary.md:150-156`「含 environment、command_raw、run_id」正面冲突 ⇒ 记 `CONTRADICTION vs spec`。

## 规格轴漂移清单（subagent 产出，计数类我核过其中两条）

`docs/security-boundary.md`：条目数**无漂移**（HARDLINE 14 @ `safety.py:182-201`、DANGEROUS 37 @ `:211-267`，与文档 `:63/:67` 一致 ✅）。其余待裁：D1 §1 的「120s 超时按拒绝」实为 600s 可配（`core.py:38-41`）；D2 §2「超时按拒绝并记忆」两处皆反（过期不 deny、不 pop，0b39829 后卡仍可操作）；D3 §6 枚举 8 值 vs `audit.py:142-144` 实为 10（多 `answered`/`skipped`，且 `:139-141` 自己要求消费者同步）；D4 §1/§5 用「链式签名，执行了什么都可追」为人操通道背书，而批量 `ssh:exec` **一条审计都不写**（`grep -rn audit electron/*.ts` 无命中）；D5 §1「allowlist 无 TTL：取舍 E」与同文档 §4/§7 自相矛盾（TTL 存在）；D6 层次表与顺序文字从未提 PlanModeGuard(6)（如今它是执行强制层）；D7 十余处行号锚点漂移（文档 `:9` 自警却又断言数字）。
`docs/plan-mode.md`：P1 §「14 项」实为 **15**；P2 §3.2 把 `service_manage` 列进工具白名单而代码刻意排除；P3 §2.2「没有任何拦截点」已过时；P4 §6.2 的前提（plan_write 能写出 plan.md）不成立；P5 §6 标题「仍未接的线」与小节「已接线」自相矛盾；P6 同一文档两处测试计数不一致（127 vs 124，均未执行核实）。

## 我判定为「已裁决、不重复上报」的项

`rm -rf /` 整串 over-block（B3）、相对路径删除判 safe（文档已登记取舍 F①）、environment 由客户端自声明（取舍 B）、`Sandbox` 只查 shell_exec（取舍 **E①**——注意 S1-04 从**纵深防御**角度重报过一次；两者同源，S1 那条是「不可禁用层与可裁剪层口径不齐」，此处文档已登记，**以 S1-04 为唯一条目**，本处不再计）、`safety.py:1404` 与 `command_from_args` 重复（B16）、`reins/__init__.py` barrel（A/B21）、`flow.py:362` 零除（B18）、到期死锁本体（0b39829 在位）、`lifecycle.py` 吞异常（被 SSE finally 与 release-run 覆盖）。

## 闸门

未跑 pytest（共享库锁，S0 已记 RC=124 实录）；`gate.py`/`guards.py`/`auto_approve.py`/`driver.py`/`agent.ts` 与锚点零差异 ⇒ 行号即锚点。S2-01/02/03/04/06 我逐行开过原文，S2-05/07/08/09 标 ◐。
