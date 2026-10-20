# 审计结论口径的载体移到 docs/contracts/audit-conclusions.md

`conclusion ∈ {…}` 这行取值口径此前只存在于 `docs/security-boundary.md §6`，而有一条后端守卫
（`backend/tests/test_ai_loop_round13c.py::test_audit_conclusion_三处清单同源`）要求
**后端枚举 `audit.LEGAL_CONCLUSIONS` ↔ 前端 `AuditView.vue` 筛选下拉 ↔ 文档口径** 三处同源。
并行会话把那批 docs 删掉后，第三条腿没有任何载体，守卫红且红得正确。

决定：**口径搬家，守卫改指新位置** —— 新建一份只装契约取值的最小文档
`docs/contracts/audit-conclusions.md`（不复活整份安全边界长文），守卫指向它。

## 备选方案（已否决）

- **恢复 `docs/security-boundary.md`**：与本轮 docs 收缩的意图相反，且会把整篇长文连同已废弃段落一起带回来。
- **守卫删掉文档那一腿**：最省事，但"人工交互通道不加事前阻断"这条设计辩护的前提是"事后审计可追"，
  口径不再有可被机器校验的载体，等于把替代防线写薄。

## 后果

契约类文档集中到 `docs/contracts/`，删除文档前必须先给它找新载体，否则守卫会红在这里 ——
这正是这条 ADR 想要留下的提示。

> 2026-10-20 注：S4-07 新增渲染层追加审计的请求体类型（`src/api/audit.ts` 的
> `AuditConclusion`）后，守卫已扩为四处，测试名现为
> `test_audit_conclusion_四处清单同源`。上文描述的三处约束不变，只是多了一处要同步。
