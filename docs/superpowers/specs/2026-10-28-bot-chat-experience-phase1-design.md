# cKunBot 聊天体验优化 · Phase 1 设计（人设与回复体验内核）

- 日期：2026-10-28
- 范围：仅 Phase 1。Phase 2（看图回复）/Phase 3（生成图片回复）各走独立 spec→plan→实现。
- 上游讨论：本轮从「稳健性/幂等（方案 1）」切换到「聊天体验更像人」；方案 1 未落 spec，作为未完成分支搁置（不丢弃，可续）。

## 1. Summary

给 cKunBot（本地陪聊 + QQ 官方机器人两条投递面）落地一组人格级、渠道感知的回复体验能力：
系统提示词增强、回复风格可控（简洁/适中/长文）、开场白、thinking 开关、表情/Markdown 排版。
核心约束：**同一套人格设置在两面各自按平台上限尽力落地**——本地无限制（流式/思考/Markdown），QQ 受官方机器人「被动回复=单条纯文本」限制（走降级）。

## 2. Goals / Non-goals

Goals
- 人格化、可控长度、渠道适配的系统提示词。
- 人格级 `reply_style`（concise|moderate|long）与 `enable_thinking` 两个新设置，端到端贯通（DB→API→前端控件→生成）。
- 开场白 `greeting`：本地新会话直发；QQ 首回合前置，规避主动消息配额。
- thinking 仅在模型 `supports_thinking` 时启用；本地以 SSE `reasoning` 事件流式展示，QQ 只取最终 content。
- 本地复用现有 Markdown 组件渲染表情/排版；QQ 侧提示词强制纯文本。

Non-goals（本阶段不做）
- 视觉输入（看图回复）→ Phase 2。
- 图像生成回复 → Phase 3（且当前仓库无图像生成端点，需外部端点，标记为可行性 spike）。
- 消息幂等/重连不丢不重 → 另一分支（方案 1），不在本 spec。
- 修改主 Agent 的提示词增强逻辑（记忆「运维智能体输入框禁用通用提示词增强」）——本 spec 只动 cKunBot 人格 system prompt，二者无关。

## 3. 平台能力事实（已核对）

- `models_config` 有 `supports_thinking` / `supports_image` / `supports_tool_calling`、`reasoning_tokens`、`max_output_tokens`。**注意（自查修正）**：`ModelsPanel.vue` 已撤掉能力勾选 UI（注释：三列「运行时无任何消费者」），库里的 `supports_*` 只剩探测/seed 的历史值且用户无法再改。**因此不以 `supports_thinking` 做门控**——沿用主 Agent 先例（按配置发 `enable_thinking`，不依赖该标记），否则手动添加的模型永远开不了思考。
- `llm_adapter.chat_stream` 已支持 `enable_thinking` + `reasoning_max_tokens`；`StreamChunk` 携带 `.reasoning` 与 `.content`。
- 本地 `/bot/chat` 走 SSE，`stream_reply` 目前仅透 `chunk.content`，**丢弃 reasoning**。
- QQ 走 `complete_reply`（收集全文）→ 单次 `message.reply(msg_type=0)` 纯文本；被动回复不可流式/编辑，额外多条/图片需主动消息（配额+审核）。
- 全仓无图像生成端点；无把消息构建成 vision content parts 的代码。
- `bot_messages` 无 `external_msg_id` 唯一索引（属方案 1 议题，本阶段不动）。

## 4. Phase 1 详细设计

### 4.1 数据层（backend/database.py）

`bot_personas` 增两列，走 `_migrate_column_if_missing` 幂等迁移：
- `reply_style TEXT NOT NULL DEFAULT 'moderate'`（取值 `concise|moderate|long`）
- `enable_thinking INTEGER NOT NULL DEFAULT 0`

`bot_messages` 增一列（用于本地思考内容随历史回显，参照主 `messages.reasoning` 先例）：
- `reasoning TEXT`（默认 NULL）

内置人格 `greeting` 已存在但闲置，本阶段开始使用；seed 的 `reply_style='moderate'`、`enable_thinking=0`。
`create_bot_persona` / `update_bot_persona` 允许写入这两列；读接口（`get_bot_persona`/`list_bot_personas`）返回这两列。

### 4.2 生成层（backend/bot/chat_service.py）

`stream_reply` 形参扩展：
```
async def stream_reply(self, conversation_id, content, external_msg_id=None, *, channel="local") -> AsyncGenerator[tuple[str,str], None]:
```
- 产出由 `str` 改为 `(kind, text)`，`kind ∈ {"content","reasoning"}`，便于路由层分事件；`complete_reply`（QQ 用）聚合仅取 `content`。
- `channel="qq"` 时不产出 `reasoning`（内部用完即弃），且 system prompt 走纯文本约束分支。
- `enable_thinking` 取 `persona.enable_thinking`（用户意图，**不 gate `supports_thinking`**，理由见第 3 节）；透传 `chat_stream(enable_thinking=..., reasoning_max_tokens=model.reasoning_tokens)`。思考降级靠「是否收到 reasoning chunk」自然判定，不靠标记。

`_system_prompt(persona, channel)` 重写为结构化分节（用空行连接）：
1. 身份：`你叫{name}，是用户的个人机器人伙伴。`
2. 性格与说话方式：`persona.personality`（有则加）。
3. 背景设定：`persona.background`（有则加）。
4. **长度与语气风格指令**（按 `reply_style`）：
   - concise：「一两句话、口语化、点到为止，别铺陈。」
   - moderate：「以简洁自然为主，确有内容再展开。」
   - long：「可分点、有条理地展开细节。」
5. **排版规则（渠道化）**：
   - local：「可用 Markdown 排版与适量表情。」
   - qq：「只用纯文本与少量表情；禁止表格/链接/代码块/标题语法。」
6. 安全底线（保留原句）：不执行命令/不调工具/不声称是人类。

长度预算（硬上限，与提示词软指令配合）：
- 目标上限 `style_cap = {concise:512, moderate:model.max_output_tokens, long:min(model.max_tokens or 4096, model.max_output_tokens*4)}`。
- 仍走 `safe_max_tokens(style_cap, prompt_text, max_output_tokens=model.max_output_tokens)` 按上下文窗口封顶（沿用现口径，防 vLLM 400）。

`assistant` 落库：把本轮 `content` 与（本地且开启思考时）聚合的 `reasoning` 一并写入 `bot_messages`。

### 4.3 开场白

- 本地：`create_bot_conversation(persona_id, channel='local')` 时若 `persona.greeting` 非空，插入一条 `role='assistant'` 消息（不走 LLM），API 返回会话+首条消息；前端新建对话即显示 greeting。
  - 仅在**新会话创建**时插入，避免每次打开重复；已有会话不补发。
- QQ：`qq_channel._handle` 判断该 conversation 是否**首回合**（历史里除 greeting 外无往轮 user/assistant 往来）；是则把回复文本前置为 `f"{greeting}\n\n{answer}"`，否则原样。不加列，用「会话消息计数」判定首回合。

### 4.4 SSE 契约（backend/routes/bot.py）

`/bot/chat` events()：消费 `(kind,text)`，按 kind 发：
- `{type:'content', content}`（不变，向后兼容）
- `{type:'reasoning', content}`（新增）
- `{type:'done', message}` / `{type:'error', detail}` 不变。
- `stream_reply(...)` 调用处传 `channel="local"`。

### 4.5 前端

- `src/api/bot.ts`：persona 类型加 `replyStyle`、`enableThinking`；createPersona/updatePersona payload 带上；`onSSEEvent` 分发新增 `reasoning` 分支（保留现有 content/done/error）。
- `CkunBotView.vue` 人格编辑弹窗：新增「回复风格」下拉（简洁/适中/长文）与「思考」开关（纯人格级布尔，**不按 `supports_thinking` 置灰**；旁边文案提示「需模型支持，模型不回思考则不显示思考块」）。无需从模型 store 取能力字段。
- 本地消息气泡：复用现有 Markdown 渲染组件（记忆「Markdown 交互逻辑复用」），不新造渲染器；`reasoning` 以可折叠「思考过程」区块显示在正式回复上方。

## 5. 错误处理

- thinking 开启但模型不回 reasoning：照发 `enable_thinking=True`（与主 Agent 一致），模型无 reasoning chunk 时思考块自然不显示，content 正常产出，不报错。
- `reply_style` 非法值：Pydantic 用 `Literal['concise','moderate','long']` 校验，422。
- greeting 前置不改变错误兜底：QQ 失败仍回原兜底句。
- 旧库缺列：迁移补默认值，读取路径对缺字段容错（`.get(key, default)`）。

## 6. 测试（TDD，红→绿）

后端：
- 风格→prompt：三种 `reply_style` 生成的 system prompt 分别含对应长度指令子串；QQ 渠道含「禁止表格/链接」且不含「Markdown 排版」，local 相反。
- thinking 透传：persona.enable_thinking=1 → 下发 kwargs `enable_thinking=True`（假 chat_stream 断言，且不依赖 supports_thinking 值）；persona.enable_thinking=0 → `enable_thinking=False`。
- reasoning 展示降级：假流产出 reasoning+content → 本地 SSE 依次出现 `reasoning`、`content`；假流只有 content → 无 `reasoning` 事件、气泡不显示思考块；QQ `complete_reply` 恒仅得 content。
- 开场白：本地新建会话插入 greeting assistant 消息且只一次；QQ 首回合回复前置 greeting、次回合不前置。
- 迁移：旧库无两列时 init 补齐默认值，`reply_style='moderate'`、`enable_thinking=0`。
- 校验：`reply_style` 非法值 → 422。

前端守卫（vitest）：
- 人格弹窗含「回复风格」下拉与「思考」开关的模板标记。
- `bot.ts` payload 含 replyStyle/enableThinking，SSE 分发含 reasoning。

## 7. 成功标准

- 用户在人格编辑器设 `reply_style`/`enable_thinking`/`greeting` 后：本地新会话即见开场白；回复按风格长短与人格语气变化；支持思考的模型能显示可折叠思考过程；QQ 端回复为纯文本、首条带开场白、长度受控不超发。
- 全部后端 pytest 绿 + 前端 vitest/typecheck 绿；旧库无破坏。

## 8. 假设与依赖

- 假设本地聊天使用现有 Markdown 组件即可渲染表情/排版（无需新增图像输出）。
- Phase 3 需用户提供 OpenAI 兼容 `/images/generations` 端点后才立项；否则挂起为可行性 spike。
- 本阶段不改 `external_msg_id` 去重与 `_locks`（属方案 1）。
- 后端 `reload=False`：实现完成后需重启后端生效。

## 9. 风险

- 模型把 Markdown/表情带进 QQ 纯文本 → 靠渠道化 prompt + 兜底（QQ 回复前剥离明显 Markdown 标记，作为 defense-in-depth，实现阶段决定是否需要）。
- `long` 风格顶到上下文上限 → 已由 `safe_max_tokens` 兜底，风险可控。
- thinking 关闭时 `reasoning` 列多存 NULL，无副作用。
- `supports_thinking` 是无人维护的历史值，故不作门控（已在第 3/4.2/5 节处理）；若未来要精确禁用非思考模型，需先给模型面板重新加回能力勾选，属额外范围。
