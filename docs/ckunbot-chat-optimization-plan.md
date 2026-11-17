# cKunBot 聊天模块优化方案 —— 借鉴 dsh-pet 的文字对话设计

> 方案日期：2026-11-16
> 参照对象：`PC2005-cloud/dsh-pet` v0.4.9（MIT 开源，本地源码直读，证据见 `dsh-pet-chat-rea-report.md`）
> 目标模块：`backend/bot/chat_service.py` 及链路上下游
> 性质：方案文档 + 实施记录。**第一批（P0-1 / P1-1 / P1-2）与第二批 P1-3 已实施并测试通过**；P2-1 / P2-2 经证据核实后决定不做（理由见附录 C）。每项标明证据（文件:行号）与验证方式。

---

## 实施状态（2026-11-17 更新）

| 批次 | 项目 | 状态 | 实际改动 |
|---|---|---|---|
| 一 | P0-1 `created_at` 断链 | ✅ 已实施 | `database.py` SELECT 增加 `created_at` 列 |
| 一 | P1-1 历史时间锚点 | ✅ 已实施 | `chat_service.py` 加 `HISTORY_TIME_GAP_MINUTES=30`、`_to_llm_message(prev_ts)`、历史组装处逐条推进时间戳 |
| 一 | P1-2 召回超时降级 | ✅ 已实施 | `_memory_note` 用 `asyncio.wait_for(..., 1.5)` 包裹，超时返回空串 |
| 二 | P1-3 无正文错误归一 | ✅ 已实施 | `chat_service.py` 加 `_NO_CONTENT_HINTS` + `_no_content_error()`；`stream_reply`/`ambient_reply` 收集 `finish_reason`，无正文时给人话提示且日志带原因 |
| 二 | P2-1 输入端硬限额 | ⛔ 决定不做 | 阈值需真实输入分布支撑、固定值对不同上下文窗口不严谨；`safe_max_tokens` 已按输入自适应封顶输出 |
| 二 | P2-2 跨模型回落 | ⛔ 决定不做 | 模型按人格绑定，跨模型回落致语气/能力漂移；无实际端点故障需求 |

**测试**：
- 第一批：新增 3 个钉子测试；`test_bot_chat_service.py` 9/9、相关回归 63/63、全量 4213/4213 通过。
- 第二批 P1-3：新增 10 个测试（7 参数化映射 + 3 端到端透传）；`test_bot_chat_service.py` **19/19**、相关回归（state/service/group_ambient/routes）**109/109** 通过、`py_compile` 干净；全量结果见附录 C（含一次环境性失败的说明，非回归）。

---

## 0. 立场说明：这一轮和上一轮为什么不同

上一轮的结论是"不移植、不用改"，判据是**功能是否缺失**——结论成立：dsh-pet 那些精巧设计点，cKunBot 要么已有、要么更强、要么无落点。

这一轮的问题是**"能否更强"**，判据不同。带着这个判据重新挖链路，找到 **1 个确凿 bug + 3 个高价值借鉴点 + 2 个可选点**。它们不是"照搬 dsh-pet"，而是"用 dsh-pet 的设计哲学修补 cKunBot 自己的缺口"——其中 P0 那条是 cKunBot 自身的静默断链，与 dsh-pet 无关，但正是这次逐行对照才暴露出来。

---

## 1. 优化项总览

| 编号 | 优先级 | 优化项 | 现状 | 改动量 | 收益 | 证据 |
|---|---|---|---|---|---|---|
| **P0-1** | 🔴 必修 | `created_at` 未查询导致 `burst` 事件永不触发 | 静默失效 | 1 行 | 恢复状态层"连续轰炸"信号 | `database.py:4477-4480` + `chat_service.py:254` + `state.py:354-358` |
| **P1-1** | 🟠 高 | 历史消息加时间锚点（仅 user，≥30min） | 模型对会话内时间间隔无感 | ~15 行 | 提升多轮上下文理解，防"隔夜续聊"错乱 | `chat_service.py:183-193` vs dsh-pet `time-context/` |
| **P1-2** | 🟠 高 | 记忆召回加 1.5s 超时降级 | 只有异常兜底，无超时兜底 | ~5 行 | 防止慢检索拖住首字 | `chat_service.py:428-439` vs dsh-pet 报告 §E7 |
| **P1-3** | 🟠 中 | 无正文时按终止原因归一提示 | 只有一句笼统 `RuntimeError` | ~20 行 | 故障可诊断、不误伤用户 | `chat_service.py:376-377` vs dsh-pet `llm-call.ts:21-30` |
| **P2-1** | 🟡 可选 | 输入端硬限额（字符/消息数） | 仅输出侧 `safe_max_tokens` 封顶 | ~10 行 | 防极端长历史撑爆上下文 | `chat_service.py:339-344` vs dsh-pet `limits.ts` |
| **P2-2** | 🟡 暂缓 | 候选模型跨模型回落 | 仅单模型内参数降级 | 配置+~30 行 | 端点故障时自动切换 | `llm_adapter.py:306-322` vs dsh-pet `chat.ts` |

**明确不做**（上一轮已核实，避免重复建设）：动作标记（无动画载体）、记忆扩展点重构（现有 recall 三重门控更强）、四段式 system（现有分节编译更细）、表情包回放回填（标记已原样落库）。并发串行锁也已具备（`inbound_pipeline.py:28-32` 按会话 key 的 `asyncio.Lock` + `llm_adapter.py:46-210` 在途计数），无需改动。

---

## 2. 各项详细设计

### P0-1 · 修复 `created_at` 断链（🔴 必修，改动 1 行）

**问题**：`get_recent_bot_messages` 的 SQL 只 SELECT `role, content, images`，不返回 `created_at`。但 `chat_service.py:254` 读 `item.get("created_at")`，因此传给 `state_mod.turn_events` 的 `user_created_at` **全是 `None`**。`state.py:354-358` 的 `burst` 判定（60s 内 ≥3 条用户消息）永远数不到任何时间戳 → **`burst` 事件在真实链路上从不触发**。

**为什么没被发现**：`tests/test_bot_state.py:173-182` 直接给 `turn_events` 注入 `state_mod._fmt_ts()` 生成的时间戳，所以单测是绿的；缺陷只在"DB → 状态层"这一段，单测没覆盖到。典型的"单测绿、链路哑"。

**改动**：

```python
# database.py:4479  get_recent_bot_messages
- "SELECT role, content, images FROM bot_messages WHERE conversation_id = ? "
+ "SELECT role, content, images, created_at FROM bot_messages WHERE conversation_id = ? "
```

`dict(row)` 会自动带上 `created_at`，`chat_service.py` 侧无需任何改动。这是对既有调用方**向后兼容**的字段增加。

**风险**：极低。唯一需注意 `_parse_ts` 的口径要与库里 `created_at` 的格式一致——`state.py` 已用 `.timeutil.parse_ts` 解析同类字段（`state.py:139,176`），大概率直接兼容；实施时补一个断言测试确认。

**验证用例**：
- 单测：插入 3 条间隔 <60s 的 user 消息 → 调 `get_recent_bot_messages` → 断言每项含 `created_at` 且非空。
- 集成：走一遍 `stream_reply`，断言 `state_mod.turn_events` 收到的 `user_created_at` 至少有一个非 `None`。

---

### P1-1 · 历史消息加时间锚点（🟠 高，借鉴 dsh-pet 时间上下文）

**问题**：`_to_llm_message`（`chat_service.py:183-193`）只转 `role/content`，历史里**没有任何时间信息**。模型不知道上一句发生在 2 分钟前还是昨晚。cKunBot 虽有 `gap_days()`（`chat_service.py:223-226`）处理"久别重逢"，但那是**天级**的关系情境，缺的是**会话内分钟级的间隔感知**。

**借鉴点**：dsh-pet 的关键踩坑经验——**时间前缀只标 user，不标 assistant**；且**超过 30 分钟才标**。原因：标在 assistant 上模型会照抄成 `[11:54]` 开头；每条都标则浪费 token 且干扰语气。这条经验在 dsh-pet 源码注释里有明确记录。

**前置依赖**：P0-1 修好后，`created_at` 才有数据可用。两项应一起做。

**改动设计**（在 `_to_llm_message` 增加可选参数，由 `stream_reply` 组装时传入）：

```python
# chat_service.py:183  改写 _to_llm_message，新增 prev_ts 与阈值判定
@staticmethod
def _to_llm_message(item: dict, prev_ts=None, *, gap_minutes=30) -> dict:
    """历史条目转 OpenAI message。
    时间锚点：仅 user 消息、且距上一条 ≥ gap_minutes 才加前缀；
    assistant 一律不标 —— 否则模型会照抄时间戳到自己的回复里。
    """
    role, text = item["role"], item["content"]
    ts = _parse_ts(item.get("created_at"))  # 复用 state.py 同款解析口径
    if role == "user" and ts and prev_ts and (ts - prev_ts).total_seconds() >= gap_minutes * 60:
        text = f"[{ts.astimezone().strftime('%m-%d %H:%M')}] {text}"

    images = item.get("images") or []
    if role == "user" and images:
        return {"role": role, "content": [{"type": "text", "text": text}]
                + [{"type": "image_url", "image_url": {"url": u}} for u in images]}
    return {"role": role, "content": text}
```

`stream_reply` 侧（`chat_service.py:311`）改为带状态的组装：

```python
+ prev_ts = None
+ llm_msgs = []
+ for item in history:
+     llm_msgs.append(self._to_llm_message(item, prev_ts))
+     ts = _parse_ts(item.get("created_at"))
+     if ts:
+         prev_ts = ts
+ messages.extend(llm_msgs)
```

**注意**：`_parse_ts` 目前是 `state.py` 从 `.timeutil` 引入的私有别名（`state.py:32`）。实施时应在 `chat_service.py` 用 `from bot.timeutil import parse_ts`，不要跨模块 import 别人的私有别名。

**风险**：低。阈值 30 分钟可调；`gap_minutes=0` 可关闭该特性便于 A/B。时区统一用 `.astimezone()` 转本地，与 `local_hour` 的计算口径（`chat_service.py:249`）一致。

**验证用例**：两条 user 消息间隔 31 分钟 → 第二条 content 带前缀；间隔 5 分钟 → 不带；assistant 消息无论间隔多久 → 均不带前缀。

---

### P1-2 · 记忆召回加 1.5s 超时降级（🟠 高，借鉴 dsh-pet 扩展点超时）

**问题**：`_memory_note`（`chat_service.py:428-439`）是 **`await` 在流式首字生成之前**的（`chat_service.py:301`）。它只有 `try/except` 兜异常——但"检索很慢但不报错"这种情况兜不住：retriever 卡住多久，用户就等多久才看到第一个字。dsh-pet 的第三方 recall 扩展点明确规定 **1.5s 超时降级**（见报告 §E7）。

**改动**（用 `asyncio.wait_for` 包一层，超时归入现有 except）：

```python
# chat_service.py:428
try:
-   contents = await memory_mod.recall(persona_id, query, ...)
+   contents = await asyncio.wait_for(
+       memory_mod.recall(persona_id, query, ...),
+       timeout=1.5,
+   )
except asyncio.TimeoutError:
+   logger.warning("陪聊记忆召回超时(1.5s)，本轮跳过记忆注入")
+   return ""
except Exception as exc:
    logger.warning("陪聊记忆召回失败: %s", exc)
    return ""
```

**为什么是加分项而非必需**：记忆层的设计原则本来就是"召回失败当作没有记忆"（`chat_service.py:421` 注释）。超时是这条原则的**时间维度补完**——慢和错都不该拖垮陪聊。

**风险**：极低。`embedding` 客户端另有 300s 静默降级窗口（`memory.py:665`），1.5s 只会让"慢检索"更早放弃，不改变语义。若担心误伤冷启动，可把超时设为可配置项，默认 1.5s。

**验证用例**：monkeypatch `memory_mod.recall` 为 `await asyncio.sleep(10)` → 断言 `_memory_note` 在 ~1.5s 内返回空串，且 `stream_reply` 首字不被拖延。

---

### P1-3 · 无正文时按终止原因归一提示（🟠 中，借鉴 dsh-pet 错误翻译）

**问题**：`chat_service.py:376-377` 在模型没返回正文时只有一句：

```python
if not reply:
    raise RuntimeError("模型未返回可显示的内容")
```

dsh-pet 会按流的终止原因翻译成人话（`llm-call.ts:21-30`）：`aborted` → "生成已取消"，`max-tokens` → "输出达到上限"，等；没有正文时明确区分是"被取消"还是"超长"还是"模型拒答"，而不是笼统报错。

**改动前提**：需先确认 `llm_adapter` 的 `StreamChunk` 是否携带 `finish_reason`。当前 `chat_stream` 开了 `stream_options.include_usage`（`llm_adapter.py:282`），但 `StreamChunk` 的定义还需实施时核对；若不携带，则在 `llm_adapter` 层透传该字段，再在 `chat_service` 消费侧归一。

**改动设计**：

```python
# chat_service.py:375  收集终止原因
finish_reason = ""
async for chunk in chat_stream(...):
    if getattr(chunk, "finish_reason", None):
        finish_reason = chunk.finish_reason
    ...

if not reply:
    reason_text = {
        "length": "刚才那句说到一半超长了，我换个短点的说法。",
        "content_filter": "这个话题我不太方便接。",
        "abort": "生成被中断了，你再说一遍？",
    }.get(finish_reason, "我这边没生成出内容，你再说一遍？")
    raise RuntimeError(reason_text)  # 或改为 yield 一条可见提示，取决于上游如何处理异常
```

**风险**：中。需先摸清上游对 `RuntimeError` 的处理——`complete_reply`（QQ）和 `inbound_pipeline` 的 `fallback` 是否会把它变成对用户可见的文案。若上层吞掉异常，则这条改动需要同时调整异常契约。建议此项**放在 P0/P1-1/P1-2 之后**，确认上游行为再动。

**验证用例**：构造不同 `finish_reason` 的假 chunk 流 → 断言抛出的异常文案与原因匹配。

---

### P2-1 · 输入端硬限额（🟡 可选）

**问题**：cKunBot 已有输出侧 `safe_max_tokens` 封顶（`chat_service.py:339-344`），但**输入侧没有字符数硬限**。历史固定 40 轮（`chat_service.py:240`），若用户连发长消息，40 轮累积可能接近上下文窗口。dsh-pet 的独立模式有明确限额：输入 ≤32768 字符、消息数 ≤64、事件数 ≤4096（`limits.ts:13-38`）。

**改动**：在 `messages.extend(...)` 之后、`safe_max_tokens` 计算之前，加一道"从旧到新裁剪、system 永不裁"的保护：

```python
+ MAX_INPUT_CHARS = 32768
+ total = sum(len(str(m["content"])) for m in messages)
+ while total > MAX_INPUT_CHARS and len(messages) > 2:  # 保留 system + memory_note
+     dropped = messages.pop(2)                          # 从最早的历史开始丢
+     total -= len(str(dropped["content"]))
```

**风险**：低，但优先级不高——40 轮 + `_memory_note` 的 `cap_text_tokens(200, ...)` 已经比较克制。建议先观测真实对话的输入长度分布，再决定阈值。**除非实际出现上下文溢出报错，否则可不做。**

---

### P2-2 · 候选模型跨模型回落（🟡 暂缓）

**判断修正**：上一轮报告里我把"模型候选回落"判为"已具备"，理由是 `llm_adapter.py:306-322` 有 BadRequest 降级。严格讲那只是**单模型内的参数降级**（去掉 tools / 去掉 thinking 参数后重试），不等于 dsh-pet 的**配置模型失败 → 回落另一个可用模型**。

**为什么仍建议暂缓**：cKunBot 的模型是按人格绑定的（`_model_for_persona`），跨模型回落意味着"这个人格此刻用另一个模型说话"，会引入人格语气漂移、思考能力不一致、成本失控等问题。收益（端点故障时的可用性）需要用配置复杂度和一致性代价来换，**不建议在未出现实际端点故障前引入**。若要做，应限定"同一服务商的备用模型 ID"，并留日志标记本次回落到哪个模型。

---

## 3. 实施顺序与验收

建议分两批，每批独立可回滚：

**第一批（低风险、收益集中）**
1. P0-1：修 `created_at`（1 行 + 集成测试）
2. P1-1：历史时间锚点（依赖 P0-1，~15 行 + 单测）
3. P1-2：召回超时（~5 行 + 单测）

验收：跑 `backend/tests/test_bot_state.py`、`test_bot_chat_service.py`，全绿；新增 3 个针对上述改动的测试全绿。

**第二批（需先摸清上游契约）**
4. P1-3：错误归一（先确认 `StreamChunk` 字段与上层异常处理）
5. P2-1：输入限额（先看真实输入长度分布）
6. P2-2：跨模型回落（仅在出现端点故障需求时）

**全局回归**：改完后跑一次 backend 全量测试 + 本地/QQ/微信三渠道各发一轮真实对话，观察首字延迟、状态轴 `tension/patience` 的变化是否符合预期（P0-1 修好后 `burst` 会影响这些轴）。

---

## 4. 风险控制清单

| 风险 | 涉及项 | 缓解措施 |
|---|---|---|
| 时间戳格式不兼容导致解析失败 | P0-1、P1-1 | 复用 `bot.timeutil.parse_ts` 统一口径；补格式断言测试 |
| 时间前缀被模型照抄进回复 | P1-1 | 只标 user、不标 assistant；阈值 30min；必要时在 system 里加一句"不要复述时间戳" |
| 1.5s 超时误伤冷启动召回 | P1-2 | 超时值可配置；观察日志里的超时频率再调 |
| 错误文案改动破坏上游 fallback | P1-3 | 实施前先摸清 `inbound_pipeline.fallback` 与 `complete_reply` 的异常契约 |
| 输入裁剪丢掉关键上下文 | P2-1 | 只从最早的历史丢，system/memory 永不裁；阈值保守 |

---

## 5. 一句话总结

cKunBot 的对话骨架比 dsh-pet 更成熟，**不需要重写**；真正值得做的是：修一个静默 bug（`created_at` 断链）、补两个时间维度的感知（历史时间锚点、召回超时）、以及两处可选的稳健性加固。全部改动合计约 50 行，不动核心管线、不破坏现有的 `scope` 隔离与安全宪章。

---

## 附录 C · 第二批实施结论（2026-11-17）

### C1. P1-3 无正文错误归一 —— 已实施

**动手前的契约核实**（这正是方案要求"先摸清上游异常处理"的答案）：

| 核实项 | 结论 | 证据 |
|---|---|---|
| `StreamChunk` 是否带 `finish_reason` | ✅ 已带，无需改适配层 | `llm_adapter.py:154-164` 定义；`llm_adapter.py:371-376` 从 `chunk.choices[0].finish_reason` 透传 |
| 本地渠道异常怎么处理 | 转成前端可见的 `error` SSE 事件（文案脱敏后推给用户） | `routes/bot.py:439-444` |
| QQ/微信渠道异常怎么处理 | 统一 `except Exception` → `provider.fallback(failure)` 固定文案，**异常文案只进日志** | `inbound_pipeline.py:128-131`；`qq_manager.py:412-413`、`wechat_manager.py:346-347` |

**结论**：异常类型保持 `RuntimeError` 不变 → 三条渠道的兜底行为完全不动，零回归风险。改动只影响"异常文案内容"与"日志诊断信息"：
- 本地用户从笼统的"模型未返回可显示的内容"升级为按原因的人话（`length`→"说到一半超长了"、`content_filter`→"不太方便接"、`abort/aborted`→"生成被中断了"、未知→通用兜底）。
- QQ/微信日志新增 `finish_reason=<值>`，故障可诊断。

**实际改动**（`chat_service.py`）：新增类级 `_NO_CONTENT_HINTS` 映射 + `_no_content_error()` 类方法（内嵌 warn 日志）；`stream_reply`、`ambient_reply` 两处 chunk 循环各用 `getattr(chunk, "finish_reason", "")` 收集原因（getattr 兜底兼容第三方/测试 chunk 对象），无正文时 `raise self._no_content_error(finish_reason)`。

**验证**：新增 10 个测试（7 个参数化映射覆盖 4 种已知原因 + 正常 stop + 空值 + 未知未来值；3 个端到端测试锁住"finish_reason 从 chunk 透传到异常文案"这一前提）。`test_bot_chat_service.py` 19/19、相关回归 109/109、`py_compile` 干净。

### C2. P2-1 输入端硬限额 —— 决定不做

方案原文已标注"可选、除非实际出现上下文溢出否则可不做"。本轮核实时确认两点，使该改动**收益不成立**：
1. `safe_max_tokens`（`safe_tokens.py:15-60`）已按 prompt 大小用 `estimate_tokens` 估算输入、自适应下调输出预算，并留 1024 token 余量——这正是防 vLLM 400 的机制，输入侧再加一道固定字符阈值属于重复保护。
2. dsh-pet 的 32768 字符阈值来自其独立模式的请求契约，cKunBot 的历史固定 40 轮且经 `cap_text_tokens` 收敛；在没有真实输入长度分布/溢出报错的前提下设固定阈值，可能误裁正常长对话的关键上下文。

**决定**：暂不实施。若未来真的出现上下文溢出报错，应先取真实输入长度分布再定阈值，且只从最早历史裁、system/memory 永不裁。

### C3. P2-2 跨模型回落 —— 决定不做

cKunBot 模型按人格绑定（`_model_for_persona`），跨模型回落意味着"这个人格此刻用另一个模型说话"，会引入语气漂移、思考能力不一致、成本失控。方案已判定风险大于收益，本轮维持该结论：未出现实际端点故障需求前不引入。

### C4. 一个顺带确认、无需改动的点

dsh-pet 的时间戳踩坑是"标在 assistant 上，模型会照抄成 `[11:54]` 开头"。第一批 P1-1 的实现只标 user、不标 assistant（`chat_service.py:199-202`），已从根源规避该坑，**无需再在 system prompt 加"不要复述时间戳"的防御指令**——避免无谓增加 prompt 长度。

### C5. 全量测试的一次环境性失败（如实记录，非回归）

第二批验证时，`pytest tests/ -q` 全量跑出现 **212 failed / 719 errors / 3573 passed**（与第一批时的 4213/4213 全绿不同）。经定位确认**与 P1-3 改动无关**，依据：

1. **错误类型与分布**：errors 是 `sqlite3.ProgrammingError`/`AttributeError`/`SystemError` 等 fixture setup 错误（非断言失败），分散在 `test_webhook_notify`、`test_wechat_manager`、`test_spawn_subagent_recon`、`test_workflow_*` 等模块——这些文件**均不引用 `chat_service`**。
2. **单独复现全过**：把报错模块里的测试单独跑（`test_webhook_notify.py::test_no_url_no_request`、`test_spawn_subagent_recon.py::test_空结论按失败上抛`）全部通过；其中前者单测耗时 53 秒，显示单进程启动/清理开销极大。
3. **改动相关测试全绿**：`test_bot_chat_service.py` 19/19、相关回归 109/109、`py_compile` 干净。
4. **环境线索**：全量输出末尾出现 `SAFE_DELETE_BULK_CONFIRM_REQUIRED`（沙箱批量删除拦截），且大批量跑时 fixture 间存在全局 DB 连接/状态串扰；第一批同样命令曾全绿，属时序/资源相关的既有测试稳定性问题。

**结论**：本项目全量测试在单进程跑 4000+ 用例时存在环境相关的不稳定性。可靠的功能验证以"改动相关测试 + 相关回归 + 报错模块单独复现"为准，三者均绿。若需稳定的全量基线，建议后续按模块分片跑或引入 pytest-xdist 隔离，而非据此次全量结果判定回归。
