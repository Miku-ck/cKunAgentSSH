# QQ 群聊：@ 后 5 分钟接话 + 每群独立 12h 主动（spec 2026-10-30-qq-group-ambient）

## 背景

现状（spec 2026-10-30-qq-proactive-message）：QQ 单聊已能真主动（召回 → 主动 →
寄存），**群聊只寄存**——群里只有被 @ 才会有反应，被 @ 那次之后群里继续聊，
机器人完全听不见也接不上。

产品预期：被 @ 过的群进入一段「可参与期」，期间群里不用再 @ 也能接话；
安静下来后，机器人按自己的节奏（约 12 小时一次）主动在群里说一句。

## 平台约束（2026-10-30 核对 bot.q.qq.com）

| 能力 | 前提 | 协议事实 |
| --- | --- | --- |
| 收到群里不 @ 的消息 | 群主在手机 QQ「设置权限」里开**接收全量消息能力** | 事件 `GROUP_MESSAGE_CREATE`，Intent `1<<25`（= 我们已开的 `public_messages`）；字段与 `GROUP_AT_MESSAGE_CREATE` 完全一致 |
| 在群里说话（被动） | 无额外授权 | 用某条消息的 `msg_id` 回复：**5 分钟 / 每条最多 5 次** |
| 在群里主动说话 | 群主开**主动推送消息能力** | 不带 `msg_id` 即主动消息；未开通回 `40034105`；频控 60/qpm(Bot)、20/qpm(关系)、1000 条/天/群 |

> botpy 1.2.1 只实现了 `parse_group_at_message_create`，没有 `group_message_create`。
> `client._connection.parser` 是一个 `event → parser` 的 dict（构造时扫 `parse_*`
> 方法生成），启动后往里补一项即可，不必改依赖。

### 做不到的

「@ 一次之后永久自由聊」不成立：被动回复窗口只有 5 分钟 / 5 次，窗口外每一条
都算主动消息，受群主授权与频控约束。所以口径是**@ 过 = 进入一小段可参与期**，
不是永久的免 @ 通行证。

## 设计

### 1. 可参与期（@ 后 5 分钟）

进程内状态（`bot/group_ambient.py`，不落库）：

| 状态 | 含义 | 常量 |
| --- | --- | --- |
| `at_until` | 最近一次 @ 的时刻 + 5 分钟 = 平台被动回复窗口 | `AT_WINDOW_MIN = 5.0` |
| `last_spoken_at` | 上一次在群里开口的时刻，用于节流 | `REPLY_GAP_S = 60.0` |
| `lines` | 最近群消息（含机器人自己说的），供接话时当上下文 | `CONTEXT_TTL_MIN = 10.0`、`CONTEXT_MAX_LINES = 20` |

判定顺序（`GROUP_MESSAGE_CREATE` 进来时）：

1. 消息里带 `<@…>`（@ 了任何人，含机器人）→ 只记上下文，不接话。机器人被 @ 的
   那条会由 `GROUP_AT_MESSAGE_CREATE` 处理，这里再回就是双份。
2. 可参与期已关 → 只记上下文，丢弃。
3. 距上次开口不足 `REPLY_GAP_S` → 只记上下文。
4. 其余 → 接话（用这条消息自己的 `msg_id` 走被动回复，合法且免费）。

### 2. 副作用边界（用户明确要求：群里的话只作临时上下文）

环境消息与环境接话**不进**下面任何一条通路：

| 通路 | 环境消息 | 说明 |
| --- | --- | --- |
| 群友发言落 `bot_messages` | ✗ | 多人混在一个会话里，落库等于伪造「用户说过」 |
| 记忆抽取（`remember`） | ✗ | 否则 A 的偏好会变成对 B 说的「我记得你……」，张冠李戴 |
| 关系记账（`record_turn`） | ✗ | 群里旁人说话不该给「你和用户」升温 |
| 状态轴更新（`state.turn_events`） | ✗ | 同上 |
| 机器人发言落 `bot_messages` | ✓ | 本地会话要能看到它在群里说了什么（审计与体验都需要） |

### 3. 群主动：每群独立 12h

| 项 | 单聊 / local | 群聊 |
| --- | --- | --- |
| 最小间隔 | 人格级 12h | **会话级 12h**（各群互不挤占） |
| 日配额 | 人格级 2 条 | **会话级 2 条** |
| 能否主动发 | 召回 → 主动 → 寄存 | 直接主动（无召回能力），无权限 → 寄存 |

`_push_qq` 的 group 分支从「一律 fallback」改为真发送：
`ok → push`、`permission/invalid/offline → queued`、`quota/content/network → skip`。

**授权无法探测**：群主没开「主动推送」，平台只会在发送时回 `40034105`。因此每 12h
最多白试一次，失败即按 `queued:permission` 寄存，下次被 @ 时以「对了，…」捎带。

### 4. 接口

```python
# bot/group_ambient.py（纯逻辑 + 进程内注册表）
note_mention(channel_id, group_openid, now=None) -> None
note_line(channel_id, group_openid, speaker, text, now=None) -> None
should_reply(channel_id, group_openid, now=None) -> bool
mark_spoken(channel_id, group_openid, now=None) -> None
context_lines(channel_id, group_openid, now=None) -> list[tuple[str, str]]
reset_for_tests() -> None

# bot/provider.py
InboundMessage.ambient: bool = False   # 环境消息（不落用户消息、不抽记忆、不记账）

# bot/inbound_pipeline.py
InboundPipeline.handle(...)             # ambient=True 时走 _handle_ambient
InboundPipeline._handle_ambient(...)

# bot/chat_service.py
BotChatService.ambient_reply(conversation_id, context, *, channel="qq") -> str

# database.py
last_proactive_at_for_conversation(conversation_id) -> str | None
proactive_sent_today_for_conversation(conversation_id, day_start) -> int
```

跨线程注意事项：`GROUP_MESSAGE_CREATE` 的 parser 与 `@` 消息一样跑在
`ckunbot-qq-*` 线程的 loop 上，接话生成会 await 模型（数十秒）——**不能阻塞收包**。
adapter 用 `asyncio.create_task` 把接话丢出去，收包立即返回。

### 5. 配置与开关

不做额外开关：可参与期只由「被 @ 过 + 5 分钟」决定。群主不开全量消息能力时，
机器人根本收不到非 @ 消息，行为退化成现状（只有 @ 有反应），无需代码分支。

## 不做

- 群友发言进记忆/关系（见 §2）。
- 群聊互动召回（平台只对单聊提供 `is_wakeup`）。
- 每条非 @ 消息都接话：节流 60s，且受 5 分钟窗口限制。
- 「主动推送/接收全量」的授权状态展示：平台不提供查询接口，只能靠发送失败反推。

## 测试

- 纯函数：窗口内/外、60s 节流、上下文 TTL 与条数上限、@ 打点续窗口。
- 平台片段：`_parse_group_message` 注入后能 dispatch 到 `on_group_message_create`；
  非 @ 消息在窗口外不上抛、窗口内以 `ambient=True` 上抛；含 `<@` 的消息不接话。
- 管线：`ambient=True` 时 `add_bot_message` 只写 assistant、不调 `record_turn` /
  `remember` / `state.update_for_turn`；发送走 `provider.outbound_parts`。
- 调度器：群聊 push 成功记 `push`、无权限降级 `queued`；群配额 12h 会话级独立
  （A 群刚发过不影响 B 群）。

## 实施回填

### 代码落位

| 位置 | 内容 |
| --- | --- |
| `backend/bot/group_ambient.py`（新） | 可参与期注册表：`note_mention` / `note_line` / `is_open` / `should_reply` / `mark_spoken` / `seen_event` / `context_lines` / `reset_for_tests`，加 `threading.Lock`（多个机器人共用一张表） |
| `backend/bot/qq_channel.py` | `AmbientGroupMessage`（补 botpy `GroupMessage` 透不出的 `author.username` / `author.bot`）；`handle_group` 里 `note_mention` 续窗口 + `mark_spoken` 起节流；`handle_group_message` + `_emit_ambient`（任务强引用）；`_emit(..., ambient=False)` 透传给 `InboundMessage` |
| `backend/bot/qq_manager.py` | `_install_group_message_parser(client)` 往 `client._connection.state.parsers` 补 `group_message_create`；`Client.on_ready` 里注册，`on_group_message_create` 交给 adapter；`send_proactive` 要求平台返回 dict 回执 |
| `backend/bot/provider.py` | `InboundMessage.ambient: bool = False` |
| `backend/bot/inbound_pipeline.py` | `handle()` 按 `ambient` 分流（锁被占则作废，不排队）；`_handle_ambient` 只取上下文 → 生成（`AMBIENT_REPLY_BUDGET_S`）→ 投递，失败/超时静默；`_deliver(..., mark_answered=True)` 返回 bool |
| `backend/bot/chat_service.py` | `ambient_reply(conversation_id, context, *, channel="qq")` + `_ambient_context_note` + `_AMBIENT_ASK` |
| `backend/database.py` | `last_proactive_at_for_conversation` / `proactive_sent_today_for_conversation` / `last_proactive_push_failure_at`；人设级两个统计新增 `exclude_groups` |
| `backend/bot/proactive.py` | `_is_group(target)`；`tick` 群聊读会话级统计、单聊读 `exclude_groups=True`、投递失败 `PUSH_BACKOFF_MIN` 退避；`_push_qq` 群聊真发（`wakeup=False`）；`_check_cold_replies` 跳过群聊 |

### 与设计的偏差

1. **parser 注入位置**：设计里写的是 `client._connection.parser`，实际写的是
   `client._connection.state.parsers`。两者是同一个 dict（`self.parser = self.state.parsers`），
   但取 `state.parsers` 在 `state` 缺失时可以整体判空返回 False（只打一行警告），
   比直接 `client.parser[...] = ...` 更抗 botpy 版本差异。
2. **接话失败不兜底**：正常回复超时会发「稍后再试」类文案，群聊接话改为**直接不说**。
   理由是这条回复本来就可有可无，往群里刷一句系统文案比不说话难看得多；
   也因此不写递送失败日志以外的任何状态。
3. **`_deliver` 返回值**：从 `None` 变成 `bool`。群聊接话要靠它判断「这句到底发出去没有」，
   只有发出去才 `note_line` 进上下文（否则下一句会以为自己说过）。旧的调用方忽略返回值，不受影响。
4. **冷场冷却不覆盖群聊**：`_check_cold_replies` 遇到群会话直接跳过（标记 checked 后 continue）。
   群里「没人接话」是常态，而这个冷却写在人设行上，会连带冻住这个人设的所有单聊。
5. **群主动失败分类**：`permission/invalid/offline → queued`（等下次 @ 捎带）、
   `quota/content/network → skip`（等下一轮），与单聊同口径；区别只是群聊没有召回档位。

### 审查修正（同日）

审查判出 1 个 Blocker + 4 个 Major，逐条落地：

1. **投递退避（Blocker）**：`skip` 不推进任何开口闸，于是被频控的会话每 60s
   重生成一句、再 skip 一次 —— 模型与账本一起刷爆（旧版 QQ 侧恒为 `queued`，被
   pending 短路挡住了，这条无限重试是真主动之后新开的口子）。现在按会话记
   `last_proactive_push_failure_at`（`action='skip' AND reason LIKE 'push:%'`），
   30 分钟内不再尝试（`PUSH_BACKOFF_MIN`）。
2. **接话让位（Major）**：接话原本和真消息抢同一把会话锁，最坏排队 150s + 自身
   150s，正好把随后那条 @ 的回复挤出 5 分钟被动窗口 —— 优先级倒置了。现在锁被
   占着就直接作废这句接话（不插队不排队），并且接话预算单独定
   `AMBIENT_REPLY_BUDGET_S = 45s`，不跟 `reply_window_s` 走。
3. **群开口不占单聊配额（Major）**：人设级统计把群的 push/queued 也算了进去，
   两个群各发一条就能把这个人格当天的单聊主动性饿死，与口径②「每群独立」相反。
   `last_proactive_at` / `proactive_sent_today` 增加 `exclude_groups`：调度器传 True，
   全量口径保留给账本。
4. **自己的回声（Major）**：`AmbientGroupMessage` 透出 `author.bot`，机器人自己的群
   消息（回复被全量事件回推）不进上下文也不触发接话 —— 自己发的文本不含 `<@`，
   原来的守卫挡不住，最坏一个窗口内自说自话好几句。
5. **接话任务强引用（Major）**：`create_task` 只被事件循环弱引用，长时间 `await`
   的接话有被回收的风险（不报错也不回复、事后查不到），改为 adapter 持集合 +
   done 回调自清。
6. **事件重投去重（Minor）**：接话不落库、没有 `answered_at` 做幂等，原来只靠 60s
   节流「碰巧」挡住重投。现在 `group_ambient.seen_event` 显式记最近 64 条事件 id。
7. **@ 回复也算一次开口（Minor）**：`handle_group` 在 `note_mention` 后补
   `mark_spoken`，避免「刚答完 @，3 秒后又抢一句」。
8. **平台没回执就不算发出（Minor）**：botpy 的 `request()` 在 aiohttp 超时时只打日志、
   隐式返回 None，`ConnectionResetError` 重试后同样丢返回值。`send_proactive` 现在
   要求返回 dict，否则按 `network` 处理（不然白占配额并记一条不存在的 push）。
9. **注释纠偏**：`group_ambient` 的加锁理由（多个机器人共用一张表，而不是「主动消息
   也跨 loop 读上下文」，它并不读）、窗口关闭后「转主动消息」这个不存在的机制、
   `queued` 的「协议上发不出」旧口径、被动回复凭据取自事件信封 id 而非 `d.id`，均已
   改写；`should_reply` 不复用 `is_open` 补上了「`threading.Lock` 不可重入」的原因。
10. **测试纠偏**：错误分类用例改用真实的 `botpy.errors.ForbiddenError`（自建同名类会
    让线上分类器改名而测试仍绿）；生成层桩补 `persona_id`，使「只读关系/状态」这支真正
    跑到（否则「不记账」那半边等于没测）；parser 注入用例补断言 `message.id` 就是事件
    信封 id（接话能被动回复的前提）。

### 验证

- 新增 `backend/tests/test_bot_group_ambient.py`（27 个用例）：窗口/节流/上下文 TTL 与条数、
  事件去重（含历史上界）、每个群独立、@ 打点、非 @ 上抛 `ambient=True`、含 `<@` 不抢答、
  60s 节流、机器人自己的回声不进上下文、重投不双答、@ 回复也起节流、
  管线「只落 assistant / 不写用户消息 / 自己的话进上下文」、失败与超时静默、
  锁被占时接话让位、接话预算不跟平台窗口走、生成层只读关系/状态而不调
  `record_turn` / `remember` / `recall` / `state.update_for_turn`。
- `backend/tests/test_bot_proactive.py`（48 个用例，本轮增/改 9 个）：群聊 push 且 `wakeup=False`、
  无权限降级、超频 skip、会话级配额互不挤占、`queued` 计入配额、群聊不记冷场冷却、
  `tick` 对群会话读会话级统计；新补：群开口不占单聊配额（含 `exclude_groups=True` 调度口径）、
  `last_proactive_push_failure_at` 只认 `push:` 开头的 skip、退避期内不生成不投递也不刷账本。
- `backend/tests/test_qq_manager.py`（20 个用例）：parser 注入后事件能 dispatch、分发表
  不可用时返回 False；注入 wrapper 的 `message.id` 就是事件信封 id（被动回复凭据的根）、
  `author.bot` 透出、平台无回执（None）按 `network` 处理。
- `backend/tests/test_qq_channel.py`（23 个用例）：错误分类改用真实的 `botpy.errors.ForbiddenError`。
- `ruff check --select F,E9,E7` 对本次全部文件通过（仓内另有一处既存 F401：
  `tests/test_wechat_manager.py` 导入了未使用的 `WeChatPayloadError`，属 HEAD 已有、
  不在本次范围）。
- `pytest tests/ -q` 全量：`1 failed, 3836 passed`。唯一失败是既存的负载敏感守卫
  `test_startup_timing.py::test_db_read_steps_run_in_one_round`：并发峰值断言（`peak >= 3`）
  通过，只有墙钟 0.167s 超 0.15s 阈值；单文件跑与单例跑均稳定通过，与本 spec 无关。

### 仍需真机确认

1. 群主开启「接收全量消息能力」后，`GROUP_MESSAGE_CREATE` 是否真的推给 botpy
   （当前只在单元测试里验证 parser 注入，没接过真实事件）。
2. 群主动消息在未授权群上的真实错误码是否就是 `40034105`（`classify_proactive_error`
   已按该码映射成 `permission`）。
3. `GROUP_MESSAGE_CREATE` 的 `author.username` 字段是否始终存在（缺就退化成「群友」）。
4. 5 分钟窗口内用不同 `msg_id` 连发多条是否会被平台风控（频控是 5 次/条，不是 5 次/窗口）。
