# cKunBot — 虚拟陪伴角色子系统（架构与决策记录）

cKunBot 是标题栏「Bot」入口（`Ctrl+Shift+9`）的虚拟陪伴角色：有性格、有记性、有分寸的聊天对象。与运维 Agent **零打通**——不执行命令、不调用工具，只共用模型配置与 `memories` 存储底座（靠 scope 隔离）。本文记录架构分层与关键取舍；术语口径见 [CONTEXT.md](../CONTEXT.md)。

## 分层

| 层 | 模块 | 职责 |
| --- | --- | --- |
| 人格 | `bot/character.py` | 诚实宪章（不可编辑、压尾）、情境反应库、阶段指南、注入/泄露兜底口径 |
| 表达 | `bot/voice.py` + `bot/stickers.py` | 表达 DNA（6 档位 → 可执行约束）、去 AI 味清洗、分条切分；表情包标记校验与收口 |
| 关系 | `bot/relation.py` | score 记账（只增 + 读时衰减）、阶段唯一口径、升温档参数 |
| 状态 | `bot/state.py` | 7 轴心理状态：事件→增量（0 次额外模型调用）、软饱和、冷却、基线回归、pacing |
| 记忆 | `bot/memory.py` | 事实抽取（11 类正则）、召回（FTS+单字+向量融合、三重门控）、保守衰减与容量边界 |
| 主动 | `bot/proactive.py` | desire、四重抑制闸、确定性选题器、调度器（60s 轮询，兼记忆维护心跳） |
| 渠道 | `bot/provider.py` + `channel_runtime.py` + `qq_*.py` | 5 成员 provider 协议；每机器人独立线程/长连接；入站管线平台无关 |
| 会话 | `bot/chat_service.py` + `inbound_pipeline.py` | 分节 prompt 组装、流式回复、幂等/超时兜底/分段投递 |

## 不变量（改代码前先读）

1. **宪章压尾且不可被人格字段覆盖**。人格自由文本以「设定材料」身份进 prompt（声明分区在材料之前），保存时覆盖类注入直接拒绝（`persona_text_risk`）。
2. **阶段只有一个口径**：relation 的 score。UI 进度条、`你们的关系（…）`、状态段里的「关系」全部同源；`describe_state` 的 stage 由调用方显式传入。
3. **状态层 0 次额外模型调用**。事件→增量是纯函数，可重放可单测；不引入「让模型自报状态」。
4. **人格记忆隔离**：所有维护 SQL 限定 `scope=persona:{id} AND kind='persona_fact'`；降权不碰 `updated_at`（与 `mstore.decay_memories` 同口径）。
5. **记忆宁漏不瞎编**：抽取走正则（可精确断言），敏感信息整条不抽，投毒话术不得成为事实；召回失败一律当没有记忆，绝不拖垮回复。
6. **QQ 协议内合规**：被动回复窗口内回复；主动「心里话」寄存 48h 捎带，不做任何「直接主动发」的尝试。

## 记忆治理（2026-10-29 决策）

- **衰减策略（保守）**：闲置超过半衰窗（45 天，锚点 `last_used_at`，从未注入退回 `created_at`）的记忆，每次维护最多下调一档（×0.5），地板 0.1。**只降权、不删除**；重复维护会继续淡化直至地板——这是「想不起的渐淡」，不是误伤。
- **豁免**：pinned（用户置顶）与关键事实（importance ≥ 0.85，覆盖称呼/生日/住址）永不衰减、不参与淘汰。
- **自进化回路**：被注入一次 importance +0.1（封顶 1.0）——「常想起的更牢，想不起的渐淡」。
- **容量边界**：每人格 200 条硬上限；超限按「最低热度（use_count）+ 最旧」淘汰，走 `mstore.retire(reason='bot_decay')` 留审计行，不物理删。
- **触发时机**：挂在 `ProactiveScheduler.tick` 尾部（60s 一轮、每轮最多一个人格、轮转、小事务），聊天热路径零阻塞；治理失败只记日志。
- **为什么不做 Agent 侧那套「跌破地板即 retire」**：陪伴场景漏一条记忆是可惜，错删一条是事故；保守策略 + 容量上限双保险足够。

## 安全边界（2026-10-29 决策）

| 威胁 | 防线 | 位置 |
| --- | --- | --- |
| 人格字段注入（「忽略以上设定」） | 保存时拒绝（可读错误）+ prompt 分区声明 + 宪章压尾 | `routes/bot.py` 校验器、`chat_service._system_prompt`、`character.persona_text_risk` |
| 记忆投毒（话术借抽取规则成为事实） | 事实内容过注入检测，命中即丢弃 | `bot/memory.extract_facts` |
| 输出泄露（复读口令/证件号） | 落库前按句删除命中敏感口径的句子；全文塌掉给占位句不给空串 | `character.strip_sensitive_lines`，接在 `strip_ai_smell` 之后 |
| 渠道凭据泄露 | SQL 读出口把 `app_secret` 折叠成 `has_secret` 布尔，密文不进 Python 对象；明文只在拨号时解密 | `database._BOT_CHANNEL_SELECT`、`get_bot_channel_secret` |
| 失效保护 | 选题/衰减/安全清洗全部 try-except 降级（清洗失败放行原文并 warning），与「记忆失败不影响回复」同一哲学 | 各模块调用点 |

## 测试

后端 `backend/tests/test_bot_*.py`（人格/状态/记忆/主动性/渠道/路由各有守卫，含 prompt 单一阶段口径、pacing 三态、衰减与容量、注入拒绝）；前端 `src/**/*bot*.test.ts` + `CkunBotView.guards.test.ts`（页面注册、贴底共享件、conv-menu 去重、装饰管线、pacing 消费）。
