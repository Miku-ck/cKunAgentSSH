# cKunBot 聊天体验优化 · Phase 1 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 cKunBot（本地陪聊 + QQ 官方机器人）落地人格级、渠道感知的回复体验：系统提示词增强、回复风格可控（简洁/适中/长文）、开场白、thinking 开关、表情/Markdown 排版。

**Architecture:** 数据层给 `bot_personas` 加 `reply_style`/`enable_thinking`、给 `bot_messages` 加 `reasoning`（幂等迁移）；生成层 `chat_service` 重写 system prompt（渠道化 + 风格指令）、`stream_reply` 产出 `(kind,text)` 并透传 thinking、本地新会话落 greeting；QQ 首回合前置 greeting；前端复用 `MarkdownStream.vue` 渲染 + 新增「回复风格」下拉与「思考」开关。

**Tech Stack:** FastAPI + Pydantic v2 + SQLite（后端 7777，`reload=False`）；Vue3 + Pinia + Vite（前端 5555）；pytest / vitest / vue-tsc。

**Spec:** `docs/superpowers/specs/2026-10-28-bot-chat-experience-phase1-design.md`（本计划据此实现，执行者两份都读）。

## Global Constraints

- 后端 `reload=False`：任何后端改动，验证前必须重启后端（dev：`npm run dev` 会拉起；或单独重启 uvicorn）。
- 运行环境 Windows + pwsh：命令在对应目录用 Windows 路径。后端测试：`cd d:\workspace\cKunAgentSSH\backend` 后 `python -m pytest ...`。前端：仓库根 `npm run test`（vitest）、`npm run typecheck`（vue-tsc）。
- CI 门禁只含 `test:backend` + `typecheck`，**不含 ruff**；不为通过 ruff 改动既有 `Optional[]`/风格（渐进式整改，非本次范围）。
- 头像已是「仅上传图片 data URL」口径（前序改动），本计划**不回退**该约束。
- **不触碰** QQ 去重 / `answered_at` / `_locks`（属另一分支「方案 1」）。本计划只做聊天体验。
- **不修改**主 Agent 的提示词增强逻辑（记忆「运维智能体输入框禁用通用提示词增强」）——只动 cKunBot 人格 system prompt。
- `supports_thinking` 是无人维护的历史值，**不做门控**：thinking 透传只看 `persona.enable_thinking`（见 spec §3/§4.2）。
- 提交：约定式提交；代码与 spec 分开提交；一个 Task 一提交。persona 字段对外用 snake_case（`reply_style`、`enable_thinking`）。

---

### Task 1: 数据层 —— 人格两列 + 消息 reasoning 列 + 幂等迁移

**Files:**
- Modify: `backend/database.py`（`bot_personas` CREATE ~201-211；`bot_messages` CREATE ~231-238；`_migrate_db` ~530-590）
- Test: `backend/tests/test_bot_routes.py`（追加迁移断言）

**Interfaces:**
- Produces: `bot_personas.reply_style TEXT DEFAULT 'moderate'`、`bot_personas.enable_thinking INTEGER DEFAULT 0`、`bot_messages.reasoning TEXT`；`_bot_persona_dict` 输出含这两键（`enable_thinking` 归一为 int）。

- [ ] **Step 1: 写失败测试**（`backend/tests/test_bot_routes.py` 末尾追加）

```python
def test_persona_chat_experience_columns_migrate_and_default(client):
    # client 为 conftest 提供的 TestClient（若命名不同，按本仓 conftest 实际 fixture 替换）
    rows = client.get("/api/bot/personas").json()
    assert rows, "至少内置 cKunBot"
    p = rows[0]
    assert p["reply_style"] == "moderate"
    assert int(p["enable_thinking"]) == 0
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_bot_routes.py::test_persona_chat_experience_columns_migrate_and_default -v`
Expected: FAIL（KeyError `reply_style` 或 SELECT * 无此列）

- [ ] **Step 3: 给 `bot_personas` CREATE 补两列**

在 `backend/database.py` `bot_personas` 定义里 `model_id TEXT,` 之后、`is_builtin` 之前插入：

```sql
                model_id     TEXT,
                reply_style  TEXT NOT NULL DEFAULT 'moderate',
                enable_thinking INTEGER NOT NULL DEFAULT 0,
                is_builtin   INTEGER NOT NULL DEFAULT 0,
```

- [ ] **Step 4: 给 `bot_messages` CREATE 补 reasoning**

在 `bot_messages` 定义里 `external_msg_id TEXT,` 之后加一行：

```sql
                reasoning     TEXT,
```

- [ ] **Step 5: 旧库幂等迁移**（`_migrate_db`，复用已有 `bot_message_cols` 变量处附近）

在 `if "answered_at" not in bot_message_cols:` 块之后追加：

```python
    if "reasoning" not in bot_message_cols:
        conn.execute("ALTER TABLE bot_messages ADD COLUMN reasoning TEXT")
    # bot_personas：旧库补 chat-experience 两列
    cursor = conn.execute("PRAGMA table_info(bot_personas)")
    persona_cols = {row[1] for row in cursor.fetchall()}
    if "reply_style" not in persona_cols:
        conn.execute(
            "ALTER TABLE bot_personas ADD COLUMN reply_style TEXT NOT NULL DEFAULT 'moderate'"
        )
    if "enable_thinking" not in persona_cols:
        conn.execute(
            "ALTER TABLE bot_personas ADD COLUMN enable_thinking INTEGER NOT NULL DEFAULT 0"
        )
```

- [ ] **Step 6: `_bot_persona_dict` 归一 enable_thinking**

改为：

```python
def _bot_persona_dict(row: sqlite3.Row) -> dict:
    result = dict(row)
    result["is_builtin"] = int(result.get("is_builtin") or 0)
    result["enable_thinking"] = int(result.get("enable_thinking") or 0)
    result.setdefault("reply_style", "moderate")
    return result
```

- [ ] **Step 7: 运行确认通过**

Run: `python -m pytest tests/test_bot_routes.py -q`
Expected: PASS

- [ ] **Step 8: 提交**

```bash
git add backend/database.py backend/tests/test_bot_routes.py
git commit -m "feat(bot): add persona reply_style/enable_thinking and message reasoning columns with idempotent migration"
```

---

### Task 2: 人格 CRUD 写入新字段

**Files:**
- Modify: `backend/database.py`（`create_bot_persona` ~2126-2147、`copy_bot_persona` ~2192-2203、`update_bot_persona` allowed 集 ~2173）
- Test: `backend/tests/test_bot_routes.py`

**Interfaces:**
- Consumes: Task 1 的列。
- Produces: `create_bot_persona(..., reply_style: str = "moderate", enable_thinking: int = 0)`；`update_bot_persona` 允许写 `reply_style`/`enable_thinking`；`copy_bot_persona` 透传两者。

- [ ] **Step 1: 写失败测试**

```python
def test_persona_roundtrip_reply_style_and_thinking(client):
    created = client.post("/api/bot/personas", json={
        "name": "小测", "personality": "p", "background": "b",
        "greeting": "g", "reply_style": "concise", "enable_thinking": 1,
    }).json()
    assert created["reply_style"] == "concise"
    assert int(created["enable_thinking"]) == 1
    patched = client.patch(f"/api/bot/personas/{created['id']}", json={"reply_style": "long"}).json()
    assert patched["reply_style"] == "long"
    assert int(patched["enable_thinking"]) == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_bot_routes.py::test_persona_roundtrip_reply_style_and_thinking -v`
Expected: FAIL（新字段被忽略，`reply_style` 仍 moderate）

- [ ] **Step 3: 扩展 `create_bot_persona`**

签名加两参、INSERT 列与占位加两值：

```python
def create_bot_persona(
    name: str,
    avatar: str = "",
    personality: str = "",
    background: str = "",
    greeting: str = "",
    model_id: Optional[str] = None,
    reply_style: str = "moderate",
    enable_thinking: int = 0,
) -> dict:
    persona_id = _new_id()
    now = _now()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO bot_personas "
            "(id, name, avatar, personality, background, greeting, model_id, "
            "reply_style, enable_thinking, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (persona_id, name, avatar, personality, background, greeting, model_id,
             reply_style, int(bool(enable_thinking)) if not isinstance(enable_thinking, int) else enable_thinking, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return get_bot_persona(persona_id)
```

> 说明：`enable_thinking` 存 0/1；入参可能是 bool/int，统一 `int(...)`。若嫌绕，直接传 `int(bool(enable_thinking))`。

- [ ] **Step 4: `copy_bot_persona` 透传**

在 `create_bot_persona(...)` 调用的 `model_id=persona["model_id"],` 之后加：

```python
        reply_style=persona.get("reply_style", "moderate"),
        enable_thinking=persona.get("enable_thinking", 0),
```

- [ ] **Step 5: `update_bot_persona` 放行**

```python
    allowed = {"name", "avatar", "personality", "background", "greeting", "model_id",
               "reply_style", "enable_thinking"}
```

- [ ] **Step 6: 运行确认通过**

Run: `python -m pytest tests/test_bot_routes.py -q`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add backend/database.py backend/tests/test_bot_routes.py
git commit -m "feat(bot): persist persona reply_style/enable_thinking through create/copy/update"
```

---

### Task 3: 路由请求模型 + 风格校验

**Files:**
- Modify: `backend/routes/bot.py`（`PersonaRequest` 29-37、`PersonaUpdateRequest` 40-48）
- Test: `backend/tests/test_bot_routes.py`

**Interfaces:**
- Consumes: Task 2 的 CRUD。
- Produces: `PersonaRequest.reply_style: Literal['concise','moderate','long'] = 'moderate'`、`enable_thinking: bool = False`；`PersonaUpdateRequest` 对应 Optional 版；非法风格 → 422。路由把值透传进 `db.create_bot_persona(**req.model_dump())`（现有）。

- [ ] **Step 1: 写失败测试**

```python
def test_persona_invalid_reply_style_rejected(client):
    r = client.post("/api/bot/personas", json={"name": "x", "reply_style": "verbose"})
    assert r.status_code == 422
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_bot_routes.py::test_persona_invalid_reply_style_rejected -v`
Expected: FAIL（未知字段被忽略，返回 200）

- [ ] **Step 3: 给两个模型加字段**

`PersonaRequest`（`model_id` 之后）：

```python
    reply_style: Literal["concise", "moderate", "long"] = "moderate"
    enable_thinking: bool = False
```

`PersonaUpdateRequest`（`model_id` 之后）：

```python
    reply_style: Optional[Literal["concise", "moderate", "long"]] = None
    enable_thinking: Optional[bool] = None
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_bot_routes.py -q`
Expected: PASS（422；`enable_thinking` bool 会被 Pydantic 转 int 存库）

> `update_persona` 现有过滤 `value is not None or key == "model_id"`——`enable_thinking=False` 属 `not None` 会保留；`reply_style` 非 None 保留。无需改 `update_persona` 主体。

- [ ] **Step 5: 提交**

```bash
git add backend/routes/bot.py backend/tests/test_bot_routes.py
git commit -m "feat(bot): validate persona reply_style/enable_thinking in API models"
```

---

### Task 4: 本地新会话落开场白

**Files:**
- Modify: `backend/database.py`（`create_bot_conversation` ~2222-2248 尾部）
- Test: `backend/tests/test_bot_routes.py`

**Interfaces:**
- Consumes: `persona.greeting`（已有）。
- Produces: `create_bot_conversation(persona_id, channel='local', ...)` 当 `channel=='local'` 且 `persona['greeting']` 非空时，额外插入一条 `role='assistant'` 消息（不走 LLM）。返回值不变（会话 dict）。

- [ ] **Step 1: 写失败测试**

```python
def test_local_conversation_created_with_greeting_message(client):
    persona = client.post("/api/bot/personas", json={
        "name": "开场君", "greeting": "初次见面，多多指教", "reply_style": "moderate",
    }).json()
    conv = client.post("/api/bot/conversations", json={"persona_id": persona["id"], "channel": "local"}).json()
    msgs = client.get(f"/api/bot/conversations/{conv['id']}/messages").json()
    assert len(msgs) == 1 and msgs[0]["role"] == "assistant" and msgs[0]["content"] == "初次见面，多多指教"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_bot_routes.py::test_local_conversation_created_with_greeting_message -v`
Expected: FAIL（0 条消息）

- [ ] **Step 3: 建会话后插入 greeting**

在 `create_bot_conversation` 的 `conn.commit()` 之后、`return get_bot_conversation(conversation_id)` 之前（先确认函数末尾结构），把返回改为落 greeting：

```python
    if channel == "local" and persona.get("greeting"):
        add_bot_message(conversation_id, "assistant", persona["greeting"])
    return get_bot_conversation(conversation_id)
```

> 若 greeting 插入需在 `conn.close()` 之外，用模块级 `add_bot_message`（自开连接）。确认 `create_bot_conversation` 主体在 try/finally 内已 `conn.close()`，greeting 插入放在 `finally` 之后（即函数体末尾、return 前），避免同连接事务交错。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_bot_routes.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/database.py backend/tests/test_bot_routes.py
git commit -m "feat(bot): seed greeting as first assistant message on local conversation create"
```

---

### Task 5: chat_service —— 渠道化 system prompt + 风格指令

**Files:**
- Modify: `backend/bot/chat_service.py`（`_system_prompt` 21-28）
- Test: `backend/tests/test_bot_chat_service.py`（不存在则新建，纯单测不依赖 HTTP）

**Interfaces:**
- Consumes: `persona` dict（含 `reply_style`）。
- Produces: `_system_prompt(self, persona: dict, channel: str = "local") -> str`。风格指令映射 + 渠道排版规则。

- [ ] **Step 1: 写失败测试**

```python
from bot.chat_service import BotChatService

def _sp(persona, channel):
    return BotChatService()._system_prompt(persona, channel)

def test_system_prompt_style_directives_and_channel():
    base = {"name": "小助手", "personality": "温和", "background": "伙伴", "reply_style": "concise"}
    concise = _sp(base, "local")
    assert "一两句话" in concise
    assert "Markdown" in concise  # local 允许排版
    longp = _sp({**base, "reply_style": "long"}, "local")
    assert "分点" in longp
    qq = _sp(base, "qq")
    assert "禁止表格" in qq and "Markdown 排版与" not in qq
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_bot_chat_service.py -v`
Expected: FAIL（旧 `_system_prompt` 无 channel 参数 → TypeError）

- [ ] **Step 3: 重写 `_system_prompt`**

```python
    _STYLE_DIRECTIVE = {
        "concise": "回复风格：一两句话、口语化、点到为止，别铺陈。",
        "moderate": "回复风格：以简洁自然为主，确有内容再展开。",
        "long": "回复风格：可分点、有条理地展开细节。",
    }

    def _system_prompt(self, persona: dict, channel: str = "local") -> str:
        sections = [f"你叫{persona['name']}，是用户的个人机器人伙伴。"]
        if persona.get("personality"):
            sections.append(f"性格与说话方式：{persona['personality']}")
        if persona.get("background"):
            sections.append(f"背景设定：{persona['background']}")
        sections.append(self._STYLE_DIRECTIVE.get(persona.get("reply_style", "moderate"),
                                                 self._STYLE_DIRECTIVE["moderate"]))
        if channel == "qq":
            sections.append("排版规则：只用纯文本与少量表情；禁止表格/链接/代码块/标题语法。")
        else:
            sections.append("排版规则：可用 Markdown 排版与适量表情。")
        sections.append("保持以上人格，进行自然、真诚的文字陪聊。不要执行命令或调用工具，不要声称自己是人类。")
        return "\n\n".join(sections)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_bot_chat_service.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/bot/chat_service.py backend/tests/test_bot_chat_service.py
git commit -m "feat(bot): channel-aware persona system prompt with reply-style directives"
```

---

### Task 6: chat_service —— stream_reply 产出 (kind,text) + thinking 透传 + reasoning 落库

**Files:**
- Modify: `backend/bot/chat_service.py`（`stream_reply` 30-79、`complete_reply` 81-89）
- Modify: `backend/database.py`（`add_bot_message` 2300-2344 加 `reasoning` 参数）
- Test: `backend/tests/test_bot_chat_service.py`

**Interfaces:**
- Consumes: `chat_stream(..., enable_thinking=..., reasoning_max_tokens=...)`、`StreamChunk.reasoning/.content`（已在 llm_adapter）。
- Produces:
  - `add_bot_message(conversation_id, role, content, external_msg_id=None, reasoning=None) -> dict`
  - `stream_reply(self, conversation_id, content, external_msg_id=None, *, channel="local") -> AsyncGenerator[tuple[str, str], None]`，产出 `("reasoning"|"content", text)`。
  - `complete_reply(self, conversation_id, content, external_msg_id=None, *, channel="qq") -> str`（仅聚合 content）。

- [ ] **Step 1: 写失败测试**

```python
import asyncio
import pytest
import bot.chat_service as cs_mod
from bot.chat_service import BotChatService


class _Chunk:
    def __init__(self, content=None, reasoning=None):
        self.content, self.reasoning = content, reasoning


@pytest.mark.asyncio
async def test_stream_reply_yields_reasoning_and_content_and_persists(monkeypatch):
    async def fake_stream(**kw):
        assert kw["enable_thinking"] is True  # persona.enable_thinking 驱动，不看 supports_thinking
        for c in [_Chunk(reasoning="想"), _Chunk(content="好")]:
            yield c
    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    persisted = {}
    def fake_add(conv, role, content, external_msg_id=None, reasoning=None):
        if role == "assistant":
            persisted["content"], persisted["reasoning"] = content, reasoning
    monkeypatch.setattr(cs_mod.db, "get_bot_conversation",
                        lambda cid: {"persona": {"name": "n", "reply_style": "moderate",
                                                 "enable_thinking": 1, "personality": "", "background": ""}})
    monkeypatch.setattr(cs_mod.db, "_model_for_persona", lambda s, p: {})
    monkeypatch.setattr(BotChatService, "_model_for_persona",
                        lambda self, p: {"base_url": "u", "api_key": "k", "model_id": "m",
                                         "max_tokens": 8192, "max_output_tokens": 4096,
                                         "reasoning_tokens": 1024, "temperature": 0.7, "top_p": 0.9})
    monkeypatch.setattr(cs_mod.db, "get_recent_bot_messages", lambda cid, n: [])
    monkeypatch.setattr(cs_mod.db, "add_bot_message", fake_add)
    out = [x async for x in BotChatService().stream_reply("c1", "在吗", channel="local")]
    assert ("reasoning", "想") in out and ("content", "好") in out
    assert persisted == {"content": "好", "reasoning": "想"}


@pytest.mark.asyncio
async def test_complete_reply_only_content(monkeypatch):
    async def fake_stream(**kw):
        yield _Chunk(reasoning="忽略我")
        yield _Chunk(content="正文")
    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    svc = BotChatService()
    monkeypatch.setattr(svc, "_model_for_persona",
                        lambda self=None, p=None: {"base_url": "u", "api_key": "k", "model_id": "m",
                                                    "max_tokens": 8192, "max_output_tokens": 4096,
                                                    "reasoning_tokens": 1024, "temperature": 0.7, "top_p": 0.9})
    monkeypatch.setattr(cs_mod.db, "get_bot_conversation",
                        lambda cid: {"persona": {"name": "n", "reply_style": "moderate",
                                                 "enable_thinking": 1, "personality": "", "background": ""}})
    monkeypatch.setattr(cs_mod.db, "get_recent_bot_messages", lambda cid, n: [])
    monkeypatch.setattr(cs_mod.db, "add_bot_message", lambda *a, **k: {})
    text = await svc.complete_reply("c1", "在吗", channel="qq")
    assert text == "正文"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_bot_chat_service.py -v`
Expected: FAIL（`stream_reply` 无 channel/未产出 tuple）

- [ ] **Step 3: `add_bot_message` 加 reasoning**（`backend/database.py`）

签名 `external_msg_id: Optional[str] = None,` 后加 `reasoning: Optional[str] = None`；INSERT 列与占位补 `reasoning`：

```python
        conn.execute(
            "INSERT OR IGNORE INTO bot_messages "
            "(id, conversation_id, role, content, external_msg_id, reasoning, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message_id, conversation_id, role, content, external_msg_id, reasoning, now),
        )
```

- [ ] **Step 4: 重写 `stream_reply`**（`backend/bot/chat_service.py`）

```python
    async def stream_reply(
        self,
        conversation_id: str,
        content: str,
        external_msg_id: str | None = None,
        *,
        channel: str = "local",
    ) -> AsyncGenerator[tuple[str, str], None]:
        conversation = await asyncio.to_thread(db.get_bot_conversation, conversation_id)
        if not conversation:
            raise ValueError("会话不存在")
        persona = conversation["persona"]
        model = await asyncio.to_thread(self._model_for_persona, persona)
        history = await asyncio.to_thread(db.get_recent_bot_messages, conversation_id, 39)
        messages = [{"role": "system", "content": self._system_prompt(persona, channel)}]
        messages.extend({"role": item["role"], "content": item["content"]} for item in history)
        messages.append({"role": "user", "content": content})
        await asyncio.to_thread(
            db.add_bot_message, conversation_id, "user", content, external_msg_id
        )

        from llm_adapter import chat_stream

        enable_thinking = bool(persona.get("enable_thinking"))
        reasoning_budget = model.get("reasoning_tokens") if enable_thinking else None
        # 简洁风格把输出上限压到 512；其余沿用模型 max_output_tokens（长度主要由提示词控制）。
        style_cap = 512 if persona.get("reply_style") == "concise" else model.get("max_output_tokens")
        max_tokens = safe_max_tokens(
            model.get("max_tokens"),
            "\n".join(str(item["content"]) for item in messages),
            max_output_tokens=style_cap,
            reasoning_tokens=reasoning_budget,
        )
        if max_tokens is None:
            max_tokens = style_cap

        parts: list[str] = []
        reasoning_parts: list[str] = []
        async for chunk in chat_stream(
            base_url=model["base_url"],
            api_key=model.get("api_key") or "EMPTY",
            model=model.get("model_id") or model["id"],
            messages=messages,
            temperature=model.get("temperature", 0.7),
            max_tokens=max_tokens,
            top_p=model.get("top_p", 0.9),
            enable_thinking=enable_thinking,
            tools=None,
            reasoning_max_tokens=reasoning_budget,
        ):
            if chunk.reasoning and channel == "local":
                reasoning_parts.append(chunk.reasoning)
                yield "reasoning", chunk.reasoning
            if chunk.content:
                parts.append(chunk.content)
                yield "content", chunk.content
        reply = "".join(parts).strip()
        if not reply:
            raise RuntimeError("模型未返回可显示的内容")
        await asyncio.to_thread(
            db.add_bot_message, conversation_id, "assistant", reply,
            None, "".join(reasoning_parts) or None,
        )

    async def complete_reply(
        self,
        conversation_id: str,
        content: str,
        external_msg_id: str | None = None,
        *,
        channel: str = "qq",
    ) -> str:
        return "".join([
            text for kind, text in self.stream_reply(
                conversation_id, content, external_msg_id, channel=channel
            ) if kind == "content"
        ])
```

> `async for` 消费 tuple 生成器：`complete_reply` 用列表推导 `for kind, text in self.stream_reply(...)` 需是异步；改为 `async for`：见 Step 5。

- [ ] **Step 5: `complete_reply` 用 async 聚合**

```python
    async def complete_reply(
        self, conversation_id: str, content: str,
        external_msg_id: str | None = None, *, channel: str = "qq",
    ) -> str:
        out: list[str] = []
        async for kind, text in self.stream_reply(
            conversation_id, content, external_msg_id, channel=channel
        ):
            if kind == "content":
                out.append(text)
        return "".join(out).strip()
```

- [ ] **Step 6: 运行确认通过**

Run: `python -m pytest tests/test_bot_chat_service.py -v`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add backend/bot/chat_service.py backend/database.py backend/tests/test_bot_chat_service.py
git commit -m "feat(bot): stream_reply yields (kind,text), passthrough thinking, persist reasoning; complete_reply content-only"
```

---

### Task 7: 本地 SSE —— 分发 reasoning + 传 channel

**Files:**
- Modify: `backend/routes/bot.py`（`/bot/chat` events() 132-145）
- Test: `backend/tests/test_bot_routes.py`

**Interfaces:**
- Consumes: `stream_reply(..., channel="local")` 产出 tuple。
- Produces: SSE 事件 `{type:'content'|'reasoning'|'done'|'error'}`；`content`/`done`/`error` 形状不变（向后兼容），新增 `{type:'reasoning','content':str}`。

- [ ] **Step 1: 写失败测试**

```python
def test_bot_chat_sse_emits_reasoning_event(client, monkeypatch):
    from bot.chat_service import BotChatService
    async def fake_stream(self, cid, content, external_msg_id=None, *, channel="local"):
        yield "reasoning", "想"
        yield "content", "好"
        import database as db
        yield_done = True
    monkeypatch.setattr(BotChatService, "stream_reply", fake_stream)
    monkeypatch.setattr("routes.bot.db.get_bot_messages", lambda cid, n: [{"id": "m", "role": "assistant", "content": "好", "created_at": ""}])
    conv = client.post("/api/bot/conversations", json={"persona_id": client.get("/api/bot/personas").json()[0]["id"], "channel": "local"}).json()
    with client.stream("POST", "/api/bot/chat", json={"conversation_id": conv["id"], "content": "在吗"}) as r:
        body = "".join(r.iter_text())
    assert '"type": "reasoning"' in body and '"type": "content"' in body and '"type": "done"' in body
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_bot_routes.py::test_bot_chat_sse_emits_reasoning_event -v`
Expected: FAIL（events() 仍按 str 迭代，无 reasoning 事件）

- [ ] **Step 3: 改 events() 消费 tuple**

```python
    async def events():
        try:
            async for kind, text in chat_service.stream_reply(
                req.conversation_id, req.content, channel="local"
            ):
                etype = "content" if kind == "content" else "reasoning"
                yield f"data: {json.dumps({'type': etype, 'content': text}, ensure_ascii=False)}\n\n"
            message = db.get_bot_messages(req.conversation_id, 1)[-1]
            yield f"data: {json.dumps({'type': 'done', 'message': message}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            import netconfig
            logger.warning("cKunBot 回复失败: %s", exc, exc_info=True)
            detail = netconfig.redact_urls_in_text(str(exc)) or "回复生成失败"
            yield f"data: {json.dumps({'type': 'error', 'detail': detail}, ensure_ascii=False)}\n\n"
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_bot_routes.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/routes/bot.py backend/tests/test_bot_routes.py
git commit -m "feat(bot): local SSE emits reasoning events, stream_reply called with channel=local"
```

---

### Task 8: QQ —— channel=qq + 首回合前置 greeting

**Files:**
- Modify: `backend/bot/qq_channel.py`（`_handle` 22-49）
- Test: `backend/tests/test_bot_qq_channel.py`（不存在则新建）

**Interfaces:**
- Consumes: `complete_reply(..., channel="qq")`、`conversation['persona']['greeting']`、`db.get_recent_bot_messages`。
- Produces: QQ 首回合（该会话历史为空）回复文本前置 `f"{greeting}\n\n{answer}"`；后续回合原样。

- [ ] **Step 1: 写失败测试**

```python
import asyncio
import pytest
from bot.qq_channel import QQEventProcessor


class _Msg:
    def __init__(self):
        self.content = "你好"
        self.event_id = "e1"
        self.id = "e1"
        self.group_openid = "g1"
        self.sent_replies = []
    async def reply(self, content, msg_type=0):
        self.sent_replies.append(content)


@pytest.mark.asyncio
async def test_first_turn_prepends_greeting(monkeypatch):
    import database as db
    conv = {"id": "c1", "persona": {"greeting": "嗨"}}
    state = {"history": []}
    monkeypatch.setattr(db, "get_or_create_bot_conversation", lambda *a, **k: conv)
    monkeypatch.setattr(db, "get_bot_message_by_external_id", lambda *a, **k: None)
    monkeypatch.setattr(db, "get_recent_bot_messages", lambda cid, n: list(state["history"]))
    svc = _Svc()
    proc = QQEventProcessor(svc, "p1", "ctx")
    m = _Msg()
    await proc._handle(m, "group:g1:ctx")
    assert m.sent_replies[0].startswith("嗨")

    state["history"] = [{"role": "user", "content": "x"}]  # 已有往来
    m2 = _Msg(); m2.event_id = m2.id = "e2"
    await proc._handle(m2, "group:g1:ctx")
    assert not m2.sent_replies[0].startswith("嗨")


class _Svc:
    async def complete_reply(self, cid, content, external_msg_id=None, *, channel="qq"):
        return "正文"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_bot_qq_channel.py -v`
Expected: FAIL（未前置 greeting）

- [ ] **Step 3: 改 `_handle`**

在 `lock` 内、`complete_reply` 调用前后调整：

```python
            first_turn = not await asyncio.to_thread(db.get_recent_bot_messages, conversation["id"], 1)
            reply = await self.chat_service.complete_reply(
                conversation["id"], content, external_msg_id=event_id, channel="qq"
            )
            greeting = (conversation.get("persona") or {}).get("greeting") or ""
            if first_turn and greeting:
                reply = f"{greeting}\n\n{reply}"
            await message.reply(content=reply, msg_type=0)
```

> `conversation["persona"]`：`get_or_create_bot_conversation` 返回的 dict 经 `_bot_conversation_dict` 已含 `persona`。若该函数返回不含 persona，则改用 `db.get_bot_conversation(conversation["id"])["persona"]`（执行时核对返回结构）。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_bot_qq_channel.py -v`
Expected: PASS

- [ ] **Step 5: 后端全量回归**

Run: `python -m pytest tests/test_bot_routes.py tests/test_bot_chat_service.py tests/test_bot_qq_channel.py -q`
Expected: 全 PASS

- [ ] **Step 6: 提交**

```bash
git add backend/bot/qq_channel.py backend/tests/test_bot_qq_channel.py
git commit -m "feat(bot): QQ channel=qq and prepend greeting on first turn"
```

---

### Task 9: 前端 API —— 类型/payload/reasoning 流

**Files:**
- Modify: `src/api/bot.ts`（`BotPersona` 3-14、`BotPersonaPayload` 16-23、`BotMessage` 36-43、`BotSseEvent` 75-80、`streamBotChat` 82-118）
- Test: `src/api/bot.test.ts`（若存在）或并入 `src/stores/bot.test.ts`

**Interfaces:**
- Consumes: 后端 snake_case 字段与 `reasoning` SSE 事件。
- Produces:
  - `BotPersona.reply_style: 'concise'|'moderate'|'long'`、`BotPersona.enable_thinking: number`
  - `BotPersonaPayload.reply_style`、`.enable_thinking`
  - `BotMessage.reasoning?: string | null`
  - `streamBotChat(id, content, {onContent, onReasoning?, signal?})`

- [ ] **Step 1: 写失败测试**（`src/stores/bot.test.ts` 顶部 mock 处补 reasoning 分发用例，或新增 `src/api/bot.test.ts`）

```typescript
// 断言 streamBotChat 会把 type:'reasoning' 转交 onReasoning
it('onReasoning receives reasoning chunks', async () => {
  const sse = ['data: {"type":"reasoning","content":"想"}\n\n',
               'data: {"type":"content","content":"好"}\n\n',
               'data: {"type":"done","message":{"id":"m","role":"assistant","content":"好","created_at":""}}\n\n']
  globalThis.fetch = vi.fn().mockResolvedValue({
    ok: true,
    body: { getReader: () => sseReader(sse) },
  })
  const reasoning: string[] = []
  await streamBotChat('c1', 'hi', { onContent: () => {}, onReasoning: t => reasoning.push(t) })
  expect(reasoning).toEqual(['想'])
})
```

> `sseReader`/`ensureApiToken` mock 复用本仓既有 bot.test 的桩；若无，按 vitest 现有 `streamBotChat` 测试写法补 `getReader` 逐块返回。

- [ ] **Step 2: 运行确认失败**

Run（仓库根）: `npx vitest run src/api/bot.test.ts`
Expected: FAIL（无 onReasoning / 类型缺字段）

- [ ] **Step 3: 补类型与事件**

`BotPersona` 加：

```typescript
  reply_style: 'concise' | 'moderate' | 'long'
  enable_thinking: number
```

`BotPersonaPayload` 加：

```typescript
  reply_style: 'concise' | 'moderate' | 'long'
  enable_thinking: boolean
```

`BotMessage` 加：

```typescript
  reasoning?: string | null
```

`BotSseEvent.type` 改为 `'content' | 'reasoning' | 'done' | 'error'`。

`streamBotChat` options 加 `onReasoning?: (text: string) => void`，`consume` 内：

```typescript
    if (event.type === 'content') options.onContent(event.content || '')
    else if (event.type === 'reasoning') options.onReasoning?.(event.content || '')
    else if (event.type === 'error') failure = event.detail || '回复生成失败'
    else if (event.type === 'done' && event.message) result = event.message
```

- [ ] **Step 4: 运行确认通过**

Run: `npx vitest run src/api/bot.test.ts`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/api/bot.ts src/api/bot.test.ts
git commit -m "feat(bot): api types + streamBotChat onReasoning for chat-experience"
```

---

### Task 10: 前端 store —— streamReasoning

**Files:**
- Modify: `src/stores/bot.ts`（`streamText` 声明 12、`selectConversation` 清理 27、`sendMessage` 82-98）
- Test: `src/stores/bot.test.ts`

**Interfaces:**
- Consumes: `streamBotChat(..., {onContent, onReasoning})`。
- Produces: `streamReasoning = ref('')`；`sendMessage` 期间累积 reasoning；切会话/重置换位清空；`done` 后随 `saved.reasoning` 落到消息。

- [ ] **Step 1: 写失败测试**

```typescript
it('captures reasoning stream into message.reasoning', async () => {
  const saved: BotMessage = { id: 'm2', conversation_id: 'c1', role: 'assistant', content: '好', reasoning: '想', created_at: '' }
  vi.mocked(streamBotChat).mockImplementation(async (_id, _t, options) => {
    options.onReasoning?.('想'); options.onContent('好'); return saved
  })
  const store = useBotStore()
  store.personas = [persona]; store.conversations = [conversation]
  store.activeConversationId = conversation.id
  await store.sendMessage('在吗')
  expect(store.messages.at(-1)?.reasoning).toBe('想')
})
```

- [ ] **Step 2: 运行确认失败**

Run: `npx vitest run src/stores/bot.test.ts`
Expected: FAIL（无 streamReasoning，或 saved 未含 reasoning）

- [ ] **Step 3: store 增 streamReasoning + 接线**

`src/stores/bot.ts`：

```typescript
  const streamReasoning = ref('')
```

`selectConversation` 内 `streamText.value = ''` 之后加 `streamReasoning.value = ''`。

`sendMessage` 起流前 `streamText.value = ''` 之后加 `streamReasoning.value = ''`；调用改为：

```typescript
      const saved = await streamBotChat(conversationId, text, {
        signal: abortController.signal,
        onContent: chunk => { if (activeConversationId.value === conversationId) streamText.value += chunk },
        onReasoning: chunk => { if (activeConversationId.value === conversationId) streamReasoning.value += chunk },
      })
```

`return { ..., streamText, streamReasoning, ... }`（导出加 `streamReasoning`）。

- [ ] **Step 4: 运行确认通过**

Run: `npx vitest run src/stores/bot.test.ts`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/stores/bot.ts src/stores/bot.test.ts
git commit -m "feat(bot): store tracks streamReasoning during local chat"
```

---

### Task 11: 前端视图 —— 弹窗两控件 + MarkdownStream 气泡 + 折叠思考

**Files:**
- Modify: `src/components/bot/CkunBotView.vue`（消息渲染 38-48、人格表单 143 附近、`personaForm` 219、`openPersona` 220-225、`savePersona` 238+）
- Test: `src/views/ssh/CkunBotView.guards.test.ts`（追加守卫）

**Interfaces:**
- Consumes: `store.streamReasoning`、`MarkdownStream`（`@/components/chat/MarkdownStream.vue`）、`BotPersona.reply_style/enable_thinking`。
- Produces: 表单含「回复风格」下拉与「思考」开关；气泡走 Markdown；思考过程可折叠。

- [ ] **Step 1: 写守卫测试**

```typescript
it('人格表单含回复风格与思考开关，气泡走 MarkdownStream', () => {
  const src = readFileSync(viewPath, 'utf8')
  expect(src).toContain('personaForm.reply_style')
  expect(src).toContain('personaForm.enable_thinking')
  expect(src).toContain('回复风格')
  expect(src).toContain('MarkdownStream')
})
```

- [ ] **Step 2: 运行确认失败**

Run: `npx vitest run src/views/ssh/CkunBotView.guards.test.ts`
Expected: FAIL

- [ ] **Step 3: `personaForm` + `openPersona` 加两字段**

```typescript
const personaForm = reactive({ name: '', avatar: '', personality: '', background: '', greeting: '', model_id: null as string | null, reply_style: 'moderate' as 'concise' | 'moderate' | 'long', enable_thinking: false })
```

`openPersona` 的 `Object.assign` 两处对象字面量各补：

```typescript
  reply_style: p.reply_style || 'moderate', enable_thinking: !!p.enable_thinking
```
（新增分支默认：`reply_style: 'moderate', enable_thinking: false`）

- [ ] **Step 4: 表单控件**（在「聊天模型」`<label>` 之后插入）

```html
        <label class="field"><span>回复风格</span>
          <select v-model="personaForm.reply_style">
            <option value="concise">简洁（一两句话）</option>
            <option value="moderate">适中</option>
            <option value="long">长文（可展开）</option>
          </select>
        </label>
        <label class="field field-inline"><span>思考</span>
          <input type="checkbox" v-model="personaForm.enable_thinking" />
          <em class="field-hint">需模型支持；模型不回思考则不显示思考块</em>
        </label>
```

`savePersona` 里 payload 构造带上 `reply_style: personaForm.reply_style, enable_thinking: personaForm.enable_thinking`（对齐既有 createPersona/updatePersona 调用点）。

- [ ] **Step 5: 气泡用 MarkdownStream + 折叠思考**

正式消息（38-41）内容单元替换 `<div class="message-content">…{{ m.content }}</div>` 为：

```html
              <div class="message-content">
                <span class="sr-only">{{ m.role === 'user' ? '你' : (store.activePersona?.name || 'Bot') }}：</span>
                <details v-if="m.role !== 'user' && m.reasoning" class="reasoning"><summary>思考过程</summary><div class="reasoning-body">{{ m.reasoning }}</div></details>
                <MarkdownStream v-if="m.role !== 'user'" :content="m.content" />
                <template v-else>{{ m.content }}</template>
              </div>
```

流式气泡（44-47）：

```html
              <div class="message-content">
                <details v-if="store.streamReasoning" class="reasoning" open><summary>思考中…</summary><div class="reasoning-body">{{ store.streamReasoning }}</div></details>
                <MarkdownStream v-if="store.streamText" :content="store.streamText" />
                <span v-else class="typing-dots" role="img" aria-label="正在输入"><i></i><i></i><i></i></span>
              </div>
```

`<script setup>` 顶部加：

```typescript
import { defineAsyncComponent } from 'vue'
const MarkdownStream = defineAsyncComponent(() => import('@/components/chat/MarkdownStream.vue'))
```

（异步引入对齐记忆：Markdown 栈不进首屏包。）新增 `.reasoning`/`.field-inline`/`.field-hint` 少量样式，沿用现有令牌。

- [ ] **Step 6: 运行守卫 + typecheck**

Run: `npx vitest run src/views/ssh/CkunBotView.guards.test.ts` → PASS
Run（仓库根）: `npm run typecheck` → 无错

- [ ] **Step 7: 前端全量测试**

Run: `npm run test`
Expected: PASS

- [ ] **Step 8: 提交**

```bash
git add src/components/bot/CkunBotView.vue src/views/ssh/CkunBotView.guards.test.ts
git commit -m "feat(bot): persona dialog style/thinking controls, Markdown bubbles, collapsible reasoning"
```

---

## 收尾验证（全部 Task 后）

- [ ] 后端全量：`cd backend` → `python -m pytest tests/test_bot_routes.py tests/test_bot_chat_service.py tests/test_bot_qq_channel.py -q` 全绿。
- [ ] 前端：`npm run test` + `npm run typecheck` 全绿。
- [ ] **重启后端**（`reload=False`），手动冒烟：新建本地会话见开场白；设 concise 回复变短、长文可展开；选思考模型开启后本地显示可折叠思考；QQ 首条带开场白、纯文本、长度受控。

## Self-Review 结论

- Spec 覆盖：风格/提示词(T5)、thinking(T6/T9/T10/T11)、开场白(T4/T8)、reasoning 落库(T3列/T6/T9类型)、SSE(T7)、迁移(T1)、CRUD/API(T2/T3)、前端控件+Markdown(T9-11) —— 全覆盖。
- 类型一致：`stream_reply`/`complete_reply` 的 `channel` kw-only、`(kind,text)` 元组、`add_bot_message(...,reasoning)`、`BotPersona.reply_style|enable_thinking`、`streamReasoning` 在各 Task 间命名一致。
- 占位：无 TBD；测试桩（`sseReader`、conftest `client` fixture、`get_or_create` 返回结构）标注了执行时按本仓实际核对的点。
