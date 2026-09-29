# S3 · 记忆子系统

锚点 `a8e588c`。**核验图例**：✅ = 我本人打开原文逐行证实；◐ = 采信 subagent 引用、我未二次打开。
本节同时**推翻了两处我方/历史前提**，见「对我方前提的纠正」节。

## 发现表

| ID | 维度 | 严重度 | 标题 | 主 file:line | 可达性 | 核验 |
|---|---|---|---|---|---|---|
| S3-01 | b | Major | 对账器无条件信任 LLM 给的 `target_id`，不校验它属于本候选的对照面 ⇒ 作废一条无关记忆，审计 `old=NULL` | `reconcile.py:370` `:415-419` + `mstore.py:193-197` | 恒（默认开对账） | ✅ |
| S3-02 | b | Major | `decay_memories` 的第二条 UPDATE 是**无年龄门槛的地板清扫**：新写入且 `importance<0.2` 的行在同一次 tick 里被作废，且不落审计 | `mstore.py:671-690`（`c2` 在 `:682-686`） | 恒（tick 每轮） | ✅ |
| S3-03 | b | Major | 主机维度用的是**进程级临时会话 id** `ssh-N`（每次主进程启动 `nextId` 归零），而不是 spec 明令的连接档案 id ⇒ A 机记忆下次启动被当成 B 机事实注入 | `retriever.py:30-37` + `ssh-manager.ts:389` vs spec `:100` | 每个多主机工作流 | ✅ |
| S3-04 | b | Major | `pinned` **不保护**对账路径：`retire`/`supersede` 无 pinned 守卫，`_similar_existing` 只按 `status='active'` 取对照面 ⇒ 用户钉住的手写事实可被 UPDATE/DELETE 掉 | `mstore.py:165-168`（retire WHERE）、`:193-197`（supersede WHERE `id=?` 裸）、`reconcile.py:115-127` | 恒 | ✅ |
| S3-05 | d | Minor | `block_update` 的「仅当前主机」守卫在 run 未绑定主机时**整体短路**（`and sid`） | `memory_tools.py:242-252` | 需模型指名外部 host 键（`ssh-N` 极易猜） | ◐ |
| S3-06 | c | Minor | `POST /agent/tick/run` 响应里没有 `summary` 键，渲染层与 client JSDoc 都按有它写 ⇒「立即整理」恒 toast「已执行」 | `routes/agent.py:1865-1868`(anchor) vs `AgentMemoryView.vue:472-480` | 每次点击 | ◐ |
| S3-07 | c | Minor | 三处同形契约缺口：`MemoryAuditRow.old_content/new_content` 与 `MemoryRow.superseded_by` 已声明已写入但 `src/` 零消费者；`PUT /agent/blocks/{key}` 允许把画像块**整块清空**（agent 侧的拒空只在 `replace_in_block`） | `mstore.py:100-101` `:527-555` / `routes/agent.py:1833-1844`(anchor) | 恒 / 一次保存 | ◐ |
| S3-08 | c | Minor | `POST /agent/memories` 的 `content` **无长度上限**，而 agent 工具侧拒 500+ 并注释「schema 描述与实现必须同源」；超长条目 `estimate_tokens` 超过整份注入额度 ⇒ 被 `retrieve_for_injection` **永久跳过**却仍计入 stats 与面板 | `routes/agent.py:1727-1731`(anchor) vs `memory_tools.py:28-31,139-141` | UI 粘贴大段文本 | ◐ |

## LEGACY #3 重判：成立 —— 且当年的「硬修方向」被证伪

**当年登记**：`_process_one` 多 item 时每个 mstore 写各自 commit ⇒ item0 已 ADD 后 item1 抛 ⇒ fail_job 留 pending ⇒ 下 tick 整 job 重跑 ⇒ item0 二次 ADD；硬修 = 把外层事务贯穿全部 mstore 写；「崩溃中途路径无干净测试 seam」⇒ 交裁决勿动。

**(i) 机制：确认为真，且不需要两个 item。** 单候选即可确定性触发：`_apply` 的 UPDATE 分支先 `await asyncio.to_thread(mstore.add_memory, …)`（`:405-414`，**已 commit**），随后 `await asyncio.to_thread(mstore.supersede, target, mid)`（`:415`）；`supersede` 在 `target` 不是有效行时抛 `ValueError`（`mstore.py:188-192`）⇒ 留下**一条 active 孤儿行 + 零条 memory_audit**（审计写在 `:417`，抛错后到不了），job 留在 pending，下 tick 重来。`fail_job` 允许 3 次，而 UI 的「重试」把 `attempts` 归零 ⇒ **每点一次再叠 3 份重复**，无上限。加剧项：重放时已被 supersede 的旧行 `use_count` 仍在，`supersede` 无状态守卫 ⇒ `use_count += old.use_count` 把热度再继承一次。

**(ii) 「硬修=贯穿全写事务」这条方向不成立。** `backend/database.py:23` 是 `_thread_local = threading.local()`（`:77-88` 首次创建即缓存到线程本地），而 reconcile 的**每一个** mstore 写都经 `asyncio.to_thread` 派发（`:382,387,396,402,406,415,417,421,425,433,436,439`）⇒ 不同池线程 = 不同底层连接 ⇒ 根本无法用一个事务罩住它们。要么改成单线程串行执行整段（动 tick 的阻塞契约），要么走下面 iii 的旁路。

**(iii) 下游去重是概率性的，且有一种模式完全无保护。** 重放时 `_similar_existing`（`:115-127`，按 `scope`+`statuses=("active",)`+排除自身检索）**会**捞到那条孤儿（查询串 `f"{title} {content}"` 与入库 doc 逐字节相同 ⇒ BM25 排首；`min_cosine` 下限拦不住：vec 命中分≈1.0，纯 FTS 命中 `relevance=None` 被显式保留）。但会不会再写一份，取决于 LLM 是否选 NOOP/UPDATE——只有 prompt 请求（`:47-52`），无机制强制。硬去重（`_norm_content` 全等 / `_DEDUP_COSINE=0.95`，`:293-320`）**只在 `memory.reconcile_llm_enabled=false` 时运行**——恰是唯一能被证明兜住的档位。零保护层：`JOB_AGENT_ADD + priority_add=True + LLM 不可用` 直接 `op="ADD"` 无条件重复。

**(iv) 用户可见后果**：AgentMemoryView 出现 2-3 份同文条目；孤儿无审计 ⇒ 抽屉历史解释不了；重复行占 `max_items` 池位；召回侧两份独立计分、gap cut 不合并 ⇒ 同一句话在 1500 token 注入额度里出现两遍，而 `mark_injected` 按行摊薄热度。

**(v) 存在比整事务重写窄得多的 seam（三条，均不动 mstore 签名）**：① 把 `target_id` 视为无效除非它 ∈ `{h.memory_id for h in similars[i]}`，按既有「缺 target」同款降级（UPDATE→ADD / DELETE→NOOP）——这一条**同时消掉本条 S3-01**，且令抛错后不再落库；② 给 `supersede` 补上 `retire` 已有的 rowcount/状态契约（返 bool、目标不存在不抛），一次失败的 UPDATE 退化为普通 ADD；③ 用 job 行做进度标记（`memory_jobs` 有一列**从未被写**的 `finished_at`，`payload` 是可改 JSON），或按 `(job_id, index)` 在每 op 前查 `memory_audit` 幂等跳过。

**(vi) 「无干净测试 seam」不准确**：`backend/tests/test_memory_v2.py:315`（`test_llm_failure_retries_then_failed`）已经在 monkeypatch `_call_llm` 并对同一 job 连跑三次 `process_jobs`；同款 5 行 + `monkeypatch.setattr(mstore,"supersede",raiser)` 即可复现本 bug 并断言 `len(list_memories())==1`。我核过 `supersede` 无状态守卫、`retire` 有（`:165-168`），与 memory 里 09-23 那轮 `test_retire_returns_false_when_row_already_inactive` 的记载一致。

## S3-01 / S3-04 的相互加固关系

`_apply:370` 的 `target = decision.get("target_id") or (hits[0].memory_id if hits else None)` —— 3af18e9 把「缺 target 时用 hits[0] 兜底」在**派发前**降级掉了（`:359-361` 我核过，注释就是「宁可误留不误杀」），但 `_apply` 里这句兜底**仍在**，只是此时只可能被 NOOP 的审计标签读到（与当年判断一致）。真正的洞在另一半：**给了 target 就全盘接受**。配合 S3-04（`_similar_existing` 不看 pinned、`supersede` 的 WHERE 只有 `id=?`），一次 index↔target 错位就足以把用户钉住的、从未作为该候选对照面出现过的记忆判成 superseded。`_cand_block_for_prompt`（`:130-142`）把所有候选的相似行喂进**同一个** prompt 块，正是错位的来源；prompt 只能「请求」decisions 数量与 index 对齐（`:39-58`）。

## 对我方前提的纠正（重要，下游切片请采信）

1. **「9 个 `/agent/memories*` 端点无路由级测试」——不成立。** `test_memory_v2.py:677/697/707` 直接调 handler。我在 S0 之前给出的 inventory 把「没有 `routes.agent` import 的测试文件」等同于「handler 未测」，口径过粗。后续切片的「无路由测试」前提需逐条重估（`routes/audit.py`、`routes/skills.py` 我本轮**仍**未找到 handler 级调用，见 S11-05 / S8）。
2. **`memory_tools.py` / `vec.py` / `embedding.py` 并非「零测试」**：`test_memory_v2.py:588-676` 端到端跑了四个工具，`:192` 有 blob 往返用例。S3 的发现全部限定在这些测试**确实没覆盖**的分支（越权 target_id、add_memory 之后抛错、`sid==""` 守卫、pinned×对账、地板清扫）。
3. **spec 侧 §11:269** 声称 cosine 回退「按 scope 分桶 + LRU」，实现是 `ORDER BY updated_at DESC LIMIT 1000` 单扫（`mstore.py:350-354`）——inventory 里「embedding 维度不符会降级算」的既有判断不受影响。

## 规格轴漂移（`2026-09-22-agent-memory-redesign-design.md`，12 条 → 9 条独立行）

其中 4 条与上方发现同因，**不重复计条**：②「所有动作写 memory_audit」= S3-02；③「pinned 对账不碰」= S3-04 ✅；④「hostId 用档案 id」= S3-03 ✅；⑩「超限/拒空只保护 agent 路径」= S3-07。其余 8 条 + 1 条反向缺口成表如下（严重度按「有无行为后果」裁，纯文档失真一律 Minor）。

| ID | 维度 | 严重度 | 核验 | 标题 | 主 file:line | 可达性 |
|---|---|---|---|---|---|---|
| S3-09 | b | Major | ◐ | spec §4.2:112/§5:178 要求「维度变更触发全量重嵌入」，实现是**直接 DROP** `memories_vec` ⇒ 换 embedding 模型后只有新记忆有可用向量，且不可恢复 | `vec.ensure_vec_table` ↔ `mstore.py:97`（全仓 grep 重嵌入仅命中这句注释） | 换 embedding 模型时 |
| S3-10 | b | Minor | ◐ | scope 体系缺写源：`host:*` 无任何产出方（episodic 硬编 `global`、抽取丢弃 LLM 返回的 scope 字段而 prompt 仍在要它），`run:` 可被 UI 写入却永不被 `context_scopes` 取回，spec 列的 `project` 全仓不存在 | `reconcile.py`（episodic 写入）+ `mstore.context_scopes` ↔ spec §4.1:101 | 恒（能力空转） |
| S3-11 | b | Minor | ◐ | spec §11:272 的非法 JSON 四项兜底（重试 1 次、`importance×0.8`、`malformed-fallback` 审计、可见降级）全部未实现 ⇒ `_call_llm` 解析失败直接返回 None | `reconcile._call_llm` ↔ spec §11:272 | 恒（LLM 返回非纯 JSON 时静默丢一整批候选） |
| S3-12 | b | Minor | ◐ | FTS 建表口径与实现不同：spec §4.2:106 写的是 SQL 侧分词，实现是应用侧 2-gram | `mstore.py` FTS 建表 ↔ spec §4.2:106 | 恒（召回行为与文档不一致，无崩溃面） |
| S3-13 | b | Minor | ◐ | spec §9:230 的「列表按 source 筛选 + 取代链展开」与 §9:239 的「embedding 探活」在 UI 均无 | `AgentMemoryView.vue` ↔ spec §9:230/§9:239 | 恒（特性缺口，非缺陷） |
| S3-14 | b | Minor | ◐ | 审计动词表（spec §4.4:150）与渲染器的 `PIN/EDIT` 不匹配，且 `set_pinned` 根本不落审计 | `mstore.set_pinned` ↔ 审计动词表 ↔ `AgentMemoryView.vue` 渲染分支 | 每次钉/取钉 |
| S3-15 | b | Minor | ✅ | `rejected` 状态后端**从不写入**，UI 却有该筛选项 ⇒ 恒空列表（一个用户永远不会命中，因而永远不会被报告的筛选项） | `mstore` 状态写入集 ↔ `AgentMemoryView.vue` 筛选下拉 ↔ spec §4.1:80 | 恒（逻辑显然，未逐行） |
| S3-16 | b | Minor | ◐ | `MemoryPanel.vue:59` 注释仍写「PUT 是全字段覆盖」，与 `a8e588c` 统一后的「不传=不改」相反（store 恒发全键 ⇒ 无行为后果，纯文档） | `MemoryPanel.vue:59` ↔ `client.ts` 的 PUT 形状 | 恒 |
| S3-17 | b | Minor | ✅ | 反向缺口（代码有、文档无）：第三档 `_overlap_hits` 词面重叠召回、按连接缓存 sqlite-vec 可用性 —— 两处均未在 spec 登记 | `mstore.py:312-327`、`vec.py:37-55` | 恒 |

## 无发现类目（subagent 明确核过，我认可其口径）

BLOB codec 往返（唯一残留：`pack_vec` 在 `mstore.py:64`/`:210` 的 try **之外**，非数值 embedding 会以 `struct.error` 逸出而非降级为「无向量」——合规 provider 下不可达，故登记不立条）；RRF 算术（`1/(60+rank+1)` 为 k=60 0-based 正解）；`memory_jobs` 状态机自洽且有 `:315` 钉；端点鉴权/所有权（`X-API-Token` + 单用户桌面）；`consciousness_tick` 双启动（`_task.done()` 检查与 `create_task` 之间无 await）；`bridge.port` 型属性错读（本子系统中**未发现**）；`setStrategyEnabled` 的 query-vs-body 陷阱（不存在，已专门核过）。

## Rejected（命中 91 清单）
`retriever.py:74-76`（B-4）、`render_line`/`_render`（B-12）、`reset_flags`（B-22）、`_apply` kwargs 样板（B-13）、`count` 无消费者（B-5，含 strategies 与 jobs 的两处同形）、`_cosine` 的 zip 不加 strict（A 行）、`_VEC_OK_CONNS` 强引用增长（`vec.py:26-34` 已声明）、post_processor model 三连（B-11）、evolution 边界常量（`evolution.py:45` 已声明）、`retire` rowcount 与 evolution 指标错位（两条历史修复+回归锁均在位，已验证不复报）。
