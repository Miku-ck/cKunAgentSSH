# QQ 主动消息：召回 → 主动 → 降级寄存（spec 2026-10-30-qq-proactive-message）

## 背景

P3 主动性（`bot/proactive.py`）对非 local 渠道一律 `queued`，docstring 写的是
「QQ 协议上无法主动发」。这条判断在 API v2 下已经过时：不带 `msg_id` / `event_id`
就是主动消息，单聊另有 `is_wakeup` 互动召回。代价是 QQ 侧的「主动找人」只能等她
下次来时捎一句，主动性体验基本为零；同时 `queued` 不在抑制器统计口径里，
desire 一旦过闸会每 60s 堆一条寄存（模型调用与队列双重浪费）。

本 spec 把 **QQ 单聊**改成真主动（召回 → 主动 → 寄存降级），**群聊仍然只寄存**
（群内无端发言是打扰，且要群主单独授权），并把抑制器口径补齐到「约 12 小时一次」。

## 官方约束（2026-10-30 核对 bot.q.qq.com）

### 被动回复

| 场景 | 有效期 | 每条消息最多回复 |
| --- | --- | --- |
| 单聊 C2C | 60 分钟 | 4 次 |
| 群聊 | 5 分钟 | 5 次 |

### 主动消息（不带 msg_id）

| 场景 | Bot 维度 | 单关系维度 | 每日上限 |
| --- | --- | --- | --- |
| 单聊（企业/个人认证） | 10/qps | 20/qpm | 1000 条/用户 |
| 单聊（未认证） | 5/qps & 30/qpm | 20/qpm | 1000 条/用户 |
| 群聊（企业/个人认证） | 60/qpm | 20/qpm | 1000 条/群 |
| 群聊（未认证） | 30/qpm | 20/qpm | 1000 条/群 |

### 互动召回（`is_wakeup: true`，仅单聊）

用户主动对话后 30 天内，分 4 个周期各可下发 1 条：**当天 / 1-3 天 / 3-7 天 / 7-30 天**。

### 平台侧失败口径

| 错误码 | 含义 | 处置 |
| --- | --- | --- |
| 40034105 | 主动消息无权限（群主未开「主动推送消息能力」等） | 降级寄存 |
| 40054013 | 用户拒收消息 | 降级寄存 |
| 40054004 | 无好友关系 | 降级寄存 |
| 40034100 | 主动消息超频控 | 跳过，下一轮 |
| 40034122 | 召回已达该区间上限 | 退回普通主动消息 |
| 40034006 | 内容违规 | 跳过，下一轮 |

> botpy 1.2.1 只按 HTTP 状态抛异常（`ForbiddenError` / `ServerError` …），
> 业务码 400xxxxx 只出现在异常文案里 —— 分类只能「异常类型 + 文案」双轨判定。

## 设计

### 1. 投递分档（按渠道与会话形态）

| 渠道 / 形态 | 档位顺序 | 说明 |
| --- | --- | --- |
| local | 直投 | 不变（落 `bot_messages`） |
| QQ 单聊 `c2c:{openid}` | 召回 → 主动 → 寄存 | 召回的周期记账在本地，平台再做一次兜底 |
| QQ 群聊 `group:{openid}` | 寄存 | 群内主动发言要群主授权，默认不做 |

`_build` 之前先查「该会话已有未投递寄存」：有就本轮跳过，既不重复生成也不堆积。

### 2. 频率口径：约 12 小时一次

- `MIN_INTERVAL_H = 12.0`（所有关系节奏统一；节奏差异改由 `EXPECTED_GAP_H` 影响 desire）。
- `DAILY_QUOTA = 2`：12h 间隔在自然日内最多触发两次，配额只做天花板。
- 静音窗（23:30–08:30）与冷场冷却阶梯（48/96/168h）不变。

### 3. 抑制器收口

`last_proactive_at` / `proactive_sent_today` 的统计集合从 `action='send'`
扩到 `('send','push','queued')`；`list_unchecked_proactive` 扩到 `('send','push')`
（push 发出即写 `delivered_at`，2h 无人回话照常记冷场）。
`expired` 与 `skip` 不参与配额与间隔统计。

### 4. 寄存队列治理

- 已有未投递寄存 → 本轮不再寄存（`queued:pending`）。
- 过期寄存由调度器每轮把 `action` 改成 `expired`（保留审计行，不物理删）。
- 巡检窗口 7 天 → 30 天：召回法定窗口就是 30 天，7 天会把 7-30 天档整段砍掉。

### 5. 接口

```python
# bot/qq_channel.py（平台侧纯函数）
parse_conversation_key(key) -> ("group"|"c2c", openid) | None
classify_proactive_error(exc) -> ("permission"|"quota"|"content"|"wakeup_limit"|"network", detail)

# bot/qq_manager.py
await QQConnection.send_proactive(conversation_key, text, *, wakeup=False) -> (kind, detail)

# bot/proactive.py（纯函数）
wakeup_period(gap_hours) -> int | None      # 0/1/2/3，超 30 天 None
should_wakeup(conversation_key, gap_hours, used_periods) -> int | None

# database.py
last_user_message_at(conversation_id) -> str | None
has_wakeup_period(conversation_id, period) -> bool          # reason LIKE 'wakeup{n}:%'
expire_stale_proactive(now_iso) -> int
```

跨线程：botpy 的 aiohttp 会话绑在 `ckunbot-qq-*` 线程自己的 loop 上，
`send_proactive` 用 `asyncio.run_coroutine_threadsafe` 把协程送过去再等结果，
后端主 loop 不阻塞。

发送 payload 走最小化直调（`Route` + `{"msg_type": 0, "content": ...}`，召回再加
`is_wakeup`）：SDK 的 `post_c2c_message` 是 `payload = locals()`，会把
`embed/ark/media/msg_id` 等以 null 一并塞进 JSON，且 1.2.1 不支持 `is_wakeup`。
项目已有先例：表情包上传就是直调 `Route`。

### 6. 日志与 reason 口径

| 场景 | action | reason |
| --- | --- | --- |
| 本地直投 | `send` | `ok:{选题来源}` |
| 单聊主动成功 | `push` | `ok:{选题来源}` |
| 单聊召回成功 | `push` | `wakeup{n}:ok:{选题来源}` |
| 平台拒绝（权限/拒收） | `queued` | `queued:{tag}:{选题来源}` |
| 平台拒绝（频控/内容/网络） | `skip` | `push:{tag}` |
| 已有寄存 | `skip` | `queued:pending` |
| 过期寄存 | `expired` | 原 reason 不变 |

## 不做

- 群聊主动推送（需要群主在入群时授权，产品默认不做；未来放开只改一处判定）。
- Bot 维度 qps 账本（1000/天/人 的额度相对「12h 一条」完全不构成约束）。
- 前端状态位（「主动消息不可用：群主未开启」）—— reason 已落在 `proactive-log`，
  界面留到有需求时再加。

## 测试

- 纯函数：`gate` 12h 统一口径、日配额 2；`wakeup_period` 四档边界与 30 天封顶；
  `should_wakeup` 群聊不召回 / 本期已用不再召回。
- 平台片段：`parse_conversation_key` 两形态 + 脏 key；`classify_proactive_error`
  五类；`_post_proactive` 的三种 payload（c2c / group / wakeup）。
- 连接：`send_proactive` 离线、脏 key、跨线程真实 loop 发送、异常分类透传。
- 调度器：push 成功记 `push`；权限拒绝降级 `queued`；频控跳过；已有寄存不重复生成；
  单聊先试召回、召回上限退回普通主动。
- 数据库：三集合统计口径、`has_wakeup_period`、`expire_stale_proactive`。

## 实施回填

### 代码落位

| 文件 | 改动 |
| --- | --- |
| `backend/bot/proactive.py` | `gate` 去掉 `speed` 参数；`DAILY_QUOTA=2`、`MIN_INTERVAL_H=12.0`、`QUEUE_TTL_H`、`WAKEUP_PERIODS_H`；新增 `wakeup_period` / `should_wakeup` / `used_wakeup_periods` / `_proactive_sender`；`tick` 加过期清理与 pending 短路；新增 `_deliver` / `_push_qq` |
| `backend/bot/qq_manager.py` | 跟踪当前在线 `client`；新增 `send_proactive`（跨线程）+ `_post_proactive`（Route 直调） |
| `backend/bot/qq_channel.py` | `PROACTIVE_SEND_TIMEOUT_S`、`parse_conversation_key`、`classify_proactive_error` |
| `backend/database.py` | `add_bot_proactive_log` 增 `delivered_at`；统计集合扩到 `('send','push','queued')`；`list_proactive_targets` 增 `bot_channel_id`/`external_id`、窗口 30 天；新增 `last_user_message_at` / `has_wakeup_period` / `expire_stale_proactive` |
| `backend/tests/` | `test_bot_proactive.py`、`test_qq_channel.py`、`test_qq_manager.py` 补用例；`test_startup_critical_path.py` 更新 schema 指纹 |

### 与设计的偏差

1. **pending 短路不落日志**：设计里的 `skip / queued:pending` 在实现中去掉了 —— 这一分支每 60s 都会命中，写库会把账本淹掉；`fired` 也不计。
2. **`queued` 计入日配额与间隔**：按设计「三集合」口径落地后，寄存也会占住 12h 间隔 —— 这是刻意的：憋着一句话时反复生成新话没有意义（`tick` 另有 pending 短路兜底）。
3. **截断而非分段**：`send_proactive` 超长直接截断到 `QQ_TEXT_LIMIT`（每段都是一条独立主动消息，分段会放大频控风险）。
4. **`is_wakeup` 一律走单聊路由**：即便传入的是 `group:` 键，`wakeup=True` 时分派到 `/v2/users/{openid}/messages`（群聊无召回能力，由调用方保证不会这样用）。
5. **schema 指纹更新但未升 `_SCHEMA_VERSION`**：DDL 只改了注释（`action` 取值说明），列与索引未动；按 `test_startup_critical_path` 的约定选择「无害重构」路径。

### 验证

- `python -m pytest tests/ -q`：3798 passed（含新增 32 个用例）。
- `ruff check --select F,E9,E7`：7 个改动文件全部通过（仓库既有文件在更宽规则集下本就有存量告警，未顺手整改）。
- 新增覆盖：12h 统一间隔与日配额 2、召回四档边界（含 30 天封顶）、`should_wakeup` 群聊/同档去重、三集合统计口径、`has_wakeup_period`、`expire_stale_proactive`、`_push_qq` 五分支（群聊/离线/召回成功/召回已用/召回上限退回/权限降级/频控跳过）、`_deliver` 三种落账、`tick` 遇 pending 不重复生成、`_post_proactive` 三种 payload、`send_proactive` 离线/脏 key/空内容/截断/跨线程成功/跨线程异常分类/超时。

### 仍需真机确认（未在本轮验证）

1. botpy 1.2.1 的 `client.api._http.request` 接受最小 payload（不带 `msg_id`/`event_id`）—— 若平台要求更多字段，改 `_post_proactive` 一处即可。
2. `is_wakeup` 是否需要单独开通权限（官方文档未标明审批项）。
3. 群聊未授权时平台返回的真实错误码是否落在 `_PERMISSION_MARKERS` 里。
4. C2C 的被动回复窗口官方是 60 分钟，而 `QQ_REPLY_TIMEOUT=150`（秒）沿用的是群聊 5 分钟口径 —— 本轮未改，避免顺带放大其它行为。
