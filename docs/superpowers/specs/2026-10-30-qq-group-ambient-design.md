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

（本节初版写的是「不做额外开关」，已被同日追加的群参与档位推翻，理由见下。）

开关只做在**人格**上，不做在机器人连接上：`bot_channels.persona_id` 是 1:1 外键，
渠道级参数与人格级等价，却多一处要同步的口径（连接改绑人格时开关会漂移）。

人格新增一列 `group_mode`（三档，从安静到吵）：

| 档位 | 含义 | 管住的路径 |
| --- | --- | --- |
| `mention_only`（默认） | 只 @ 才答 | 接话与群主动都不走 |
| `ambient` | @ 后 5 分钟窗口内可接话 | 只放行接话 |
| `proactive` | 接话 + 每群独立 12h 主动 | 两道都放行 |

单聊主动性仍由原有的 `proactive_enabled`（人格编辑器的「主动找你」）独掌：
两者打扰程度与平台前提不同（一个是对着一个人说话，一个是多人场合插话），
拆开后「单聊可以主动、群里别闹」这种组合才表达得出来。

闸口各在一处：接话归 `InboundPipeline.handle()`（渠道适配层不认识人格），
群主动归 `list_proactive_targets()` 的 SQL（拦在这里才不会白跑状态快照与意愿计算）。

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
- 档位（§5）：`ambient_allowed` 三档与脏值/空值、档位不开时管线不碰模型；
  群会话要 `proactive` 才进巡检清单而本地/单聊会话不受影响（兼 NULL 三值逻辑）；
  路由层默认值与乱值 422；旧库补列落在最保守档。

## 实施回填

### 代码落位

| 位置 | 内容 |
| --- | --- |
| `backend/bot/group_ambient.py`（新） | 可参与期注册表：`note_mention` / `note_line` / `is_open` / `should_reply` / `mark_spoken` / `seen_event` / `context_lines` / `reset_for_tests`，加 `threading.Lock`（多个机器人共用一张表）；`GROUP_MODES` / `AMBIENT_MODES` / `ambient_allowed()` 是群参与档的唯一事实源 |
| `backend/bot/qq_channel.py` | `AmbientGroupMessage`（补 botpy `GroupMessage` 透不出的 `author.username` / `author.bot`）；`handle_group` 里 `note_mention` 续窗口 + `mark_spoken` 起节流；`handle_group_message` + `_emit_ambient`（任务强引用）；`_emit(..., ambient=False)` 透传给 `InboundMessage` |
| `backend/bot/qq_manager.py` | `_install_group_message_parser(client)` 往 `client._connection.state.parsers` 补 `group_message_create`；`Client.on_ready` 里注册，`on_group_message_create` 交给 adapter；`send_proactive` 要求平台返回 dict 回执 |
| `backend/bot/provider.py` | `InboundMessage.ambient: bool = False` |
| `backend/bot/inbound_pipeline.py` | `handle()` 按 `ambient` 分流（锁被占则作废，不排队；档位不放开在 `_may_ambient`）；`_handle_ambient` 只取上下文 → 生成（`AMBIENT_REPLY_BUDGET_S`）→ 投递，失败/超时静默；`_deliver(..., mark_answered=True)` 返回 bool |
| `backend/bot/chat_service.py` | `ambient_reply(conversation_id, context, *, channel="qq")` + `_ambient_context_note` + `_AMBIENT_ASK` |
| `backend/database.py` | `last_proactive_at_for_conversation` / `proactive_sent_today_for_conversation` / `last_proactive_push_failure_at`；人设级两个统计新增 `exclude_groups`；`bot_personas.group_mode` 列（建表 + 旧库补齐 + 读写白名单），`list_proactive_targets()` 按档位滤掉群会话 |
| `backend/bot/character.py` + `backend/routes/bot.py` | `persona_options()` 新增「群参与」档位文案；`PersonaRequest` / `PersonaUpdateRequest` 加 `group_mode`（Literal 三值，乱值 422） |
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

### 群参与档位（同日追加）

追加背景：用户问「主动聊天要不要在机器人连接或人格里加个设置项」。核代码后
发现两个事实：单聊/群主动都已有闸（`proactive_enabled`，默认关），而**接话什
么都不受控制** —— 只要群主开了全量消息能力，即使主动性关着也会在群里插话。

1. **不做渠道级参数**：`bot_channels.persona_id` 是 1:1 外键，两边等价，多一层
   只会多一处漂移（连接改绑人格时开关跟不跟着走说不清）。
2. **一个枚举而不是两三个布尔**：三档本身互斥，平铺成布尔会出现
   「不接话但群主动」这种没有意义的组合。
3. **默认最保守档**，所以这是一次**默认行为变更**：升级后存量人格的接话与群主动
   都会关下来，需要用户在人格编辑器里把「群参与」调上去。宁可错杀不行错说：
   多接一句话用户只会觉得机器人忽然长了嘴，少接一句他至少知道去看开关。
4. **脏值与读不到人格都按最保守档读**（`ambient_allowed` 的兼容分支）。
5. **副本不继承档位**：与 `proactive_enabled` 同口径（`copy_bot_persona` 也不复制
   它），免得「复制一个号」就多开一张嘴。
6. **群级静音（就这个群别说）本次不做**：`listChannelConversations` 已能列出
   QQ 会话，将来要加只差一个更新端点与会话行上的开关。
7. **SQL 里的三值逻辑坑**：初版写 `NOT (c.external_id LIKE 'group:%' AND ...)`，
   本地会话的 `external_id` 是 NULL → `NULL AND TRUE` = NULL → `NOT NULL` = NULL
   → 本地会话整条从清单里消失，不报错只静默少行。两边都包上 IFNULL 后才对。
8. **降档不追收已寄存的那句**：群里 `proactive → mention_only` 后，48h 内来消息时
   旧寄存仍会以「对了，」捎上。刻意不为它加闸：捎带动作发生在一条**被 @ 后的被动
   回复**里，而 @ 回复在任何档位都是允许的；档位管的是「没人 @ 时能不能说话」，
   不是「不能提之前想说的话」。真要它整台闭嘴是关掉那个机器人连接（或把机器人
   移出群），不在这个档位里叠第二层开关；也不值得为了这个边缘口径
   在 @ 回复的热路径上多读一次快照。

### 手动探口与群回复不 @ 人（同日追加）

追加背景：用户重启项目、把「群参与」开到最高档之后**仍然看不见任何效果**，同时
希望群里的回复不要挂「回复某人」。两条都不是配置问题：

- 自动路径对新人格基本是空转：`bot_relations.intimacy` 建表默认 5，而 `desire()`
  有一条「intimacy < 20 一律封顶在 `FIRE_THRESHOLD - 0.01`」的硬底线 —— 档位、
  静音窗、配额调到什么程度都开不了口。调 θ 等于把「不熟的人别打扰」这条产品
  判断改掉，代价远大于「看得见效果」，所以给的是**手动口子**而不是降阈值。
- @ 不是我们的参数：官方 `POST /v2/groups/{group_openid}/messages` 没有「不要 @」
  开关，「回复某人」是平台在**带 `msg_id`** 的被动回复上自己加的样式。唯一不发 @
  的办法是改走不带 `msg_id` 的主动消息通道，代价是那条通道要群主开「主动推送」。

#### 1. 手动探口（会话栏「试一句」）

| 项 | 口径 |
| --- | --- |
| 入口 | `POST /bot/proactive/probe`，body `{conversation_id}`；只读侧会话栏的「试一句」按钮（只挂在 QQ tab） |
| 拦不拦 | 意愿分、12h 间隔、静音窗、日配额、冷场冷却**照算一遍但不拦**，算出来的原因就是回显正文 |
| 记账 | 照常落 `bot_proactive_log`、照常占会话级与人设级的 12h 与日配额 —— 不拿「只是试一下」骗自己 |
| 唯一豁免 | 账本 `reason` 前缀 `manual:`，`_check_cold_replies` 认它并跳过 —— 试发不是「她自己想说话」，没人接不该把人设冻 48h |
| 回显 | 念 `latest_proactive_row()`（账本上那一行）：屏幕上的结论与落库结果不会两套口径 |
| 渠道 | 微信当场 409「这个渠道不支持主动发消息」：没有主动通道，默默寄存一句永远发不出去更糟 |
| 人格没了 | 报 `missing` 且不烧模型 |

`gate()` 返回 `(ok, reason)`，探口在 `_ok` 时把 `gate_reason` 置**空串**而不是带回
`"ok"`：前端据此决定要不要挂那句「自动开口本来会卡在」，原样带回 `"ok"` 等于每句
话都谎称「本来会被拦」。

#### 2. 群回复不 @ 人（人格开关 + 出站剔 @）

- 人格新增 `mention_sender`（默认 1）。关掉后**只有群会话**的这一句改走
  `InboundMessage.send_plain`（不带 `msg_id`）；单聊本来就不 @ 人，给它挂上这条
  通道等于白白换成限频、要权限的路径，所以适配器只在 `conversation_key`
  以 `group:` 开头时才生成 `send_plain`。
- 回落只认平台明确拒绝：`permission` / `invalid` → 回落被动回复（宁可带 @ 也不让
  这句话消失）；`network` **不回落** —— 发没发出去不确定，回落就是群里同样的两句。
  plain lane 自身不消耗 `seq`（回落时被动回复仍从原序号继续）。
- 通道判定读**会话快照** `conversation["persona"]["mention_sender"]`：@ 回复是热路径，
  不该为一句开关再查一次表；`revise_persona_snapshots` 保证人格编辑即回填，旧快照
  缺字段按默认「照旧 @」处理。`group_mode` 仍在 `_may_ambient` 读实时人格。
- 出站剔 @：`text_format.strip_at_markup` 在 `outbound_parts`（管线唯一漏斗）里抹掉
  模型照抄进来的 `<@…>` / `<at id=…></at>`，并折叠留下的连续空格 —— QQ 纯文本里没有
  「在文本里 @ 某人」这种语法，留着就是给人看一串源码。
- 副本口径：`group_mode` / `proactive_enabled` 是「开口资格」，复制人格不开第二张嘴；
  `mention_sender` 是「说话分寸」，跟 `reply_style` 一起继承。

#### 3. 这一轮踩到的坑

1. **botpy 的 `payload = locals()`**：就算不传 `msg_id`，SDK 也会把 `"msg_id": null`
   塞进 JSON，平台按「带了 `msg_id` 但取不到那条消息」判 `invalid`。所以 `_post_group_plain`
   用 `Route` 直调 `_http.request` 并显式构造最小 payload，表情图也一样只带 `media`。
2. **`int(x or 1)` 读布尔列**：关掉的 `0` 会被 `or` 吃成 `1`，开关拨了没反应。写成
   `1 if raw is None else int(raw or 0)`：只有字段真的缺失才默认开。
3. **`has_wakeup_period` 的匹配前缀**：原本是 `reason LIKE 'wakeupN:%'`，加了 `manual:`
   前缀就漏判已用过的召回档（可能重复捎带同一档），改为子串 `LIKE '%wakeupN:%'`。
4. **结构变更按铁律同步**：新增 `mention_sender` 列 → `_SCHEMA_VERSION` 4 → 5，
   `test_startup_critical_path.py` 的 `_SCHEMA_SOURCE_FINGERPRINT` 同步换新值。
5. **探口返回不能顺手改成 dict**：`_deliver` 的 `bool` 返回值被 3 个现有用例钉着，
   改成 dict 会连带改掉投递语义；回显需要的动作/原因走账本那一行拿。
6. **拿类直接调实例方法（用户真机点「试一句」时当场炸出来的）**：`_compose` 里那行
   `BotChatService._system_prompt(persona, channel, ...)` 漏了实例，Python 3 下这是普通
   函数：`persona` 被当成 `self`、`channel` 被当成 `persona`，下一行 `persona.get("name")`
   就是 `AttributeError: 'str' object has no attribute 'get'`。这行在 HEAD 里躺了一整轮：
   自动路径被 desire 硬底线挡着从不真跑到它，而每个用例都把 `_compose` 整段桩掉
   —— 桩掉的恰好是唯一会碰它的那条线。修成 `svc._system_prompt(...)`；同一个毛病
   还盖住了 `_state_snapshot`（也被每个用例桩着，新人格没关系行时读的是空 dict）。
   现补两个不桩生成层的用例：`_compose` 单独真跑（只桩模型流）与探口整条线真跑
   （连 `_state_snapshot` 与 `pick_topic` 一起真跑）—— 探口、快照、生成、投递四段的
   接口形状必须被一个用例真正串过一次。

### 验证

- 新增 `backend/tests/test_bot_group_ambient.py`（30 个用例）：窗口/节流/上下文 TTL 与条数、
  事件去重（含历史上界）、每个群独立、@ 打点、非 @ 上抛 `ambient=True`、含 `<@` 不抢答、
  60s 节流、机器人自己的回声不进上下文、重投不双答、@ 回复也起节流、
  管线「只落 assistant / 不写用户消息 / 自己的话进上下文」、失败与超时静默、
  锁被占时接话让位、接话预算不跟平台窗口走、生成层只读关系/状态而不调
  `record_turn` / `remember` / `recall` / `state.update_for_turn`；
  档位追加：`ambient_allowed` 三档与脏值/空值、`GROUP_MODES` 顺序、
  档位不开时管线不碰模型也不投递。
- `backend/tests/test_bot_proactive.py`（60 个用例）：群聊 push 且 `wakeup=False`、
  无权限降级、超频 skip、会话级配额互不挤占、`queued` 计入配额、群聊不记冷场冷却、
  `tick` 对群会话读会话级统计；群开口不占单聊配额（含 `exclude_groups=True` 调度口径）、
  `last_proactive_push_failure_at` 只认 `push:` 开头的 skip、退避期内不生成不投递也不刷账本；
  档位追加：群会话要开到 `proactive` 才进巡检清单（单聊不受档位影响）、
  本地会话不被档位误删（正是那条 SQL 三值逻辑 bug 的回归守卫）；
  探口追加：闸要拦时照发且回显看得见原因、闸本该过时 `gate_reason` 为空串、
  离线降级 `queued`（不许报「已发出」）、人格不在不烧模型、手动那句照常落账与占
  12h、不计冷场冷却、`has_wakeup_period` 认得 `manual:` 前缀；生成层追加：`_compose`
  自己组装 system prompt（人格名/称呼/渠道口径/状态快照全部真进 prompt）、探口不桩
  快照不桩生成整条线跑通（就是那条真机 AttributeError 的回归守卫）。
- `backend/tests/test_qq_manager.py`（20 个用例）：parser 注入后事件能 dispatch、分发表
  不可用时返回 False；注入 wrapper 的 `message.id` 就是事件信封 id（被动回复凭据的根）、
  `author.bot` 透出、平台无回执（None）按 `network` 处理。
- `backend/tests/test_qq_channel.py`（29 个用例）：错误分类改用真实的 `botpy.errors.ForbiddenError`；
  本轮追加：群入站带 `send_plain` 而单聊不带、plain lane 发的 payload 里就没有 `msg_id`（表情图
  也一样只带 `media`）、无权限回落被动回复、网络不确定不回落、回落不白吃一个 `seq`、
  出站永不带 @ 标记。
- `backend/tests/test_channel_runtime.py`（19 个用例，本轮追加 2 个）：人格关掉 `mention_sender`
  时群回复走 plain 通道、开关开着时照旧 @（读的是会话快照而不是再查一次人格）。
- `backend/tests/test_bot_routes.py`（44 个用例）：新建默认最保守档、
  POST/PATCH 能落档且乱值 422、局部更新不冲掉总闸、`persona-options` 三档有序且文案非空、
  副本不继承档位；本轮追加：`mention_sender` 默认开、局部更新能把 `False` 落下去
  （`False` 不是缺字段）、副本继承这个分寸、探口端点只跑被点名那个会话并原样回显、
  会话不存在 404 / 渠道不支持 409 / 空 id 当场 422。
- `backend/tests/test_bot_store.py`（16 个用例）：旧库 DROP 列后重跑迁移，
  补出来的 `group_mode` 必须落在 `mention_only`、`mention_sender` 必须是 1（默认开），
  会话快照要把 `mention_sender` 带下去。
- 结构变更按铁律同步：`_SCHEMA_VERSION` 3 → 4（群参与档）→ 5（`mention_sender`），
  `test_startup_critical_path.py` 的 `_SCHEMA_SOURCE_FINGERPRINT` 跟着换两次（守卫用例跑过）。
- `ruff check --select F,E9,E7` 对本次全部文件通过（仓内另有一处既存 F401：
  `tests/test_wechat_manager.py` 导入了未使用的 `WeChatPayloadError`，属 HEAD 已有、
  不在本次范围）。
- `pytest tests/ -q` 全量（档位实现后）：`3846 passed`。上一轮那处负载敏感守卫
  `test_startup_timing.py::test_db_read_steps_run_in_one_round`（0.15s 墙钟阈值，并发峰值
  断言本就过）本轮未触发；它仍是余量偏薄的既存隐患，与本 spec 无关。
- 探口与 `mention_sender` 实现后全量：`3867 passed`。那条 0.15s 墙钟守卫当轮被抖到
  0.153s（当时同一机器上并行跑着 vue-tsc 与 ruff），单独重跑 `tests/test_startup_timing.py`
  5 passed：峰值并发断言一直过，只有墙钟那一半会被负载误伤 —— 这正是它余量偏薄的形状。
- 真机报错（`_compose` 拿类调实例方法）修好后全量：`3870 passed`，本轮墙钟守卫未触发。
- 前端：`vue-tsc -p tsconfig.web.json --noEmit` 零错误；`npx vitest run` 全量
  `172 files / 2111 tests passed`（人格表单多一个必填字段，旧 fixture 因为是可选字段而不碎）。
- 本轮前端新增/追加：`src/utils/bot-probe.test.ts`（5 个用例：五种动作各说各的、闸本该过时不许
  出现「卡在」、未知动作原样念不冒充已发出）、`src/stores/bot.test.ts`（16 个，追加 4：回显只落
  probeNote 不进消息列表、失败不弹聊天错误条、在途不重发、换会话清回显）、
  `CkunBotView.guards.test.ts`（33 个，追加 2：「试一句」入口只在 QQ 挂且结果只落回显条、
  `mention_sender` 默认读的口径不能写成 `=== 1`）。
- 本轮全量 `npx vitest run`：`173 files / 2122 tests`，`2121 passed`，唯一一条失败落在他人 WIP 的
  `src/components/ssh/git/GitLaneGraph.vue:139`（写字面色值 `#04240f` 撞了令牌守卫），
  不在本次范围也不该由本次改动代为修正。

### 仍需真机确认

1. 群主开启「接收全量消息能力」后，`GROUP_MESSAGE_CREATE` 是否真的推给 botpy
   （当前只在单元测试里验证 parser 注入，没接过真实事件）。
2. 群主动消息在未授权群上的真实错误码是否就是 `40034105`（`classify_proactive_error`
   已按该码映射成 `permission`）。
3. `GROUP_MESSAGE_CREATE` 的 `author.username` 字段是否始终存在（缺就退化成「群友」）。
4. 5 分钟窗口内用不同 `msg_id` 连发多条是否会被平台风控（频控是 5 次/条，不是 5 次/窗口）。
