# cKunBot 头像体系与图库（Phase 4）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 头像去马赛克（淡色首字占位）+ 服务端头像图库 + 设置新增「Bot 管理」分类（图库管理 / 你的头像）+ QQ 会话本地自定义显示名/头像。

**Architecture:** 后端新增 `avatar_gallery` 表与 CRUD 路由、`bot_user_avatar` 设置键、`bot_conversations.display_name/display_avatar` 两列与 PATCH 路由（仅 qq）；前端新增 `avatarPlaceholderStyle` 纯函数取代 identicon、`AvatarPicker` 统一选择器、设置面板 `BotPanel`，QQ 记录页与本地用户气泡接头像。

**Tech Stack:** FastAPI + Pydantic v2 + SQLite；Vue3 + Pinia + TS；pytest(asyncio STRICT) / vitest / vue-tsc。

**Spec:** `docs/superpowers/specs/2026-10-28-bot-avatar-gallery-phase4-design.md`（§7 六阶段 → 本计划 13 任务）

## Global Constraints

- 不引入任何新第三方依赖。
- QQ 通道纯文本/只读不变量不破：本计划只加**展示层覆盖列**，不改消息写入、不碰「先落库再读历史」去重不变量（`chat_service.stream_reply` 顺序不动）。
- 头像口径全线统一：前端 `readAvatarFile` 方形 256px webp（`src/utils/persona-avatar.ts`，data URL ≤350_000）；后端校验 `_AVATAR_DATA_URL = ^data:image/(png|jpe?g|webp|gif);base64,`（IGNORECASE）+ 长度 `Field(max_length=400_000)`。**不是** Phase 2 聊天图的 1.5M 口径。
- 迁移幂等：新表 `CREATE TABLE IF NOT EXISTS`；新列 `PRAGMA table_info` 探测后 `ALTER`（对齐 database.py 现有 bot 迁移风格）。
- 显示覆盖语义：`display_name`/`display_avatar` 空串 = 回落默认（title / peerLabel / 淡色占位）；PATCH 成功**不更新 `updated_at`**（展示层编辑不该把会话顶到列表前）——本计划裁定。
- 图库「重命名」不做：spec §4.2 路由清单只有 GET/POST/DELETE，无改名通道；label 仅在上传时一次性填写——本计划裁定（YAGNI）。
- 头像存**自己的 data URL 副本**而非图库 id 引用：删图库项不影响已设头像（spec §5）。
- UI 令牌化：新增样式只用现有 `--spacing-*` / `--radius-*` / `--text-*` / `--bg-elevated` 等令牌；淡色占位色由纯函数算 HSL，不硬编码色值。
- 测试命令（pwsh）：后端 `cd d:\workspace\cKunAgentSSH\backend && python -m pytest tests/<file> -q 2>&1 | Select-Object -Last 5`；前端 `npx vitest run <files> 2>&1 | Select-Object -Last 15`；全量门禁 `python -m pytest -q` / `npm run test` / `npm run typecheck`。
- 每任务只 `git add` 本任务文件；约定式提交（中文描述）；红→绿两跑必须先看到 FAIL。

---

## 阶段一 · 后端（spec §7.1）

### Task 1: avatar_gallery 表 + database.py CRUD

**Files:**
- Modify: `backend/database.py`（schema 块约 L270 `ix_bot_messages_external` 索引之后；函数区约 L2758 `delete_bot_conversation` 之后）
- Create: `backend/tests/test_bot_gallery.py`

**Interfaces:**
- Produces: `db.create_bot_gallery_item(data_url: str, label: str = "") -> dict`（含 `id/label/data_url/created_at`）、`db.list_bot_gallery(limit: int = 200) -> list[dict]`、`db.delete_bot_gallery_item(gallery_id: str) -> bool`。Task 2 的路由消费这三个函数。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_bot_gallery.py`：

```python
"""Phase 4 头像图库：库函数与路由共用本文件的 fixture（对齐 test_bot_routes.py 风格）。"""
import database as db


def test_gallery_create_list_delete(base_db):
    item = db.create_bot_gallery_item("data:image/webp;base64," + "A" * 100, label="小猫")
    assert item["id"] and item["label"] == "小猫"
    assert item["data_url"].startswith("data:image/webp;base64,")
    assert item["created_at"]

    listed = db.list_bot_gallery()
    assert [i["id"] for i in listed] == [item["id"]]

    assert db.delete_bot_gallery_item(item["id"]) is True
    assert db.delete_bot_gallery_item(item["id"]) is False
    assert db.list_bot_gallery() == []


def test_gallery_label_defaults_empty(base_db):
    item = db.create_bot_gallery_item("data:image/png;base64,BBBB")
    assert item["label"] == ""


def test_gallery_table_migration_is_idempotent(base_db):
    # 二次 init 不得因表已存在而报错（幂等前提）
    import os
    db._close_all_connections()
    db.init_db(base_db)
    assert db.list_bot_gallery() == []
```

- [ ] **Step 2: 跑到失败**

Run: `cd d:\workspace\cKunAgentSSH\backend && python -m pytest tests/test_bot_gallery.py -q 2>&1 | Select-Object -Last 5`
Expected: FAIL，`AttributeError: module 'database' has no attribute 'create_bot_gallery_item'`

- [ ] **Step 3: 最小实现**

`backend/database.py` schema executescript 块内、`ix_bot_messages_external` 索引语句之后追加：

```sql
            -- Phase 4 头像图库：集中存上传并压缩后的头像 data URL，供各处头像复用。
            -- 条目存自己的副本（非引用），删图库项不影响已设置的头像。
            CREATE TABLE IF NOT EXISTS avatar_gallery (
                id         TEXT PRIMARY KEY,
                label      TEXT NOT NULL DEFAULT '',
                data_url   TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
```

函数区 `delete_bot_conversation` 之后追加（`_new_id()`/`_now()` 为文件内既有工具）：

```python
def create_bot_gallery_item(data_url: str, label: str = "") -> dict:
    gallery_id = _new_id()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO avatar_gallery (id, label, data_url, created_at) VALUES (?, ?, ?, ?)",
            (gallery_id, label, data_url, _now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM avatar_gallery WHERE id = ?", (gallery_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def list_bot_gallery(limit: int = 200) -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM avatar_gallery ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def delete_bot_gallery_item(gallery_id: str) -> bool:
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM avatar_gallery WHERE id = ?", (gallery_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
```

- [ ] **Step 4: 跑到通过**

Run: 同 Step 2。Expected: 3 passed

- [ ] **Step 5: 提交**

```powershell
cd d:\workspace\cKunAgentSSH; git add backend/database.py backend/tests/test_bot_gallery.py; git commit -m "feat(bot): avatar_gallery 头像图库表与库函数 CRUD"
```

---

### Task 2: 图库路由 GET/POST/DELETE /bot/gallery

**Files:**
- Modify: `backend/routes/bot.py`（文件末尾追加）
- Modify: `backend/tests/test_bot_gallery.py`（追加 client fixture 与路由测试）

**Interfaces:**
- Consumes: Task 1 的三个 db 函数；文件内既有 `_AVATAR_DATA_URL`（L23）。
- Produces: `GET /bot/gallery`、`POST /bot/gallery`（body `{label?, data_url}`）、`DELETE /bot/gallery/{item_id}`。Task 7 的前端 `botApi` 按此形状对接。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_bot_gallery.py` 顶部补 import 与 fixture，文件末尾追加：

```python
import os

import fastapi
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_workspace):
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "bot-gallery.db"))
    from routes.bot import router

    app = fastapi.FastAPI()
    app.include_router(router, prefix="/api")
    with TestClient(app) as test_client:
        yield test_client
    db._close_all_connections()
```

```python
def test_gallery_route_crud(client):
    url = "data:image/webp;base64," + "A" * 100
    created = client.post("/api/bot/gallery", json={"label": "小猫", "data_url": url})
    assert created.status_code == 200, created.text
    item_id = created.json()["id"]

    assert client.get("/api/bot/gallery").json()[0]["id"] == item_id
    assert client.delete(f"/api/bot/gallery/{item_id}").json() == {"deleted": True}
    assert client.get("/api/bot/gallery").json() == []
    assert client.delete(f"/api/bot/gallery/{item_id}").status_code == 404


def test_gallery_route_rejects_non_image_and_oversize(client):
    # 与头像同口径：非图片 data URL / 表情 / 超 400_000 一律 422
    for bad in ["data:text/plain;base64,AAAA", "🐟", "data:image/webp;base64," + "A" * 400_001]:
        assert client.post("/api/bot/gallery", json={"data_url": bad}).status_code == 422
    # data_url 必填
    assert client.post("/api/bot/gallery", json={"label": "无图"}).status_code == 422
```

- [ ] **Step 2: 跑到失败**

Run: `cd d:\workspace\cKunAgentSSH\backend && python -m pytest tests/test_bot_gallery.py -q 2>&1 | Select-Object -Last 5`
Expected: 路由两测试 404≠200 FAIL（client fixture 需要文件已 import 的 `os/fastapi/pytest/TestClient`）

- [ ] **Step 3: 最小实现**

`backend/routes/bot.py` 末尾追加：

```python
class GalleryItemRequest(BaseModel):
    label: str = Field("", max_length=50)
    data_url: str = Field(..., max_length=400_000)

    @field_validator("data_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        # 头像口径（256px webp data URL），不是 Phase 2 聊天图的 1.5M 通道
        if not _AVATAR_DATA_URL.match(value):
            raise ValueError("头像图库仅支持上传的图片")
        return value


@router.get("/bot/gallery")
def list_gallery():
    return db.list_bot_gallery()


@router.post("/bot/gallery")
def add_gallery_item(req: GalleryItemRequest):
    return db.create_bot_gallery_item(req.data_url, label=req.label)


@router.delete("/bot/gallery/{item_id}")
def remove_gallery_item(item_id: str):
    if not db.delete_bot_gallery_item(item_id):
        raise HTTPException(status_code=404, detail="图库条目不存在")
    return {"deleted": True}
```

- [ ] **Step 4: 跑到通过**

Run: 同 Step 2。Expected: 5 passed

- [ ] **Step 5: 提交**

```powershell
cd d:\workspace\cKunAgentSSH; git add backend/routes/bot.py backend/tests/test_bot_gallery.py; git commit -m "feat(bot): 头像图库管理路由 GET/POST/DELETE /bot/gallery"
```

---

### Task 3: 「你的头像」端点（app_settings 键 bot_user_avatar）

**Files:**
- Modify: `backend/routes/bot.py`（末尾追加）
- Modify: `backend/tests/test_bot_gallery.py`（追加）

**Interfaces:**
- Consumes: 既有 `db.get_app_setting(key, default)` / `db.set_app_setting(key, value)`（database.py L3247/L3265）；`_validate_avatar`（routes/bot.py L26，空串合法 = 清除）。
- Produces: `GET /bot/user-avatar -> {"avatar": str}`、`PUT /bot/user-avatar`（body `{avatar}`，空串清除）。

- [ ] **Step 1: 写失败测试**

```python
def test_user_avatar_roundtrip_and_clear(client):
    assert client.get("/api/bot/user-avatar").json() == {"avatar": ""}
    url = "data:image/webp;base64," + "C" * 100
    assert client.put("/api/bot/user-avatar", json={"avatar": url}).json()["avatar"] == url
    assert client.get("/api/bot/user-avatar").json()["avatar"] == url
    assert client.put("/api/bot/user-avatar", json={"avatar": ""}).json()["avatar"] == ""


def test_user_avatar_rejects_emoji_and_oversize(client):
    assert client.put("/api/bot/user-avatar", json={"avatar": "🐟"}).status_code == 422
    assert client.put(
        "/api/bot/user-avatar", json={"avatar": "data:image/webp;base64," + "D" * 400_001}
    ).status_code == 422
```

- [ ] **Step 2: 跑到失败**

Run: `cd d:\workspace\cKunAgentSSH\backend && python -m pytest tests/test_bot_gallery.py -q 2>&1 | Select-Object -Last 5`
Expected: 两新测试 404/405 FAIL

- [ ] **Step 3: 最小实现**

`backend/routes/bot.py` 末尾追加：

```python
class BotUserAvatarRequest(BaseModel):
    # 空串 = 清除（回落淡色占位）；与 persona.avatar 同闸门，走既有 _validate_avatar
    avatar: str = Field("", max_length=400_000)

    _check_avatar = field_validator("avatar")(_validate_avatar)


_USER_AVATAR_KEY = "bot_user_avatar"


@router.get("/bot/user-avatar")
def get_user_avatar():
    return {"avatar": db.get_app_setting(_USER_AVATAR_KEY, "") or ""}


@router.put("/bot/user-avatar")
def put_user_avatar(req: BotUserAvatarRequest):
    db.set_app_setting(_USER_AVATAR_KEY, req.avatar)
    return {"avatar": req.avatar}
```

- [ ] **Step 4: 跑到通过**

Run: 同 Step 2。Expected: 7 passed

- [ ] **Step 5: 提交**

```powershell
cd d:\workspace\cKunAgentSSH; git add backend/routes/bot.py backend/tests/test_bot_gallery.py; git commit -m "feat(bot): 「你的头像」读写端点（app_settings bot_user_avatar）"
```

---

### Task 4: QQ 会话显示覆盖两列 + PATCH /bot/conversations/{id}

**Files:**
- Modify: `backend/database.py`（迁移函数区 `_migrate_bot_channels(conn)` 调用后，约 L593；函数区 Task 1 新增函数之前均可，建议紧跟 `delete_bot_conversation`）
- Modify: `backend/routes/bot.py`
- Modify: `backend/tests/test_bot_routes.py`（末尾追加）

**Interfaces:**
- Consumes: `db.get_bot_conversation`（含 channel）、既有幂等迁移风格（PRAGMA 探测 → ALTER）。
- Produces: `bot_conversations.display_name/display_avatar`（TEXT NULL）；`db.update_bot_conversation_display(conversation_id, display_name=None, display_avatar=None) -> Optional[dict]`（None=不改，空串=清除；不碰 `updated_at`）；`PATCH /bot/conversations/{id}`；`_conversation_payload` 输出自动带上两列（`dict(row)` 透传，无需改 payload 函数）。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_bot_routes.py` 末尾追加（`client`/`create_persona` 已在该文件）：

```python
def test_qq_conversation_display_override(client):
    persona = create_persona(client)
    # QQ 会话由通道侧创建，路由只收 local —— 测试直接走库函数造数据
    qq_conv = db.create_bot_conversation(persona["id"], channel="qq", external_id="group:GG1")
    local_id = client.post(
        "/api/bot/conversations", json={"persona_id": persona["id"]}
    ).json()["id"]

    patched = client.patch(
        f"/api/bot/conversations/{qq_conv['id']}",
        json={"display_name": "夜宵群", "display_avatar": "data:image/webp;base64,AAAA"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["display_name"] == "夜宵群"
    assert patched.json()["display_avatar"] == "data:image/webp;base64,AAAA"

    # 本地会话不吃显示层覆盖（spec §4.4：仅 qq）
    assert client.patch(
        f"/api/bot/conversations/{local_id}", json={"display_name": "x"}
    ).status_code == 400
    # 非法头像 422、不存在的会话 404
    assert client.patch(
        f"/api/bot/conversations/{qq_conv['id']}", json={"display_avatar": "🐟"}
    ).status_code == 422
    assert client.patch(
        "/api/bot/conversations/nope", json={"display_name": "x"}
    ).status_code == 404

    # 列表一次带回覆盖列（QQ 记录侧栏与标题栏都要用）
    row = client.get("/api/bot/conversations?channel=qq").json()[0]
    assert row["display_name"] == "夜宵群" and row["display_avatar"] == "data:image/webp;base64,AAAA"


def test_qq_display_update_does_not_bump_updated_at(client):
    persona = create_persona(client)
    qq_conv = db.create_bot_conversation(persona["id"], channel="qq", external_id="group:GG2")
    patched = client.patch(
        f"/api/bot/conversations/{qq_conv['id']}", json={"display_name": "改名不改序"}
    ).json()
    assert patched["updated_at"] == qq_conv["updated_at"]


def test_conversation_display_migration_idempotent(base_db):
    # conftest 的 base_db 已 init 过一次；再 init 不得 Duplicate column name
    db._close_all_connections()
    db.init_db(base_db)
    persona = db.create_bot_persona(name="小刻")
    conv = db.create_bot_conversation(persona["id"], channel="qq", external_id="group:GG3")
    assert conv["display_name"] is None
    assert db.get_bot_conversation(conv["id"])["display_avatar"] is None
```

- [ ] **Step 2: 跑到失败**

Run: `cd d:\workspace\cKunAgentSSH\backend && python -m pytest tests/test_bot_routes.py -q 2>&1 | Select-Object -Last 8`
Expected: FAIL（PATCH 405 / 列不存在）

- [ ] **Step 3: 实现**

`backend/database.py`：`_migrate_bot_channels(conn)` 调用行之后追加：

```python
    # bot_conversations：QQ 会话本地显示覆盖两列（Phase 4 头像体系）。
    # 仅展示层，QQ 真实群名/群头像不采集不改写；NULL = 未设置，走默认回落。
    cursor = conn.execute("PRAGMA table_info(bot_conversations)")
    conversation_cols = {row[1] for row in cursor.fetchall()}
    if "display_name" not in conversation_cols:
        conn.execute("ALTER TABLE bot_conversations ADD COLUMN display_name TEXT")
    if "display_avatar" not in conversation_cols:
        conn.execute("ALTER TABLE bot_conversations ADD COLUMN display_avatar TEXT")
```

同文件函数区（`delete_bot_conversation` 之后）追加：

```python
def update_bot_conversation_display(
    conversation_id: str,
    display_name: Optional[str] = None,
    display_avatar: Optional[str] = None,
) -> Optional[dict]:
    """QQ 会话显示层覆盖。None = 不改，空串 = 清除回落默认。

    刻意不动 updated_at：改显示名不该把一条沉睡会话顶到列表最前。
    """
    assignments: list[str] = []
    params: list = []
    if display_name is not None:
        assignments.append("display_name = ?")
        params.append(display_name)
    if display_avatar is not None:
        assignments.append("display_avatar = ?")
        params.append(display_avatar)
    if not assignments:
        return get_bot_conversation(conversation_id)
    params.append(conversation_id)
    conn = _get_conn()
    try:
        cur = conn.execute(
            f"UPDATE bot_conversations SET {', '.join(assignments)} WHERE id = ?", params
        )
        conn.commit()
        if not cur.rowcount:
            return None
        return get_bot_conversation(conversation_id)
    finally:
        conn.close()
```

`backend/routes/bot.py`：`delete_conversation` 路由之后追加：

```python
class BotConversationUpdateRequest(BaseModel):
    # 仅 QQ 会话可用的本地显示覆盖；空串 = 清除回落默认，不设 min_length
    display_name: Optional[str] = Field(None, max_length=50)
    display_avatar: Optional[str] = Field(None, max_length=400_000)

    _check_avatar = field_validator("display_avatar")(_validate_avatar)


@router.patch("/bot/conversations/{conversation_id}")
def update_conversation(conversation_id: str, req: BotConversationUpdateRequest):
    conversation = db.get_bot_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    # 显示覆盖只服务 QQ 记录页：本地会话标题就是人格名，盖了反而误导
    if conversation.get("channel") != "qq":
        raise HTTPException(status_code=400, detail="仅 QQ 会话支持自定义显示名/头像")
    updated = db.update_bot_conversation_display(
        conversation_id, **req.model_dump(exclude_unset=True)
    )
    if not updated:
        raise HTTPException(status_code=404, detail="会话不存在")
    return _conversation_payload(updated)
```

- [ ] **Step 4: 跑到通过**

Run: 同 Step 2。Expected: 全部 passed（含旧测试回归）

- [ ] **Step 5: 提交**

```powershell
cd d:\workspace\cKunAgentSSH; git add backend/database.py backend/routes/bot.py backend/tests/test_bot_routes.py; git commit -m "feat(bot): QQ 会话显示层覆盖 display_name/display_avatar 与 PATCH 路由"
```

---

## 阶段二 · 前端纯逻辑与组件（spec §7.2）

### Task 5: avatarPlaceholderStyle / placeholderInitial 纯函数

**Files:**
- Modify: `src/utils/bot-chat-decor.ts`
- Modify: `src/utils/bot-chat-decor.test.ts`

**Interfaces:**
- Consumes: 文件内既有私有 `hashStr`（L10）。
- Produces: `avatarPlaceholderStyle(seed: string): { background: string; color: string }`、`placeholderInitial(label?: string | null, seed?: string | null): string`。Task 6 的两个组件消费。

- [ ] **Step 1: 写失败测试**

`src/utils/bot-chat-decor.test.ts` import 清单加 `avatarPlaceholderStyle, placeholderInitial`，追加：

```ts
describe('淡色占位头像 avatarPlaceholderStyle / placeholderInitial', () => {
  it('同一 seed 恒定同色，不同 seed 颜色可区分', () => {
    expect(avatarPlaceholderStyle('group-A3F1')).toEqual(avatarPlaceholderStyle('group-A3F1'))
    expect(avatarPlaceholderStyle('user-7B22').background).not.toBe(
      avatarPlaceholderStyle('user-1E99').background,
    )
  })

  it('淡底深字：低饱和高明度背景 + 同色系深字，明暗主题都可读', () => {
    const style = avatarPlaceholderStyle('peer-x')
    expect(style.background).toMatch(/^hsl\(\d+, 30%, 88%\)$/)
    expect(style.color).toMatch(/^hsl\(\d+, 45%, 32%\)$/)
  })

  it('空/脏 seed 回落固定色，不抛错', () => {
    expect(avatarPlaceholderStyle('')).toEqual(avatarPlaceholderStyle(''))
    expect(avatarPlaceholderStyle('   ').background).toMatch(/^hsl\(\d+, 30%, 88%\)$/)
  })

  it('placeholderInitial：优先文案首字，回落种子尾号，都没有给 ?', () => {
    expect(placeholderInitial('晚风')).toBe('晚')
    expect(placeholderInitial('群聊 · A3F1')).toBe('群')
    expect(placeholderInitial('', 'groupA3F1')).toBe('1')
    expect(placeholderInitial()).toBe('?')
  })
})
```

- [ ] **Step 2: 跑到失败**

Run: `cd d:\workspace\cKunAgentSSH; npx vitest run src/utils/bot-chat-decor.test.ts 2>&1 | Select-Object -Last 10`
Expected: FAIL（函数未导出 / import 报错）

- [ ] **Step 3: 实现**

`src/utils/bot-chat-decor.ts` 的 `identiconSvg` 之后追加（本任务不删 identicon，Task 6 一并移除）：

```ts
/**
 * 淡色占位头像样式（spec §4.1，取代 identicon 马赛克）：
 * 种子哈希 → 色相；背景固定低饱和高明度（s=30%、l=88%），文字取同色系深字
 * （s=45%、l=32%）——明暗两套主题下都可读，且不会出现纯白纯黑的刺眼极值。
 */
export function avatarPlaceholderStyle(seed: string): { background: string; color: string } {
  const effective = (seed && seed.trim()) || '__fallback__'
  const hue = hashStr(effective) % 360
  return {
    background: `hsl(${hue}, 30%, 88%)`,
    color: `hsl(${hue}, 45%, 32%)`,
  }
}

/** 占位头像要显示的字：优先展示文案首字（Array.from 防 emoji/代理对截半），回落种子尾号大写。 */
export function placeholderInitial(label?: string | null, seed?: string | null): string {
  const text = (label || '').trim()
  if (text) return Array.from(text)[0]
  const s = (seed || '').trim()
  return s ? s.slice(-1).toUpperCase() : '?'
}
```

- [ ] **Step 4: 跑到通过**

Run: 同 Step 2。Expected: 全部 passed

- [ ] **Step 5: 提交**

```powershell
cd d:\workspace\cKunAgentSSH; git add src/utils/bot-chat-decor.ts src/utils/bot-chat-decor.test.ts; git commit -m "feat(bot): 淡色占位头像纯函数 avatarPlaceholderStyle/placeholderInitial"
```

---

### Task 6: PersonaAvatar/GeneratedAvatar 去马赛克，identiconSvg 移除

**Files:**
- Modify: `src/components/bot/PersonaAvatar.vue`
- Modify: `src/components/bot/GeneratedAvatar.vue`
- Modify: `src/components/bot/CkunBotView.vue`（3 处调用点补 label，见 Step 3.4）
- Modify: `src/utils/bot-chat-decor.ts`（删 `identiconSvg` 与 `prng`/`GRID`）
- Modify: `src/utils/bot-chat-decor.test.ts`（删 identicon describe 块与 import）
- Modify: `src/views/ssh/CkunBotView.guards.test.ts`（追加守卫）

**Interfaces:**
- Consumes: Task 5 两个纯函数。
- Produces: `PersonaAvatar` props 扩为 `{ avatar?, seed?, label? }`（无图渲染淡色首字圈）；`GeneratedAvatar` 变薄壳透传 `{ seed?, label? }` 到 PersonaAvatar。Task 10/11/12 直接依赖这两者。

- [ ] **Step 1: 写失败测试（守卫 + 旧测试改写）**

`src/views/ssh/CkunBotView.guards.test.ts` 追加：

```ts
  it('头像不再马赛克：identicon 全移除，占位走淡色首字', () => {
    const decor = read('src/utils/bot-chat-decor.ts')
    expect(decor).not.toContain('identiconSvg')
    expect(decor).toContain('avatarPlaceholderStyle')
    const persona = read('src/components/bot/PersonaAvatar.vue')
    expect(persona).toContain('avatar-placeholder')
    expect(persona).toContain('placeholderInitial')
    const generated = read('src/components/bot/GeneratedAvatar.vue')
    expect(generated).not.toContain('identiconSvg')
    expect(generated).toContain('PersonaAvatar')
    expect(read('src/components/bot/CkunBotView.vue')).not.toContain('identiconSvg')
  })
```

`src/utils/bot-chat-decor.test.ts`：删除 `describe('生成式匿名头像 identiconSvg', …)` 整块（4 个 it）及 import 里的 `identiconSvg`。

- [ ] **Step 2: 跑到失败**

Run: `cd d:\workspace\cKunAgentSSH; npx vitest run src/views/ssh/CkunBotView.guards.test.ts src/utils/bot-chat-decor.test.ts 2>&1 | Select-Object -Last 10`
Expected: 新守卫 FAIL（decor 仍含 identiconSvg）

- [ ] **Step 3: 实现**

3.1 `src/components/bot/PersonaAvatar.vue` 整文件替换：

```vue
<script setup lang="ts">
import { computed } from 'vue'
import { isImageAvatar } from '@/utils/persona-avatar'
import { avatarPlaceholderStyle, placeholderInitial } from '@/utils/bot-chat-decor'

// 头像两态：上传图 > 淡色首字占位（同 seed 恒同色，每个人不一样 —— spec §4.1 去马赛克）。
// 非 data URL（旧数据里的表情/文本）不当图片，走占位而不是空圈。
const props = defineProps<{ avatar?: string | null; seed?: string | null; label?: string | null }>()
const image = computed(() => {
  const avatar = props.avatar
  return avatar && isImageAvatar(avatar) ? avatar : ''
})
const style = computed(() => avatarPlaceholderStyle(props.seed || props.label || ''))
const initial = computed(() => placeholderInitial(props.label, props.seed))
</script>

<template>
  <img v-if="image" class="avatar-img" :src="image" alt="" />
  <span v-else class="avatar-placeholder" :style="style" aria-hidden="true">{{ initial }}</span>
</template>

<style scoped>
.avatar-img { width: 100%; height: 100%; display: block; border-radius: inherit; object-fit: cover; }
.avatar-placeholder { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; border-radius: inherit; font-size: 0.42em; font-weight: 600; line-height: 1; user-select: none; }
</style>
```

3.2 `src/components/bot/GeneratedAvatar.vue` 整文件替换：

```vue
<script setup lang="ts">
import PersonaAvatar from './PersonaAvatar.vue'

// 对端头像入口：无真实数据（官方机器人拿不到），渲染淡色首字占位。
// 薄壳保留组件名是因为 QQ 侧栏/消息流两个调用点都以它表意「这是生成的、不是真的」。
defineProps<{ seed?: string | null; label?: string | null }>()
</script>

<template>
  <PersonaAvatar :seed="seed" :label="label" />
</template>
```

3.3 `src/utils/bot-chat-decor.ts`：删除 `prng`、`GRID`、`identiconSvg` 三处（Task 5 已确认无其他引用；`hashStr` 保留并被 `avatarPlaceholderStyle` 使用），并把文件头注释里「确定性生成匿名头像」一句改为「淡色首字占位头像」。

3.4 `src/components/bot/CkunBotView.vue` 调用点补 label（精确串替换）：
- L33：`<GeneratedAvatar :seed="peerSeed(c.external_id)" />` → `<GeneratedAvatar :seed="peerSeed(c.external_id)" :label="peerLabel(c.external_id)" />`
- L103：`<GeneratedAvatar v-else :seed="peerSeed(store.qqActiveConversation?.external_id)" />` → 同行追加 `:label="peerLabel(store.qqActiveConversation?.external_id)"`
- L45 与 L76/L89 的 `<PersonaAvatar :avatar="store.activePersona?.avatar" />` → 追加 `:label="store.activePersona?.name"`（三处）；L102 `<PersonaAvatar v-if="d.item.role === 'assistant'" :avatar="qqActivePersona?.avatar" />` → 追加 `:label="qqActivePersona?.name"`。

- [ ] **Step 4: 跑到通过**

Run: `cd d:\workspace\cKunAgentSSH; npx vitest run src/views/ssh/CkunBotView.guards.test.ts src/utils/bot-chat-decor.test.ts src/api/bot.test.ts 2>&1 | Select-Object -Last 8; npm run typecheck 2>&1 | Select-Object -Last 3`
Expected: 全 passed；typecheck EXIT 0

- [ ] **Step 5: 提交**

```powershell
cd d:\workspace\cKunAgentSSH; git add src/components/bot/PersonaAvatar.vue src/components/bot/GeneratedAvatar.vue src/components/bot/CkunBotView.vue src/utils/bot-chat-decor.ts src/utils/bot-chat-decor.test.ts src/views/ssh/CkunBotView.guards.test.ts; git commit -m "refactor(bot): 头像去马赛克——identicon 移除，淡色首字占位接管"
```

---

### Task 7: API 客户端扩展 + AvatarPicker 统一选择器

**Files:**
- Modify: `src/api/bot.ts`
- Create: `src/components/bot/AvatarPicker.vue`
- Create: `src/components/bot/AvatarPicker.test.ts`
- Modify: `src/api/bot.test.ts`

**Interfaces:**
- Consumes: Task 2/3/4 的路由形状。
- Produces: `BotGalleryItem`、`botApi.listGallery/addGalleryItem/deleteGalleryItem/getUserAvatar/setUserAvatar/updateConversationDisplay`、`BotConversation.display_name/display_avatar`；`AvatarPicker` props `{ modelValue?, seed?, label? }` + emit `update:modelValue`。Task 8-12 消费。

- [ ] **Step 1: 写失败测试**

`src/api/bot.test.ts` 追加（沿用文件顶部 `vi.mocked(request)` 模式）：

```ts
  it('图库与你的头像走 request 同一通道并序列化载荷', async () => {
    vi.mocked(request).mockResolvedValue({ id: 'g1' })
    await botApi.addGalleryItem({ label: '小猫', data_url: 'data:image/webp;base64,AAA' })
    expect(request).toHaveBeenCalledWith('/bot/gallery', {
      method: 'POST',
      body: JSON.stringify({ label: '小猫', data_url: 'data:image/webp;base64,AAA' }),
    })

    await botApi.deleteGalleryItem('g1')
    expect(request).toHaveBeenCalledWith('/bot/gallery/g1', { method: 'DELETE' })

    vi.mocked(request).mockResolvedValue({ avatar: '' })
    await botApi.setUserAvatar('data:image/webp;base64,UU')
    expect(request).toHaveBeenCalledWith('/bot/user-avatar', {
      method: 'PUT',
      body: JSON.stringify({ avatar: 'data:image/webp;base64,UU' }),
    })
  })

  it('QQ 会话显示覆盖 PATCH 只带给出的字段', async () => {
    vi.mocked(request).mockResolvedValue({ id: 'q1' })
    await botApi.updateConversationDisplay('q1', { display_name: '夜宵群' })
    expect(request).toHaveBeenCalledWith('/bot/conversations/q1', {
      method: 'PATCH',
      body: JSON.stringify({ display_name: '夜宵群' }),
    })
  })
```

新建 `src/components/bot/AvatarPicker.test.ts`（读源码守卫式，仓库对 .vue 的既有测试策略）：

```ts
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const src = readFileSync(resolve(__dirname, '../../../src/components/bot/AvatarPicker.vue'), 'utf8')

describe('头像选择器 AvatarPicker', () => {
  it('预览 / 从图库选 / 上传 三来源齐备，压缩口径只沿用 readAvatarFile 一份', () => {
    for (const needle of ['PersonaAvatar', '从图库选', '上传图片', 'listGallery', 'readAvatarFile']) {
      expect(src).toContain(needle)
    }
    expect(src).not.toContain('createImageBitmap')
  })

  it('存库尽力而为：图库写入失败不阻断头像已设成功', () => {
    expect(src).toContain('addGalleryItem')
    expect(src).toMatch(/catch \{[^}]*\}/)
  })
})
```

- [ ] **Step 2: 跑到失败**

Run: `cd d:\workspace\cKunAgentSSH; npx vitest run src/api/bot.test.ts src/components/bot/AvatarPicker.test.ts 2>&1 | Select-Object -Last 10`
Expected: FAIL（botApi 方法不存在 / 文件不存在）

- [ ] **Step 3: 实现**

`src/api/bot.ts`：`BotConversation` 接口 `preview` 字段后追加：

```ts
  /** Phase 4 QQ 会话本地显示覆盖：空/缺省 = 回落 title 与代号占位头像。 */
  display_name?: string | null
  display_avatar?: string | null
```

`BotGalleryItem` 类型（接口区）+ `botApi` 对象追加方法：

```ts
export interface BotGalleryItem {
  id: string
  label: string
  data_url: string
  created_at: string
}
```

```ts
  listGallery: () => request<BotGalleryItem[]>('/bot/gallery'),
  addGalleryItem: (payload: { label?: string; data_url: string }) => request<BotGalleryItem>('/bot/gallery', { method: 'POST', body: JSON.stringify(payload) }),
  deleteGalleryItem: (id: string) => request<{ deleted: boolean }>(`/bot/gallery/${id}`, { method: 'DELETE' }),
  getUserAvatar: () => request<{ avatar: string }>('/bot/user-avatar'),
  setUserAvatar: (avatar: string) => request<{ avatar: string }>('/bot/user-avatar', { method: 'PUT', body: JSON.stringify({ avatar }) }),
  updateConversationDisplay: (id: string, payload: { display_name?: string; display_avatar?: string }) => request<BotConversation>(`/bot/conversations/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
```

新建 `src/components/bot/AvatarPicker.vue`：

```vue
<script setup lang="ts">
import { ref } from 'vue'
import PersonaAvatar from './PersonaAvatar.vue'
import { readAvatarFile } from '@/utils/persona-avatar'
import { botApi, type BotGalleryItem } from '@/api/bot'
import { toFriendlyMessage } from '@/api/base'

// 统一头像入口（spec §4.6）：预览 + 上传（沿用 readAvatarFile 方形 256 webp 一份口径）
// + 服务端图库选取。PersonaEditor / Bot 管理「你的头像」/ QQ 会话头像三处复用。
const props = defineProps<{ modelValue?: string | null; seed?: string | null; label?: string | null }>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

const busy = ref(false)
const error = ref('')
const fileInput = ref<HTMLInputElement | null>(null)
const showGallery = ref(false)
const gallery = ref<BotGalleryItem[]>([])
const keepInGallery = ref(true)

async function toggleGallery() {
  showGallery.value = !showGallery.value
  if (showGallery.value && !gallery.value.length) {
    try { gallery.value = await botApi.listGallery() }
    catch (e) { error.value = toFriendlyMessage(e, '图库读取失败') }
  }
}

function pick(item: BotGalleryItem) {
  emit('update:modelValue', item.data_url)
  showGallery.value = false
}

async function pickFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  busy.value = true
  error.value = ''
  try {
    const url = await readAvatarFile(file)
    emit('update:modelValue', url)
    // 存库尽力而为：图库写失败不该让「头像已设好」回滚成失败
    if (keepInGallery.value) {
      try { gallery.value = [await botApi.addGalleryItem({ data_url: url }), ...gallery.value] } catch { /* 忽略 */ }
    }
  } catch (e) { error.value = toFriendlyMessage(e, '头像处理失败') }
  finally { busy.value = false }
}
</script>

<template>
  <div class="avatar-picker">
    <span class="avatar-preview"><PersonaAvatar :avatar="modelValue" :seed="seed" :label="label" /></span>
    <div class="avatar-picker-body">
      <div class="avatar-picker-actions">
        <button type="button" class="btn" :disabled="busy" @click="fileInput?.click()">{{ busy ? '处理中…' : '上传图片' }}</button>
        <button type="button" class="btn" @click="toggleGallery">{{ showGallery ? '收起图库' : '从图库选' }}</button>
        <button v-if="modelValue" type="button" class="btn" @click="emit('update:modelValue', '')">清除</button>
      </div>
      <label class="keep-gallery"><input type="checkbox" v-model="keepInGallery" /> 上传后存入图库</label>
      <small>PNG / JPG / WebP，自动裁成方形 256px</small>
      <div v-if="error" class="form-error">{{ error }}</div>
      <div v-if="showGallery" class="gallery-grid">
        <button
          v-for="item in gallery" :key="item.id" type="button" class="gallery-item"
          :title="item.label || '未命名'" @click="pick(item)"
        >
          <img :src="item.data_url" :alt="item.label || '图库头像'" />
        </button>
        <p v-if="!gallery.length" class="gallery-empty">图库还是空的，先上传一张吧。</p>
      </div>
    </div>
    <input ref="fileInput" class="avatar-file" type="file" accept="image/png,image/jpeg,image/webp,image/gif" @change="pickFile" />
  </div>
</template>

<style scoped>
.avatar-picker { display: flex; gap: var(--spacing-12); align-items: flex-start; }
.avatar-preview { width: 56px; height: 56px; flex: 0 0 56px; display: grid; place-items: center; border-radius: 50%; overflow: hidden; background: var(--bg-input); font-size: 1.5385rem; }
.avatar-picker-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: var(--spacing-6); }
.avatar-picker-actions { display: flex; gap: var(--spacing-8); flex-wrap: wrap; }
.keep-gallery { display: flex; align-items: center; gap: var(--spacing-4); color: var(--text-secondary); font-size: 0.7692rem; }
.avatar-picker-body small { color: var(--text-tertiary); font-size: 0.7692rem; }
.avatar-file { display: none; }
.form-error { color: var(--error); font-size: 0.8462rem; }
.gallery-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(48px, 1fr)); gap: var(--spacing-6); margin-top: var(--spacing-4); }
.gallery-item { padding: 0; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); overflow: hidden; background: transparent; cursor: pointer; aspect-ratio: 1; }
.gallery-item img { width: 100%; height: 100%; display: block; object-fit: cover; }
.gallery-empty { grid-column: 1 / -1; color: var(--text-tertiary); font-size: 0.8462rem; }
</style>
```

- [ ] **Step 4: 跑到通过**

Run: 同 Step 2。Expected: 全 passed；随后 `npm run typecheck 2>&1 | Select-Object -Last 3` EXIT 0

- [ ] **Step 5: 提交**

```powershell
cd d:\workspace\cKunAgentSSH; git add src/api/bot.ts src/api/bot.test.ts src/components/bot/AvatarPicker.vue src/components/bot/AvatarPicker.test.ts; git commit -m "feat(bot): AvatarPicker 统一头像选择器与图库/头像/会话覆盖 API 客户端"
```

---

## 阶段三 · 设置「Bot 管理」（spec §7.3）

### Task 8: 设置分类接线 + BotPanel（你的头像区）

**Files:**
- Modify: `src/stores/settings.ts`（L29 `SettingCategory`）
- Modify: `src/components/common/settings-nav-icons.ts`
- Modify: `src/components/common/settings-nav-icons.test.ts`
- Modify: `src/components/common/SettingsDialog.vue`
- Create: `src/components/settings/panels/BotPanel.vue`
- Create: `src/components/settings/panels/bot-panel.test.ts`

**Interfaces:**
- Consumes: Task 7 的 `AvatarPicker`、`botApi.getUserAvatar/setUserAvatar`；`PANELS` 单一清单模式（SettingsDialog L81-89）。
- Produces: `SettingCategory` 含 `'bot'`；`openSettings('bot')` 可达。Task 9 往 BotPanel 里加图库区。

- [ ] **Step 1: 写失败测试**

`src/components/common/settings-nav-icons.test.ts`：`toHaveLength(7)` → `toHaveLength(8)`，标题文案「七个图标」→「八个图标」。

新建 `src/components/settings/panels/bot-panel.test.ts`：

```ts
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const root = resolve(__dirname, '../../../..')
const read = (path: string) => readFileSync(resolve(root, path), 'utf8')

describe('设置 Bot 管理分类', () => {
  it('类型、图标、面板三处同一清单接线（漏一处编译/守卫就红）', () => {
    expect(read('src/stores/settings.ts')).toContain("| 'bot'")
    const dialog = read('src/components/common/SettingsDialog.vue')
    expect(dialog).toContain('Bot 管理')
    expect(dialog).toContain('bot:')
    expect(dialog).toContain('markRaw(BotPanel)')
    expect(read('src/components/common/settings-nav-icons.ts')).toContain('IconBot')
  })

  it('面板含「你的头像」区并接入 user-avatar 端点', () => {
    const panel = read('src/components/settings/panels/BotPanel.vue')
    expect(panel).toContain('你的头像')
    expect(panel).toContain('AvatarPicker')
    expect(panel).toContain('getUserAvatar')
    expect(panel).toContain('setUserAvatar')
  })
})
```

- [ ] **Step 2: 跑到失败**

Run: `cd d:\workspace\cKunAgentSSH; npx vitest run src/components/common/settings-nav-icons.test.ts src/components/settings/panels/bot-panel.test.ts 2>&1 | Select-Object -Last 10`
Expected: FAIL（7≠8；文件不存在）

- [ ] **Step 3: 实现**

`src/stores/settings.ts`：`| 'ops'` 后加 `| 'bot'`。

`src/components/common/settings-nav-icons.ts` 末尾追加：

```ts
export const IconBot = svgIcon('IconBot', () => [
  h('rect', { x: 4, y: 8, width: 16, height: 12, rx: 3 }),
  line(12, 4, 12, 8),
  circle(12, 3, 1),
  circle(9.5, 13, 1),
  circle(14.5, 13, 1),
  path('M2 13v3M22 13v3'),
])
```

`src/components/common/SettingsDialog.vue`：import 区加 `import BotPanel from '@/components/settings/panels/BotPanel.vue'`；图标 import 行加 `IconBot`；`PANELS` 的 `ops:` 行后加：

```ts
  bot:        { label: 'Bot 管理', icon: IconBot,      panel: markRaw(BotPanel) },
```

新建 `src/components/settings/panels/BotPanel.vue`：

```vue
<template>
  <div class="panel">
    <header class="panel-header">
      <h3>Bot 管理</h3>
      <p class="panel-desc">cKunBot 头像体系 —— 服务端头像图库与「你的头像」；QQ 会话的自定义显示名/头像在记录页就地编辑</p>
    </header>

    <div class="panel-body">
      <div class="field-group">
        <h4>你的头像</h4>
        <p class="field-desc">本地会话里你这一侧的头像；留空则显示淡色首字占位「你」。QQ 会话中你不发言，不适用。</p>
        <AvatarPicker v-model="userAvatar" seed="ckun-user" label="你" @update:model-value="saveUserAvatar" />
        <p v-if="saveError" class="form-error">{{ saveError }}</p>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import AvatarPicker from '@/components/bot/AvatarPicker.vue'
import { botApi } from '@/api/bot'
import { toFriendlyMessage } from '@/api/base'

const userAvatar = ref('')
const saveError = ref('')

onMounted(async () => {
  try { userAvatar.value = (await botApi.getUserAvatar()).avatar || '' }
  catch { /* 读不到就保持空：面板可继续编辑，保存时再报错 */ }
})

// 选中即存（图库项 / 上传结果都会触发 update:modelValue），不做额外保存按钮。
async function saveUserAvatar(value: string) {
  saveError.value = ''
  try { await botApi.setUserAvatar(value ?? '') }
  catch (e) { saveError.value = toFriendlyMessage(e, '头像保存失败') }
}
</script>

<style scoped>
.panel { display: flex; flex-direction: column; gap: var(--spacing-16); }
.panel-header h3 { margin: 0; font-size: 1.1538rem; color: var(--text-primary); }
.panel-desc { margin: var(--spacing-4) 0 0; color: var(--text-tertiary); font-size: 0.8462rem; }
.panel-body { display: flex; flex-direction: column; gap: var(--spacing-16); }
.field-group { display: flex; flex-direction: column; gap: var(--spacing-6); }
.field-group h4 { margin: 0; font-size: 0.95rem; color: var(--text-primary); }
.field-desc { margin: 0; color: var(--text-tertiary); font-size: 0.8077rem; }
.form-error { color: var(--error); font-size: 0.8462rem; }
</style>
```

（样式类名与结构对照相邻面板既有约定；若 `settings-panel-base.css` 已提供 `.panel/.panel-header` 全局类则删除 style 块中重复项——执行时以 `MemoryPanel.vue` 实际引用方式为准，其余断言不变。）

- [ ] **Step 4: 跑到通过**

Run: 同 Step 2，再 `npm run typecheck 2>&1 | Select-Object -Last 3`
Expected: 全 passed；typecheck EXIT 0（PANELS 的 Record 编译期强制 bot 项存在）

- [ ] **Step 5: 提交**

```powershell
cd d:\workspace\cKunAgentSSH; git add src/stores/settings.ts src/components/common/settings-nav-icons.ts src/components/common/settings-nav-icons.test.ts src/components/common/SettingsDialog.vue src/components/settings/panels/BotPanel.vue src/components/settings/panels/bot-panel.test.ts; git commit -m "feat(settings): 设置新增 Bot 管理分类与你的头像区"
```

---

### Task 9: BotPanel 头像图库管理区

**Files:**
- Modify: `src/components/settings/panels/BotPanel.vue`
- Modify: `src/components/settings/panels/bot-panel.test.ts`

**Interfaces:**
- Consumes: `botApi.listGallery/addGalleryItem/deleteGalleryItem`、`readAvatarFile`。
- Produces: BotPanel 图库网格（上传[可带 label]/删除）。无下游任务依赖。

- [ ] **Step 1: 写失败测试**

`bot-panel.test.ts` 追加：

```ts
  it('图库管理区：网格展示、上传（可命名）、删除', () => {
    const panel = read('src/components/settings/panels/BotPanel.vue')
    for (const needle of ['头像图库', 'listGallery', 'addGalleryItem', 'deleteGalleryItem', 'gallery-grid', 'readAvatarFile']) {
      expect(panel).toContain(needle)
    }
  })
```

- [ ] **Step 2: 跑到失败**

Run: `cd d:\workspace\cKunAgentSSH; npx vitest run src/components/settings/panels/bot-panel.test.ts 2>&1 | Select-Object -Last 8`
Expected: FAIL

- [ ] **Step 3: 实现**

`BotPanel.vue` 的 `field-group`「你的头像」之后追加区块：

```vue
      <div class="field-group">
        <h4>头像图库</h4>
        <p class="field-desc">集中管理的头像（方形 256px）；各处头像可选自图库。删除图库项不影响已设置的头像 —— 它们存的是自己的副本。</p>
        <div class="gallery-upload">
          <input v-model.trim="galleryLabel" class="gallery-label-input" maxlength="50" placeholder="命名（可留空）" />
          <button type="button" class="btn" :disabled="uploading" @click="galleryInput?.click()">{{ uploading ? '处理中…' : '上传到图库' }}</button>
          <input ref="galleryInput" class="gallery-file" type="file" accept="image/png,image/jpeg,image/webp,image/gif" @change="uploadToGallery" />
        </div>
        <p v-if="galleryError" class="form-error">{{ galleryError }}</p>
        <div class="gallery-grid">
          <div v-for="item in gallery" :key="item.id" class="gallery-cell">
            <img :src="item.data_url" :alt="item.label || '图库头像'" />
            <div class="gallery-tools">
              <small>{{ item.label || '未命名' }}</small>
              <button type="button" class="btn tiny" @click="removeFromGallery(item.id)">删除</button>
            </div>
          </div>
          <p v-if="!gallery.length" class="gallery-empty">图库还是空的。</p>
        </div>
      </div>
```

`<script setup>` 追加：

```ts
import type { BotGalleryItem } from '@/api/bot'
import { readAvatarFile } from '@/utils/persona-avatar'

const gallery = ref<BotGalleryItem[]>([])
const galleryLabel = ref('')
const galleryInput = ref<HTMLInputElement | null>(null)
const uploading = ref(false)
const galleryError = ref('')

onMounted(async () => {
  try { gallery.value = await botApi.listGallery() }
  catch { /* 列表读取失败不阻塞面板，上传/删除时会再报错 */ }
})

async function uploadToGallery(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  uploading.value = true
  galleryError.value = ''
  try {
    const dataUrl = await readAvatarFile(file)
    const created = await botApi.addGalleryItem({ label: galleryLabel.value, data_url: dataUrl })
    gallery.value = [created, ...gallery.value]
    galleryLabel.value = ''
  } catch (e) { galleryError.value = toFriendlyMessage(e, '图库上传失败') }
  finally { uploading.value = false }
}

async function removeFromGallery(id: string) {
  galleryError.value = ''
  try {
    await botApi.deleteGalleryItem(id)
    gallery.value = gallery.value.filter((item) => item.id !== id)
  } catch (e) { galleryError.value = toFriendlyMessage(e, '图库删除失败') }
}
```

注意：文件里已有 `onMounted`（读 user-avatar），第二个 `onMounted` 并存合法（composition API 允许重复注册），或合并为一个函数 —— 执行时取合并写法，两个请求并行 `await Promise.all([...])`，各自 catch。`gallery-grid` 等样式在 scoped 块补充（网格用 `repeat(auto-fill, minmax(72px, 1fr))`，其余令牌同上）。

- [ ] **Step 4: 跑到通过**

Run: 同 Step 2，再 `npm run typecheck 2>&1 | Select-Object -Last 3`
Expected: 全 passed

- [ ] **Step 5: 提交**

```powershell
cd d:\workspace\cKunAgentSSH; git add src/components/settings/panels/BotPanel.vue src/components/settings/panels/bot-panel.test.ts; git commit -m "feat(settings): Bot 管理面板头像图库管理区"
```

---

## 阶段四 · QQ 记录页与本地用户气泡（spec §7.4）

### Task 10: QQ 会话自定义显示名/头像（store + 记录页编辑入口）

**Files:**
- Modify: `src/stores/bot.ts`
- Modify: `src/stores/bot.test.ts`
- Modify: `src/components/bot/CkunBotView.vue`
- Modify: `src/views/ssh/CkunBotView.guards.test.ts`

**Interfaces:**
- Consumes: `botApi.updateConversationDisplay`（Task 7）、`PersonaAvatar` 的 `avatar/seed/label` 三态（Task 6）、`AvatarPicker`（Task 7）。
- Produces: `store.updateQqDisplay(id, payload)`（PATCH 后原位替换 `qqConversations` 项）。

- [ ] **Step 1: 写失败测试**

`src/stores/bot.test.ts`：顶部 `vi.mock('@/api/bot')` 工厂对象补三行 `updateConversationDisplay: vi.fn(), getUserAvatar: vi.fn(), setUserAvatar: vi.fn(),`；`beforeEach` 里补 `vi.mocked(botApi.getUserAvatar).mockResolvedValue({ avatar: '' })`。追加：

```ts
  it('updateQqDisplay 走 PATCH 并原位替换 QQ 会话', async () => {
    const qq = { ...conversation, id: 'q1', channel: 'qq' as const }
    vi.mocked(botApi.updateConversationDisplay).mockResolvedValue({ ...qq, display_name: '夜宵群' })
    const store = useBotStore()
    store.qqConversations = [qq]
    store.qqActiveId = 'q1'
    await store.updateQqDisplay('q1', { display_name: '夜宵群' })
    expect(botApi.updateConversationDisplay).toHaveBeenCalledWith('q1', { display_name: '夜宵群' })
    expect(store.qqConversations[0].display_name).toBe('夜宵群')
  })
```

`src/views/ssh/CkunBotView.guards.test.ts` 追加：

```ts
  it('QQ 会话显示覆盖：优先 display_name/display_avatar，空则回落默认', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    expect(view).toContain('c.display_name || c.title')
    expect(view).toContain('updateQqDisplay')
    expect(view).toContain('自定义显示')
    // 覆盖只挂在 qq 侧本地会话不吃：编辑入口必须在 isQq 分支内
    expect(view).toMatch(/qq-display[\s\S]*AvatarPicker/)
  })
```

- [ ] **Step 2: 跑到失败**

Run: `cd d:\workspace\cKunAgentSSH; npx vitest run src/stores/bot.test.ts src/views/ssh/CkunBotView.guards.test.ts 2>&1 | Select-Object -Last 10`
Expected: FAIL（updateQqDisplay 不存在 / 守卫串缺失）

- [ ] **Step 3: 实现**

`src/stores/bot.ts`：`refreshQq` 之后追加，并加入 return 导出清单（`refreshQq,` 旁）：

```ts
  /** QQ 会话本地显示覆盖（Phase 4）：PATCH 成功后原位替换，列表不重拉——
   *  服务端保证 display 编辑不动 updated_at，重拉反而会把滚动位置弄丢。 */
  async function updateQqDisplay(id: string, payload: { display_name?: string; display_avatar?: string }) {
    const updated = await botApi.updateConversationDisplay(id, payload)
    qqConversations.value = qqConversations.value.map((c) => (c.id === id ? updated : c))
    qqError.value = ''
  }
```

`src/components/bot/CkunBotView.vue` 四处：

3.1 侧栏 QQ 条目（现 L33-34）替换：

```vue
              <button class="conversation-main" @click="selectQq(c.id)">
                <span class="qq-title"><i class="conv-peer-avatar"><PersonaAvatar :avatar="c.display_avatar" :seed="peerSeed(c.external_id)" :label="c.display_name || peerLabel(c.external_id)" /></i><span class="qq-title-text">{{ c.display_name || c.title }}</span></span>
                <small>{{ peerLabel(c.external_id) }} · {{ qqPersonaLabel(c) }} · {{ formatLocalDateTime(c.updated_at) }}</small>
              </button>
```

（`GeneratedAvatar` 换成 `PersonaAvatar`：有 display_avatar 上图、无则同一套淡色首字占位，两态一个组件吃掉。`GeneratedAvatar` 至此无调用点，连同文件删除并入本任务 Step 3.5。）

3.2 QQ 标题栏（现 L52-59）：`<strong>` 内 `{{ qqPersonaName }}` 保持；`small` 行 `{{ peerLabel(store.qqActiveConversation.external_id) }}` → `{{ store.qqActiveConversation.display_name || peerLabel(store.qqActiveConversation.external_id) }}`；标题栏右侧关系徽标前加编辑开关按钮：

```vue
          <button type="button" class="btn tiny qq-display-toggle" @click="qqDisplayEditorOpen = !qqDisplayEditorOpen">自定义显示</button>
```

3.3 QQ 标题栏下方（`chat-main` 内、`bot-messages` 前）加编辑抽屉：

```vue
        <div v-if="qqDisplayEditorOpen && store.qqActiveConversation" class="qq-display-editor">
          <label class="field">
            <span>会话显示名</span>
            <input v-model.trim="qqDisplayName" maxlength="50" :placeholder="store.qqActiveConversation.title" />
          </label>
          <AvatarPicker v-model="qqDisplayAvatar" :seed="peerSeed(store.qqActiveConversation.external_id)" :label="qqDisplayName || store.qqActiveConversation.title" />
          <div class="qq-display-actions">
            <button class="btn" @click="resetQqDisplay">恢复默认</button>
            <button class="btn btn-primary" :disabled="qqDisplaySaving" @click="saveQqDisplay">{{ qqDisplaySaving ? '保存中…' : '保存' }}</button>
          </div>
          <p v-if="qqDisplayError" class="form-error">{{ qqDisplayError }}</p>
        </div>
```

`<script setup>` 状态与函数（`peerLabel` import 行不动，新增 `AvatarPicker` import）：

```ts
import AvatarPicker from './AvatarPicker.vue'

const qqDisplayEditorOpen = ref(false)
const qqDisplayName = ref('')
const qqDisplayAvatar = ref('')
const qqDisplaySaving = ref(false)
const qqDisplayError = ref('')

// 打开编辑器时以当前会话值打底；空串 = 显式清除回落默认。
watch(qqDisplayEditorOpen, (open) => {
  if (!open) return
  const c = store.qqActiveConversation
  qqDisplayName.value = c?.display_name || ''
  qqDisplayAvatar.value = c?.display_avatar || ''
  qqDisplayError.value = ''
})

async function saveQqDisplay() {
  const c = store.qqActiveConversation
  if (!c) return
  qqDisplaySaving.value = true
  qqDisplayError.value = ''
  try {
    await store.updateQqDisplay(c.id, { display_name: qqDisplayName.value, display_avatar: qqDisplayAvatar.value })
    qqDisplayEditorOpen.value = false
  } catch (e) { qqDisplayError.value = toFriendlyMessage(e, '保存失败') }
  finally { qqDisplaySaving.value = false }
}

function resetQqDisplay() {
  qqDisplayName.value = ''
  qqDisplayAvatar.value = ''
}
```

3.4 QQ 消息流对端头像（现 L103）：`<GeneratedAvatar v-else :seed="…" />` → `<PersonaAvatar v-else :avatar="store.qqActiveConversation?.display_avatar" :seed="peerSeed(store.qqActiveConversation?.external_id)" :label="store.qqActiveConversation?.display_name || peerLabel(store.qqActiveConversation?.external_id)" />`；`GeneratedAvatar` import 行删除。

3.5 删除 `src/components/bot/GeneratedAvatar.vue`（`git rm src/components/bot/GeneratedAvatar.vue`）——调用点已清零，留即死代码（本仓死代码清理规范）。Task 6 守卫断言的 `generated` 两条读取改为 `expect(() => read('src/components/bot/GeneratedAvatar.vue')).toThrow()`。

CSS：`.qq-display-editor`（`padding: var(--spacing-12) var(--spacing-16); border-bottom: 1px solid var(--border-subtle); background: var(--bg-elevated); display: flex; flex-direction: column; gap: var(--spacing-8);`）、`.qq-display-actions`（flex gap）。全部走既有令牌。

- [ ] **Step 4: 跑到通过**

Run: 同 Step 2，再 `npm run typecheck 2>&1 | Select-Object -Last 3`
Expected: 全 passed；typecheck EXIT 0

- [ ] **Step 5: 提交**

```powershell
cd d:\workspace\cKunAgentSSH; git add src/stores/bot.ts src/stores/bot.test.ts src/components/bot/CkunBotView.vue src/views/ssh/CkunBotView.guards.test.ts src/components/bot/PersonaAvatar.vue; git add -A src/components/bot/GeneratedAvatar.vue; git commit -m "feat(bot): QQ 会话自定义显示名/头像（记录页就地编辑，display 优先回落默认）"
```

---

### Task 11: 本地会话用户气泡头像位

**Files:**
- Modify: `src/stores/bot.ts`
- Modify: `src/stores/bot.test.ts`
- Modify: `src/components/bot/CkunBotView.vue`
- Modify: `src/views/ssh/CkunBotView.guards.test.ts`

**Interfaces:**
- Consumes: `botApi.getUserAvatar`（Task 7，store mock 已在 Task 10 补齐）、`PersonaAvatar` 占位态（Task 6）。
- Produces: `store.userAvatar: string`、`store.loadUserAvatar()`（init 时调用）；本地用户气泡右侧头像位。

- [ ] **Step 1: 写失败测试**

`src/stores/bot.test.ts` 追加：

```ts
  it('init 读取「你的头像」；读取失败回落空占位不阻塞初始化', async () => {
    vi.mocked(botApi.getUserAvatar).mockRejectedValueOnce(new Error('boom'))
    const store = useBotStore()
    await store.init()
    expect(store.userAvatar).toBe('')
    vi.mocked(botApi.getUserAvatar).mockResolvedValueOnce({ avatar: 'data:image/webp;base64,UHJ' })
    await store.loadUserAvatar()
    expect(store.userAvatar).toBe('data:image/webp;base64,UHJ')
  })
```

`src/views/ssh/CkunBotView.guards.test.ts` 追加：

```ts
  it('本地会话用户气泡有头像位：你的头像图 > 淡色「你」占位', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    expect(view).toContain('store.userAvatar')
    expect(view).toMatch(/\.bot-message\.user \.msg-avatar \{[^}]*order: 2/)
    // QQ 是只读记录，用户侧不发言 → 头像位只出现在 local 分支（v-else 紧邻 assistant 头像）
    expect(view).toContain('<span v-else class="msg-avatar" aria-hidden="true"><PersonaAvatar :avatar="store.userAvatar" seed="ckun-user" label="你" /></span>')
  })
```

- [ ] **Step 2: 跑到失败**

Run: `cd d:\workspace\cKunAgentSSH; npx vitest run src/stores/bot.test.ts src/views/ssh/CkunBotView.guards.test.ts 2>&1 | Select-Object -Last 10`
Expected: FAIL

- [ ] **Step 3: 实现**

`src/stores/bot.ts`：`qqError` ref 声明后追加：

```ts
  /** 「你的头像」（Phase 4）：Bot 管理面板设置，本地会话用户气泡使用；QQ 里你不发言不吃它。 */
  const userAvatar = ref('')

  async function loadUserAvatar() {
    try { userAvatar.value = (await botApi.getUserAvatar()).avatar || '' }
    catch { userAvatar.value = '' }
  }
```

`init()` 体内并行加载处（现有 `await Promise.all` 或顺序 fetch 块，执行时对齐现状）加 `loadUserAvatar()`；return 导出补 `userAvatar, loadUserAvatar,`。

`src/components/bot/CkunBotView.vue`：本地消息流（现 L76 的 `<span v-if="d.item.role !== 'user'" …>` 之后）加：

```vue
                <span v-else class="msg-avatar" aria-hidden="true"><PersonaAvatar :avatar="store.userAvatar" seed="ckun-user" label="你" /></span>
```

流式 assistant 气泡（L88-89）不动。CSS 追加一行：

```css
.bot-message.user .msg-avatar { order: 2; }
```

（`.bot-message` 是 flex，`order: 2` 把头像排到气泡右侧——与既有 `.chat-main.qq .bot-message.assistant .msg-avatar { order: 2; }` 同一手法；`.bot-message.user` 本身 `align-self: flex-end` 已右对齐。）

- [ ] **Step 4: 跑到通过**

Run: 同 Step 2，再 `npm run typecheck 2>&1 | Select-Object -Last 3`
Expected: 全 passed

- [ ] **Step 5: 提交**

```powershell
cd d:\workspace\cKunAgentSSH; git add src/stores/bot.ts src/stores/bot.test.ts src/components/bot/CkunBotView.vue src/views/ssh/CkunBotView.guards.test.ts; git commit -m "feat(bot): 本地会话用户气泡接入「你的头像」"
```

---

## 阶段五 · PersonaEditor 收编（spec §7.5）

### Task 12: PersonaEditor 头像改用 AvatarPicker

**Files:**
- Modify: `src/components/bot/PersonaEditor.vue`
- Modify: `src/views/ssh/CkunBotView.guards.test.ts`

**Interfaces:**
- Consumes: `AvatarPicker`（Task 7）。
- Produces: 无下游；`isImageAvatar(form.avatar)` 保存门槛保持（spec：上传后才能保存）。

- [ ] **Step 1: 写失败测试（守卫先行改写）**

`src/views/ssh/CkunBotView.guards.test.ts` 的「人格头像只能上传图片」用例改为：

```ts
  it('人格头像走统一选择器，表情路径已全移除', () => {
    // 编辑表单已拆到 PersonaEditor：头像逻辑收进 AvatarPicker 后，编辑器不再自持压缩/文件输入
    const editor = read('src/components/bot/PersonaEditor.vue')
    expect(editor).toContain('AvatarPicker')
    expect(editor).not.toContain('readAvatarFile')
    expect(editor).not.toContain('avatar-file')
    for (const gone of ['改用表情', 'avatar-emoji', '🤖']) {
      expect(editor).not.toContain(gone)
      expect(read('src/components/bot/CkunBotView.vue')).not.toContain(gone)
    }
  })
```

- [ ] **Step 2: 跑到失败**

Run: `cd d:\workspace\cKunAgentSSH; npx vitest run src/views/ssh/CkunBotView.guards.test.ts 2>&1 | Select-Object -Last 8`
Expected: FAIL

- [ ] **Step 3: 实现**

`src/components/bot/PersonaEditor.vue`：

- 模板「头像」field（现 L7-20 的 `.avatar-picker` 块 + `avatarError`）整块替换为：

```vue
        <div class="field">
          <span>头像</span>
          <AvatarPicker v-model="form.avatar" :seed="form.name" :label="form.name" />
        </div>
```

- `<script setup>`：删 `avatarInput/avatarBusy/avatarError` refs 与 `pickAvatar` 函数、`readAvatarFile` import（`isImageAvatar` 仍被保存按钮门槛用，保留）；import 区加 `import AvatarPicker from './AvatarPicker.vue'`。
- scoped CSS 中与旧 `.avatar-picker*`/`.avatar-file` 相关的重复规则删除（AvatarPicker 自带样式）；`.field` 布局沿用编辑器现状。

- [ ] **Step 4: 跑到通过**

Run: `cd d:\workspace\cKunAgentSSH; npx vitest run src/views/ssh/CkunBotView.guards.test.ts src/components/bot/AvatarPicker.test.ts 2>&1 | Select-Object -Last 8; npm run typecheck 2>&1 | Select-Object -Last 3`
Expected: 全 passed；typecheck EXIT 0

- [ ] **Step 5: 提交**

```powershell
cd d:\workspace\cKunAgentSSH; git add src/components/bot/PersonaEditor.vue src/views/ssh/CkunBotView.guards.test.ts; git commit -m "refactor(bot): PersonaEditor 头像接入统一 AvatarPicker"
```

---

## 阶段六 · 收尾（spec §7.6）

### Task 13: 全量验证门禁

**Files:**
- 无新改动；只跑三套门禁并修复暴露的回归（修复文件归入当次提交）。

**Interfaces:**
- Consumes: 全部前序任务。
- Produces: 门禁全绿 → 进入整枝 CodeReview（SDD 流程负责，不在本任务内派发）。

- [ ] **Step 1: 后端全量**

Run: `cd d:\workspace\cKunAgentSSH\backend && python -m pytest -q 2>&1 | Select-Object -Last 5`
Expected: 全绿（0 failed）。QQ 只读/去重/纯文本用例不许回归——spec §6 明令。

- [ ] **Step 2: 前端全量 + 类型**

Run: `cd d:\workspace\cKunAgentSSH; npm run test 2>&1 | Select-Object -Last 10; npm run typecheck 2>&1 | Select-Object -Last 3`
Expected: vitest 全绿；typecheck EXIT 0。

- [ ] **Step 3: 若有失败按 systematic-debugging 定位根因修复，重跑；全绿且无新改动则本任务无提交。**

---

## Self-Review（计划 ↔ spec 对账）

- spec §4.1（淡色占位 + 两组件去马赛克）→ Task 5/6 ✓；§4.2（图库表/路由/BotPanel 管理区）→ Task 1/2/9 ✓；§4.3（你的头像：设置键 + 面板入口 + 用户气泡）→ Task 3/8/11 ✓；§4.4（QQ 两列 + PATCH + 读回落）→ Task 4/10 ✓；§4.5（设置分类三处）→ Task 8 ✓；§4.6（AvatarPicker 三处复用）→ Task 7/8/10/12 ✓；§5 边界（副本非引用、422 口径、空 seed 兜底）→ Task 2/3/4 校验测试 + Task 5 兜底测试 ✓；§6 测试策略逐条有归属任务 ✓。
- 计划内裁定（ledger 需同步）：① 图库「重命名」砍掉——spec 路由清单无 PATCH gallery，label 上传时一次填写；② display 编辑不动 `updated_at`；③ `GeneratedAvatar` 在 Task 10 调用点清零后删除文件（Task 6 保留薄壳是过渡态）。
- 类型一致性：`avatarPlaceholderStyle/placeholderInitial`（T5 定义 → T6 消费）；`BotGalleryItem`（T7 → T9）；`updateQqDisplay`（T10 store → T10 守卫）；PATCH 返回 `BotConversation`（T4 后端形状 = T7 前端类型，display 两列经 `_conversation_payload` 透传）。
- 无占位符：所有测试与实现代码块均为可直接落盘内容；两处「执行时对齐现状」仅指既有文件内的锚点行选择（init 并行加载结构、面板样式引用方式），不影响断言内容。
