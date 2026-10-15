# Agent 记忆系统整体重设计 — 设计规格

日期：2026-09-22
状态：已实施（§9/§10 口径已随第五轮清理对齐代码；第六轮清理删除了本 spec 遗留的零调用助手 `mstore.pending_backfill_ids` / `text.match_query`）
范围：`backend/agent/memory*`、`backend/database.py`、`backend/routes/agent.py`、`backend/post_processor.py`、`backend/agent/harness/bridle.py`、`backend/agent/consciousness_tick.py`、`backend/agent/reflection.py`、前端记忆 UI / 设置面板。

---

## 1. 背景与目标

现有"三层记忆"（working / episodic / semantic）+ reflection-evolution 第二条知识通路存在结构性缺陷：

| # | 缺陷 | 证据 |
|---|------|------|
| 1 | 三条写入通路（episodic 规则摘要 / tick 固化 / LLM 提取）互不感知，去重仅 cosine≥0.85 单层，矛盾与过时记忆共存 | `memory.py`、`consciousness_tick.py`、`post_processor.py` |
| 2 | 检索为全表拉取 + Python 侧余弦，无索引；embedding 不可用时降级 n-gram 伪向量，语义能力趋零 | `memory.py:270-321`（代码自认饱和） |
| 3 | 记忆无 scope/实体维度（主机、偏好、经验混在一张平面文本表），误注入靠阈值硬扛 | `database.py:280-298` |
| 4 | `agent_memories` 与 `agent_strategies` 两条跨会话知识通路职责重叠 | `reflection.py:446-557` |
| 5 | run 间上下文摘要不落地、压缩是截断拼接非语义摘要 | `bridle.py:173-354` |
| 6 | 用户干预面仅"看+删"；`memory.strategy` 死配置；无编辑/新增/审计 UI | `AgentMemoryView.vue`、`routes/settings.py:246-258` |

目标：按 2025-2026 主流范式（Mem0 对账管线、Letta 自编辑记忆块、Zep 失效不删除、Claude Code 文件型分层注入）重建统一记忆系统，解决"错误的旧经验被当真相注入"这一运维场景核心风险。

非目标（调研判定为单体本地场景过度设计）：知识图谱/社区检测、多租户 namespace、模型 rerank 服务、四时间戳全套双时态、云端记忆共享。

## 2. 已确认决策（用户对齐记录）

1. **范围**：三层记忆 + reflection 通路整体重设计，允许破坏性重构。
2. **检索底座**：sqlite-vec + FTS5 混合（含降级路径，见 §11）。
3. **交互模式**：预检索注入（保下限）+ `memory_search`/`memory_add` 等 agent 工具（省 token、可多跳）双通道。
4. **更新/遗忘**：Mem0 式 ADD/UPDATE/DELETE/NOOP 后台对账 + `valid_at/invalid_at/superseded_by` 失效标记（失效不物理删）。
5. **反思通路**：并入统一记忆（`kind=procedural`）；演化超参归入设置层。
6. **存量数据**：一次性迁移进新 schema，打 `pending_reconcile` 由后台批量对账消化；spec 一次定稿，实施分阶段。
7. **架构方案**：方案 B —— 统一后台管线（A 骨架）+ 自编辑画像块层（Letta 薄层）。

## 3. 架构总览

```
                 ┌────────────────────────────────────────────┐
 写入源           │            memory_jobs（唯一写入口）        │
 ①run收尾 ───────►│  episodic_summary / candidate_fact /       │
 ②agent工具 ─────►│  agent_add / migration_backfill            │
 ③迁移批量 ──────►│                                            │
                 └───────────────┬────────────────────────────┘
                                 ▼  （consciousness tick 消费，每 tick ≤20 job）
                        ┌─────────────────┐
                        │   Reconciler     │ 相似检索top8 → 一次LLM批量判定
                        │ ADD/UPDATE/      │ → 应用裁决 + memory_audit 留痕
                        │ DELETE/NOOP      │
                        └───────┬─────────┘
                                ▼
        ┌───────────────────────────────────────────────┐
        │ memories（事实/情景/经验，带scope+生命周期）      │
        │  + memories_vec（sqlite-vec） + memories_fts   │
        │ memory_blocks（画像块，≤3个常驻键）             │
        │ memory_audit（裁决留痕）                       │
        └───────────────────────┬───────────────────────┘
                                ▼
              ┌─────────────────────────────────┐
   预检索注入 ◄─┤ Retriever：vec + FTS(BM25) 并行   │
   (≤5条/1500t)│ RRF融合 + scope硬过滤 + 断层截断  │
   memory_search工具 ◄──┘（同一服务，三个消费方共用） │
              └─────────────────────────────────┘
```

常驻注入 = memory_blocks（画像）+ 预检索 memories top-K；动态查询 = `memory_search` 工具；写入 = job 管线（agent 工具只是投 job 的高优先级入口）。

## 4. 数据模型

### 4.1 `memories`

```sql
CREATE TABLE IF NOT EXISTS memories (
    id             TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,              -- semantic / episodic / procedural
    scope          TEXT NOT NULL DEFAULT 'global', -- global / host:{hostId} / project / run:{runId}
    title          TEXT NOT NULL DEFAULT '',   -- 短标题，列表与索引用
    content        TEXT NOT NULL,
    source         TEXT NOT NULL,              -- llm_extract / agent_tool / user_ui / migration / tick_consolidation
    status         TEXT NOT NULL DEFAULT 'active', -- active / pending_reconcile / invalidated / superseded / rejected
    importance     REAL NOT NULL DEFAULT 0.5,
    pinned         INTEGER NOT NULL DEFAULT 0, -- 用户固定，对账不碰
    use_count      INTEGER NOT NULL DEFAULT 0,
    last_used_at   TEXT,                       -- 仅"真正注入上下文"时更新（区别于现实现 retrieve 即 touch）
    valid_at       TEXT,                       -- 事实生效时间（简化双时态）
    invalid_at     TEXT,                       -- 失效时间，NULL=当前有效
    superseded_by  TEXT REFERENCES memories(id),
    embedding      BLOB,                       -- float32 序列化；NULL=仅FTS可检
    embedding_src  TEXT,                       -- 'api:{model}'；不再存在伪向量
    source_conv_id TEXT,
    agent_run_id   TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_memories_scope_status ON memories(scope, status);
CREATE INDEX ix_memories_kind_status  ON memories(kind, status);
```

- `kind=procedural` 吸收原 reflection：形如"上次 X 主机磁盘告警用 du 逐级定位解决，直接 df -h 会漏看 inode"的经验教训。
- `mode` 列取消：本项目只有 ssh 模式，mode 过滤是死维度；`host:{hostId}` scope 承接"按连接过滤"的真实需求（hostId 用现有连接档案 id，`host_profile.py` 同源）。
- scope 由写入源提供：agent 工具显式声明；LLM 抽取默认 `global`，候选带 host 提示词时抽为 `host:*`；episodic 从 run 关联的 host 推断。

### 4.2 索引虚表（与 `memories` 同事务同步）

```sql
CREATE VIRTUAL TABLE memories_fts USING fts5(title, content, tokenize='unicode61');
CREATE VIRTUAL TABLE memories_vec USING vec0(embedding float[<model_dim>]);
```

- 中文分词：写入 FTS 前用与现 `_extract_terms` 同族的轻量切词（沿用中文 2-gram，不引 jieba——零依赖原则，2-gram 对 BM25 足够）；FTS 查询串做同样切分。
- `embedding_src` 决定 vec 表是否收录：仅 `api:` 来源入 vec 表；embedding 不可用时该记忆只有 FTS 通路（见 §11 降级矩阵）。
- vec 维度在首个 api embedding 成功时定型并记录到 `app_settings:memory.vec_dim`；维度变更（换 embedding 模型）触发全量重嵌入 job（后台批量，旧向量继续服务直到替换完成）。

### 4.3 `memory_blocks`（画像层，常驻注入）

```sql
CREATE TABLE memory_blocks (
    key         TEXT PRIMARY KEY,   -- 固定枚举：user-preferences / host-profile:{hostId} / agent-rules
    value       TEXT NOT NULL,
    limit_chars INTEGER NOT NULL DEFAULT 2000,
    version     INTEGER NOT NULL DEFAULT 1,
    updated_by  TEXT NOT NULL,      -- agent_tool / user_ui
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE memory_block_history (   -- 每次变更留痕，可回滚
    key TEXT, version INTEGER, value TEXT, updated_by TEXT, created_at TEXT DEFAULT (datetime('now'))
);
```

- `user-preferences`：用户明示的持久偏好（"以后 kubectl 都带 -n prod"）。
- `host-profile:{hostId}`：**取代 `host_profile.py` 现有静态画像注入**——初始画像仍由探测生成，但以块形式落库，agent 运维中可增量改写（记下"该机 nginx 配置在 /usr/local/nginx"）。
- `agent-rules`：agent 自总结的操作守则（吸收原 reflection 中"高频通用策略"，其余教训进 `kind=procedural`）。
- 规则：块只允许 `block_update`（old_string 精确替换，Letta 式），不允许整块清空；超限拒绝写入；仅当前连接主机的 host 块注入。历史保留最近 20 版。

### 4.4 `memory_jobs` 与 `memory_audit`

```sql
CREATE TABLE memory_jobs (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL,     -- episodic_summary / candidate_fact / agent_add / backfill_reconcile
    payload TEXT NOT NULL,                        -- JSON
    status TEXT NOT NULL DEFAULT 'pending',       -- pending / done / failed
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    priority INTEGER NOT NULL DEFAULT 5,          -- agent_add=1 优先消费
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE memory_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT, job_id TEXT,
    action TEXT NOT NULL,                          -- ADD / UPDATE / DELETE / NOOP / INVALIDATE
    old_content TEXT, new_content TEXT,
    reason TEXT NOT NULL,                          -- LLM 裁决理由 / 用户操作 / 衰减规则
    created_at TEXT DEFAULT (datetime('now'))
);
```

## 5. 写入管线与对账

**唯一写入口 = Reconciler**（`pending` job 的应用阶段）。四个写入源全部投 job 或直写（仅 `user_ui` 编辑/新增与 `pinned` 例外，见下）：

| 源 | 现实现 | 新行为 |
|---|---|---|
| run 收尾 | 规则摘要直接入库（`routes/agent.py:976-1000`） | 投 `episodic_summary` job：含任务、工具序列、失败点、结果要点；tick 里由低预算 LLM 写成自然语言情景条目（不再拼模板），失败回退现有规则摘要 |
| 对话后 LLM 提取 | `post_processor._extract_memories` 直接入库+cosine去重 | 抽取 prompt 保留（升级为要求输出 title/scope/kind），产物改投 `candidate_fact` job；删除其本地去重逻辑（对账收敛到 Reconciler 一处） |
| agent 工具 | 无 | `memory_add` 投 `agent_add` job（priority=1，下一个 tick 秒消化；非阻塞对话） |
| 用户 UI | 无 | **直写** `memories`，`source=user_ui, pinned=1`，跳过对账；解 pin 后才进入对账池 |
| tick 固化 | 规则提取 episodic→semantic 直写 | 删除该通路；episodic→procedural 的整固归 Reconciler 统一处理 |

**Reconciler 判定协议**：候选 → 混合检索同 scope ∪ global 的相似 top-8 → **一次** LLM 调用批量判定（输入：候选列表 + 相似旧记忆带 id；输出 JSON：每条 `{op: ADD|UPDATE|DELETE|NOOP, target_id?, new_content?, reason}`）→ 应用：
- UPDATE：旧行 `status=superseded, invalid_at=now, superseded_by=新行`，新行继承 `use_count` 累计（热度连续）；
- DELETE：仅用于"被证伪的旧经验"，同样置 `invalidated` 不物理删；
- 所有动作写 `memory_audit`。

**tick 改造**（`consciousness_tick.py` 保留骨架，任务集重定义）：
1. 消费 jobs（每 tick ≤20 个，`agent_add` 优先）；
2. 整固扫描：每类 kind 池容量修剪（超 `memory.max_items` 时按 importance+新近淘汰最差的 `status→invalidated`）；
3. 衰减：90 天未注入且 `use_count=0` → importance -0.1（下限 0.2，低于地板转 invalidated）；
4. 重嵌入批处理（仅当模型变更时）。
现有"热点 importance+0.05"自增强回路**删除**（P3 修过一次自增强，这次连奖励一起断）。

**演化超参**：`agent_strategies.evolved_config` 迁至 `app_settings:evolution.*`，读写点从 reflection 解耦，仍按现状覆盖运行参数。

## 6. 检索与注入

**Retriever（单一服务，三消费方共用：预检索 / `memory_search` 工具 / Reconciler 对账）**：

```
输入: query, scope_filters(host|global), kinds?, top_k
并行: ① sqlite-vec KNN top-20（有 api 向量的行）
      ② FTS5 BM25 top-20（中文 2-gram 切分后 MATCH）
合并: 去重 → RRF(k=60) 排序
过滤: scope 硬过滤 + kind 过滤；status 默认仅 active，pending_reconcile 可查但注入时加 `(待确认)` 前缀（invalidated/superseded/rejected 永不出现在结果，仅供审计链展示）
截断: 绝对下限（沿用 0.3 语义分带经验，RRF 归一后）+ 相邻分差 >0.15 断层截尾
输出: 带 id/kind/scope/title/content/importance/来源链接
```

- `use_count/last_used_at` 只在**实际写进 prompt** 后更新（修复现"检索未注入也计数"）。
- 检索超时 8s 与降级兜底（recency）保留现实现（`routes/agent.py:456-494`）。
- 8s 超时时按"仅 FTS→仅 recency"两级降级，向量通路失败不阻塞对话。

**预检索注入**：run 前用 `用户首条消息 + 当前主机块内容摘要` 作复合 query，top-5、**总预算 ≤1500 token**（`memory.inject_budget_tokens`，默认 1500），注入仍走 bridle `[相关记忆]` system 消息位（沿用现位置，改动最小），每条格式 `[mem:{id}] (kind/scope) title: content`——带 id 供 agent 在工具与回复中引用。对话型意图跳过注入（沿用 `intention.py` 判定）。

**反思注入删除**：`routes/agent.py` system prompt 中"近3条 score≥0.5 反思"通路（`reflection.py:498-557`）移除——procedural 记忆走统一检索，不再开第二条后门。

## 7. Agent 工具面（4 个新工具）

| 工具 | 参数 | 行为 |
|---|---|---|
| `memory_search` | query, kinds?, top_k≤10 | 调 Retriever，返回精简列表；run 中途自主多跳 |
| `memory_add` | content, title?, kind, scope, importance? | 投高优 job；不直接入库（防绕过对账） |
| `memory_invalidate` | memory_id, reason | 置 `invalidated`+审计；agent 发现注入记忆已过时的即时修正通道（运维场景关键：旧经验被当场证伪） |
| `block_update` | key, old_string, new_string | 画像块精确替换，超限/未命中拒绝，写历史 |

注册进 reins 工具面（与现有工具同套权限/审批框架——`memory_add`/`block_update` 属低风险自动批准，`memory_invalidate` 亦只标失效不删原文）。system prompt 工具指引段补一行使用时机："涉及历史经验或其他主机先例时先 `memory_search`；用户给出持久偏好/主机特性时 `block_update` 或 `memory_add`。"

## 8. 上下文组装与压缩（Bridle / 记忆块接入）

- `bridle._build` 组装顺序改为：**blocks 段（`[画像]` user-preferences + 当前主机 + agent-rules，无条件常驻，预算 ≤1200 token）→ system → `[相关记忆]` → history → `[上下文摘要]` → run 消息**。
- **`MemoryBudget` 统一预算**：新增单一函数按 `model.max_tokens` 依次分配（blocks 1200 / 记忆注入 1500 / history 条数窗 × 单条均值 / 摘要 25% 上限 / 余量给输出+安全垫），替换 `bridle.py:36-85` 的字符估算；token 估算统一收敛到 `cost_tracker.estimate_tokens`。`safe_tokens.py` 的 `safe_max_tokens` 也随之改为委托该估算器（模块仍存活，被 `routes/agent.py` 调用），并未废弃。
- run 内压缩保持"溢出时规则摘要"（零延迟，不动）；**新增** run 成功后 `episodic_summary` job 让 tick 用 LLM 重写为语义摘要再入库（压缩质量在管线里解决，不拖慢对话）——即"快路径规则保底、后台 LLM 提质"。
- `context_summary`（run 内滚动摘要）持久化到 `agent_runs` 行，供轨迹回放与 episodic job 作输入，不再随 run 消亡。

## 9. 前端 UI 与设置

**`AgentMemoryView.vue` 升级为记忆管理面板**（入口：**设置 → 记忆 → 「打开记忆管理」**，懒挂载）：

> 入口沿革：本节原写「AI 面板入口不变」。外来提交 `e309598`（标题只说删死配置 `memory.strategy`）
> 顺手撤下了 AI 面板上的「记忆」按钮与挂载点，使整块面板 0 引用；第五轮清理把入口改挂到设置面板，
> 不再回到 `SshAiPanel.vue`（该组件同时承载目标/工件线）。
- 列表：按 kind/scope/status/source 筛选，显示 title + importance + use_count + 状态徽标（superseded/invalidated 灰显可展开"被谁取代"链）；
- 操作：编辑（置 `pinned` 提示）、pin/unpin、手动新增（直写+`user_ui`）、删除（置 invalidated，非物理删）；
- **审计抽屉**：单条记忆的 `memory_audit` 历史（为什么被改/谁改的）；job 队列状态（pending/failed + 错误 + "立即整理"手动触发 tick）；
- blocks 编辑：三个画像块直接可编辑（带 diff 预览与历史回滚按钮）。

**设置**：
- 移除死配置 `memory.strategy`；
- `memory.enabled`（总开关）、`memory.max_items` 保留并进设置 UI（现缺 UI）；
- 新增 `memory.inject_budget_tokens`(1500)、`memory.search_timeout_s`(8)、`memory.reconcile_llm_enabled`(true，关掉则退化为纯阈值去重)。
- embedding 沿用现有模型行配置，不新增设置项；模型行 embedding 探活结果在设置面板显示（"语义检索：可用/仅关键词"）。
  - **未实现**：后端没有探活端点，`mstore.stats()` 只返回 memories/jobs 计数，设置面板因此不显示这一行。
    第五轮清理只做「文档与代码对齐」，不在清理轮里造新能力；本条留作后续需求。

## 10. 存量迁移（启动时执行，幂等）

原设计（落笔时假定 v1 表叫 `agent_memories`）：

1. 旧表 `RENAME TO agent_memories_archive / agent_strategies_archive`（保留 90 天，UI 不展示）。
2. 映射灌入新表：`type→kind` 直映；旧 `mode` 丢弃；全部 `source=migration, status=pending_reconcile`；旧 embedding 若为 `api:` 来源**且维度与新模型一致**则搬运为 BLOB，否则置 NULL 等重嵌入——伪向量（`local-256`）**一律丢弃**（它们正是"误注入"源头）。
3. `agent_strategies(strategy_type='reflection')→kind=procedural`；`evolved_config→app_settings`。
4. 迁移完投 `backfill_reconcile` job 批（每 tick 消化 ≤20 条），矛盾旧记忆在首次整理时被裁决；`pinned=0, importance` 沿用旧值。
5. 迁移失败 → 启动回滚 rename，系统以旧表只读模式起（记 error 日志 + UI 提示），不阻塞使用。

**落地口径**（`database._init_memory_v2` / `_migrate_memory_v1_to_v2`，与上面不一致处以本节为准）：

1. **不改名归档 v1 业务表**。v2 总表占用的正是 `memories` 这个表名，而它被 v1 三层记忆用过一次（列 `id/mode/content/…`，无 `kind`）—— `CREATE TABLE IF NOT EXISTS` 撞同名旧表会静默跳过，缺的是整张表的列。所以升级时先验 `PRAGMA table_info` 形状，把 v1 的 `memories` **改名为 `memories_v1_layers` 原样保留**（不删一行），再建真表。`agent_memories` / `agent_strategies` 保持原名，作为迁移源留在库里，UI 不展示。
2. 映射是**复制不是搬运**：`type→kind` 直映（非法 `type` 归 semantic），旧行不删；`memories.id` 沿用旧 id（reflection 行为 `mig-{id}`）配 `INSERT OR IGNORE`，因此整批可重跑。`source=migration`；`episodic→active`（事实记录不对账），其余（`semantic`/`procedural`）`→pending_reconcile`；旧 embedding 仅 `api:` 来源搬成 float32 BLOB，伪向量丢弃。**`mode` 丢弃、统一 `scope='global'`**，"维度与新模型一致"未单独校验（维度只在装 sqlite-vec 时由 `ensure_vec_table` 把关）。
3. `agent_strategies(strategy_type='reflection')→kind=procedural`（直接 active）。**`evolved_config→app_settings` 未做**：演化配置仍在 `agent_strategies`，运行期由 `routes/agent.py:_load_evolved_config` 读取，属另一条在跑的通路，不属记忆 v2 的读面。
4. 每 **10** 条 `pending_reconcile` 投一个 `backfill_reconcile` job（`priority=7`，id `mj-mig-{下标}`），每 tick 取 ≤**20** 个 job（`mstore.next_jobs`）。FTS/向量侧表在迁移里同步写（`_index_side`，分词与 `mstore._fts_sync` 同源）——否则面板列得出、检索搜不到。
5. 迁移失败**不回滚 rename**（除 1 里的改名，没有别的 rename）：`conn.rollback()` + 照样置 `memory_v2.migrated='1'`，v2 从零积累、旧表整表保留。理由是重跑半截迁移比重跑整批更危险（部分搬运的 embedding 无法分辨）。唯一例外：第 1 条那次真表重建会**清掉标记**重跑整批——缺列的旧表上这次迁移不可能成功过，不清就等于把存量永久锁在 `agent_memories` 里（正是 09-23 修掉的升级失效）。
6. **旧表并非只读**：`agent_strategies` 至今仍被 `reflection.py`（写反思）和 `evolution.py:save_evolved_config` 写入，`agent_memories` 则已无写入方。原第 5 条"以旧表只读模式起"的说法按此理解。

回归：`backend/tests/test_memory_v2_legacy_table.py`（预置 v1 形状表 + 被 poison 的迁移标记）。

## 11. 降级与错误处理矩阵

| 故障 | 行为 |
|---|---|
| embedding API 不可用 | 新记忆 `embedding=NULL` 只进 FTS；预检索退化纯 BM25；候选照常入队但 Reconciler 判定用 FTS 相似（照常 LLM 裁决，检索召回差些）；**绝不写伪向量**（现 `_fallback_embed` 整条删除） |
| sqlite-vec 扩展加载失败（Windows 打包/无 load_extension） | 启动探测，退化 Python 侧余弦暴力扫（保留现有实现思路但按 scope 分桶 + LRU，仅作 fallback）；FTS5 是 sqlite 编译期默认，理论恒可用；均不可用时纯 recency 兜底 |
| LLM 对账失败（job 处理出错） | job `attempts+1`，3 次后 `status=failed` 进 UI 可见；候选暂存为 `pending_reconcile` 状态（可检索、注入时加 `(待确认)` 前缀） |
| Retriever 超时 | 8s 上限 → FTS-only → recency，两级（现兜底保留） |
| 对账 LLM 输出非法 JSON | 重试 1 次，再失败按 ADD 处理但 importance×0.8 并标 audit `reason=malformed-fallback` |
| 记忆总表失控（>2×max_items） | tick 修剪强制生效；`pending_reconcile` 不参与修剪（防止 job 丢失） |

## 12. 测试策略

- **单测**（`backend/tests/test_memory_v2_*.py`，逐阶段随工）：schema/迁移幂等、scope 过滤、RRF 融合次序、断层截断、Reconciler 四操作与审计、job 重试到 failed、衰减与修剪、块 `block_update` 精确替换与超限拒绝、伪向量不入库、MemoryBudget 分配。
- **行为回归**：现有 `test_memory_retrieval_round16.py`、`test_memory_max_items_round11.py` 迁移改写为新引擎断言；Stirrups 溢出压缩测试不动。
- **小型对抗集**（自建，借鉴 LongMemEval 题型）：同主机矛盾经验对账后只剩 active 一条且有审计链；global 记忆注入、他 host 记忆不注入；对话型意图零注入；pinned 不被对账碰。
- 全部经 `asyncio.to_thread` 的 sqlite 访问保持线程池模式；**集成测试不并发跑 pytest**（WAL 锁，既有教训）。

## 13. 实施分期（每阶段独立可交付、可回滚）

| 期 | 内容 | 交付判据 |
|---|---|---|
| P1 | 新 schema + 迁移 + Retriever（vec/FTS 双路+RRF）+ 读路径切换；旧三写入源经临时适配层直写新表（`status=pending_reconcile`，不经对账） | 检索质量不倒退（对抗集 scope/注入用例绿）；降级矩阵可用 |
| P2 | `memory_jobs` + Reconciler + tick 改造 + 三写入源投 job + 审计 | 对账四操作与审计链测试绿；job 积压可控 |
| P3 | memory_blocks + 4 个 agent 工具 + system prompt 接入 + 删 reflection 注入后门 | 工具端到端；块常驻不挤占（预算测试） |
| P4 | 前端记忆管理面板 + 设置项 + 死配置清理 | UI 走查 + API 契约测试 |
| P5 | 删除 `agent_memories` 旧路径 / `_fallback_embed` / `memory.py` 旧类 / `agent_strategies` 读依赖 + 文档 | grep 无残留引用；全量测试绿 |

依赖关系：P2 依赖 P1 表结构；P3 依赖 P2（写入口收口后才放 agent 直写通道）；P4 依赖 P1-P3 接口；P5 收口。

## 14. 主要触点文件

新增：`backend/agent/memory/`（包：`store.py` `index.py` `retriever.py` `reconcile.py` `blocks.py` `jobs.py` `budget.py`）、`backend/tests/test_memory_v2_*.py`、`src/components/agent/MemoryPanel/*`。

> 落地时的实际文件名与设计稿不同（P1/P2 期间合并了模块）：`backend/agent/memory/` 下是
> `mstore.py`（store+jobs 合一）/ `retriever.py` / `reconcile.py` / `blocks.py` / `budget.py` /
> `text.py` / `vec.py` / `embedding.py`；前端没有 `MemoryPanel/*` 目录，管理面板仍在
> `src/components/agent/AgentMemoryView.vue`（入口在设置面板的 `MemoryPanel.vue`，两者不是同一个组件）。
改造：`backend/database.py`（新表+迁移）、`backend/routes/agent.py`（预检索/注入/反思后门）、`backend/post_processor.py`（提取投 job）、`backend/agent/consciousness_tick.py`（job 消费）、`backend/agent/harness/bridle.py`（blocks 段+预算）、`backend/agent/harness/reins/*`（工具注册）、`backend/agent/host_profile.py`（升级为块）、`backend/agent/reflection.py`/`evolution.py`（并轨）、`src/components/agent/AgentMemoryView.vue`、设置面板。
删除：`backend/agent/memory.py`（P5）、伪向量与 `_fallback_embed`（P5）、`memory.strategy` 配置（P4）。

## 15. 调研出处

Mem0 论文/文档（ADD/UPDATE/DELETE/NOOP、scope）、MemGPT/Letta（memory blocks、self-edit、sleep-time）、Zep/Graphiti（bi-temporal 简化、失效不删）、LangGraph（checkpointer/store、background 写入）、Claude Code（分层文件记忆、compaction）。详见调研记录（对话存档 2026-09-22）。
