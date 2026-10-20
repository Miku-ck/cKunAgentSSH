# 契约：审计结论取值（conclusion）

这条口径原本写在 `docs/security-boundary.md §6`，那份文档退役后搬到这里单独持有
（搬家理由见 `docs/adr/0004-audit-conclusion-contract-carrier.md`）。载体换了，约束没换：
**四份清单逐字相等**，由 `backend/tests/test_ai_loop_round13c.py::test_audit_conclusion_四处清单同源` 钉死。

- 枚举唯一来源：`backend/agent/audit.py` 的 `LEGAL_CONCLUSIONS`
- 前端筛选下拉：`src/views/ssh/AuditView.vue`
- 渲染层追加审计的请求体类型：`src/api/audit.ts` 的 `AuditConclusion`
- 本节

前端可筛而后端写不进的取值 = 永远为空的筛选；后端能写而前端筛不出的取值 = 审计页里
看不见的记录。而「人工交互通道不加事前阻断」这条设计辩护的前提就是事后审计可追 ——
口径写窄等于把替代防线写薄。

`conclusion ∈ {executed, failed, blocked, denied, auto_allowed, approved, released,
revoked, answered, skipped}`

| 取值 | 含义 |
| --- | --- |
| `executed` | 命令已下发执行 |
| `failed` | 工具执行失败（每次失败都会写一条，此前既筛不出也没有样式） |
| `blocked` | 被 hardline 规则阻断，未下发 |
| `denied` | 用户在审批里拒绝 |
| `auto_allowed` | 命中自动放行档位，未经人工批准 |
| `approved` | 人工审批通过 |
| `released` | 授权已释放 |
| `revoked` | 授权已撤销 |
| `answered` | 澄清向导终态：用户已作答（是决策，不是执行结果） |
| `skipped` | 澄清向导终态：用户跳过 |

非法取值仍落 `executed` 以保住枚举列的有界性，但原始结论会追加进 `result`（在本条记录的
哈希覆盖范围内）并记一条 warning —— 过去它是静默改写，等于让一条不可篡改的链式记录声称
「这条命令执行过了」。

> 这条「非法取值改 executed」是 `append_record` 内部对 Agent 流程的兜底。
> `POST /api/audit/append` 的 HTTP 边界不沿用它：客户端 bug 当场 422，不合法的结论
> 一条都不落库（拼错的 `"executed "` 不许伪装成一次真实执行）。

## 渲染层人工通道的写入口（S4-07）

批量下发与 Docker 变更动作走 `ssh:exec` IPC，不经过 Agent 的 AuditEngine 拦截器，
原先在审计链上完全不可见。现在由渲染层在动作后调用 `POST /api/audit/append`，
逐目标追加记录：

- `action`：`bulk_ssh_exec` / `docker_action`；`target_type=ssh`；
  `target_id` 为 `连接名 (用户名@主机)`（无连接记录时回落会话 shell/名称）；
- `environment`：仅当连接记录标记 `prod` 时为 `prod`，其余一律 `test`；
- `operator` 由服务端从 OS 用户名派生，请求体多传字段一律 422；
- `executed` = 命令已下发。远端返回非零退出码仍记 `executed`（退出码进 `result`）；
  `EXEC_TIMEOUT` 也记 `executed`，`result` 标注 `remote outcome unknown` —— 超时不
  代表没执行，记 `failed` 会诱导用户重试；
- 审计写失败返回 500，渲染层只提示、不回滚已发生的远端动作：审计是事后证据，
  不是执行闸门；批量下发会把多个目标的写入失败聚合成一次警告；
- 只覆盖「下发/变更」，不覆盖读路径（容器列表、日志、inspect、批量安全判定），
  否则一次刷新就会往链里灌噪声。

已知未覆盖：终端内直接敲的命令，以及 `ssh:killProcess`（仪表盘杀进程）仍不经审计
写入口，属于后续通道扩展项。
