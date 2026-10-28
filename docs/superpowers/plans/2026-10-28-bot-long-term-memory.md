# cKunBot 跨会话长期记忆实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 cKunBot 跨会话记得用户的事：能从用户的话里认出可记的事实、下一段会话注入生成上下文、用户可查看与删除。

**Architecture:** 复用 Agent 记忆表 `memories`（`scope='persona:{id}'`、`kind='persona_fact'`）→ 新模块 `backend/bot/memory.py` 负责规则抽取与存取（写入走 `mstore.add_memory`，失效走 `mstore.retire`，取代走 `mstore.supersede`）→ `chat_service.stream_reply` 读侧把记忆渲染成一条独立 `role='system'` 消息插在人格 prompt 之后、历史之前，写侧在助手消息落库后抽取入库 → `/bot/personas/{id}/memories` 四个端点 → 人格编辑器新增「它记得你什么」区。

**Tech Stack:** FastAPI + Pydantic v2 + SQLite；Electron + Vue3 + Pinia + Vite；pytest(asyncio) / vitest / vue-tsc。

**Spec:** docs/superpowers/specs/2026-10-28-bot-long-term-memory-design.md

## Global Constraints

- **不新建表、不改 Agent 侧代码**：记忆落在 `memories`，只调用 `backend/agent/memory/mstore.py` 的公开接口。
- **scope 隔离是硬约束**：人格记忆只写 `persona:{persona_id}`；Agent 的注入路径只读 `global` / `host:*`，两边互不可见（用测试钉死）。
- **不依赖 embedding**：陪聊侧没有 embedding 客户端，检索一律纯 FTS（`mstore.fts_hits` 带 scope 过滤，FTS5 不可用时自动退化为词重叠）。
- **记忆是补充不是对话**：记忆块只进送给模型的 `messages`，**绝不写进 `bot_messages`**（可见记录保持干净）。
- **记忆层故障不影响陪聊**：召回与写入各自 try/except 兜底，异常只记 warning。
- **零额外模型调用**：抽取走确定性规则，不在陪聊主链路里再插一次 LLM。
- **敏感信息整条不抽**：身份证 / 银行卡 / 手机号 / 密码密钥 / 门牌号命中即放弃整条消息。
- 与在飞的多渠道重构**并存不重构**：只增量改 `database.py` 之外的 `bot/` 与 `routes/bot.py`，不碰 `bot/provider.py` / `bot/inbound_pipeline.py` / `bot/channel_runtime.py` / `routes/bot_channels.py`。
- 红→绿 TDD：先写失败测试跑出 RED，再实现到 GREEN；每任务只 `git add` 自身文件、约定式提交。

命令：`python -m pytest tests/<file> -q`（在 `backend/` 下）；`npx vitest run`、`npx vue-tsc --noEmit -p tsconfig.web.json`（在仓库根）。

---

### Task 1: 抽取器 — 规则表与敏感/负向判定

**Files:**
- 新增 `backend/bot/memory.py`（本任务只放纯函数：规则表、敏感拦截、负向指令、`extract_facts`）
- 新增 `backend/tests/test_bot_memory.py`

**Interfaces:**
- `extract_facts(user_text: str) -> list[Fact]`；`Fact(title, content, importance, confidence, slot)`
- `has_sensitive(text) -> bool`；`is_negated(text) -> bool`；`detect_forget(text) -> str | None`
- `scope_for(persona_id) -> str`

- [x] Step 1: 写失败测试 —— 11 类规则各一例、否定优先于肯定、敏感整条不抽、负向指令抑制、
      「别记这个」不是定向删除、闲聊不误采（连续重复字）、居住黑名单。
- [x] Step 2: 跑出 RED（`ModuleNotFoundError: bot.memory`）。
- [x] Step 3: 实现 —— 规则表按「否定先于肯定、忌口先于厌恶」排序；捕获组长度上限 + 连续重复字拒绝；
      居住词表黑名单；敏感 5 类正则；`_NEGATE` / `_FORGET` / `_DEICTIC` 三件套；
      `slot` 区分 single/multi。
- [x] Step 4: GREEN —— `pytest tests/test_bot_memory.py -q` → 8 passed。
- [x] Step 5: 提交。

### Task 2: 存取层 — 写入/去重/取代/失效/列表 + 召回

**Files:**
- 改 `backend/bot/memory.py`（追加存取层）
- 改 `backend/tests/test_bot_memory.py`（追加 10 例）

**Interfaces:**
- `remember(persona_id, conversation_id, user_text) -> int`（新写入条数）
- `forget(persona_id, keyword=None) -> int`、`forget_by_id(memory_id) -> bool`、`clear_all(persona_id) -> int`
- `add_manual(persona_id, content, conversation_id=None) -> str | None`
- `list_memories(persona_id) -> list[dict]`、`recall_sync(persona_id, query, limit=3) -> list[str]`

- [x] Step 1: 写失败测试 —— 写入落在 `persona:{id}` scope、同内容不重写、单值槽位取代（断言
      `status='superseded'` 且 `superseded_by` 指向新行）、multi 槽位并存、人格间不串台、
      定向遗忘、全清、召回返回人话（无 `[mem:`）、召回未命中退回最近几条、
      **Agent scope（`global`/`host:*`）检索不到人格记忆**。
- [x] Step 2: RED。
- [x] Step 3: 实现 —— 读侧自建只读查询（`scope=? AND status='active'`，同秒写入按 `rowid DESC` 兜底排序）；
      写侧一律走 `mstore.add_memory`（保证 `_fts_sync`）；`forget_by_id` 额外限制 `kind='persona_fact'`；
      召回两路：FTS（`mstore.fts_hits`，bigram 索引）命中则只取命中项；
      未命中走单字重合度排序 —— bigram 桥接不了「你还记得我叫什么吗」→「用户叫小林」；
      两路皆空才退回最近 3 条。
- [x] Step 4: GREEN —— 18 passed。
- [x] Step 5: 提交。

### Task 3: 生成层 — 记忆注入 + 抽取触发

**Files:**
- 改 `backend/bot/chat_service.py`
- 改 `backend/tests/test_bot_service.py`（追加 5 例）

**Interfaces:**
- `BotChatService._memory_note(persona_id, query) -> str`（空串表示无记忆）
- `BotChatService._remember_turn(persona_id, conversation_id, user_text) -> None`

- [x] Step 1: 写失败测试 —— 记忆块是**独立的 system 消息**且位置在人格 prompt 之后、历史之前；
      内容是人话不带 `[mem:`；无记忆时不追加第二条 system；记忆块不进 `bot_messages`；
      用户消息在助手消息落库后被抽取入库；**记忆层抛异常时回复照常产出**。
- [x] Step 2: RED。
- [x] Step 3: 实现 —— `messages` 构造处插入 `{"role":"system","content":...}`；
      注入文案「关于对方，你记得这些事（当事实用，别主动罗列，也别编造没记住的）」；
      `cap_text_tokens(block, 200)` 封顶；助手消息落库后 `asyncio.to_thread(remember, ...)`；
      两处均 try/except 兜底并记 warning。
- [x] Step 4: GREEN —— `pytest tests/test_bot_service.py -q` → 17 passed。
- [x] Step 5: 提交。

### Task 4: API — 四个记忆端点

**Files:**
- 改 `backend/routes/bot.py`
- 改 `backend/tests/test_bot_routes.py`（追加 5 例）

**Interfaces:**
- `GET /bot/personas/{persona_id}/memories` → `[{id,title,content,created_at}]`
- `POST /bot/personas/{persona_id}/memories`（body `{content}`）→ `{id}`
- `DELETE /bot/memories/{memory_id}` → `{deleted}`
- `POST /bot/personas/{persona_id}/memories/clear` → `{cleared}`

- [x] Step 1: 写失败测试 —— 列表按人格隔离、人格不存在 404、空内容 422、
      单条删除后再删 404、全清返回条数。
- [x] Step 2: RED（4 failed）。
- [x] Step 3: 实现 —— `MemoryRequest(content: str, min_length=1, max_length=200)`；
      路由层统一先查人格存在再操作；删除走 `memory_mod.forget_by_id`。
- [x] Step 4: GREEN —— `pytest tests/test_bot_routes.py -q` → 28 passed。
- [x] Step 5: 提交。

### Task 5: 前端 — API 方法 + 编辑器记忆区

**Files:**
- 改 `src/api/bot.ts`（`BotMemory` 类型 + 4 个方法）
- 改 `src/components/bot/PersonaEditor.vue`（第六区「它记得你什么」）
- 新增 `src/components/bot/PersonaEditor.guards.test.ts`

**Interfaces:**
- `botApi.listMemories / addMemory / deleteMemory / clearMemories`
- 编辑器内 `memories` / `memoryBusy` / `memoryError` / `memoryDraft` + `loadMemories` / `forgetMemory` / `forgetAll` / `addMemory`

- [x] Step 1: 写守卫测试 —— 编辑器含「它记得你什么」与四个 API 调用、记忆区 `v-if="persona"`、
      API 层有四个端点、聊天界面不出现记忆控件。
- [x] Step 2: RED。
- [x] Step 3: 实现 —— `onMounted(loadMemories)`；列表行「内容 + 时间 + 忘记」；
      手动添加输入框 + 「记住」；「全部忘记」；读取失败只在记忆区提示。
- [x] Step 4: GREEN —— `npx vitest run src/components/bot ...` → 39 passed；`vue-tsc` clean。
- [x] Step 5: 提交。

### Task 6: 收尾全量验证

- [x] 后端 bot 相关全量：`cd backend && python -m pytest tests/test_bot_memory.py tests/test_bot_service.py tests/test_bot_routes.py tests/test_bot_store.py tests/test_bot_character.py -q` → 全绿。
- [x] 前端全量：`npx vitest run` → 全绿；`npx vue-tsc --noEmit -p tsconfig.web.json` → clean。
- [x] 全量后端 `pytest tests/ -q`：已知 flaky（`test_clear_history_runs` 的 Windows 目录清理 OSError、
      `test_command_key_decoy_bypass` 单独跑通过）与本改动无关；bot 相关用例不得回归。
- [x] 文档：`docs/superpowers/specs/2026-10-28-bot-long-term-memory-design.md`（本计划配套 spec）。

## Self-Review

- **Spec 覆盖**：写侧规则与敏感/负向判定(§核心模型)=Task 1；存取与 scope 隔离=Task 2；
  注入形态与降级=Task 3；API=Task 4；前端=Task 5；错误处理=Task 2/3 的 try/except + 守卫测试。✅
- **无占位符**：各步含真实代码与断言。✅
- **类型一致**：`BotMemory{id,title,content,created_at}` 与后端列表返回一致；
  `remember/forget/forget_by_id/clear_all/add_manual/list_memories/recall_sync` 前后端调用点对齐。✅
- **裁定**：记忆块用 `system` 角色而非 `user` 角色（事实不是对方说的话）；
  记忆块不落库（不是一次对话）；单值槽位用 `supersede` 让位，multi 槽位并存；
  召回未命中退回最近几条（陪伴场景里「它确实记得」比相关性更重要）。
- **未做（有意）**：LLM 抽取开关、记忆 TTL、跨人格共享记忆、聊天界面记忆面板。
