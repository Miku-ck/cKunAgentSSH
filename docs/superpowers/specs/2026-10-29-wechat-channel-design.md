# 微信渠道接入（微信对话开放平台 · 客服消息回调）设计

日期：2026-10-29　状态：已实现（P0 完成；待真实凭据 + 公网隧道联调，见「上线步骤」）

## 背景

多渠道机器人骨架（spec 2026-10-28）已经切好 provider 缝，但注册表里只有 QQ：
`channel_runtime.default_providers()` 返回 `(QQProvider(),)`。用户要把 cKunBot
接到自己的微信机器人（微信对话开放平台 `@sheep9b4b7`），让人格在微信里接着聊。

微信对话开放平台有三套互不相干的接口：

| 接口组 | 文档目录 | 平台角色 |
| --- | --- | --- |
| 接入智能对话 v2 | `confapi/dialog/bot/*`：token / import / fetch / publish / progress / query | 平台是小脑：我们调它，问一句拿一句 `answer`；**整组没有推送接口** |
| **客服消息收发** | `confapi/thirdkefu/*`：flow / recivemsg / sendmsg / getstate / changestate / h5auth | **我们是大客服系统**：平台把用户消息推给我们，我们用自己的接口回复 |
| 第三方服务接口 | `confapi/thirdapi/*` | 意图命中才回调一个技能，5 秒超时，无法主动发 |

「机器人」命名的文档（`dialog/bot/*`）容易被当成接入机器人的正路，但 `query` 的语义是
「把你自己渠道里用户的问题转给平台机器人」，前提是你已经能收到用户消息，它自己不开通道。
`recivemsg.html` 明确写的是推送 —— 路径配置在「发布管理 → 应用绑定 → 开放API → 回调地址」，
报文含 `encrypted`，`MsgSignature/TimeStamp/Nonce` 填空字符串。这条既是通道入口，也是
本设计必须要有公网 HTTPS 的原因。`import`/`query` 接进来只会让微信托管机器人自己回答，
cKunBot 的人格与记忆全程不在链路上，因此不接。

本次接的是第二组：**平台把消息回调给我们，我们通过 `sendmsg` 以人工客服身份回复**。
这样人格、记忆、主动性全部留在 cKunBot，平台只做通道。

## 目标

- `provider="wechat"` 的渠道能像 QQ 渠道一样：一条 `bot_channels` 行、一个人格、
  在联系页连接/断开、开机恢复、故障隔离。
- 入站：平台 POST 加密回调 → 解密 → 走同一条 `InboundPipeline`（不加平台分支）。
- 出站：`sendmsg` 一条消息对应一轮回复里的一个气泡；分段、节奏、兜底沿用管线。
- 配置页能填 APPID / Token / EncodingAESKey，并能复制回调地址。
- 不引入公网 SDK 依赖：加解密用 `cryptography` 或纯 stdlib 实现，HTTP 用已有 `httpx`。

## 非目标

- 不做微信侧的意图/问答配置托管（那是平台后台的事）。
- 不解析语音、图片、小程序卡片等富内容；P0 只处理纯文本，其余记日志后忽略。
- 不做公众号自身的 XML 被动回复（5 秒窗口那条路）；走的是客服消息 API。
- 不自动部署隧道；只提供回调地址拼装与检查。
- 不做「一个微信机器人绑多个人格」；仍然 1 机器人 = 1 人格。

## 核心模型（为什么这条路能成立）

1. 在开放平台后台配置「回调地址」后，用户每发一条消息，平台都会 POST
   `{"encrypted": "..."}` 到该地址（`from=0`）。
2. 我们用 APPID + EncodingAESKey 解出 XML：`userid / appid / content/msg /
   from / kfstate / channel / createtime`。
3. **接管**：对每个用户调用一次
   `POST https://chatbot.weixin.qq.com/openapi/kefustate/change/{TOKEN}`
   （`kefustate=personserving`）。之后平台机器人不再回答该用户，也不会再推
   `from=1` 的机器人回复——整条对话只有我们在说。
4. 回复：
   `POST https://chatbot.weixin.qq.com/openapi/sendmsg/{TOKEN}`，body
   `{"encrypt": AES(XML)}`，XML 里 `<openid>`、`<msg>`、`<channel>` 来自入站。
5. 断开/删除渠道时对最近会话里的 openid 调 `kefustate=complete`，把用户还给平台
   机器人，避免他们永久停在「人工接入中」。

回调鉴权只有一层：**能用本渠道 EncodingAESKey 解开、且解出的 appid 与本渠道一致**。
平台文档明确写 `MsgSignature/TimeStamp/Nonce` 传空、不做签名校验，所以不实现签名。
伪造请求在 AES 解密阶段就失败，不产生任何状态变更。路径里的 `channel_id` 是随机 id，
只用于路由，不是密钥。

## 凭据

| 字段 | 来源 | 存储 | 用途 |
| --- | --- | --- | --- |
| APPID | 开放API 页 | `app_id`（明文，本来就会显示） | XML 内的 `appid`，解密后校验 |
| TOKEN | 开放API 页 | `app_secret`（DPAPI 信封） | `sendmsg/{TOKEN}`、`kefustate/*/{TOKEN}` 的 URL 段 |
| EncodingAESKey | 开放API 页 | **新列 `extra_secret`**（DPAPI 信封） | 报文 AES-CBC 密钥（43 位 base64） |

`app_secret` 在 QQ 里是 AppSecret，在微信里是 Token：语义统一为「渠道的第二个凭据」。
第三个凭据走通用列 `extra_secret`，未来再有平台需要第三项也不用改表。

provider 声明自己需要哪些凭据字段与界面标签，路由据此校验，不在路由里写 `if provider ==`：

```python
class QQProvider:
    credential_labels = {"app_secret": "AppSecret"}

class WeChatProvider:
    credential_labels = {"app_secret": "Token", "extra_secret": "EncodingAESKey"}
```

## Provider 契约扩展

```python
@dataclass(frozen=True)
class ChannelConfig:
    channel_id: str
    app_id: str
    app_secret: str          # QQ: AppSecret / WeChat: Token
    persona_id: str
    extra_secret: str = ""   # WeChat: EncodingAESKey；默认值保证 QQ 与测试不受影响

class WeChatProvider:
    name = "wechat"
    reply_window_s = 150          # 与 QQ 对齐；客服消息本身有 48h 窗口，不是瓶颈
    credential_labels = {"app_secret": "Token", "extra_secret": "EncodingAESKey"}
    async def start(cfg, on_inbound, *, wait_ready=True, ready_timeout=20.0)
    def outbound_parts(text) -> list[str]
    def fallback(kind) -> str
```

`start()` 不做网络握手：微信是纯推送。它只做本地自检——Token 非空、AESKey 可解出
32 字节、用该密钥做一次 encrypt→decrypt 往返——通过即 `online`，detail 为
「已就绪，等待微信回调」。自检失败抛 `ChannelConnectError(fatal=True)`，与 QQ
凭据错误同一条降级路径（清 `enabled`，开机不再重试）。

## 入站链路

`POST /bot/wechat/{channel_id}`（**不挂 `/api`**，因此天然绕过 X-API-Token 中间件）：

1. 读 body，超过 64KB 直接 413；JSON 解析失败 400。
2. `runtime.handle(channel_id)` 拿不到连接（渠道离线）→ 返回 `{"errcode":0}` 丢弃
   并记 warning。回调不能因此重投堆积。
3. 连接把 `body["encrypted"]` 解密；`encrypted` 这个字段名只用于入站，
   出站是 `encrypt`，不要写反。
4. `from != 0`（机器人/人工客服发言）、`event` 非空（进入/离开等）、解密后 `msg`
   为空 → 记 debug 后 ack。
5. 解出的 `appid` 与本渠道 `app_id` 不一致 → 400（多半是回调地址填错机器人）。
6. 构造 `InboundMessage`：
   - `conversation_key = f"wx:{userid}"`
   - `event_id = sha1(f"{userid}|{createtime}|{msg}")`，平台重投同一条消息时幂等
   - `send` 闭包：绑定这份入站里的 `openid/appid/channel`，每次调用发一条 `sendmsg`
7. **立即返回** `{"errcode": 0, "errmsg": "ok"}`（平台文档的返回格式），随后
   两个后台任务并行：
   - `_ensure_serving(openid)`：`kefustate=personserving`，每个 openid 30 分钟内
     只调一次（字典缓存 + in-flight 去重），失败只记日志，不挡回复
   - `pipeline.handle(inbound, provider, persona_id)`：生成 + 分段投递

ACK 必须在解密后立刻发：生成要几十秒，而平台回调有超时与重投语义，不能同步等。

## 出站链路

`send(part)` → 组 XML → AES 加密 → `POST /openapi/sendmsg/{TOKEN}`：

```xml
<xml>
  <appid><![CDATA[wx...]]></appid>
  <openid><![CDATA[o...]]></openid>
  <msg><![CDATA[这段是回复]]></msg>
  <channel>0</channel>
</xml>
```

- 渠道 7（H5）不传 `appid`；其余渠道传入站 XML 里的 `appid`。
- `]]>` 必须转义成 `]]]]><![CDATA[>`，否则文本截断且 XML 非法。
- 返回 `errcode != 0` 视为发送失败，抛异常让管线保留「未答复」状态，平台重投时能重答。
- HTTP 走 `netconfig.new_client()`，沿用用户的代理/超时策略。

`outbound_parts`：剥离 Markdown → 按空行分气泡 → 每段超过 600 字符再按句读切
（客服消息文本按 UTF-8 计约 2048 字节上限，600 个汉字留足余量）。最多
`MAX_REPLY_BUBBLES` 条。空行分条直接复用 `bot.voice.split_reply_bubbles`。

## 客服状态接管与释放

- `personserving` 缓存 30 分钟：用户持续聊天只调一次；过期再补一次，防止后台被
  人工改成 `complete` 后平台机器人插话。
- **释放**：显式「断开」或「删除机器人」时，取该渠道最近 20 条会话的
  `external_id`（`wx:{openid}`），并发调 `complete`，总预算 5 秒，失败只记日志。
  应用退出（`stop_all`）不释放，避免无谓的关机延迟。

## 数据模型与迁移

```sql
ALTER TABLE bot_channels ADD COLUMN extra_secret TEXT NOT NULL DEFAULT '';
```

- 新库直接建列；旧库 `_migrate_bot_channels` 里 `PRAGMA table_info(bot_channels)` 补齐。
- `_SCHEMA_VERSION` 2 → 3，并更新 `test_startup_critical_path` 的
  `_SCHEMA_SOURCE_FINGERPRINT`。
- `_BOT_CHANNEL_SELECT` 增 `CASE WHEN extra_secret = '' THEN 0 ELSE 1 END AS
  has_extra_secret`，密文仍不进 Python 对象。
- 新增 `get_bot_channel_extra_secret()` / `set_bot_channel_extra_secret()`。
- QQ 行 `extra_secret` 为空，`has_extra_secret=false`，行为不变。

## API

沿用现有 `/api/bot/channels`，新增两个字段与一个全局配置：

```
POST /api/bot/channels            {provider, display_name, persona_id, app_id,
                                   app_secret, extra_secret}
PUT  /api/bot/channels/{id}        同上（空串/缺省 = 保留原值）
GET  /api/bot/channels             每行带 has_secret、has_extra_secret
GET  /api/bot/webhook              {"base_url": "https://xxx.trycloudflare.com"}
PUT  /api/bot/webhook              {"base_url": "..."}（空串清除）
```

- 创建时按 provider 的 `credential_labels` 校验必填项，缺哪个报哪个名字。
- `channel_id` 只出现在回调地址里；渠道列表不需要回显凭据。
- 全局公网地址存 `app_settings['bot.webhook_base_url']`，校验 `http(s)://` 并去掉尾斜杠。
- 回调地址 = `{base_url}/bot/wechat/{channel_id}`；`base_url` 为空时前端只显示路径并提示先填公网地址。

## 前端

- `ChannelCard` 新增 provider 选择（仅新建时可选，已建渠道不允许改 provider）。
- 字段按 provider 渲染：
  - QQ：AppID / AppSecret，副标题「WebSocket 长连接」
  - 微信：APPID / Token / EncodingAESKey，副标题「回调推送 · HTTPS」
- 微信卡片额外一行「回调地址」：只读文本框 + 复制按钮；上方一个「公网地址」输入框
  （全局，保存到 `/api/bot/webhook`），旁边三步指引：
  1. 隧道把本机端口映射到公网 HTTPS
  2. 公网地址填到本卡
  3. 回调地址整条粘到「发布管理 → 应用绑定 → 开放API → 回调地址」
- 凭据为空时的 placeholder 与 QQ 一致：「已保存；留空表示不修改」。
- 空状态文案改成「新增一个 QQ 或微信机器人」。

## 代码落点

| 文件 | 内容 |
| --- | --- |
| `backend/bot/wechat_crypto.py` | AES-CBC encrypt/decrypt、密钥校验、`WeChatCryptoError` |
| `backend/bot/text_format.py` | `strip_markdown` / `split_text` / `split_bubbles_to_parts`（从 qq_channel 抽出，两渠道共用） |
| `backend/bot/wechat_channel.py` | XML 解析/组装、入站字段提取、`WECHAT_TEXT_LIMIT` |
| `backend/bot/wechat_manager.py` | `WeChatConnection`、`WeChatProvider`、sendmsg/kefustate 调用 |
| `backend/routes/wechat_callback.py` | 公网回调路由 |
| `backend/routes/bot_channels.py` | `extra_secret`、凭据按 provider 校验、webhook base URL 接口 |
| `backend/bot/channel_runtime.py` | 注册 `WeChatProvider`、传 `extra_secret`、暴露 `handle()` 与 `disconnect()` |
| `backend/bot/chat_service.py` | 纯文本排版分支把 `wechat` 与 `qq` 并列；audience 走 private |
| `src/api/bot.ts` | provider 联合类型、`has_extra_secret`、webhook base API |
| `src/components/bot/ChannelCard.vue` | provider 选择、微信字段、回调地址复制 |
| `src/components/bot/ConnectionPage.vue` | 全局公网地址的读取与保存 |

## 测试

- `test_wechat_crypto.py`：往返、错误密钥、appid 不符、非法 base64/padding、43 位校验。
- `test_wechat_channel.py`：入站 XML 提取（from/event/appid/msg）、富文本忽略、
  sendmsg XML 转义（`]]>`、`&`、引号）、600 字分段、Markdown 清洗。
- `test_wechat_manager.py`：start 自检（坏 AESKey fatal）、accept 解密 + ack +
  管线调用、from=1 忽略、sendmsg 请求体形状（mock httpx）、发送失败上抛、
  personserving 去重、stop 释放最近 openid、任务清理。
- `test_wechat_callback_routes.py`：无需 X-API-Token、未知渠道 ack、坏密文 400、
  超大 body 413、appid 不符 400。
- 跟改：`test_channel_runtime.py`（注册表含 wechat、extra_secret 透传、handle/
  disconnect）、`test_bot_channels_store.py` / `test_bot_channels_routes.py`
  （`extra_secret` 存取与不回显、凭据校验文案）、`test_bot_channels_migration.py`
  （补列）、`test_bot_routes.py`（回调路由注册）、`test_startup_critical_path.py`
  （版本指纹）。
- 前端：`src/api/bot.test.ts`、`ChannelCard.guards.test.ts`、
  `ConnectionPage.guards.test.ts`（微信字段渲染、回调地址拼接、provider 不可改）。

## 已知取舍与风险

- **首条消息竞态**：接管请求与平台机器人对同一条消息的回答是并行的，第一条可能被
  平台机器人也答一次。缓解：开放平台的机器人不要配问答/技能；从第二条起完全接管。
- **48 小时窗口**：客服消息只能发给最近 48h 内互动过的用户。本设计全部是被动回复，
  不受影响；主动消息调度对非 `local` 渠道一律记 `queued` 寄存（`bot/proactive.py`），
  只在对方下次来消息时捎带，不会有主动外发撞上窗口限制。
- **限频未知**：官方文档未给 sendmsg 频率。当前只在收到消息后回 1–3 条，远低于常规
  客服量；真遇到限频由管线「首段失败不标已答」+ 平台重投兜底。
- **公网入口是硬前提**：桌面应用后端监听 127.0.0.1，必须由 cloudflared/frp 等隧道
  暴露 HTTPS。后端不需要改监听地址（隧道连本机回环）。
- **只处理文本**：图片/语音/卡片不回复；用户发图片会得不到回应。
- **回调无签名**：遵循官方文档「不做签名校验」，安全性由 AES 密钥 + appid 校验承担。
- **永久删除渠道**：已释放最近 20 个用户；更早的用户会留在 `personserving`，
  直到用户在平台侧手动结束会话。

## 上线步骤

前置三条缺一不可：开放API 已申请并绑定（回调地址输入框可见）、有一个 HTTPS 公网入口、
拿到 APPID / Token / EncodingAESKey。前两条在平台侧，代码无法替代。

1. 开放平台 → 发布管理 → 应用绑定 → 开放API：复制 APPID / Token / EncodingAESKey。
2. 起隧道：`cloudflared tunnel --url http://127.0.0.1:<后端端口>`，拿到 HTTPS 域名。
3. 连接页新增「微信」机器人：填名称、选人格、填三项凭据。
4. 公网地址填隧道域名（失焦保存，`PUT /api/bot/webhook`）。
5. 复制卡片里的回调地址 `https://<域名>/bot/wechat/<channel_id>`，粘到开放API 的
   「回调地址」并保存。
6. 点「连接」：状态应为「在线 / 已就绪，等待微信回调」。这一步不握手，只做本地 AES 自检。
7. 微信里给机器人发一条消息：应收到 1–3 条人格回复；日志出现接管记录。
8. 点「断开」：最近 20 条会话的用户被 `complete` 释放回平台机器人。

失败对号：状态「连接异常」= 凭据自检失败；回调返回 400 = 解出的 appid 与本渠道不一致
（回调地址填错机器人）；日志提示渠道未连接 = 没有点连接，或连接失败后已清 `enabled`。

验收：人格回复而非平台问答库答案；连续两轮平台机器人不插话；同一条消息重投不产生
二次回复；断开后平台机器人重新接管；渠道卡片不出现任何密钥明文。
