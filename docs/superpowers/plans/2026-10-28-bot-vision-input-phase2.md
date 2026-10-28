# cKunBot 多模态 · Phase 2（看图回复）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 cKunBot 本地会话支持用户发送图片、机器人看图后给出文字回复；QQ 通道零改动。

**Architecture:** DB `bot_messages` 增 `images` 列（内联 data URL JSON 数组，与头像同口径）→ `stream_reply` 把带图的用户消息构造成 OpenAI 多模态 content 数组喂模型（适配层已原样透传，无需改）→ `/bot/chat` 接收并校验 `images` → 前端 📎 上传压缩待发图 + 气泡渲染 `m.images`。

**Tech Stack:** FastAPI + Pydantic v2 + SQLite；Electron + Vue3 + Pinia + Vite；pytest(asyncio) / vitest / vue-tsc。

**Spec:** docs/superpowers/specs/2026-10-28-bot-vision-input-phase2-design.md

## Global Constraints

- 仅本地会话；`channel=="qq"` 一律忽略 images、走纯文本（零回归）。
- 落库顺序不变：**先 `add_bot_message(user)` 再 `get_recent_bot_messages(40)`**（QQ 重投去重不变量，勿改成"先读历史再 append"）。
- 迁移幂等：`PRAGMA table_info(bot_messages)` 探测后再 `ALTER TABLE ADD COLUMN`。
- 图片校验：≤4 张、单张 base64 字符 ≤ 1,500,000、仅 `^data:image/(png|jpe?g|webp|gif);base64,`（前后端同口径）。
- **不用 `supports_image` 门控**（其勾选 UI 已撤、无写入方）；非 vision 模型发图 → 端点 400 → 现有 SSE error 分支降级。
- 不加新依赖（openai SDK 现有能力即可）。
- v1 需带文字才能发送（`content` 维持 `min_length=1`）；图片为文字的可选附加。
- 每任务只 `git add` 自身文件、约定式提交；红→绿 TDD：先写失败测试跑出 RED，再实现到 GREEN。

命令：`python -m pytest backend/tests/<file> -v`（在 `backend/` 下跑 `pytest tests/<file>`）；`npm run test`、`npm run typecheck`。

---

### Task 1: DB — `bot_messages.images` 列 + 存取归一

**Files:**
- Modify: `backend/database.py`（CREATE TABLE `bot_messages:233-242`、迁移段 `:544-554`、`add_bot_message:2364-2405`、`get_bot_messages:2446-2462`、`get_recent_bot_messages:2492-2502`）
- Test: `backend/tests/test_bot_service.py`

**Interfaces:**
- Produces: `add_bot_message(conversation_id, role, content, external_msg_id=None, reasoning=None, images=None) -> dict`（返回 dict 含 `images: list[str]`）；`get_recent_bot_messages`/`get_bot_messages` 每项含 `images: list[str]`（JSON 文本解析回 list，NULL/脏值→`[]`）。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_bot_service.py` 末尾追加：

```python
def test_add_and_read_bot_message_images(bot_db):
    imgs = ["data:image/webp;base64,AAAA", "data:image/png;base64,BBBB"]
    saved = db.add_bot_message(bot_db["id"], "user", "看这张", None, None, imgs)
    assert saved["images"] == imgs
    recent = db.get_recent_bot_messages(bot_db["id"], 10)
    assert recent[-1]["images"] == imgs
    listed = db.get_bot_messages(bot_db["id"], 10)
    assert listed[-1]["images"] == imgs


def test_bot_message_images_default_empty_list(bot_db):
    saved = db.add_bot_message(bot_db["id"], "user", "纯文本")
    assert saved["images"] == []
    assert db.get_recent_bot_messages(bot_db["id"], 10)[-1]["images"] == []
    assert db.get_bot_messages(bot_db["id"], 10)[-1]["images"] == []


def test_bot_messages_images_column_present(bot_db):
    # CREATE TABLE + 迁移闸门都应保证列存在（bot_db fixture 已跑 init_db）
    conn = db._get_conn()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bot_messages)").fetchall()}
    finally:
        conn.close()
    assert "images" in cols
```

- [ ] **Step 2: 跑出 RED**

Run: `pytest tests/test_bot_service.py -k "images" -v`（在 `backend/`）
Expected: FAIL —`add_bot_message() got an unexpected keyword argument` 或 `KeyError: 'images'`。

- [ ] **Step 3: 实现**

`database.py` — CREATE TABLE `bot_messages` 在 `reasoning TEXT,` 后加一行 `images TEXT,`。
迁移段（`:553` reasoning 之后）加：
```python
    if "images" not in bot_message_cols:
        conn.execute("ALTER TABLE bot_messages ADD COLUMN images TEXT")
```
加模块级解析器（放在 `add_bot_message` 上方）：
```python
def _loads_images(value):
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
```
`add_bot_message` 签名加 `images: Optional[list] = None`，入库与返回：
```python
    images_json = json.dumps(images) if images else None
    ...
    "INSERT OR IGNORE INTO bot_messages "
    "(id, conversation_id, role, content, external_msg_id, reasoning, images, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    (message_id, conversation_id, role, content, external_msg_id, reasoning, images_json, now),
    ...
    if cur.rowcount == 0:
        row = conn.execute(...).fetchone()
        deduped = dict(row)
        deduped["images"] = _loads_images(deduped.get("images"))
        return deduped
    ...
    return {
        ..., "reasoning": reasoning, "images": images or [], "answered_at": None, "created_at": now,
    }
```
`get_bot_messages` 循环内 `item["images"] = _loads_images(item.get("images"))`。
`get_recent_bot_messages` SELECT 改 `SELECT role, content, images FROM ...`，循环把每行 `_loads_images(row["images"])` 塞回 dict。

- [ ] **Step 4: 跑出 GREEN**

Run: `pytest tests/test_bot_service.py -k "images" -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/database.py backend/tests/test_bot_service.py
git commit -m "feat(bot): persist per-message images on bot_messages (Phase 2 vision storage)"
```

---

### Task 2: 生成层 — `stream_reply` 多模态组装（仅本地）

**Files:**
- Modify: `backend/bot/chat_service.py`
- Test: `backend/tests/test_bot_service.py`、`backend/tests/test_bot_chat_service.py`

**Interfaces:**
- Consumes: Task 1 的 `add_bot_message(..., images=)`、`get_recent_bot_messages` 带 `images`。
- Produces: `stream_reply(conversation_id, content, external_msg_id=None, *, channel="local", images=None)`；新增静态方法 `_to_llm_message(item: dict) -> dict`。

- [ ] **Step 1: 写失败测试**

在 `test_bot_service.py` 追加：
```python
@pytest.mark.asyncio
async def test_stream_reply_builds_multimodal_for_user_images(bot_db, monkeypatch):
    from bot.chat_service import BotChatService

    captured = {}

    async def fake_stream(**kwargs):
        captured["messages"] = kwargs["messages"]
        yield SimpleNamespace(content="一只猫", reasoning="")

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    imgs = ["data:image/webp;base64,AAAA"]
    [c async for c in BotChatService().stream_reply(bot_db["id"], "这是什么", images=imgs)]

    user_msgs = [m for m in captured["messages"][1:] if m["role"] == "user"]
    assert user_msgs[-1]["content"] == [
        {"type": "text", "text": "这是什么"},
        {"type": "image_url", "image_url": {"url": "data:image/webp;base64,AAAA"}},
    ]
    # 落库带图：历史回看能取到 images
    assert db.get_recent_bot_messages(bot_db["id"], 10)[-1]["images"] == imgs


@pytest.mark.asyncio
async def test_qq_channel_ignores_images(bot_db, monkeypatch):
    from bot.chat_service import BotChatService

    captured = {}

    async def fake_stream(**kwargs):
        captured["messages"] = kwargs["messages"]
        yield SimpleNamespace(content="在", reasoning="")

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    await BotChatService().complete_reply(bot_db["id"], "在吗", channel="qq")

    for m in captured["messages"][1:]:
        assert isinstance(m["content"], str)  # QQ 面永不构造数组
```

- [ ] **Step 2: 跑出 RED**

Run: `pytest tests/test_bot_service.py -k "multimodal or ignores_images" -v`
Expected: FAIL — `stream_reply() got an unexpected keyword argument 'images'`。

- [ ] **Step 3: 实现**

`chat_service.py`：`stream_reply` 签名加 `images: list | None = None`。替换落库+历史段（`:68-73`）：
```python
        send_images = images if channel == "local" else None
        await asyncio.to_thread(
            db.add_bot_message, conversation_id, "user", content, external_msg_id,
            None, send_images,
        )
        history = await asyncio.to_thread(db.get_recent_bot_messages, conversation_id, 40)
        messages = [{"role": "system", "content": self._system_prompt(persona, channel)}]
        messages.extend(self._to_llm_message(item) for item in history)
```
新增静态方法（类内）：
```python
    @staticmethod
    def _to_llm_message(item: dict) -> dict:
        images = item.get("images") or []
        if item["role"] == "user" and images:
            return {
                "role": "user",
                "content": [{"type": "text", "text": item["content"]}]
                + [{"type": "image_url", "image_url": {"url": u}} for u in images],
            }
        return {"role": item["role"], "content": item["content"]}
```
末尾 `safe_max_tokens` 的 prompt_text 拼接处（`:97`）：`str(item["content"])` 对 list 也安全（`str([...])`），保持不变即可（不改口径）。
更新 `test_bot_chat_service.py` 里 `fake_add` 签名加 `images=None`（`:22`），避免 7 参位置报错。

- [ ] **Step 4: 跑出 GREEN**

Run: `pytest tests/test_bot_service.py tests/test_bot_chat_service.py -v`
Expected: PASS（含既有 reasoning/dedup/style 用例不回归）。

- [ ] **Step 5: 提交**

```bash
git add backend/bot/chat_service.py backend/tests/test_bot_service.py backend/tests/test_bot_chat_service.py
git commit -m "feat(bot): assemble multimodal user messages for local vision input (Phase 2)"
```

---

### Task 3: API — `BotChatRequest.images` 校验 + 透传

**Files:**
- Modify: `backend/routes/bot.py`
- Test: `backend/tests/test_bot_routes.py`

**Interfaces:**
- Consumes: Task 2 `stream_reply(..., images=)`。
- Produces: `POST /bot/chat` body 接受 `images: list[str]`（≤4、单张 ≤1.5M、须图片 data URL），透传给 `stream_reply`。

- [ ] **Step 1: 写失败测试**

沿用本文件既有 `client` fixture（Pydantic 在进入 handler 前就校验 images，422 无需真实会话）：
```python
def test_bot_chat_rejects_too_many_images(client):
    r = client.post("/api/bot/chat", json={
        "conversation_id": "x", "content": "看这些",
        "images": ["data:image/webp;base64,AAAA"] * 5,
    })
    assert r.status_code == 422


def test_bot_chat_rejects_non_image_data_url(client):
    r = client.post("/api/bot/chat", json={
        "conversation_id": "x", "content": "看",
        "images": ["data:text/plain;base64,AAAA"],
    })
    assert r.status_code == 422
```

- [ ] **Step 2: 跑出 RED**

Run: `pytest tests/test_bot_routes.py -k "images" -v`
Expected: FAIL — 当前 `BotChatRequest` 无 `images`，多余字段被忽略 → 200/非 422。

- [ ] **Step 3: 实现**

`routes/bot.py` `BotChatRequest`（`:60-62`）：
```python
class BotChatRequest(BaseModel):
    conversation_id: str
    content: str = Field(..., min_length=1, max_length=20000)
    images: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("images")
    @classmethod
    def _check_images(cls, value):
        for url in value:
            if not _AVATAR_DATA_URL.match(url):
                raise ValueError("图片仅支持上传的图片")
            if len(url) > 1_500_000:
                raise ValueError("单张图片过大，请换一张更小或更简单的")
        return value
```
`events()` 调用处（`:138-140`）传 images：
```python
            async for kind, text in chat_service.stream_reply(
                req.conversation_id, req.content, channel="local", images=req.images or None
            ):
```

- [ ] **Step 4: 跑出 GREEN**

Run: `pytest tests/test_bot_routes.py -v`
Expected: PASS（含既有 reasoning SSE 用例）。

- [ ] **Step 5: 提交**

```bash
git add backend/routes/bot.py backend/tests/test_bot_routes.py
git commit -m "feat(bot): validate and forward chat images on /bot/chat (Phase 2 API)"
```

---

### Task 4: 前端工具 + API — `readChatImage` & `streamBotChat` images

**Files:**
- Create: `src/utils/chat-image.ts`
- Modify: `src/api/bot.ts`
- Test: `src/utils/chat-image.test.ts`、`src/api/bot.test.ts`

**Interfaces:**
- Produces: `readChatImage(file: File): Promise<string>`（等比缩到长边 ≤1280、webp、≤1.5M 字符、仅图片）；`MAX_CHAT_IMAGES = 4`；`BotMessage.images?: string[] | null`；`streamBotChat(id, content, options)` 的 `options.images?: string[]`，请求体含 `images`。

- [ ] **Step 1: 写失败测试**

`src/api/bot.test.ts` 追加：
```typescript
  it('请求体带上 images（无图时为空数组）', async () => {
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(new TextEncoder().encode('data: {"type":"done","message":{"id":"m1","role":"assistant","content":"好"}}\n\n'))
        c.close()
      },
    })
    const fetchMock = vi.fn().mockResolvedValue(new Response(stream, { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await streamBotChat('c1', '看这张', { onContent: () => {}, images: ['data:image/webp;base64,AAAA'] })
    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)
    expect(body.images).toEqual(['data:image/webp;base64,AAAA'])
    vi.unstubAllGlobals()
  })
```
`src/utils/chat-image.test.ts`（新建）：
```typescript
import { describe, expect, it } from 'vitest'
import { MAX_CHAT_IMAGES } from '@/utils/chat-image'

describe('chat-image', () => {
  it('最多 4 张', () => {
    expect(MAX_CHAT_IMAGES).toBe(4)
  })
})
```

- [ ] **Step 2: 跑出 RED**

Run: `npm run test -- src/utils/chat-image src/api/bot`
Expected: FAIL — `@/utils/chat-image` 不存在 / body 无 images。

- [ ] **Step 3: 实现**

新建 `src/utils/chat-image.ts`：
```typescript
const IMAGE_DATA_URL = /^data:image\/(?:png|jpe?g|webp|gif);base64,/
export const MAX_CHAT_IMAGES = 4
const MAX_EDGE = 1280
const MAX_DATA_URL_LENGTH = 1_500_000

export function isChatImage(dataUrl: string): boolean {
  return IMAGE_DATA_URL.test(dataUrl)
}

/** 等比缩放到长边 ≤1280 的 webp data URL，供 vision 模型看图；不做方形裁切（与头像口径不同）。 */
export async function readChatImage(file: File): Promise<string> {
  if (!file.type.startsWith('image/')) throw new Error('请选择图片文件')
  let bitmap: ImageBitmap
  try { bitmap = await createImageBitmap(file) }
  catch { throw new Error('这张图片打不开，请换一张') }
  try {
    const scale = Math.min(1, MAX_EDGE / Math.max(bitmap.width, bitmap.height))
    const w = Math.max(1, Math.round(bitmap.width * scale))
    const h = Math.max(1, Math.round(bitmap.height * scale))
    const canvas = document.createElement('canvas')
    canvas.width = w; canvas.height = h
    const ctx = canvas.getContext('2d')
    if (!ctx) throw new Error('当前环境无法处理图片')
    ctx.imageSmoothingQuality = 'high'
    ctx.drawImage(bitmap, 0, 0, w, h)
    const url = canvas.toDataURL('image/webp', 0.85)
    if (!isChatImage(url)) throw new Error('图片编码失败，请换一张')
    if (url.length > MAX_DATA_URL_LENGTH) throw new Error('图片太大，请换一张更小或更简单的')
    return url
  } finally { bitmap.close() }
}
```
`src/api/bot.ts`：`BotMessage`（`:87-95`）加 `images?: string[] | null`；`streamBotChat` options（`:139`）加 `images?: string[]`；请求体（`:147`）改：
```typescript
      body: JSON.stringify({ conversation_id: conversationId, content, images: options.images ?? [] }),
```

- [ ] **Step 4: 跑出 GREEN**

Run: `npm run test -- src/utils/chat-image src/api/bot`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/utils/chat-image.ts src/utils/chat-image.test.ts src/api/bot.ts src/api/bot.test.ts
git commit -m "feat(bot): add chat-image util and image payload on bot API (Phase 2)"
```

---

### Task 5: 前端 store — `sendMessage(content, images?)`

**Files:**
- Modify: `src/stores/bot.ts`
- Test: `src/stores/bot.test.ts`

**Interfaces:**
- Consumes: Task 4 `streamBotChat(..., { images })`、`BotMessage.images`。
- Produces: `sendMessage(content: string, images?: string[])`。

- [ ] **Step 1: 写失败测试**

`src/stores/bot.test.ts` 追加：
```typescript
  it('sendMessage 透传 images 并乐观带图追加用户消息', async () => {
    const imgs = ['data:image/webp;base64,AAAA']
    vi.mocked(streamBotChat).mockImplementation(async (_id, _t, options) => {
      expect(options.images).toEqual(imgs)
      return { id: 'm2', conversation_id: 'c1', role: 'assistant', content: '看到了', created_at: '' }
    })
    const store = useBotStore()
    store.personas = [persona]; store.conversations = [conversation]; store.activeConversationId = 'c1'
    await store.sendMessage('这是什么', imgs)
    expect(store.messages[0].role).toBe('user')
    expect(store.messages[0].images).toEqual(imgs)
  })
```

- [ ] **Step 2: 跑出 RED**

Run: `npm run test -- src/stores/bot`
Expected: FAIL — `options.images` undefined（sendMessage 未透传）。

- [ ] **Step 3: 实现**

`src/stores/bot.ts` `sendMessage`（`:145-183`）改签名与乐观追加、透传：
```typescript
  async function sendMessage(content: string, images?: string[]) {
    const text = content.trim()
    const imgs = (images || []).filter(Boolean)
    const conversationId = activeConversationId.value
    if (channel.value !== 'local' || !text || !conversationId || streaming.value) return
    ...
    messages.value.push({
      id: `local-${Date.now()}`, conversation_id: conversationId, role: 'user', content: text,
      images: imgs, created_at: new Date().toISOString(),
    })
    ...
      const saved = await streamBotChat(conversationId, text, {
        signal: controller.signal,
        images: imgs,
        onContent: ...,
        onReasoning: ...,
      })
```

- [ ] **Step 4: 跑出 GREEN**

Run: `npm run test -- src/stores/bot`
Expected: PASS（含既有流式/reasoning/失败用例）。

- [ ] **Step 5: 提交**

```bash
git add src/stores/bot.ts src/stores/bot.test.ts
git commit -m "feat(bot): pass images through bot store sendMessage (Phase 2)"
```

---

### Task 6: 前端视图 — 📎 上传待发条 + 气泡图片渲染

**Files:**
- Modify: `src/components/bot/CkunBotView.vue`
- Test: `src/views/ssh/CkunBotView.guards.test.ts`

**Interfaces:**
- Consumes: Task 4 `readChatImage`/`MAX_CHAT_IMAGES`、Task 5 `sendMessage(content, images)`。

- [ ] **Step 1: 写失败测试**

`CkunBotView.guards.test.ts` 追加：
```typescript
  it('本地会话支持附图发送与图片气泡（Phase 2）', () => {
    const view = read('src/components/bot/CkunBotView.vue')
    for (const needle of ['readChatImage', 'MAX_CHAT_IMAGES', 'pendingImages', 'composer-attach', 'pickImages']) {
      expect(view).toContain(needle)
    }
    expect(view).toContain('m.images')
    expect(view).toContain('msg-image')
    expect(view).toContain('store.sendMessage(text, imgs)')
  })
```

- [ ] **Step 2: 跑出 RED**

Run: `npm run test -- src/views/ssh/CkunBotView.guards`
Expected: FAIL — 视图缺上述标记。

- [ ] **Step 3: 实现**

`CkunBotView.vue`：
- import：`import { readChatImage, MAX_CHAT_IMAGES } from '@/utils/chat-image'`。
- script 内加状态与函数：
```typescript
const attachInput = ref<HTMLInputElement | null>(null)
const attachBusy = ref(false)
const attachError = ref('')
const pendingImages = ref<string[]>([])

async function pickImages(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  if (!files.length) return
  attachBusy.value = true
  attachError.value = ''
  const room = MAX_CHAT_IMAGES - pendingImages.value.length
  if (room <= 0) { attachError.value = `最多 ${MAX_CHAT_IMAGES} 张图片`; attachBusy.value = false; return }
  try {
    for (const file of files.slice(0, room)) pendingImages.value.push(await readChatImage(file))
    if (files.length > room) attachError.value = `最多 ${MAX_CHAT_IMAGES} 张图片`
  } catch (e) { attachError.value = toFriendlyMessage(e, '图片处理失败') }
  finally { attachBusy.value = false }
}
function removePendingImage(index: number) { pendingImages.value.splice(index, 1) }
```
- `send()` 改：
```typescript
async function send() {
  const text = draft.value
  if (!text.trim() || store.streaming) return
  const imgs = pendingImages.value
  draft.value = ''
  pendingImages.value = []
  await store.sendMessage(text, imgs)
}
```
- 模板：composer `<form>` 内 textarea **前**加附件按钮与隐藏 input：
```html
          <button type="button" class="composer-attach" :disabled="attachBusy || store.streaming || pendingImages.length >= MAX_CHAT_IMAGES" title="添加图片" aria-label="添加图片" @click="attachInput?.click()">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M21.44 11.05l-9.19 9.19a5 5 0 01-7.07-7.07l9.19-9.19a3.5 3.5 0 014.95 4.95l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/></svg>
          </button>
          <input ref="attachInput" type="file" class="composer-attach-input" accept="image/*" multiple @change="pickImages" />
```
- 待发条 + 错误：在 `<form class="chat-composer">` **上方**插入：
```html
        <div v-if="!store.isQq && store.activeConversationId && (pendingImages.length || attachError)" class="composer-attachments">
          <div v-for="(u, i) in pendingImages" :key="i" class="attach-thumb">
            <img :src="u" alt="待发图片" />
            <button type="button" class="attach-remove" aria-label="移除图片" @click="removePendingImage(i)">×</button>
          </div>
          <p v-if="attachError" class="attach-error">{{ attachError }}</p>
        </div>
```
- 气泡渲染：`bot-messages` 本地分支 message-content 内，正文前加：
```html
                <div v-if="m.images?.length" class="msg-images">
                  <img v-for="(u, i) in m.images" :key="i" :src="u" class="msg-image" alt="图片" />
                </div>
```
（置于 `<details>` 与 `MarkdownStream`/`<template v-else>` 之前。）
- CSS（`<style scoped>` 内追加）：
```css
.composer-attach { width: 32px; height: 32px; flex: 0 0 32px; display: inline-flex; align-items: center; justify-content: center; padding: 0; border: 0; border-radius: 50%; background: transparent; color: var(--text-tertiary); cursor: pointer; }
.composer-attach:hover:not(:disabled) { color: var(--text-primary); background: var(--bg-hover); }
.composer-attach:disabled { opacity: .5; cursor: default; }
.composer-attach svg { width: 18px; height: 18px; }
.composer-attach-input { display: none; }
.composer-attachments { display: flex; flex-wrap: wrap; gap: var(--spacing-8); align-items: center; margin: 0 max(var(--spacing-24), calc((100% - 760px) / 2)) var(--spacing-8); }
.attach-thumb { position: relative; width: 60px; height: 60px; border-radius: var(--radius-md); overflow: hidden; border: 1px solid var(--border-subtle); }
.attach-thumb img { width: 100%; height: 100%; object-fit: cover; }
.attach-remove { position: absolute; top: 2px; right: 2px; width: 18px; height: 18px; border: 0; border-radius: 50%; background: rgba(0,0,0,.55); color: #fff; cursor: pointer; line-height: 1; }
.attach-error { color: var(--danger-text); font-size: 0.8462rem; }
.msg-images { display: flex; flex-wrap: wrap; gap: var(--spacing-6); margin-bottom: var(--spacing-6); }
.msg-image { max-width: 200px; max-height: 200px; border-radius: var(--radius-md); border: 1px solid var(--border-subtle); object-fit: contain; }
```

- [ ] **Step 4: 跑出 GREEN + typecheck**

Run: `npm run test -- src/views/ssh/CkunBotView.guards` 然后 `npm run typecheck`
Expected: 测试 PASS、typecheck 无错。

- [ ] **Step 5: 提交**

```bash
git add src/components/bot/CkunBotView.vue src/views/ssh/CkunBotView.guards.test.ts
git commit -m "feat(bot): add image attach and image bubbles to cKunBot local chat (Phase 2)"
```

---

### Task 7: 收尾全量验证

- [ ] 后端全量：`cd backend && python -m pytest -q` → 除已记录的 `test_http_log_diagnostics::test_log_level_apply_installs_single_handler_and_sets_root`（Phase 1 已证实预存 flaky、与本改动无关）外全绿；QQ 相关用例（`test_qq_channel.py`/`test_bot_qq_channel.py`）不得回归。
- [ ] 前端全量：`npm run test` 全绿、`npm run typecheck` clean。
- [ ] 若发现新失败：按 systematic-debugging 定位根因（先复现、查改动），只修本改动引入的问题，不越界改无关项。

## Self-Review

- **Spec 覆盖**：DB images 列(§4.1)=Task1；多模态组装/渠道忽略/QQ 去重不变(§4.2)=Task2；请求校验/透传(§4.3)=Task3；前端 util/api/store/view(§4.4)=Task4-6；错误降级(§5)由既有 SSE error 分支承载（无新代码，Task3 校验覆盖非法输入）。✅
- **无占位符**：各步含真实代码/断言。✅
- **类型一致**：`stream_reply(..., images=)`、`sendMessage(content, images?)`、`readChatImage(file):Promise<string>`、`BotMessage.images?: string[]|null`、`MAX_CHAT_IMAGES=4`、1.5M 上限前后端同值。✅
- **裁定**：v1 需带文字（content `min_length=1` 不变，图片为附加）；助手历史不回喂图（`_to_llm_message` 仅 user 且带图才构造数组）。
