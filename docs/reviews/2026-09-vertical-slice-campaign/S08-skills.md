# S8 · 技能系统

锚点 `a8e588c`。范围：`routes/skills.py`(5 端点) / `skill_registry.py` / `tools/builtin/load_skill.py` / `builtin_skills.py` / `skills_builtin/` / `main.ts` skills IPC / `SkillsPanel.vue` + `stores/skills.ts` / `client.ts:560-566`。

## 缝表（本切片契约，已核对为**一致**）

| 渲染层 | 端点 | 路由 | 判定 |
|---|---|---|---|
| `client.ts:560 listSkills(mode?)` → `/api/skills?mode=` | GET | `routes/skills.py:29` | ok，`mode` 为内部常量（chat/ssh），手拼 query 无编码风险面 |
| `stores/skills.ts:239` 显式传 `CHAT_MODE` | — | — | ok，且 `:267` 对 `mode_enabled` 缺失做了旧后端退回：`item.mode_enabled === undefined ? item.enabled === 1 : !!item.mode_enabled` |
| `client.ts:563 updateSkillEnabled(id, enabled, mode?)` | PATCH | `:48` | ok |
| 删除 / sync-builtin / sync-packaged | POST/DELETE | `:84 / :110 / :168` | ok |

`TS 侧 Skill.enabled: boolean`（`stores/skills.ts:23`）对线上 int 0/1 是一处**类型谎报**，但唯一读取点 `:267` 已按两种形态收敛为真 boolean，无消费者会踩到 → 不作发现，仅登记。

## 发现表

| ID | 维度 | 严重度 | 标题 | 主 file:line | 可达性 |
|---|---|---|---|---|---|
| S8-01 | d | Minor | **LEGACY #5 确认成立**（实测重复块为 6+1 行，不是当年记的「前导三行」） | `skill_registry.py:169-174 + :176` vs `:201-206 + :208` | 恒（纯冗余） |
| S8-02 | a | Minor | `build_skill_catalog` 的 `json.loads` 无兜底，而同文件 `match_skills` 有 → 一行脏 `trigger_words` 令技能目录静默消失 | `skill_registry.py:185` vs `:116-119` | 需 DB 行为非法 JSON |
| S8-03 | b | Minor | `ReflectionRein` docstring 声称「路由层只需监听该事件即可触发」，而路由从不监听 `reflection_request` | `lifecycle.py:93` vs S1-06 | 恒（文档失真） |
| S8-04 | a | **Major** | 技能在设置里关掉/删掉后，Agent 仍可能继续用旧的技能集：`_reload_skill_registry()` 吞异常，而 `reload()` 失败或被 5s 节流跳过时缓存仍标记已加载 ⇒ 读侧永不自愈 | `routes/skills.py:16-22,74,80,90,160` ↔ `skill_registry.py:28-43`（reload）/ `:37`（_loaded）/ `:65,149,156`（读侧 lazy 自愈） | ✅（HEAD 逐行核过；工作树副本因并行会话在改而下移 ~7 行） |

## 详情

### S8-01 [dim d][Minor] LEGACY #5 重判：成立，且比当年记载的更大
`build_skill_catalog` 与 `build_skill_preload` 的前导**完全逐字相同**，共 7 行而非 3 行：

```
skills = self.get_skills_by_ids(skill_ids)          # :169 / :201
enabled = self.get_enabled_skills(mode)             # :170 / :202
enabled_ids = {s['id'] for s in enabled}            # :171 / :203
skills = [s for s in skills if s['id'] in enabled_ids]  # :172 / :204
if not skills:                                      # :173 / :205
    return ""                                       # :174 / :206
skills.sort(key=lambda s: s.get('priority', 0), reverse=True)  # :176 / :208
```

- 两个 `enabled_ids` 集合推导还各自触发一次 `_disabled_ids_for_mode` → **每次装配 prompt 都真读一次 DB**（`:48` docstring 明写「每次真读」是刻意的，但两遍是同一次调用的重复，不是设计）。
- 抽取方向显然（一个 `_filter_enabled_in_mode(skill_ids, mode) -> list` 返回已排序列表），属零行为变更；按本轮「只报告」口径不动。
- 与 91 清单第 10/11/13 条不同：那几条是「合并需 adapter / 参数化回调，收益低于风险」，本条是**逐字相同的 7 行**，不在当年驳回的理由内。

### S8-02 [dim a][Minor] 同一份脏数据，两个方法一个防一个不防
- LOCATION：`skill_registry.py:185`（`json.loads(s.get('trigger_words','[]'))` 裸调）← 调用链 `reins/lifecycle.py:60 asyncio.to_thread(registry.build_skill_catalog, …)` → 被 `lifecycle.py:77-78` 的 `except Exception … logger.warning("技能注入失败")` 吞掉。对照 `skill_registry.py:116-119`：`match_skills` 对**同一个字段**包了 `try/except → trigger_words = []`。
- CLAIM：一行非法 JSON 的 `trigger_words` 会让 `build_skill_catalog` 抛 `json.JSONDecodeError`；因抛点在 catalog 组装中途，`lifecycle.py` 的宽 catch 使整次技能注入**整体作废**（auto 匹配已跑完的结果一并丢），run 照常执行但 system_prompt 里没有任何技能目录，用户侧只见「技能开了却不生效」。同文件的 `match_skills` 已承认这种行可能存在并做了逐条降级，catalog 侧漏了同款防护。
- 可达性：需要库里存在非法 JSON。写侧路径（`routes/skills.py:138`、`builtin_skills.py:86/117`）都是 `list[str]` 经 `json.dumps`，`database.py:497` 的 `ALTER TABLE … DEFAULT '[]'` 也会给旧行填合法默认 ⇒ **正常路径产不出脏行**，只有手工改库/外部迁移会。定 Minor 正因如此；但 `post_processor._parse_skills_lenient`（S8↔S9 边界）的存在说明作者认定过「技能正文可能畸形」这一现实。
- 佐证：`_MAX_CONTENT_LEN`/截断说明（`load_skill.py:15-22,63-69`）与 `skills_builtin` 三包（`linux-ops-triage`/`nginx-service-diagnose`/`ops-change-safety`）均正常；`load_skill` 本身**无发现**（slug 先 `.strip()` 再查、模式校验、截断已在描述里如实声明、`risk_level="safe"` 只读）。仅记：`:58`/`:73` 两处把**未 strip 的原值**回显给用户/模型，与查询用的值不同形——纯观感，不单列。

### S8-03 [dim b][Minor] 反思触发通路的文档与实现各说各话（功能未坏）
- LOCATION：`lifecycle.py:88-94`（ReflectionRein docstring：「保证无论成功/失败/超上限都会推送 reflection_request 事件，**路由层只需监听该事件即可触发**异步反思任务」）↔ 锚点 `routes/agent.py:1092`（done 分支内直接 `_run_reflection_and_evolution`）与 `:1130`（`if error_seen and final_state is None:` 的补调度）。
- CLAIM：路由层从不读 `reflection_request` 事件（S1-06 已证 `src/` 与该事件零消费者、`routes/agent.py` 亦无监听点），反思的实际调度是路由自己写的两条：done 分支一条、`error_seen` 兜底一条。**docstring 描述的契约不是生效中的契约。**
- 重要澄清（避免下游切片误读为功能缺陷）：我核了锚点 `:1107-1150`，`error_seen and final_state is None` 的兜底确实覆盖了「只有 error、没有 done」的运行，因此 docstring 里担心的「失败运行经常错过反思」**并不存在**；而 `:1130` 注释还专门记录了「原实现在 error 分支内直接调度 ⇒ 同一 run 落两条策略、污染 Evolution 的 recent-10 窗口」这个已修的坑。本条只裁文档，不裁行为。
- 顺带证实与 S1-01 的分工：**客户端断连**（`:851 break`）既无 done 也无 error ⇒ `error_seen` 为假 ⇒ 补调度也不触发，反思同样缺席。这条缺席属 S1-01，不另计。

### S8-04 [dim a][Major] 关掉的技能关不掉：写成功了，运行时缓存没换
- LOCATION（`skill_registry.py` 的行号按 **HEAD**，工作树副本因并行会话在改而整体下移 ~7 行；
  `routes/skills.py` 未被占用，行号两处一致）：`routes/skills.py:16-22` `_reload_skill_registry()` 把
  `reload()` 的任何异常收成 `logger.warning` 后正常返回；调用点 `:74`（per-mode 启停）、`:80`（全局启停）、
  `:90`（删除）、`:160`（内置同步）。`skill_registry.py:28-34`（HEAD 行号）`reload()` 在距上次失败不足
  `_RETRY_INTERVAL=5s` 时**直接 return**（连查询都不发）；`:37` 只在成功时置 `_loaded = True`，失败路径
  既不置假也不抛。读侧 `:65`（`get_enabled_skills`）、`:149`（`get_skills_by_ids`）、`:156`（`get_by_slug`）
  的自愈条件是 `if not self._loaded` —— 而缓存**早就 loaded 过一次**，于是这个条件永远不成立。
- CLAIM：用户在设置面板禁用或删除一个技能 ⇒ DB 写入成功 ⇒ 路由回 200 ⇒ `reload()` 失败或被 5s 节流
  跳过（两条路都不可见）⇒ 内存里那份 `_skills` 仍是旧的、且 `_loaded=True` ⇒ 下一次 Agent run 的触发词匹配、
  技能目录、`load_skill` 全部照旧命中**用户已经关掉/删掉的那个技能**。没有自愈点：读侧的 lazy reload
  被 `_loaded` 挡住，写侧的下一次刷新要等用户再改一次技能（改的是同一条则可能被同一节流窗口吞掉）。
  删除比禁用更糟：`delete_skill` 之后技能行已不在库里，而 `get_by_slug` 仍能从缓存返回它并通过
  `enabled` 校验 ⇒ `load_skill` 把一条**已不存在的技能正文**喂给模型。
- 这不是「性能换一致」的取舍：设置面板给的是「已保存」的确定回执，Agent 用的是另一套事实。
  与 S11-01 同族（把「没生效」说成「已生效」），但方向相反、后果更硬 —— 那次是少报，这次是多跑。
- 修法（必须在 `skill_registry.py` 侧做，路由侧兜不住）：① `reload()` 返回 bool，并在失败分支
  `self._loaded = False` —— 读侧已有的 lazy reload 于是成为真正的自愈通道；② `_RETRY_INTERVAL` 只该限制
  **读侧反复查故障库**，不该让写侧的显式刷新也吃这个窗口（写侧应强制绕过节流）；③
  `_reload_skill_registry()` 不许再吞：至少 `logger.error` 带上动作与 skill_id，让「保存了但运行时没生效」
  在日志里可读。守卫形状：改完一条技能后，`get_skill_registry()` 缓存里那条的 `enabled` 必须等于刚写入的值。
- 阻塞状态：本轮记录时 `backend/skill_registry.py` 正被并行会话修改（工作树 M），②③两处改动会与之冲突，
  故只登记不动码。待其落库后按当前 HEAD 复核再修。

## 未覆盖声明

`main.ts:1229-1245` 的 skills IPC 组（4 通道，转发到 `/api/skills*`，`callBackendApi` 注入 token 的腿在 S0 已核）与 `SkillsPanel.vue`(193) 的纯展示逻辑，本轮只做了**存在性与调用方向**核对，未逐行审查——按 ⚪/🟡 面权重与「面板零测试」的既定现实，判定为低产出区，如需可单独补一轮。
