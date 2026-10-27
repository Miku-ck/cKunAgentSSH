# 生产环境审批策略调整：用户确认优先，无人值守才要求预登记

日期：2026-10-27。前置：同日完成的 log_cleanup / 向导误弹修复（工作区未提交）。
触发事故：会话在 prod 主机 192.168.150.40 清理 `.trae-cn-server/manager-logs`，
界面连续出现 4 张拒绝卡（pip、`rm .cache/pip/*`、`log_cleanup` 未预授权）与 3 步澄清
向导，用户结论：「审批策略过于严格，用户确认应该是第一优先级」。

两处独立缺陷：
1. **prod 危险操作一律只认预登记 allowlist**，交互会话里也不给审批卡 —— 人在场却
   无法确认，只能先去设置页登记。这是本次要改的策略。
2. **`rm -rf /home/<user>/.trae-cn-server/manager-logs` 是 safe 调用**（agent 托管日志
   目录豁免），却仍弹澄清向导 —— 向导在问一件不需要授权就会直接执行的事。
   这是向导触发条件的问题，与 prod 无关，test 环境同样复现。

## 1. 用户决定（已确认）

- prod 危险（非 hardline）操作：**恢复交互审批** —— 有人在场时弹卡（仅本次 / 始终允许），
  只有「始终允许」写入 allowlist。放弃「预登记 + 临时覆盖」与「只放行白名单工具」。
- **prod 不再出现澄清向导**；test 环境保留向导。

## 2. 授权语义矩阵

「交互」= `ctx.metadata[AWAIT_APPROVAL_KEY]` 为真。该键是 Driver 审批等待闸的唯一开关
（`reins/core.py:33`），与「卡片发出去后能否真的停住等」同源，不引入第二个交互标志。

| 环境 | 命令行分类 | 运行形态 | 已有授权 | 结果 |
|---|---|---|---|---|
| prod | hardline | 任意 | 无视 | 无条件阻断（既有语义，不变） |
| prod | dangerous | 交互 | 永久 allowlist 命中 | 静默放行 |
| prod | dangerous | 交互 | 未命中 | **审批卡**（仅本次 / 始终允许），等用户点击 |
| prod | dangerous | 无人值守（定时/headless/runbook） | 永久 allowlist 命中 | 静默放行 |
| prod | dangerous | 无人值守 | 未命中 | 拒绝，文案改为「无人值守无法弹卡」+ 预登记指引 |
| prod | safe / moderate 可逆 | 任意 | — | 静默放行（不变） |
| test | dangerous | 交互 | 未命中 | 审批卡（不变） |
| 任意 | dangerous | test 非交互 | 未命中 | 拒绝（不变） |

工具级危险（`service_manage stop/restart` 在 prod 升级为 dangerous）按同一矩阵执行。
`log_cleanup` 的 dry_run 只读预览继续走既有 `is_readonly_preview` 豁免，不弹卡。

### 2.1 自动审批（permission_mode="auto"）

prod 仍然**不交给模型裁决**：交互 prod 未授权时直接弹卡，不先问 LLM。理由：生产环境里
「模型替人批准危险命令」正是 prod 强化的原始动机，本次只放松「人能否确认」，不放松
「谁能确认」。auto 模式在 test 环境的行为不变。

### 2.2 无人值守的拒绝文案

保留 `POLICY_DENY_MARKER = "未预授权"` 子串（`headless_run._is_deny_signal`、
ConfidenceMonitor 的扣分豁免与专用求助卡都靠它），只改人读部分：

> 当前为生产环境，且本次运行无人值守（定时任务 / 工作流），无法弹卡等待确认。
> 危险命令（reason）需预先在权限 allowlist 中登记；或在交互会话中重新发起以现场确认。

模型注入同步改为：说明「此路径拿不到用户确认」，并给出两条真实出路（预登记 /
交互重试），继续禁止构造变体。审计 result 仍用 `prod_deny_no_allowlist`（现有
报表与测试按前缀匹配，不为此改字符串）。

## 3. 澄清向导触发条件

在现有 `_blocked_before_approval` 基础上改为统一跳过判定，满足任一即不建题：

1. **非交互 run**：没人能作答；`_register_clarification_wait` 本来就不等，建题只会留下
   一张永远无人回答的向导，并把模型拖进重试循环。
2. **prod 环境**：用户决定。危险调用由审批卡承载（卡片自带命令原文 + 后果说明），
   safe 调用直接执行，向导在 prod 没有信息增量。
3. **hardline**：无条件阻断，答什么都执行不了（既有）。
4. **有效风险为 safe**：不需要授权就会执行的调用，不该先拦下来问 3 个问题。
   本次事故的 `rm -rf .../manager-logs` 正落在此档。有效风险的口径与 Gate 一致：
   有绑定命令看内容分类，否则看 ToolMeta（向导已在 prod 前一步跳过，此处只剩 test 口径）。

有效风险为 dangerous / moderate 不可逆的 test 环境交互调用，向导行为不变。
`permission_gate` 不再参与向导判定（不再需要查 allowlist），从
`ClarificationInterceptor` 字段与 `defaults.py` 注入中移除；`saddle` 保留（算绑定命令）。

## 4. 实现落点

| 文件 | 改动 |
|---|---|
| `backend/agent/harness/reins/guards.py` | prod 分支条件改为「prod 且非交互」；交互 prod 落回既有 `check_permission` → 发卡路径；auto 裁决加 `env != "prod"` 条件；无人值守文案改写 |
| `backend/agent/harness/reins/gate.py` | 同上（工具级 dangerous） |
| `backend/agent/harness/reins/clarification.py` | 跳过判定改为第 3 节四条；移除 `permission_gate` 字段及相关 allowlist 查询 |
| `backend/agent/harness/reins/defaults.py` | 构造 `ClarificationInterceptor` 时不再传 `permission_gate` |
| `backend/agent/harness/reins/auto_approve.py` | 头注释补充：prod 排除在裁决之外，但交互确认不受影响 |
| `backend/agent/harness/driver.py` | `_register_approval` 注释里「prod 未预登记 = 直接否掉」一句已过时，改为「非交互 prod 未登记」 |
| `docs/p0-ops-agent-enhancements.md` | §5 与 M1 中「prod 仅接受预登记、拒绝临时审批」按新矩阵修订（回滚不再强制两步预登记） |

前端不改交互逻辑，只改两处会变成假话的文案：`ConnectionModal.vue` 的 prod 连接提示与
`RollbackPanel.vue` 的 prod 提示（原文写「不弹临时审批卡」），统一改为「交互会话弹卡确认 /
无人值守需预登记」；`RollbackPanel.test.ts` 的文案断言同步。`ApprovalCard` 已按
risk=dangerous 只给「允许本次 / 始终允许」，且已有生产环境角标与警示，无需改动。

## 5. 测试计划

新增 / 改写（均不依赖真实 SSH）：

- `test_sse_permission_and_run_lifecycle.py`
  - 交互 prod + 未预登记 service_manage stop → `permission_needed`、带 `approval_key`、
    reason 不含 `POLICY_DENY_MARKER`；grant once 后重判放行。
  - 非交互 prod + 未预登记 → 仍拦截且含 marker（现有断言保留，补「无人值守」文案断言）。
  - 预登记命中 → 静默放行（现有）。
- `test_permission_improvements.py`
  - 交互 prod 危险 shell → 发卡而不是拒绝；非交互 → 拒绝且 marker / allowlist 指引保留。
  - prod + auto 模式 → `try_auto_approve` 不被调用（monkeypatch 计数）。
- `test_clarification_wizard.py`
  - prod（无论是否预登记）→ 不建题（改写 `test_prod_allowlisted_still_asks`）。
  - 事故命令 `rm -rf <托管日志目录>` → 不建题（safe 档）。
  - test + dangerous 模糊调用 → 仍建题；非交互 → 不建题；hardline → 不建题（既有）。
- `test_clarification_blocking.py`：`test_unattended_run_does_not_wait` 改写为
  「不建向导、直接执行」；其余交互用例不变。
- `test_clarification_answer_binding_and_labels.py` / `test_harness_feedback_loop.py`：
  测试 ctx 标记为交互（向导本就是交互态能力），断言不变。
- `test_log_cleanup_tool.py`：prod 删除调用改为「非交互上下文」表述，断言不变。
- `test_confidence_monitor_false_positive.py`：既有用例都用非交互 ctx，应原样通过；
  不新增豁免逻辑。
- 前端：`npx vitest run src/components/agent/RollbackPanel.test.ts`。
- 全量 `python -m pytest backend/tests`（基线 3375 passed）。

## 6. 取舍与非目标

- **不做「prod 也只认模型自动审批」**：auto 模式在 prod 维持排除。
- **不做 allowlist 自动过期策略、不做一次性批量的「全部批准」放宽**：与本事故无关。
- **无人值守仍要求预登记**：定时任务/runbook 没人能点卡；若允许临时授权，授权键根本
  没有用户动作产生。回滚任务走交互会话，因此现在可以现场确认。
- **非交互下跳过向导的副作用**：定时任务里「模糊但 safe」的破坏性命令会直接执行。
  定时任务本身是用户预先编排的，且原行为是无限重试等一张没人答的题；接受此变化。
- 已登记 allowlist 的 prod 命令继续静默放行 —— 预登记仍是推荐的稳态做法，只是不再是
  唯一出路。
