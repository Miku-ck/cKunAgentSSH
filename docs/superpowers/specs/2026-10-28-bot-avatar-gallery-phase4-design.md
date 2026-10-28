# cKunBot 头像体系与图库设计（Phase 4 · 头像美化）

**日期：** 2026-10-28
**范围：** 仅美化现有会话的头像显示 + 服务端头像图库 + QQ 会话自定义名/头像。**不新建多人格群聊子系统。**

## 1. 背景与现状

- 本地会话：1 对 1（你 + 一个当前人格）。人格头像 = 上传图（`PersonaAvatar` 仅渲染 data URL 图片，无图时空圈）；**用户气泡当前没有头像位**。
- QQ 会话：只读、纯文本。对端头像用 `GeneratedAvatar` = 基于 `identiconSvg(seed)` 生成的**方块马赛克**（种子取群/私聊 openid）。真实群名/群头像官方机器人拿不到，代码里明确"不采集"，现在显示的是匿名代号（如「群聊 · A3F1」）。
- Phase 1 已做「会话快照瘦身」：`bot_messages`/会话不复制头像 data URL，展示时读时按 persona_id 关联实时人格。
- Phase 2（看图回复）链路独立，本设计不触碰。

## 2. 目标

1. 头像不再是 identicon 马赛克：无上传图时用**按人不同的淡色底 + 首字/代号**占位，能区分不同说话者（会话级/角色级）。
2. 服务端**头像图库**：上传若干图，集中管理，供各处头像复用选取。
3. 支持给**「你自己」**设头像（本地会话用户气泡显示）。
4. **QQ 会话**支持本地自定义**显示名 + 会话头像**（真实数据不改，仅展示覆盖）。

## 3. 非目标（明确排除）

- **群内按真人细分说话者**：现 QQ 消息只记 `role`，不记每条消息的发送者 openid；要做到需给 `bot_messages` 补记 sender_openid 且 QQ 通道写入改造 —— 与"只是美化、不新建子系统"冲突，**列为后续期**，本设计不做。
- 不改 QQ 纯文本/只读/去重不变量；不碰 Phase 2 看图多模态。
- 不引入新第三方依赖。

## 4. 设计

### 4.1 淡色占位头像（取代马赛克）
- 新增纯函数 `avatarPlaceholderStyle(seed: string): { background: string; color: string }`（放 `src/utils/bot-chat-decor.ts`，复用现有 `hashStr`）：种子哈希 → HSL **低饱和、高明度淡色背景**（s≈30%、l≈88%，明/暗主题下都可读）+ 同色系**深字**。同 seed 恒同色，不同 seed 区分。
- `GeneratedAvatar.vue`：不再走 `identiconSvg`；改为渲染淡色底 + **首字/代号**文本（人格名首字、QQ 用 `peerLabel` 尾号如「群 · A3F1」或首字）。组件对外 props 保持 `seed` + 新增可选 `label`（要显示的字）。
- `PersonaAvatar.vue`：无 data URL 图时，渲染淡色首字圈（父级传入 `name`/`seed`），替换现在的空圈。
- 遵循 UI 令牌化规范：占位色为函数算出的 HSL，容器尺寸/圆角用现有令牌。

### 4.2 服务端头像图库
- `database.py`：新表 `avatar_gallery (id TEXT PK, label TEXT, data_url TEXT, created_at TEXT)`，`CREATE TABLE IF NOT EXISTS`（幂等）。
- 图片入库口径**沿用头像压缩**：`readAvatarFile` 方形 256px webp（~20KB），**非** Phase 2 聊天图的 1.5M 口径；后端校验匹配图片 data URL 正则 + 单条 `data_url` 长度上限 400_000（与现有 persona.avatar 同闸门）。图库总量不设硬上限但列表分页/懒加载。
- 路由 `routes/bot.py`：`GET /bot/gallery`、`POST /bot/gallery`（body: label?, data_url）、`DELETE /bot/gallery/{id}`。
- 管理区：`src/components/settings/panels/BotPanel.vue`，设置里新增分类 `bot`「Bot 管理」（见 4.5）。网格展示图库、上传（走 `readAvatarFile`）、重命名/删除。

### 4.3 「你的头像」
- 存后端 `app_settings` 单键 `bot_user_avatar`（data URL，随头像口径 ≤400_000）。读写走现有 settings get/set 通道。
- 设置入口在 **Bot 管理面板**顶部「你的头像」：从图库选 / 直接上传 / 清除。
- 本地会话用户气泡新增头像位（右侧淡色/图片圈），文案"你"。

### 4.4 QQ 会话自定义名 / 头像
- `bot_conversations` 加两列 `display_name TEXT`、`display_avatar TEXT`（幂等 `PRAGMA table_info` 探测后 `ALTER`，对齐 Phase 1 迁移风格）。
- 仅 `channel == 'qq'` 的会话提供编辑入口（QQ 记录页会话标题就地改/选图）；读时展示优先用 `display_name`/`display_avatar`，为空回落现有逻辑（`peerLabel` 代号 / `GeneratedAvatar` 淡色占位）。本地会话不吃这两列。
- 头像选择复用图库 + 上传；`display_avatar` 存单份 data URL（≤400_000），不在消息里复制。
- 路由：新增 `PATCH /bot/conversations/{id}`（body: display_name?, display_avatar?），服务端校验渠道为 qq 且字段合法。

### 4.5 设置新增「Bot 管理」分类
- `stores/settings.ts`：`SettingCategory` 联合类型加 `'bot'`。
- `SettingsDialog.vue`：`PANELS` 加 `bot: { label: 'Bot 管理', icon: IconBot, panel: markRaw(BotPanel) }`（单一清单，编译期兜底）。
- `settings-nav-icons.ts`：加自绘 `IconBot`；同步 `settings-nav-icons.test.ts` 的类目断言。

### 4.6 头像选择器统一
- 抽一个 `AvatarPicker`（预览 + 「从图库选」弹层 + 「上传」按钮，上传可选"同时存入图库"）；`PersonaEditor`、Bot 管理「你的头像」、QQ 会话头像三处复用，避免三份重复逻辑。

## 5. 错误与边界
- 非图片/超大 data URL：后端 422/400，前端 `toFriendlyMessage` 友好提示。
- 图库项被引用后又删除：persona/conversation/user 头像存的是**自己的 data URL 副本**（非引用 id），删图库项不影响已设头像 —— 规避悬空引用。
- 明/暗主题淡色可读：占位用低饱和高亮背景 + 深字，避免纯白纯黑。
- seed 为空/脏：回落固定淡色 + 通用首字，绝不空白/破图。

## 6. 测试策略
- 后端：`avatar_gallery` CRUD、`bot_user_avatar` 读写、QQ 会话 `display_name/display_avatar` 迁移幂等 + PATCH（非 qq 拒绝）+ 校验（非法 data URL/超大 422）；`get_or_create`/`list` 读回带新列且归一。
- 前端纯逻辑单测：`avatarPlaceholderStyle` 同种子稳定、不同种子异色、空种子兜底。
- 守卫测试：`GeneratedAvatar` 不含 identicon/马赛克、含淡色占位；`BotPanel` 含图库上传；QQ 会话改名入口存在。
- 全量门禁：后端 `pytest` 全绿、前端 `vitest` 全绿、`typecheck` clean；QQ 只读/纯文本用例不回归。

## 7. 分阶段建议（供计划拆分）
1. 后端：图库表+路由、`bot_user_avatar` 设置、QQ 会话两列+PATCH+读回归一。
2. 前端 util+组件：`avatarPlaceholderStyle`、`GeneratedAvatar`/`PersonaAvatar` 去马赛克、`AvatarPicker`。
3. 设置「Bot 管理」分类 + `BotPanel`（图库管理 + 你的头像）。
4. QQ 记录页会话改名/选头像；本地用户气泡头像位。
5. PersonaEditor 头像改为 `AvatarPicker`（从图库选/上传）。
6. 收尾全量验证 + CodeReview。

## 8. 假设（复核时如不符请指出）
- 「给自己设头像」用于**本地会话**（QQ 里你不发言）。
- 图库图片按**头像口径**（方形 256 webp）存，不作为大图查看。
- 群内按真人分头像**本设计不做**，作后续期。
- QQ 会话显示名/头像仅**本地覆盖**，不影响 QQ 真实群、不改真实数据。
