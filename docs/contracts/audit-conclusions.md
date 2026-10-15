# 契约：审计结论取值（conclusion）

这条口径原本写在 `docs/security-boundary.md §6`，那份文档退役后搬到这里单独持有
（搬家理由见 `docs/adr/0004-audit-conclusion-contract-carrier.md`）。载体换了，约束没换：
**三份清单逐字相等**，由 `backend/tests/test_ai_loop_round13c.py::test_audit_conclusion_三处清单同源` 钉死。

- 枚举唯一来源：`backend/agent/audit.py` 的 `LEGAL_CONCLUSIONS`
- 前端筛选下拉：`src/views/ssh/AuditView.vue`
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
