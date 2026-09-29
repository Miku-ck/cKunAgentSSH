# S11 · 审计链与运维观测

锚点 `a8e588c`。实读范围：`backend/agent/audit.py`(433) 全文关键段、`backend/routes/audit.py`(103) 全文、`src/views/ssh/AuditView.vue` 结论/环境筛选项、`src/stores/audit` 侧 `client.ts` 契约。
**核验图例**：✅ = 我本人打开原文证实；◐ = 仅登记未二次核实。

## 先说结论：审计链本体是干净的

本轮唯一一次「读完整个模块而几乎无发现」的切片。逐条确认在位的强点：

- 链式签名 `_chain_hash = sha256(prev|canon)`（`:71-73`），`hash` 列带 UNIQUE（`:109`）。
- `append_record` 用 `BEGIN IMMEDIATE` + 内层 try/ROLLBACK/else:COMMIT 把「读尾 hash → 算链 → INSERT」原子化（`:161-197`）；`_last_hash` 在**同一事务内**取 `ORDER BY id DESC LIMIT 1`（`:213-214`），无并发分叉窗口。
- 同步 sqlite 的异步外壳 `append_record_async` 走 `asyncio.to_thread`（`:205-210`，注释写明了原实现在事件循环里 BEGIN IMMEDIATE 会被 `busy_timeout` 冻住 5s）。
- SQL 全参数绑定：`_build_filters`（`:229-245`）以 `_FILTER_COLUMNS` 定序 + **`strict=True` 的 zip**（那是 a8cb56c 加的安全位），`f"{col} = ?"` 的 col 只可能来自常量元组 ⇒ 无注入面。
- `query`/`count` 共用同一 `_build_filters`，注释记录了「曾手抄漏 action/run_id 导致 total 与 items 分叉」的既往缺陷。
- `archive_and_clear`（`:350-433`）是本切片最讲规矩的函数：归档先落盘再 DELETE、删除与封缄同事务、封缄记录作为新链 genesis（`prev_hash=""`）且把 `old_last_hash` 写进 result ⇒ `verify()` 对 live 链自洽、离线可重放旧链。空表时 `archived=0` 不动库。
- **跨层同步义务已被履行**：`audit.py:139-141` 注释「增删结论值时同步 `AuditView.vue` 的筛选下拉」。我逐项对了一遍——后端白名单 10 值（`:142-144`）与 `AuditView.vue:39-51` 的 10 个 `<option>` **完全一致**，且前端注释也反过来引用了后端条数。这条「文档说的和代码做的一致」在本轮属于罕见的正例。
- `verify()`（`:293-328`）逐行重算 hash 与 prev_hash，断链后继续计数以便定位，异常时返回 `intact:False + error`（不伪装成「完好」）。

## 发现表

| ID | 维度 | 严重度 | 标题 | 主 file:line | 核验 |
|---|---|---|---|---|---|
| S11-01 | a | Minor | `query`/`count` 把 DB 异常吞成空列表/0 ⇒ 审计页在故障时显示「没有记录」而非报错，与轮转后清空观感不可区分 | `audit.py:268-271,288-291` | ✅ |
| S11-02 | a | Minor | `_canonical` 以裸 `|` 连接 12 字段且**不转义字段内的 `|`**，`hash` 上的 UNIQUE 约束使相邻字段跨界移位可致 INSERT 冲突 ⇒ 该条审计被静默丢弃 | `audit.py:55-68,109,198-202` | ✅（机制核过，触发概率极低，见下） |
| S11-03 | c | Tradeoff | `command_raw` 明文入库并经 `/audit/export` 整表 CSV 落盘：命令行内联口令（`mysql -u root -pSECRET`、`curl -u`）随审计证据一同外带 | `audit.py:63,154-155` + `routes/audit.py:66-84` | ✅ |
| S11-04 | d | Minor | 封缄记录自身落 `environment="test"`（`_DEFAULT_ENV`），对生产主机做轮转后，按 prod 过滤审计看不到这条「谁清的库」 | `audit.py:398-404` vs `:30` | ✅ |
| S11-05 | b | Minor | `routes/audit.py` 四个端点**无任何路由级测试**（仅 `test_audit_coverage`/`test_audit_rotate` 测 `agent.audit` 本体），`/audit/export` 的 CSV 构造（空表时 `fieldnames=["id"]` 只出一个表头）无钉 | `routes/audit.py:25,66-84` | ✅ |
| S11-06 | b | — | 规格轴：审计覆盖声明与 reality 的差，**归口 S2 的 D4**（批量 `ssh:exec` 零审计）与 **S2-06**（审批记录缺 run_id/environment），本切片不重复计条 | `docs/security-boundary.md:26,138-139,150-156` | ✅（引用侧我看过） |

## 详情

### S11-01 [dim a][Minor] 读侧把故障伪装成空
`query()` 与 `count()` 的 `except Exception: logger.warning(...); return [] / 0`（`:268-271`、`:288-291`）。写侧静默丢审计是**已定契约**（`append_record` docstring「审计失败静默，返回 None 不阻塞 Agent」），但读侧同样静默的后果不同：`AuditView` 拿到 `{items:[],total:0}` 会渲染成「没有审计记录」的空态，用户无法区分「确实没发生过操作」「刚做过轮转」「库出问题了」。校验端点 `verify()` 反而做对了——异常路径显式 `intact:False` 并带 `error` 字段。**同一模块两种故障呈现口径**。

### S11-02 [dim a][Minor] 规范化编码的边界移位
`_canonical` 把 12 个字段 `str()` 后用 `|` 直连（`:55-68`），字段内容自身可含 `|`（`command_raw` 在运维场景里**几乎必然**含管道）。于是相邻两字段之间发生「一个字段尾部多出的 `|` 被下一个字段吸收」的移位，可令逻辑上不同的记录算出同一 canon ⇒ 同一 `hash` ⇒ 撞 `UNIQUE` ⇒ `append_record` 落进 `:198-202` 的兜底 ⇒ **这条审计静默消失**（正是 S2-06/D4 之外另一种丢法）。
诚实定级：需要两条记录同时存在且恰好在字段边界上互相移位，`conclusion` 走白名单、`run_id` 是 hex、`target_id` 是 grant-key 哈希，可移位的只有 `command_raw|result`、`action|command_hash`、`result|conclusion` 三对邻位 ⇒ 现实中极低概率，属**规范化编码缺陷**（正确形状是长度前缀或转义分隔符），不是可达 bug。列出来是因为 `AuditView` 的导出/校验语义都建立在「一条记录一条不可伪造的 canon」这个前提上。

### S11-03 [dim c][Tradeoff] 证据链与明文凭据同源
`_canonical` 含 `command_raw`、`command_hash` 只是摘要，明细留明文是防篡改证据的必然选择；但 `/audit/export`（`routes/audit.py:66-84`）把**整表**（含 `command_raw`）导出成带 BOM 的 CSV 供下载，归档路径 `audit_archive_*.csv` 也落在**数据库同目录**（`audit.py:384-393`）且从不清理。任何内联在命令行里的口令因此获得两个额外出口。是否可接受属产品决策：若要收敛，方向是导出时按 `_SUDO_STDIN_RE`/凭据形态做掩码，或在 `security-boundary.md` 明确登记为已接受取舍——现在文档里**没有**这一条。

### S11-05 [dim b][Minor] 无测试的读侧
`test_audit_coverage.py`(3) / `test_audit_rotate.py`(3) 全部指向 `agent.audit`，无一处 import `routes.audit`。⇒ 四个端点的线程池化（注释里写了三处「审查修复：同步 sqlite 移入线程池」）、`{items,total}` 形状、CSV 表头与 BOM、`Content-Disposition` 文件名，**均无回归锁**。本轮基线未跑（共享库锁），故只是登记缺口，未证伪任何行为。

## 本切片顺带确认在位的历史修复

- `zip(..., strict=True)`（a8cb56c）仍在 `_build_filters`。
- `append_record_async` 的 to_thread 化、`begin immediate` 序列化、`conclusion` 白名单含 `released/revoked/answered/skipped`（审批到期死锁轮的产物）均在位。
- `routes/audit.py` 的三处「同步 sqlite 移入线程池」注释与实现一致（`asyncio.to_thread` 分别包了 query+count 的 lambda、verify、export 的 `_build`）。

## 未覆盖声明

`agent/cost_tracker.py`(188)、`backend/logconfig.py`(60)、`routes/settings.py` 的 `/settings/log-level`(`:578-583`) 与 `/settings/diagnostics`(`:640+`) 只做了**存在性与调用方向**核对（诊断快照的 http 段读的是 netconfig 内存缓存，与 `/settings/http` 同源，无分叉——已在 S0 记录）。`cost_tracker` 有 `test_cost_budget_breaker.py`(9) 覆盖，本轮按 ⚪ 权重未逐行审。
