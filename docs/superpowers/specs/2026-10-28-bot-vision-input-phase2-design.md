# cKunBot 多模态 · Phase 2 设计（看图回复）

- 日期：2026-10-28
- 范围：仅 Phase 2（本地会话视觉输入）。Phase 3（生成图片回复）暂缓——用户决定「图片生成现在先不做」，待其 OpenAI 兼容 `/images/generations` 端点就绪后另立 spec。
- 上游：Phase 1（人设与回复体验内核）已完成合入（`docs/superpowers/specs/2026-10-28-bot-chat-experience-phase1-design.md`）。本阶段沿用其渠道化、落库顺序、迁移幂等等约束。

## 1. Summary

让 cKunBot 本地会话支持用户发送图片、机器人「看图回复」：前端上传/粘贴图片压缩为 data URL，后端把当前用户消息构造成 OpenAI 多模态 content 数组喂给聊天模型，模型返回的文字回复照常流式。**QQ 通道维持纯文本不变**（被动回复单条纯文本，无图片输入位）。图片以内联 data URL 落库（与头像 `persona.avatar` 同口径），供历史回显与重喂。

## 2. Goals / Non-goals

Goals
- 本地 `/bot/chat` 接受图片输入（data URL 列表），组装多模态消息喂模型，得到看图后的文字回复。
- 图片端到端贯通：前端选择/压缩 → API → `stream_reply` → 落库 → 历史回显与重喂。
- 图片输入校验（≤4 张/单张 base64 ≲1.5MB/仅图片 data URL）与既有头像校验同口径。
- 迁移幂等、旧库无破坏；QQ 路径零回归。

Non-goals（本阶段不做）
- 图像生成回复 → Phase 3（暂缓）。不引入 `generate_image` 工具、`persona.image_model_id`、`llm_adapter.generate_image`。
- QQ 端图片输入/输出。
- 图片磁盘 sidecar 存储（本期内联 data URL）。
- 图片历史二次压缩/裁剪 token 优化的复杂策略（仅做「只回喂用户图、不回喂助手图」的简单控量）。

## 3. 平台能力事实（已核对）

- `llm_adapter.chat_stream` 原样把 `messages` 透传给 OpenAI 兼容端点（`client.chat.completions.create`）：**标准 `image_url` content-part 数组天然可通路**，适配层无需改动，无需 vision 专属代码。
- 全仓无 `/images/generations` 调用与生图端点（Phase 3 的地基缺失，故暂缓合理）。
- `bot_messages`（`backend/database.py:233`）当前列：`id, conversation_id, role, content, external_msg_id, answered_at, reasoning, created_at`；`add_bot_message`（:2364）写 `content/external_msg_id/reasoning`；`get_recent_bot_messages`（:2492）只 `SELECT role, content`；`get_bot_messages`（:2446）为 `SELECT *`。
- 迁移闸门对 `bot_messages` 用 `PRAGMA table_info(bot_messages)` + 逐列 `ALTER TABLE ADD COLUMN`（:544-554，Phase 1 已加 `reasoning`），本阶段沿用同一模式加 `images`。
- 头像校验先例：`routes/bot.py` 的 `_AVATAR_DATA_URL = ^data:image/(png|jpe?g|webp|gif);base64,`，`avatar` 字段上限 400_000 字符。图片输入校验复用同一正则与思路。
- QQ `_handle`/`_send_reply`（`bot/qq_channel.py`）：`content=(message.content or "")`，`complete_reply(..., channel="qq")`；本阶段不触碰（回归安全）。
- 前端本地气泡渲染走 `MarkdownStream`（marked+DOMPurify），`CkunBotView.vue` 消息区。图片用独立 `<img>` 渲染，不依赖 Markdown img 白名单。
- 记忆约束命中：`多模态功能作用域限制`（仅本地）、`图片输入校验规则`（≤4 张、单张 base64 ≲1.5MB、超限 400）。

## 4. 详细设计

### 4.1 数据层（backend/database.py）

`bot_messages` 增一列，走既有 `PRAGMA table_info(bot_messages)` 幂等迁移：
- `images TEXT`（默认 NULL；存 JSON 数组字符串，元素为 data URL 或 http URL）。同步补进 `CREATE TABLE IF NOT EXISTS bot_messages`。

`add_bot_message` 形参扩展：
```
def add_bot_message(conversation_id, role, content, external_msg_id=None, reasoning=None, images=None) -> dict
```
- `images: Optional[list[str]] = None`；入库时 `json.dumps(images)`（None/空列表写 NULL），返回 dict 增 `images`（解析回 list，缺省 `[]`）。

`get_recent_bot_messages`：SELECT 增列 `role, content, images`，dict 带 `images`（解析 JSON，NULL→`[]`）。

`get_bot_messages`：`SELECT *` 已含 `images` 列，但原始值是 JSON 文本——读取后统一 `json.loads` 归一成 list（与 `add_bot_message` 返回口径一致），避免前端拿到字符串。

### 4.2 生成层（backend/bot/chat_service.py）

`stream_reply` 形参扩展（仅本地消费 images）：
```
async def stream_reply(self, conversation_id, content, external_msg_id=None, *, channel="local", images=None) -> AsyncGenerator[tuple[str,str], None]
```
- **落库顺序不变**：仍是「先 `add_bot_message(user)` 再 `get_recent_bot_messages(40)`」，保证 QQ 重投去重不变量；用户消息落库时带上 `images`（仅本地有值；QQ 调用不传 images）。
- 构造喂模型的 messages：
  - **当前/历史用户消息**：若该消息带 `images`（非空），content 构造为多模态数组
    `[{"type":"text","text":content}] + [{"type":"image_url","image_url":{"url":u}} for u in images]`；
    无图时维持纯文本字符串（不破坏非 vision 模型/纯文本回合）。
  - **助手消息**：恒纯文本（`content`），**不回喂图片**（控 token；本阶段助手也无生成图）。
  - system prompt 不变（Phase 1 的 `_system_prompt(persona, channel)`）。
- `channel="qq"`：忽略传入 images（若有），走纯文本（回归安全）。
- 流结束落库助手消息：`add_bot_message(..., role="assistant", content=reply, reasoning=..., images=None)`（本阶段助手无图）。
- `complete_reply`（QQ）签名不变，透传 `channel`，不接 images。

> Vision 模型能力**不做门控**（与 Phase 1 不用 `supports_thinking` 门控同理）：用户给非 vision 模型发图，端点会 400，经现有 SSE error 分支回落到错误提示，属可接受的降级，不新增 `supports_image` 勾选（其 UI 已撤、无写入方）。

### 4.3 请求/API（backend/routes/bot.py）

`BotChatRequest` 增字段：
```
images: list[str] = Field(default_factory=list, max_length=4)
```
- 每张用与头像同款 `_AVATAR_DATA_URL` 正则校验（`field_validator`），任一非图片 data URL → 422/400。
- 单张长度上限约 1.5M 字符（对应记忆「图片输入校验规则」），超限 400。总量上限：≤4 张。
- `/bot/chat` 调用处：`stream_reply(req.conversation_id, req.content, channel="local", images=req.images or None)`。
- SSE 契约：`events()` 按 kind 映射不变；本阶段仍只产出 `content`/`reasoning`（无新事件类型）；`done` 的 message 来自 `get_bot_messages(conversation_id, 1)[-1]`（即助手回复，本阶段其 `images` 为空）。**用户气泡的图片不靠 done 定稿**：发送时前端乐观追加的 user 消息已带 images 即时渲染，后端已将 user 消息连图落库，重开会话时 `get_bot_messages` 回读还原。

### 4.4 前端

- `src/api/bot.ts`：
  - `BotMessage` 加 `images?: string[] | null`。
  - `streamBotChat` options 加 `images?: string[]`（随请求体发送）；payload 构造带上 `images: options.images ?? []`。
  - `BotSseEvent` 无需新类型（本阶段不新增事件 kind）。
- `src/stores/bot.ts`：`sendMessage(content, images?)` 增第二参，透传给 `streamBotChat`；用户乐观追加的 user 消息带 `images`（即时渲染）；助手回复仍由 `done` 定稿（本阶段无助手图）。重开会话时 user 图由 `get_bot_messages` 回读还原。
- `src/components/bot/CkunBotView.vue`：
  - 输入区加 📎 附件按钮 + 隐藏 `<input type="file" accept="image/*" multiple>`（仅本地会话显示）；选择后经 FileReader → 压缩（沿用头像压缩口径，若无则原样 data URL）→ 待发缩略图条（可删单张）→ 发送时随消息带上、发送后清空待发列表。
  - 客户端预校验：≤4 张、单张 base64 长度 ≲1.5MB、类型 `image/*`，超限提示并拒发（与后端 400 同口径，记忆「图片输入校验规则」）。
  - 消息气泡：`m.images?.length` 时渲染 `<img>` 缩略图条（用户图与助手图共用同一渲染，本阶段实际只有用户图）；点击可放大（原生 `target` 或简单 lightbox，按现有组件能力决定，不新造复杂查看器）。
  - persona 编辑弹窗：本阶段无新增字段（Phase 3 的 `image_model_id` 不做）。

## 5. 错误处理

- 图片 data URL 非法/超限：Pydantic `field_validator` + `max_length` → 422；前端预校验先行拦截，后端为最终闸。
- 非 vision 模型收到图：端点 400 → `stream_reply` 抛错 → SSE `error` 事件回落提示（不崩、不半截）。属可接受降级，不做能力门控。
- `images` 列 JSON 解析失败（脏数据）：`get_recent_bot_messages`/`get_bot_messages` 读取时 `try/except → []`，不让单条坏数据打断历史加载。
- 旧库缺 `images` 列：`PRAGMA` 幂等 `ALTER` 补齐（默认 NULL），读取路径 `.get("images") or []` 容错。
- QQ 面：调用链不传 images、`stream_reply` 即便被传也忽略（`channel!="local"` 分支），零行为变化。

## 6. 测试（TDD，红→绿）

后端（pytest）：
- 存储：`add_bot_message(..., images=[...])` 落库并返回 `images` list；`get_recent_bot_messages` 返回带 `images`；`get_bot_messages` 把 JSON 文本归一成 list；空/None→`[]`。
- 迁移幂等：旧库无 `images` 列时 init 补齐 NULL，重复 init 不报错。
- 多模态组装：假 `chat_stream` 断言——当轮/历史用户消息带 images 时，对应 message 的 content 为含 `image_url` 的数组、无图时为纯字符串；助手消息恒纯文本（不回喂图）。
- 渠道回归：`channel="qq"` 即使传 images 也不构造数组、走纯文本；QQ 去重顺序不变（先落库再读历史）仍成立（复用 Phase 1 既有 QQ 重投测试不破）。
- 校验：`BotChatRequest.images` 超 4 张 / 非图片 data URL / 单张超长 → 422/400；合法通过并透传到 `stream_reply`。

前端守卫（vitest）：
- `api/bot.ts`：payload 含 `images`，`BotMessage` 类型含 `images`。
- `CkunBotView.vue`：模板含附件按钮/文件 input 与 `m.images` 的 `<img>` 渲染标记；store `sendMessage` 第二参透传。

## 7. 成功标准

- 用户在本地会话点📎选图（或粘贴）发送后：机器人能基于图片内容给出文字回复；历史回显保留图片缩略图；重开会话/翻页图片仍在。
- 非 vision 模型发图给出清晰错误提示而非崩溃。
- 后端全量 pytest 绿（除 Phase 1 已记录的 `test_http_log_diagnostics` 预存 flaky 外无新增失败）；前端 vitest + `npm run typecheck` 绿；QQ 面零回归、旧库无破坏。

## 8. 假设与依赖

- 假设用户所选聊天模型是 vision-capable 且其 OpenAI 兼容端点接受 `image_url` content-part（vLLM 多模态、或托管 vision API）。不满足时走错误降级。
- 图片以内联 data URL 存 `bot_messages.images`，与头像 400k 上限同量级；单会话大量高清图会增大 DB 体积，属本期接受的取舍（磁盘 sidecar 为后续可选优化）。
- Phase 3（生图）待其 `/images/generations` 端点就绪后另立 spec；本期不预留其字段与代码路径。
- 后端 `reload=False`：实现完成后需重启后端生效；真机图片往返需用户端点就绪后手动冒烟。

## 9. 风险

- 历史里累积多张 data URL → 重喂 token 膨胀：靠「助手图不回喂、仅最近 40 条内用户图、单张压缩 + ≤4 张/条」控制在上限内；必要时后续加「仅最近 N 条带图」策略。
- 大 base64 走 SSE/JSON 落库的性能：单条消息图片体量受校验上限约束，风险可控。
- DOMPurify 是否放行 `<img data:*>`：本阶段用独立 `<img :src>` 直渲，不经 Markdown 管道，规避白名单问题。
- 非 vision 模型误配：无能力门控，靠错误提示引导用户换模型；若后续要精确提示，需给模型面板重新加回 `supports_image` 勾选（属额外范围，与 Phase 1 对 `supports_*` 的判断一致）。