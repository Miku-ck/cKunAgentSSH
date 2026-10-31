# 计划到期回溯：让主动消息有真实依据（spec 2026-10-31-plan-followup）

## 背景

陪聊主动性（`bot/proactive.py`）的机制层已经完整：`desire()` 确定性推导意愿、
四重抑制闸、分渠道投递、冷场冷却阶梯。真正的问题在**内容层**——她开口时没有
足够具体的理由。

`pick_topic()` 现在只有三个来源：

| 来源 | 依据 | 效果 |
| --- | --- | --- |
| `fatigue_care` | 上一轮停在用户喊累 | 有时效，命中率低 |
| `fresh_fact` | 3 天内新写入的记忆 | 是「陈述事实」，不是「接续事件」 |
| `old_memory` | 旧记忆低频翻（每日概率 0.12） | 像「忽然想起」，但无具体依据 |

两个约束同时封死了可选素材：

1. **人格宪章禁止编造经历**（`bot/character.py` SAFETY_CHARTER 第 2、5 条）：
   不能把「我上周去吃了火锅」当真实经历引用，不能假装有身体需求。所以「给她
   一份虚拟生活线」这条路是关着的。
2. **真实素材被拍平了**：`bot/memory.py` 的「计划」抽取规则把时间词放在非捕获组，
   「周末要去看展」最终只留下「用户的计划：看展」——**时间点被丢弃**。

于是最像「真人突然想起你」的那类素材（你说过的事到期了）根本没有数据支撑。

本 spec 让**计划到期回溯**落地：用户说过「周末要去看展」，周日过后她主动问一句
「之前你说要去看展，后来去了吗」。所有依据来自用户亲口说过的话，不产生任何虚构。

## 数据现状（实施前核实）

| 事实 | 影响 |
| --- | --- |
| bot 记忆复用 agent 的 `memories` 表（`scope=persona:{id}`，`kind=persona_fact`） | 不新增记忆表 |
| `memories.valid_at` 全项目零引用；`invalid_at` 已被 `mstore.retire` 占用为失效时间 | 不能借用 `invalid_at` |
| `_write_facts` 按 `content` 精确去重 | 同一件事换个时间说，**记忆不会更新**，时间锚必须独立存放 |
| 「计划」规则 `importance=0.4` | 是 `maintain_persona_memories` 首批降权淘汰对象，追问素材不能依赖记忆存活 |
| `memories` 无 metadata/payload 列 | 结构化时间只能另存 |

## 设计

### 1. 链路总览

```
用户说「周末要去看展」
  → memory.extract_facts        : plan_text="看展", when_text="周末"
  → plan.record_hooks           : 解析窗口 → bot_plan_hooks(state=pending)
  → memory._write_facts         : 记忆照旧落库（与钩子互不依赖）
  ... 窗口结束 ...
  → pick_topic 命中 plan_due    : 高优选题（低于 fatigue_care）
  → _compose 照旧生成           : 「之前你说要去看展，后来去了吗」
  → _deliver 成功               : state=asked（只问一次）
  → 超窗 36h 未开口              : state=expired
```

### 2. 时间窗口模型

以**用户那句话的时刻**（本地时区）为基准 `base` 计算窗口。全项目时区口径统一
取 `astimezone()`，与静音窗判断同源，不引入 UTC/本地混算。

| 时间词 | 窗口 |
| --- | --- |
| 待会 / 等会儿 | `[base, base+4h)` |
| 今晚 | `[当日 18:00, 次日 00:00)` |
| 明天 / 后天 | 对应整日 `[00:00, 次日 00:00)` |
| 周末 | 本周 `[周六 00:00, 下周一 00:00)`；若 base 已是周日，取当日整日 |
| 下周 | `[下周一 00:00, 再下周一 00:00)` |
| 下个月 | `[下月 1 日 00:00, 下下月 1 日 00:00)` |

窗口一律**左闭右开**：`due_end` 是「这件事可能已经结束」的最早时刻，
`due_end <= now` 即进入可回溯区间。

三条硬规则：

- **解析不出窗口就不建钩子**，静默放弃。宁可不问，不能问错。
- **不需要额外的窗口上限常量**：时间词表最长的是「下个月」（最多 62 天），
  窗口天然封顶。
- **时间词只参与窗口计算，绝不进入文案**。「下个月」这类相对时间在 30 天后
  说出来是错乱的，文案恒为「他之前提过：{plan_text}，问问后来怎么样了」，
  由模型自行组织语气。

两个时间常量（都定义在 `bot/plan.py`）：

```python
PLAN_FOLLOWUP_H = 36      # 窗口结束后多久之内还可以问
PLAN_NEAR_WINDOW_H = 4    # 「待会 / 等会儿」的窗口长度
```

### 3. 抽取层（`bot/memory.py`）

「计划」规则把时间词变成命名组：

```python
(
    "计划",
    re.compile(
        r"(?:我)?(?P<when>明天|后天|下周|周末|下个月|今晚|待会|等会儿)\s*"
        r"(?:要|想|准备|打算|得去)?\s*(?P<x>[\u4e00-\u9fa5A-Za-z0-9]{2,25})"
    ),
    "用户的计划：{x}",
    0.4,
    MULTI,
),
```

`Fact` 增加两个字段（frozen dataclass 带默认值，向后兼容）：

```python
@dataclass(frozen=True)
class Fact:
    title: str
    content: str
    importance: float = 0.8
    confidence: float = 0.9
    slot: str = MULTI
    when: str = ""      # 计划的时间词（「周末」），其余规则为空
    subject: str = ""   # 计划正文（「看展」），其余规则为空
```

`_build` 用 `match.groupdict().get("when", "")` 与归一后的 `groups["x"]` 分别填充。
`subject` 存在的理由：钩子要独立于记忆存活，直接把 `plan_text` 存进表里，
不能靠对 `content` 做「用户的计划：」前缀切片（前缀文案一改就会静默错位）。

**`content` 保持不变**（仍是「用户的计划：看展」），时间词不进记忆正文。理由：

- `content` 会整段注入 prompt、在「它记得你什么」面板逐条展示；
- 塞进「（周末）」会让面板长出「用户的计划：看展（周末）」「用户的计划：看展
  （下周）」两条近乎重复的条目，同时污染注入文本；
- `_write_facts` 按 content 去重，格式化正文还会放大去重误伤面。

### 4. 钩子写入与记忆去重解耦

用户先说「周末要去看展」、两天后说「下周要去看展」，第二次 content 相同会被
`_write_facts` 直接跳过——但**时间必须更新**。所以钩子由抽取结果独立驱动：

```python
# bot/memory.py remember_async 内
facts = extract_facts(user_text)
plan.record_hooks(persona_id, conversation_id, facts, base=datetime.now().astimezone())
return _write_facts(persona_id, conversation_id, facts, vectors)   # 原有逻辑不动
```

`base` 用处理时刻近似用户发言时刻（与 `bot_messages.created_at` 同一基准，
两者相差在秒级，对「今晚 / 明天 / 周末」这类粗粒度窗口不构成影响）。

`record_hooks` 只处理 `when` 非空的 Fact，内部 `try/except` 兜底，异常只记
warning，不影响记忆写入与聊天主链路。

### 5. 钩子表

```sql
CREATE TABLE IF NOT EXISTS bot_plan_hooks (
    id              TEXT PRIMARY KEY,
    persona_id      TEXT NOT NULL,
    conversation_id TEXT,                 -- 溯源用，可为空
    memory_id       TEXT,                 -- 溯源用；记忆被淘汰后失效，不参与判定
    plan_text       TEXT NOT NULL,        -- 「看展」
    when_text       TEXT NOT NULL,        -- 「周末」，审计与调试用
    due_start       TEXT NOT NULL,
    due_end         TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'pending',
    asked_at        TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plan_due ON bot_plan_hooks(state, due_end);
CREATE INDEX IF NOT EXISTS idx_plan_persona ON bot_plan_hooks(persona_id, state);
```

落位在 `init_db` 的 `CREATE TABLE IF NOT EXISTS` 区。**这是真正的结构变更，必须
`_SCHEMA_VERSION` 5 → 6**：`init_db` 在 user_version 命中时整段跳过 DDL
（`database.py` 第 96 行的铁律），不升版本老库会永远缺这张表，运行时报
`no such table`。同时把 `_SCHEMA_SOURCE_FINGERPRINT` 更新为实测值
（`test_startup_critical_path.py` 有守卫，会打印正确值）。

与 `2026-10-30-qq-proactive-message` 的区别在于：那次只改了 DDL 注释，列与索引未动，
所以可以不升版；本次是新增表，必须升。

**钩子不依赖记忆存活**：计划记忆 `importance=0.4`，是记忆维护首批淘汰对象；
钩子自带 `plan_text`，记忆没了照样能问。`memory_id` 只做溯源，不参与任何判定。

### 6. 去重与修正

按 `persona_id + plan_text` 归一化（strip 后精确匹配，不做语义近似）：

- 同人格已有 `state='pending'` 且 `plan_text` 相同 → 旧钩子置 `superseded`，
  插入新钩子（新时间才是对的）；
- 按人格而非会话去重——她记得的是「你这个人」，单聊与群聊说的同一件事是同一件；
- `plan_text` 已由 `_strip_leading_verbs` 归一（「要去看展」→「看展」）；
- 一条消息里多个计划 → 各建一条；下次开口只取最早到期的那条。

### 7. 选题器扩展（`bot/proactive.py`）

返回值从 `tuple[str, str]` 改为 `Topic(NamedTuple)`：

```python
class Topic(NamedTuple):
    describe: str
    source: str
    plan_id: str = ""     # 仅 plan_due 来源非空
```

保留 `topic[0]` / `topic[1]` 下标访问，现有调用点与测试不用改。

优先级：`fatigue_care` > `plan_due` > `fresh_fact` > `old_memory`。
理由：疲劳是用户当下状态，最硬；计划有 36h 时效窗，比泛泛的新事实更具体；
旧事钩子仍在最后。

查询（一条索引查询，走 `idx_plan_due`）：
`state='pending' AND due_end <= now AND due_end >= now - PLAN_FOLLOWUP_H`，
`ORDER BY due_end ASC LIMIT 1`——最早到期的那件事先问。

### 8. 状态机

四态，**刻意不做 `answered`**：

```
pending ──_deliver 成功(send/push/queued)──> asked      (终态)
   │
   ├──同 persona+plan_text 新钩子──> superseded          (终态)
   └──due_end 超过 now-36h 仍未开口──> expired           (终态)
```

- `asked` 之后不再判任何事：只问一次，用户回不回都不影响行为。
- 加 `answered` 需要在 inbound 热路径回写、且换不来任何行为差异，不做。
- `asked` 只在 `_deliver` 返回 `True` 时标记；`skip`（平台拒收）不标记，下一轮
  重试——避免「她想说但没说成，这件事就永久作废」。
- 手动探口（`probe`）同样标记 `asked`：试发也是真发了，点了按钮这件事就算问过。
  与现有 probe 占 12h 间隔同哲学，不拿「只是试一下」骗自己。
- 过期清理：`tick()` 开头与 `expire_stale_proactive` 并列调
  `db.expire_plan_hooks(now - 36h)`，保留审计行不物理删。

### 9. 决策层改动（plan_due 独立权重项 + 硬底线例外）

`desire()` 的硬底线是 `intimacy < 20` 时无条件压到阈值以下，注释写的是
「不熟的人哪怕话说一半也不打扰」。但计划回溯不是未完话题，是「你亲口说过的
事」，对陌生关系恰恰是最自然的开口依据；不处理的话，这个功能在新人格上完全
不可见（`intimacy` 建表默认 5，永远过不了闸）。

**不能复用 `pending_topic` 的 0.25 权重**——算一遍就知道行不通：
`intimacy=10` 时 0.30 项只有 `0.30×sigmoid(-1)=0.081`，加 novelty 0.08 与
pending 0.25 才 0.41，离 0.55 还差 0.14。复用了权重却不破底线，等于什么都没改。

所以 `plan_due` 做成**独立权重项 0.40**，比 `pending_topic` 的 0.25 更硬：
它有时效性（36h 窗口，过期作废），而未完话题可以等下次。

```python
desire(state, *, speed, pending_topic, gap_hours, plan_due: bool = False) -> float
# 权重：0.40 * plan_due（独立项，不与 pending_topic 叠加）
```

硬底线改为 `if intimacy < 20.0 and not plan_due:`。

数值校验（`intimacy=15, novelty=60, energy=0.8`）：
`0.30×sigmoid(-0.75)=0.096` + `0.20×0.6=0.12` + `0.40` = **0.616 ≥ 0.55**，
带 plan_due 能过、不带则 0.216 过不了。极端冷淡（`novelty=0, energy=0.5,
tension=50`）时 `0.096+0.40-0.30-0.25 < 0`，仍被闸拦住——放行的是「有具体
依据时能开口」，不是「无视状态乱开口」。

四重抑制闸（12h 间隔 / 静音窗 / 冷场冷却 / 日配额）全程不变。

### 10. 错误处理

| 层 | 失败 | 处置 |
| --- | --- | --- |
| 抽取 | 时间词解析失败 | 不建钩子，`logger.debug`，记忆照常写入 |
| 写入 | 钩子表读写异常 | `try/except` 包住，`logger.warning`，`remember_async` 正常返回 |
| 选题 | 钩子查询异常 | 复用 `tick` 现有 `topic=None` 回退，退到自由发挥 |

### 11. 日志与 reason 口径

`bot_proactive_log.reason` 已有 `ok:{选题来源}` 约定，`plan_due` 直接接入：

| 场景 | action | reason |
| --- | --- | --- |
| 计划回溯开口（local） | `send` | `ok:plan_due` |
| 计划回溯开口（QQ 主动/召回） | `push` | `ok:plan_due` / `wakeup{n}:ok:plan_due` |
| 计划回溯开口（QQ 降级寄存） | `queued` | `queued:{tag}:plan_due` |

不新增埋点，现有 `proactive-log` 面板即可按来源筛选。

### 12. 文案注入

`_compose` 的 topic 分支已要求「围绕这件事开口、别把原话一字不差背出来」，
无需改动。选题描述固定为：

```
他之前提过：{plan_text}，问问后来怎么样了
```

不带时间词、不带具体日期。

## 代码落位

| 文件 | 改动 |
| --- | --- |
| `backend/bot/plan.py` | **新增**：`plan_window(when_text, base)` 纯函数 + `record_hooks` + `due_hook` 查询封装；`PLAN_FOLLOWUP_H` |
| `backend/bot/memory.py` | 「计划」规则加 `when` 命名组；`Fact.when` / `Fact.subject`；`_build` 取值；`remember_async` 调 `record_hooks` |
| `backend/bot/proactive.py` | `Topic` NamedTuple；`pick_topic` 加 `plan_due` 分支与优先级；`desire` 加 `plan_due` 参数；`_deliver` 标记 `asked`；`tick` 过期清理 |
| `backend/database.py` | `bot_plan_hooks` DDL + 索引；`_SCHEMA_VERSION` 5 → 6；`add_plan_hook`（内部 supersede 同文本 pending）/ `due_plan_hook` / `mark_plan_asked` / `expire_plan_hooks` |
| `backend/tests/` | 新增 `test_bot_plan.py`；`test_bot_proactive.py` 补选题与标记用例；`test_bot_memory.py` 补抽取用例；`test_startup_critical_path.py` 升版本 + 更新指纹 |

代码注释统一用 `spec 2026-10-31-plan-followup` 标签串联。

## 不做

- **`answered` 状态**：只问一次，回不回不影响任何行为。
- **计划放弃的语义识别**（「不去了」「取消了」）：需要 NLU 判断，误判代价高于
  收益。用户回一句「没去成」本身就是自然对话，不算错。
- **事前提醒**（「明天那个展别忘了」）：已评估并放弃——有行动价值，但对不熟的
  关系像唠叨，且沾日程助手味（宪章第 6 条不执行命令）。
- **接入真实世界其它事件源**（时间节律、关系里程碑、用户作息、运维任务）：
  本轮只做计划回溯，其余作为后续独立 spec。
- **虚拟生活线**：与 SAFETY_CHARTER 第 2、5 条冲突，不碰。
- **前端面板展示待回溯计划**：`proactive-log` 的 `ok:plan_due` 已够排障，
  界面留到有需求时再加。
- **复用 `memories.valid_at`**：该列是 agent 侧预留的语义列，占用会在未来冲突，
  且追问状态依然无处安放。

## 测试

- `plan_window` 纯函数：7 个时间词 × 跨月 / 跨年 / 周日说「周末」/ 23:59 说
  「今晚」/ 脏文本返回 `None`；固定 `base` 断言可重放。
- `extract_facts`：「周末要去看展」→ `when="周末", plan="看展"`；`content` 仍不含
  时间词；无时间词的计划不产生 `when`。
- `record_hooks`：同文本不同时间 → 旧钩子 `superseded`；不同 persona 互不干扰；
  解析失败不落行；记忆被去重跳过时钩子照常写入。
- `due_hook`：`due_end == now` 边界、`now-36h` 边界、状态过滤。
- `pick_topic`：`fatigue_care` 压过 `plan_due`；`plan_due` 压过 `fresh_fact`；
  返回 `Topic` 且 `plan_id` 非空。
- `_deliver`：成功标记 `asked`；`skip` 不标记；`probe` 也标记。
- `tick`：过期清理生效；记忆行被淘汰后钩子仍能触发。
- 启动指纹测试：`bot_plan_hooks` 纳入表结构断言。
