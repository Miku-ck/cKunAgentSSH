# 多渠道机器人接入（多 QQ 机器人 ＋ provider 缝）设计

日期：2026-10-28　状态：已确认，待实施

## 背景

现在「连接」页只能连一个 QQ 机器人：运行时是模块级单例（`qq_manager.py:263`），
线程存活即拒绝第二个连接（`qq_manager.py:127`），凭据是 `app_settings` 里五条扁平的
`bot.qq.*` 键。用户需要「一个人格配一个机器人账号」（例：两个人格、两个 QQ 机器人），
跨群跨私聊都稳定地以该人格出现；并且人格可随时编辑。钉钉等平台以后再说，但这次要把
接口缝切在正确的位置，避免将来推翻数据模型。

## 目标

- 同时运行 N 个 QQ 机器人，每个绑定一个人格；故障互相隔离。
- 人格可在配置里编辑；编辑后同一群/同一个人另起一条会话，旧会话保留可读。
- 删除机器人连带删除它的聊天记录。
- 定义一条窄 provider 契约，使接入钉钉＝新增一个实现文件，不改连接页、存储与聊天服务。

## 非目标

- 本次不写任何钉钉代码。钉钉只用来验证契约形状。
- 不做会话手动别名（openid 认不出群名，但本次不加字段；靠人格名区分）。
- 不做多 bot 分摊平台额度、不做主动发消息（官方额度限制）。
- 不改 `bot_conversations.channel` 列名与取值（`local` / `qq`），避免与新增混在同一次改动。

## 核心模型

**一个机器人账号 = 一行 `bot_channels` = 一个人格。** 人格可变；绑定关系永远唯一。

会话身份 = `(channel_id, persona_id, external_id)`。人格改了就查不到旧行，自动为同一群
新建一条会话；旧会话带着旧 `persona_id` 与旧快照保留。这取代了今天把 `context_id`
塞进 `external_id` 字符串的做法（`qq_channel.py:70`），后者整条链删除。

人格从 A 改成 B 再改回 A 时，A 时期的老会话会重新命中，历史接着走。

## Provider 契约

```python
@dataclass(frozen=True)
class InboundMessage:
    channel_id: str                          # bot_channels.id
    conversation_key: str                    # 平台内身份，如 "group:{openid}"
    event_id: str                            # 幂等键
    text: str
    send: Callable[[str], Awaitable[None]]   # 发送一段；闭包，不是原始消息对象

class ChannelProvider(Protocol):
    name: str                                # "qq"
    reply_window_s: float                    # 生成预算
    async def start(self, cfg: ChannelConfig, on_inbound) -> ConnectionHandle: ...
    def outbound_parts(self, text: str) -> list[str]: ...
    def fallback(self, kind: Literal["timeout", "failure"]) -> str: ...
```

`send` 必须是闭包：QQ 内部 `message.reply(...)`，钉钉内部 POST 临时的 `sessionWebhook`。
调用方永远拿不到原始消息对象，所以换平台不需要改管线。

## 共享入站管线

`InboundPipeline.handle(inbound, provider)` 承接现有 `qq_channel.py:75-144` 的逻辑，
去掉 QQ 常量：

1. 按 `conversation_key` 加锁
2. `get_or_create_bot_conversation(persona_id, "qq", key, channel_id=...)`
3. 查 `answered_at` 幂等：已送达则丢弃
4. 首回合判定（必须在生成前）
5. `complete_reply(..., channel=provider.name)`，超时 = `reply_window_s`
6. 首回合且有人格开场白 → 前置拼进第一条
7. `provider.outbound_parts(text)` 逐段 `inbound.send(part)`
8. 第一段成功即 `mark_bot_message_answered`；后续段失败只记日志

## 数据模型

```sql
CREATE TABLE IF NOT EXISTS bot_channels (
    id           TEXT PRIMARY KEY,
    provider     TEXT NOT NULL,                                   -- 'qq'
    display_name TEXT NOT NULL,
    persona_id   TEXT NOT NULL REFERENCES bot_personas(id) ON DELETE RESTRICT,
    app_id       TEXT NOT NULL DEFAULT '',
    app_secret   TEXT NOT NULL DEFAULT '',                        -- DPAPI 信封
    enabled      TEXT NOT NULL DEFAULT '0',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- bot_conversations 增列
channel_id TEXT REFERENCES bot_channels(id) ON DELETE CASCADE

-- 唯一索引改为
CREATE UNIQUE INDEX ix_bot_conversations_external
    ON bot_conversations(channel_id, persona_id, external_id)
    WHERE external_id IS NOT NULL;
```

级联链：删 `bot_channels` → 删 `bot_conversations` → 删 `bot_messages`。本地陪聊会话
`channel_id IS NULL`，不受影响。

## 迁移（`_migrate_db`，幂等）

1. 建表随主 `executescript` 走，新库直接有。
2. `PRAGMA table_info(bot_conversations)` 补 `channel_id`。
3. `PRAGMA index_info(ix_bot_conversations_external)`：列仍是 `(channel, external_id)`
   时 DROP 并按新定义重建。
4. 老单例配置落成一行：哨兵键 `bot.channels.legacy_migrated` 未置位、且
   `bot.qq.app_id` 非空时，插入一行 `provider='qq'`、`display_name` 取人格名（缺省
   「QQ 机器人」）、继承 `app_secret`（已是信封）与 `enabled`；然后置哨兵。
   哨兵是必须的——否则用户删光机器人后，下次启动旧键会把渠道复活。
   绑过的人格已被删除时退到内置人格；一个人都没有就不动，等用户来配。
5. 回填：`channel='qq'` 的会话 `channel_id` 指向该行；`external_id` 取前两段
   （`group:{openid}:{context_id}` → `group:{openid}`）。
   **剥离会撞唯一索引**：人格走过 A→B→A 时，同一个群在 A 下会留下 `context_id` 不同的
   多行（`context_id` 每次改人格都重生），剥离后即同 `(channel_id, persona_id, external_id)`。
   迁移按 `updated_at` 升序处理，把较早的会话消息并入较新的那条再删除旧行。
   这一步必须做，否则迁移抛 IntegrityError、整个后端起不来。
6. 旧 `bot.qq.*` 键保留不动（无消费方），不删用户数据。

## 运行时注册表

- `QQConnection`：现有 `QQChannelManager` 的实例逻辑改名，实现 `ConnectionHandle`。
  线程、事件循环、重连退避、fatal 判定、5 秒优雅停止逐条照搬；「已在运行」的门从
  manager 挪进 connection，按 id 生效。
- `ChannelRuntime`（新文件 `bot/channel_runtime.py`）：`dict[channel_id, QQConnection]`
  ＋ 一把只保护该 map 的锁 ＋ `start/stop/stop_all/status/statuses`。删除模块级单例。
- 线程名 `ckunbot-qq-{id前8位}`，否则多 bot 的线程 dump 分不清。
- 开机恢复遍历 `enabled='1'` 的行，**每行单独 try**，一个失败不影响其余启动。
- `MAX_BOT_CHANNELS = 8` 兜底（在路由层，不在运行时），不硬限业务语义。
- 可注册的渠道类型由 `default_providers()` 决定，路由用 `runtime.supports(name)` 校验；
  请求模型里不写死 `Literal`——加一个平台就是「写实现 ＋ 进注册表」两件事。

## API

`backend/routes/bot_channels.py` 整体替换 `bot_qq.py`（前端同批改，不留兼容层）。

```
GET    /api/bot/channels         行 ＋ 各自运行状态（一次取全）
POST   /api/bot/channels         {provider, display_name, persona_id, app_id, app_secret}
PUT    /api/bot/channels/{id}    display_name / app_id / app_secret / persona_id
DELETE /api/bot/channels/{id}    先 stop 再删（级联清会话）
POST   /api/bot/channels/{id}/connect
POST   /api/bot/channels/{id}/disconnect
```

规则：
- 响应里每个渠道带 `conversation_count`（SQL 子查询算出），删除确认文案要用它说清会带走多少条会话。
- 密钥在 SQL 层就用 `CASE` 折成 `has_secret`，密文一次都不进 Python 对象。
- 在线时改人格 → 409（现有规则保留），其余字段可在线改。
- `app_secret` 空串表示保留原值；响应只给 `has_secret`。
- 首次创建必须带 `app_secret`，否则 400。
- 人格被机器人绑定时，删除人格返回 409（路由预检 `count_bot_channels`，FK RESTRICT 兜底）。

## 前端

- `src/api/bot.ts`：`BotChannel` 类型 ＋ 六个方法，删掉 `BotQqConfig` 单例那套。
- `ConnectionPage.vue`：`v-for` 渠道卡 ＋「新增机器人」；整页一次 5 秒轮询
  `GET /bot/channels`，不再每卡各轮一次。
- `ChannelCard.vue`：一张卡装一个渠道，自己的状态灯、保存/连接/断开/删除（删除二次确认，
  文案说明会删掉多少条会话）。凭据字段按 provider 渲染，本次只有 QQ。
- 陪聊页不动：会话已显示人格名，且 1 bot = 1 persona，足以区分。

## 测试

- `test_bot_channels_store.py`：渠道 CRUD、密钥读写、启用位、级联删除、会话身份唯一性
  （两机器人同群 = 两条；改人格 = 新条且旧条保留；A→B→A = 接回老会话）。
- `test_bot_channels_migration.py`：旧配置成行、`channel_id` 回填、`context_id` 剥离、
  唯一索引重建、哨兵防复活、新库不产垃圾行。
- `test_channel_runtime.py`：`FakeProvider` 三渠道并发、故障隔离、stop 只停自己、
  restore 跳过坏渠道；入站管线的幂等、首回合前置开场白、超时与失败兜底、分段投递
  与「首段成功即标已答」也在这组里跑（管线只用 InboundMessage，不见平台对象）。
- `test_qq_channel.py`：QQ 事件 → InboundMessage（key 只含平台身份）、空消息丢弃、
  event_id 回落、出站清洗与分段、provider 每次 start 交出新句柄。
- `test_qq_manager.py`：连接生命周期（握手失败/401 fatal/缺依赖/超时清理/掉线重连/
  退避期间停止/重连中认证失败）＋ 两个连接互相独立。
- `test_bot_channels_routes.py`：密钥永不回显、上下线写 enabled、fatal 清 enabled、
  可重试失败保留 enabled、在线改人格 409、删除回报会话数、数量上限、不支持的 provider。
- 跟改：`test_bot_routes.py` 的路由注册断言、`src/api/bot.test.ts`、
  `ConnectionPage.guards.test.ts`（钉住「卡片不再各自轮询」）、新增 `ChannelCard.guards.test.ts`。
- 删除：`test_bot_qq_channel.py`（greeting 行为已由 test_channel_runtime.py 覆盖）、
  `test_qq_routes.py`（被 routes 新测试取代）。

## 已知取舍

- 两个人格槽位可以绑同一个人格：关系账本按 `persona_id` 计（`bot_relations` 主键），
  两机器人会共享熟悉度。不额外加约束。
- 改人格后旧会话仍在列表里（带旧人格名）。这是真实历史，不隐藏。
- QQ 群名/昵称仍拿不到（白名单/内邀），靠人格名区分机器人。
