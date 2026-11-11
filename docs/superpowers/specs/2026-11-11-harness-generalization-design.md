# 通用 Harness 化设计 — 第一刀：身份通用化

- 日期：2026-11-11
- 状态：设计已逐节确认（边界划分 / 数据流与迁移 / 测试守卫）
- 范围：仅第一刀（身份通用化）。本地执行后端与代码专家为第二刀，见 §9。
- 引用口径：文中 `文件:行号` 均为 **2026-11-11 改动前** 已核实的锚点；实现后行号会漂移，以符号名为准。

## 1. 背景与目标

现状产品默认身份是运维智能体：未选专家时，非对话任务注入运维身份（`SSH_MODE_PROMPT`）与运维方法论（`OPS_METHOD_*`），纯对话任务才用中性身份（`CHAT_MODE_PROMPT`）。身份切换由意图判定驱动（`backend/routes/agent.py:220`，已核实）。

目标：**通用 harness 是默认身份，运维是可选专家之一**。领域覆盖运维 / 代码 / 研究 / 文案，且领域不硬编码——专家包可增删。

有利条件（已核实）：

- Harness 核心（Driver / Reins / Saddle / Stirrups / Bridle）不含运维逻辑，是通用 ReAct 驾驭装备（`backend/agent/harness/driver.py:166`）。
- 三层扩展体系已就位：Skill = 过程性知识（怎么做）；Expert = 运行策略配置包（只能收紧不能放宽）；Plugin = 可执行能力供给（`backend/agent/expert_policy.py:3`）。
- 专家 CRUD、前端选择入口（`src/components/settings/panels/ExpertsPanel.vue`、HomeComposer）、身份段注入机制（`backend/routes/agent.py:992`）均已存在。
- 中性对话身份已存在（`backend/prompts.py:36`）。

## 2. 决策记录

| 决策 | 结论 | 理由 |
| --- | --- | --- |
| 专家选择方式 | 手动为主 + 自动建议 | 建议经用户确认才生效；不静默换安全姿态，与项目 fail-closed 文化一致 |
| 代码执行落点 | 默认本地，连接 SSH 则远程 | 本地执行后端是新增子系统，留第二刀 |
| 第一刀 | 身份通用化先行 | 最小、最快验证专家边界；研究/文案纯生成即可用，运维零回归 |
| 方案 | A：身份随专家走 | 唯一真正成立"通用是默认"的方案。B（双轨保留）默认身份仍是运维，不达目标；C（全自动路由）与手动为主冲突 |

## 3. 非目标（第一刀不碰）

- 本地执行后端与本地安全姿态（第二刀）
- 代码专家包（依赖第二刀的本地执行）
- Harness 循环、权限/审计、记忆、反思/进化机制
- cKunBot 子系统（与运维 Agent 零打通是设计，`CONTEXT.md:25`）
- 产品改名

## 4. 架构：边界划分

### 4.1 核心保留（领域中性）

- **中性基础身份**：由 `CHAT_MODE_PROMPT` 演化——通用 AI 助手 + 跨领域纪律（如实汇报、不调不存在的工具、时间注入、记忆使用指引）。去掉其中"本轮是纯对话"的任务性措辞（基础身份不得自称纯对话）；纯对话引导仍由对话分支单独注入。
- **意图机制保留但降级**：conversation 门控（纯对话物理清空工具表）是通用机制，不动；`is_change` 保留为通用任务元数据；`None → 按变更处理` 的不对称取舍原样保留（`backend/routes/agent.py:247`）。意图判定不再决定身份。
- 专家解析、Harness、权限/审计、记忆、反思/进化：零改动。

### 4.2 搬进运维专家包（三块运维味文本）

1. 角色段 `SSH_MODE_PROMPT`（`backend/prompts.py:10`）→ 运维专家 `identity_prompt`。
2. 方法论 `OPS_METHOD_READONLY` / `OPS_METHOD_CHANGE`（`backend/prompts.py:48` / `:55`）→ **内置运维技能**，稳定 slug（如 `ops-methodology`），含两变体 + 选择规则：引用任务性质提示选变体；CHANGE 第 0 条的"实际只读自行降级"条款保留。
3. 工具效率引导中的运维味段落（log_cleanup allowlist、生产环境删除纪律等，`backend/routes/agent.py:254`）→ 运维技能；通用部分（优先专用工具、避免重复读取、offset/limit 片段读取）留核心。

### 4.3 四个内置专家包

| 专家 | slug | 身份 | 工具 | 记忆 scope |
| --- | --- | --- | --- | --- |
| 通用（新默认） | `general` | 无叠加，全继承 | 全继承 | inherit |
| 运维专家 | `general_ops`（不变） | 运维角色段 + 运维技能 | 全继承 | inherit |
| 研究 | `research` | 研究员身份 | 收紧：web 搜索/记忆/todo | global |
| 文案 | `copywriting` | 文案身份 | 收紧：web 搜索/记忆/todo | global |

- "无执行"的口径：工具集不含 `shell_exec` 与全部远程执行/文件工具（`ops_*`、`file_*`）。具体工具名以实现时工具注册表为准，守卫按交集断言。
- `general_ops` slug 保留：它是落库稳定键（`backend/database.py:795` 种子，已核实）。`DEFAULT_EXPERT_SLUG` → `general`（`backend/agent/expert_policy.py:29`）。
- 内置专家 API 不可编辑/删除（`backend/db/experts.py:137` / `:153` 的 `is_builtin=0` 守卫，已核实），配置以代码为权威。

## 5. 数据流：Prompt 组装新顺序

```
时间 → 中性基础身份 → 专家身份段 → workflow 姿态 → goal → 画像块
     → 用户 system_prompt
     → [对话分支: 纯对话引导 | 非对话分支: 通用工具纪律 + 任务性质提示]
     → 插件段 → (SkillRein 注入运维方法论技能)
```

三个关键变化：

1. **专家身份段从末尾追加挪到基础身份之后**。现状追加在整个 prompt 最后（`backend/routes/agent.py:994`），与组装注释"身份层放最靠前以获得最高注意力"自相矛盾；运维角色段原在第 2 位，挪末尾等于降权。
2. **任务性质提示通用化**：非对话分支注入 `[任务性质] 只读 / 可能变更 / 未判定（按变更对待）`。这是通用元数据，不是运维逻辑；运维技能引用它选变体。
3. **方法论走现有 SkillRein 注入通道**，不新开场。

已确认的取舍：方法论从"system prompt 按 is_change 确定性二选一注入"变为"技能注入 + 提示 + 模型自选变体"。运维 prompt 不再与旧版字节等价，回归守卫改为**语义等价**（角色段、方法论两变体文本、工具集三者齐全）。

## 6. 兼容与迁移

一次 `_SCHEMA_VERSION` 升级（`backend/database.py:812`，已核实：版本最后落盘，中途失败下次启动重来）：

| 动作 | 口径 |
| --- | --- |
| 种 `general` / `research` / `copywriting` | `INSERT WHERE NOT EXISTS slug`（与现有种子同口径） |
| 改 `general_ops` 内置行 | `UPDATE WHERE slug='general_ops' AND is_builtin=1`：名→运维专家、description、identity_prompt→运维角色段、skill_ids→运维技能。**is_builtin=0 的用户复制行绝不碰** |
| builtin 工厂 | `builtin_general_ops()` 改为运维包配置；新增 `builtin_general()` / `builtin_research()` / `builtin_copywriting()` |

**无人值守入口显式绑专家（防"静默失去方法论"回归闸）**：

- Runbook 头less run 天然运维 → 显式绑 `general_ops`。
- 存量定时任务迁移时绑 `general_ops`（保持旧行为）；新建定时任务提供专家选择器，默认 `general`。

**前端建议交互（非阻塞）**：

- 触发：未选专家 + intention 判非纯对话 + 命中现有运维 marker 词表（变更动词/服务器/端口/504 等，`backend/agent/intention.py` 的 `_CHANGE_MARKERS` 等，已核实）。注意 intention 输出的是任务性质（QUERY/CHANGE/CONVERSATION）而非领域标签，故谓词 = 任务性质 + 关键词级词表命中，零新分类器；具体谓词由实现计划钉住。
- 形态：SSE 建议事件 → 输入区上方 chip："检测到运维任务，建议切到运维专家 **[切换并重跑] [忽略]**"。
- 当前 run 不阻塞继续跑（通用身份 + 全工具，能力不缺只是缺方法论纪律）；"切换并重跑"复用现有停止 + 重发机制。
- 建议判定失败静默 no-op，不拖 run。

## 7. 错误处理

- 专家配置损坏 fail-closed 抛 `ValueError`（现有铁律，不动；绝不把解析失败的 JSON 当继承）。
- 迁移幂等：中途失败 schema 版本不落盘，下次启动重来（现有机制）。
- 建议事件是增益不是关键路径：任何异常不得影响 run 本身。

## 8. 测试守卫

项目惯例：文件名只写守什么（`test_<域>_<守什么>.py`）；**每个新守卫先证伪一次**（先红后绿）；ci-check.sh 是唯一门禁。

### 存量迁移

| 测试 | 处理 |
| --- | --- |
| `test_expert_policy_legacy_noop.py` | 红线基准 general_ops → `general`：未选专家 ≡ general，六个运行面 + 身份段空 + 快照仅归因差异 |
| `test_expert_policy_safety_intersection.py` | 求交语义不动；"全继承基底"夹具换 `builtin_general()` |
| `test_expert_routes_crud.py` 种子测试 | 更新为四个内置专家 |

### 新守卫

| 守卫 | 守什么 | 证伪方式 |
| --- | --- | --- |
| 运维包语义等价 | 运维专家解析后：角色段关键句 + 方法论两变体齐全 + 工具/权限全继承 | 摘掉技能引用 → 红 |
| 核心 prompt 中性 | general 策略下 system prompt 不含"智能运维助手 / 远程运维方法 / log_cleanup 纪律" | 把 SSH_MODE_PROMPT 注回核心 → 红 |
| 领域包收紧 | research/copywriting 工具集 ∩ 执行工具 = ∅；memory_scope=global；全局禁用项不被复活 | 给文案包加 shell_exec → 红 |
| 身份段位置 | 专家身份紧随基础身份、先于 goal/画像 | 挪回末尾追加 → 红 |
| 任务性质提示 | 非对话含提示行，None→按变更；对话分支不含 | 删掉 None 分支 → 红 |
| 迁移幂等与边界 | 旧库迁移后四内置齐全；is_builtin=0 用户行逐字节不动；二次迁移无变化 | 去掉 is_builtin=1 条件 → 红 |
| 无人值守显式绑定 | runbook run 解析到运维专家；存量 schedule 迁移后绑 general_ops | 解绑 → 红 |
| 建议信号 | 未选专家+运维信号→发事件；已选专家→不发；判定失败→静默 no-op | 失败路径抛异常 → 红 |
| 前端 chip | 建议 chip 渲染 [切换并重跑][忽略]，非阻塞 | guards.test.ts 断言缺失 → 红 |

对话门控（conversation 物理清空工具表）既有守卫保持绿。

### 验证节奏

开发期靶向 pytest + typecheck/vitest；整刀完成后跑一次 ci-check.sh 全量门禁（约 20 分钟，期间不改树）。运维零回归判据：运维包语义等价守卫 + 存量运维相关测试全绿。

## 9. 第二刀展望（仅记录方向，不在本 spec 实现）

- **本地执行后端**：本机文件/shell 执行 + 独立本地安全姿态；执行目标策略进专家策略包——运维专家保持 SSH-only fail-closed，不静默降级本机。
- **代码专家包**：身份 + 本地/远程执行工具 + 代码纪律技能；执行落点"默认本地，连 SSH 则远程"。
- **自动建议扩展**：从运维信号扩展到代码/研究/文案信号。

## 10. 验收标准

1. 未选专家：中性身份，prompt 无运维味文本（中性守卫绿）。
2. 选运维专家：语义等价守卫绿，存量运维相关测试全绿。
3. 研究/文案专家可选，执行工具物理缺席。
4. 旧库迁移：四内置齐全，用户自定义行逐字节不动，二次迁移幂等。
5. 无人值守：runbook 与存量定时任务保持方法论。
6. 建议 chip：运维信号触发、非阻塞、切换并重跑可用。
7. ci-check.sh 全量绿。