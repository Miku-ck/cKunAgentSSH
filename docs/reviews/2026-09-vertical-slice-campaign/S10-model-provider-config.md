# S10 · 模型与 Provider 配置

锚点 `a8e588c`。范围：`routes/settings.py:70-271`（`/settings/models/*` 的 default PATCH/GET、list/create/patch/delete/test）/ `database.py` 模型 CRUD（`list_models_config:1584`、`get_model_config:1600`、`_resolve_model_pk:1558`、`create/update:1632/1688`、`set_default_model:1771`、`get_default_model:1800`、`seed_models_async:858`、`_insert_seed_model:920`、`_probe_first_model:831`、`_fix_model_name_and_id:811`）/ `llm_adapter.py` / `harness/llm_client.py` / `netconfig.py` / `post_processor.py`；渲染层 `ModelsPanel.vue`(1143)、`stores/models.ts`、`useModelSelector.ts`、`client.ts:255-297`。规格轴：`docs/superpowers/specs/2026-09-02-model-seed-env-driven-design.md`。

`database.py`、`routes/settings.py`、`src/api/client.ts`、`ModelsPanel.vue`、`stores/settings.ts`、`src/stores/ssh-ai.ts`、`ChatPanel.vue`、`backend/main.py` **全部落在并行会话的 21 个脏文件里** ⇒ 本片所有后端/类型行号取自 `git show a8e588c:<path>`（下文标 anchor），前端组件同样标 anchor 并逐一核对过引用区间。凡「工作树已在修」的，正文单列一行，按裁决口径不算重复开工单。

## 缝表

| 渲染层 | 端点 | 处理器 | 判定 |
|---|---|---|---|
| `client.ts:428 getDefaultModel()`（**无 mode 形参**） | GET `/settings/models/default` | anchor `routes/settings.py:84-94` | **契约错位** — 端点只问 SSH 档，类型面无法问全局档（S10-02） |
| `client.ts:431 setDefaultModel(id, mode)` | PATCH `/settings/models/default` | anchor `:65-81` | ok（`"global"→None`、其余→`SSH_MODE` 的第四份映射） |
| `client.ts:255-297 ModelConfigPayload` | GET `/settings/models` | anchor `database.py:1584-1598` | 逐行掩 `api_key`✓，但**声明了 `reasoning_tokens` 而面板从不提交**（S10-05） |
| `client.ts createModelConfig/updateModelConfig` | POST/PATCH `/settings/models[/{id}]` | anchor `:96-125 / :126-160` | 键集由 `test_settings_key_contract_round22.py:111-141` 双向钉住 —— 但该测试只验「前端发的后端收得住 + 后端必填前端有发」，**不验后端可选键前端在不在类型面上** ⇒ S10-05/S10-11/S10-12 全落在盲区 |
| `ModelsPanel.vue:7-8` 档位说明文案 | — | — | **对定时任务为假**（S10-01） |
| `ModelsPanel.vue:114 {{ m.top_p ?? 0.9 }}` | — | anchor `llm_client.call():45-54`（**无 top_p 形参**） | 「能显示、能写、不改行为」（S10-04） |
| `POST /settings/models/{id}/test` | anchor `:168-255` | base_url 原样进日志与响应 detail（S10-03/S10-13） |

## 发现表

| ID | 维度 | 严重度 | 核验 | 标题 | 主 file:line | 可达性 |
|---|---|---|---|---|---|---|
| S10-01 | a | Major | ✅ | anchor `schedule_engine._resolve_model()` 只读全局 ⊕，而面板 ★ 文案明写「SSH 默认 = 对话/终端/**定时任务**读的那档」 | anchor `schedule_engine.py:439` + `:291-293` ↔ anchor `ModelsPanel.vue:7-9` | 恒（只勾 ★ 的配置形态是面板唯一默认入口） |
| S10-02 | a | Major | ✅ | `GET /settings/models/default` 的兜底链跳过全局档直落 `list[0]` ⇒ 两个面板对「默认模型是哪条」给出不同答案 | anchor `routes/settings.py:87-92` ↔ `ssh-ai.ts:78-81`、`ChatPanel.vue:132` | 恒（只勾 ⊕ 不勾 ★ 时） |
| S10-03 | b | Major | ✅ | `base_url` 无 scheme/凭据校验、不脱敏回传渲染端并原样进日志 —— 而同仓对 proxy URL 三件全做 | anchor `routes/settings.py:24`、`database.py:1594`、`routes/settings.py:193-197` ↔ `netconfig.py:61-88`、`routes/settings.py:595-627` | 需用户把凭据写进 base_url（企业网关文档确有此形态）；面板对该字段零引导 |
| S10-04 | c | Major | ✅ | `models_config.top_p` 在 anchor 只有终端预测一个读者，Agent 交互/定时/子 Agent 三条主链恒 0.9 | anchor `llm_client.py:45-54,75-85` ↔ `llm_adapter.py:192,232`；唯一读点 anchor `routes/terminal.py:129,144` | 恒 |
| S10-05 | c | Major | ✅ | `reasoning_tokens`：类型面声明、后端可写、运行时真消费，但 anchor 面板从不提交 ⇒ 所有模型钉在默认 8192 | anchor `client.ts:270,292` ↔ `ModelsPanel.vue:367-380,491-502`（form 无此字段）↔ 消费点 anchor `routes/agent.py:826`、`schedule_engine.py:381-386`、`safe_tokens.py:53` | 恒（任何想调思考预算的用户） |
| S10-06 | d | Major | ✅ | `reasoning_tokens=0` 塌成「思考不设上限」，与其自身 Field 说明相反（0-塌陷族第三例，前次只修了 `None` 那一半） | anchor `llm_adapter.py:264` ↔ `routes/settings.py:37` 说明、`safe_tokens.py:51-53` 反义、`schedule_engine.py:382-385` 注释 | anchor 需手调 API；面板补输入项后即一键可达 |
| S10-07 | d | Major | ✅ | 空 `api_key` 被当成「未配置模型」⇒ 面板默认 provider（自定义/vLLM）建的模型永久拿不到 LLM 自动审批与命令解释 | anchor `auto_approve.py:106`、`command_explain.py:391` ↔ anchor `ModelsPanel.vue:182,335,372,511`、`routes/settings.py:25`、`llm_adapter.py:82,90` | 立刻可达且是默认路径（`provider` 初值 `'vllm'`） |
| S10-08 | d | Major | ✅ | `_fix_model_name_and_id` 每次 `init_db` 无条件执行，任何含 `/` 的显示名都会与 model_id 互换，且用户改回来下次启动再翻 | anchor `database.py:519-520`（调用点无 once 闸门）+ `:811-828` | 可达；形态是「改名后下次重启莫名坏掉」 |
| S10-09 | d | Major | ✅ | 种子探测的 3s 预算被用户 HTTP 超时**替换**（read 另有 60s 下限），启动关键路径最长可挂 20 分钟，违反 spec 的「总预算 ≤6s、绝不阻塞启动」 | anchor `database.py:843` → anchor `netconfig.py:173-184,225-232` + anchor `main.py:74` ↔ spec `:54` | 需 `VLLM_BASE_URL` + `http.timeout_seconds>0` + 端点可连不响应 |
| S10-10 | d | Major | ✅ | `_probe_first_model` 不发 `Authorization`，而它服务的种子恰恰用得上 `settings.vllm_api_key` ⇒ 有鉴权的端点自动种子静默永久失效 | anchor `database.py:831-855`（签名无 key、`_c.get(url)` 无 header）↔ `:876,888,903` ↔ `config.py:64` | 凡端点要求鉴权即必现 |
| S10-11 | c | Minor | ✅ | `update_model_config` 支持写 `is_default`，`UpdateModelRequest` 却没这字段 ⇒ 死分支 + `PATCH {"is_default":true}` 得 200 且什么都没改 | anchor `database.py:1701,1735-1738` vs anchor `routes/settings.py:42-57` | 可达（手调 API/未来前端按 DB 能力面写 payload） |
| S10-12 | c | Minor | ✅ | `sort_order` 后端可写且列表按它排序，前端类型面与面板都没有它 ⇒ 顺序用户不可控，而 `GET /default` 的 `list[0]` 兜底正按这个不可控顺序选人 | anchor `routes/settings.py:56`、`database.py:1590,1662-1663` ↔ anchor `client.ts:280-295` | 能力缺口，但有 S10-02 的连带后果 |
| S10-13 | b | Minor | ✅ | 测试连接端点把上游响应体前 200 字节原样回传渲染端并写日志 —— 同一顾虑在 `available` 列表上堵了，在 body 上没堵 | anchor `routes/settings.py:211`（注释）vs `:242,246,251` ↔ anchor `stores/settings.ts:485` → `ModelsPanel.vue:608` | 可达（填错 key 即触发 401 分支） |
| S10-14 | b | Minor | ✅ | `api_key` 没有任何「清除」途径：后端跳过空串、面板不发空串，两者互指对方 | anchor `database.py:1708` ↔ anchor `ModelsPanel.vue:391,509-515` | 可达（有 key → 无 key 的迁移做不完，保存还回成功 toast） |
| S10-15 | b | Minor | ✅ | 掩码识别靠 `"****"` 子串哨兵：真实密钥含该子串则永不可写回（静默 no-op + 成功 toast） | anchor `database.py:1712` ↔ `_mask_api_key:1542-1544` | 理论可达（密钥字符集含 `*`），概率极低但排查成本高 |
| S10-16 | a | Minor | ✅ | `_DEFAULT_COLUMNS` 不是单一来源：tier→列映射另有 4 处独立实现，其中 anchor 读侧对 `"global"` **反向**（落进 SSH 列） | anchor `database.py:1812` vs `:1768`（表）+ `:1735-1738`、`:1669/1681`、`routes/settings.py:77` | 当前不可达（无调用方传 `"global"`）⇒ 潜伏 |
| S10-17 | a | Minor | ✅ | `get_default_model` 用 `LIMIT 1` 无 `ORDER BY`，双默认时读数不确定，而 `list_models_config` 的排序键是 `sort_order, created_at` | anchor `database.py:1814` vs `:1590`；对照 `:1576`、`:1619` 两处都写了 `ORDER BY rowid LIMIT 1` | 需先存在 2 行同档默认（anchor 写路径保证唯一 ⇒ 仅手改库/并发交错） |
| S10-18 | a | Minor | ✅ | 后处理头图兜底只读全局档，且三条分支都以字面量 `"Qwen3-VL"` 收尾 —— 那是本 spec 明确删除的旧硬编码种子名 | anchor `post_processor.py:921-929` ↔ spec `:11-13` | 生产路径不可达（anchor `routes/agent.py:1031` 恒传 id）⇒ 潜伏 |
| S10-19 | d | Minor | ✅ | `create_model_config` 路由把**任何**异常折成 400 + `str(e)`：「database is locked」这类服务端故障显示为输入错误 | anchor `routes/settings.py:104-125` ↔ 同文件 `:294-297`、`:562-564`（都是 500 + 中文说明） | 可达（并发写库时，即本仓共享锁常态） |
| S10-20 | d | Minor | ✅ | `netconfig.TIMEOUT_MIN = 5` 后端零消费者（写入区间一律 low=0），而**前端有个同名常量是 0** 且真在驱动 UI | anchor `netconfig.py:34` ↔ anchor `database.py:2142-2143`、`routes/settings.py:488` ↔ anchor `useOpsSettings.ts:22,69,90,161` + `OpsPanel.vue:53,71` | 残留声明 + 跨上下文同名异值，易误读为「存在 5s 保护」 |
| S10-21 | d | Minor | ✅ | 测试连接报错「连接超时 (Ns)」里的 N 不是真正生效的上限（read 被抬到 ≥60s） | anchor `routes/settings.py:228-237` ↔ anchor `netconfig.py:173-184` | 可达（用户设过 `http.timeout_seconds` 且 ≤60 时读数与实际不符） |
| S10-22 | d | Minor | ✅ | `_resolve_model_pk` 的双键回退在 `get_model_config` 又抄一遍；`model_id` 无唯一约束，重名时静默命中 rowid 最小者 | anchor `database.py:1558-1581` vs `:1613-1621`、schema `:213-233` | 重名可达（面板无去重校验、create 也不查）；PK/名称冲突不可达 ⇒ 降级 |
| S10-23 | b | Minor | ✅ | spec 组件图写 `_seed_models(conn)`/`_insert_model`，代码是 `seed_models_async`/`_insert_seed_model`；spec 未随 P2-E9 更新 | `specs/2026-09-02-…:44-47` ↔ anchor `database.py:858,920` | 恒（文档漂移） |
| S10-24 | c | Tradeoff | ✅ | `routes/agent.py:34 from routes.terminal import _get_model_config` 的跨 router 私有依赖：低危但零收益，且本仓已有 `safe_tokens.py` 专门为此类耦合建过解 | anchor `routes/agent.py:34` ↔ anchor `routes/terminal.py:29-31` ↔ `safe_tokens.py:1-5` docstring | 恒；风险面是「谁改那个包装就顺手改了 Agent 主链，而两处都没有测试」 |

## 详情

### S10-01 [dim a][Major] 档位优先级：13 个消费者里 3 处不一致，而面板文案按 13 个都一致来写
- LOCATION：anchor `schedule_engine.py:439` `default = db.get_default_model(None, mask_key=False)` —— 只查 `is_default`（⊕），完全不查 `is_default_ssh`（★）。落空即 anchor `:291-293` `raise RuntimeError("未配置默认模型，无法执行定时任务")`。文案侧：anchor `ModelsPanel.vue:7-8` 原文「两档默认互不影响：★ SSH 默认 = 对话 / 终端 / **定时任务**读的那档；⊕ 全局默认 = SSH 档未设时，起标题等后台链路兜底读的那档。」
- 对照口径（我逐个 grep 过）：`routes/terminal.py:88-93`、`routes/agent.py:567-574`、`memory/reconcile.py:83-84`、`memory_tools.py:42-43`、`spawn_subagent.py:204-215` 一律 **ssh→global 两级**；只有 `schedule_engine:439` 与 `post_processor:923` 是单档读。
- CLAIM：只勾 ★ 不勾 ⊕ 的用户（这是面板唯一的默认勾选入口，`ModelsPanel.vue:244`），交互对话跑在 ★ 模型上，定时任务却报「未配置默认模型」——而模型明明配置了；若两档都有，定时任务静默跑在用户以为已经让位的 ⊕ 那条上。
- **严重度并列（不自行裁死）**：S10 审查路把本条判 **Critical**，我判 **Major**。理由：失败方向是显式抛错并被记成 `failed`（不是静默错模型执行、不损数据、不绕审批）；且 `ModelsPanel.vue:7` 那句文案在 anchor 上确实骗人，属跨层契约错位致功能失效。**若你认为「面板承诺的档位语义与运行时不符」应按主路径不可用计 Critical，请直接改本行裁决。**
- **工作树已在修（未提交）**：`git diff a8e588c` 显示在写版把该行改为 `db.get_default_model(SSH_MODE, mask_key=False) or db.get_default_model(None, mask_key=False)`，并补了 docstring「与 reconcile.py / spawn_subagent.py / terminal 同一口径」，同时新增 `from prompts import SSH_MODE` 与 `_DEFAULT_TOP_P`。本条只对 anchor 成立。

### S10-02 [dim a][Major] 兜底链少了中间一档
- LOCATION：anchor `routes/settings.py:84-94`
  ```python
  model = db.get_default_model(mode=SSH_MODE)
  if not model:
      models = db.list_models_config()
      if models: return models[0]        # ← 跳过 is_default（⊕）
      raise HTTPException(404, …)
  ```
- CLAIM：这是 4 级读路径里唯一「SSH 档 → 任意模型」而没有全局档的一条。只勾 ⊕ 不勾 ★ 时：`src/stores/ssh-ai.ts:78-81` 经 `api.getDefaultModel()`（anchor `client.ts:428-429`，**无 mode 形参，问不到全局档**）拿到的预选是 `sort_order` 第一条；同一时刻 `ChatPanel.vue:132` 自己客户端派生的默认显示（`?? list.find(m => m.is_default) ?? list[0]`）却是 ⊕ 那条 ⇒ **两个面板对「默认模型是什么」给出不同答案**。`sort_order` 用户又不可控（S10-12），等于兜底在挑一条没人选的模型。

### S10-03 [dim b][Major] 同一件事在 proxy 侧被当密钥、在 base_url 侧被当普通字段
- LOCATION：anchor `routes/settings.py:24` `base_url: str = Field(..., min_length=1)`（无 scheme 白名单、无 userinfo 剥离）；anchor `database.py:1584-1598` 的列表投影只对 `api_key` 调 `_mask_api_key`（`:1594`），`base_url` 原样回传；anchor `ModelsPanel.vue:95` 直接 `{{ m.base_url }}`；anchor `routes/settings.py:178-197` 测试端点把它拼成 `models_url` 并 `log.info(… base_url=%s …)`（`:193-197`）写进日志文件，同时作为 `Authorization: Bearer` 的发出目标。
- 对照（同仓已有实现）：anchor `netconfig.normalize_proxy_url:61-75` 做 scheme 白名单 + 主机名校验；`redact_proxy:78-88` **显式丢 userinfo 只留 scheme://host:port**；`_mask_value:623-627` 让 proxy 分支**先于一切**，注释理由是「口令可能是纯数字，不能让数值例外放它过去」；`_DIAG_SECRET_HINTS:595-596` 把 `"proxy"` 列入密钥子串，注释原文「代理 URL 可带密码」。
- CLAIM：既然承认「URL 可以带密码」，那 `https://user:pass@gw/v1` 这种形态（部分企业 OpenAI 兼容网关文档就是这么给端点的）在模型侧会同时出现在响应体、UI 和日志里，而面板对该字段零引导（anchor `ModelsPanel.vue:217` 端点输入框无 placeholder/提示）。定 Major 不到 Critical：不是默认形态，需要用户主动把凭据写进 URL。

### S10-04 / S10-05 [dim c][Major] 两条形同「类型面与能力面都在，就是没接线」
- **S10-04 top_p**：anchor `llm_client.py:45-54` 的 `call()` 形参列表里没有 `top_p`，`:75-85` 转发 `chat_stream` 时也不带 ⇒ `chat_stream` 用自身默认（anchor `llm_adapter.py:192,232` = 0.9）。而 anchor `routes/settings.py:38,55` 接受写入、anchor `ModelsPanel.vue:113-114` 渲染 "Top-P" 读数、anchor `:503-505` 的注释还写明「`top_p` 仅新建时发送默认值；编辑时 UI 从未暴露该字段，不发以保留后端原值」。`git grep top_p a8e588c -- backend/ | grep -v tests` 里非 DB/写侧的命中只有 `routes/terminal.py:129,144` —— **唯一读者是命令行补全预测**。后处理侧另有一列已裁决（91 清单 item 24）。**工作树已在修（未提交）**：在写版补了 `contracts.AgentConfig.top_p` + `__post_init__` 校界、`llm_client` 形参、`stirrups` 传参、`routes/agent.py` 取模型行值，并给面板加滑杆。
- **S10-05 reasoning_tokens**：anchor `client.ts:270,292` 声明 `reasoning_tokens?: number`，anchor `routes/settings.py:37,54` 接受并带 `ge=0` 与语义说明，运行时**真消费**（anchor `routes/agent.py:826 max_reasoning_tokens=model_cfg.get("reasoning_tokens")`、`schedule_engine.py:381-386`、`safe_tokens.py:53`）。但 anchor `ModelsPanel.vue` 的 `form`（`:367-380`）里没有该字段、`openEditDialog`（`:437-462`）不回读、`doSave`（`:491-502`）不发 ⇒ `git grep reasoning_tokens a8e588c -- src/` 只命中那两行类型声明。**用户既看不到也改不了思考预算**，而面板的「思考模式」勾选（`:239`）暗示的正是这条通道，它写的 `supports_thinking` 又无人读（已裁决 item 23）——两条通道一条空转一条缺失。
- 连带风险（裁决时值得一并考虑）：在写版给面板补上输入项后，S10-06 的 0-塌陷从「需要 curl」变成「面板一键可达」，而它的**新文案**（在写版）写的是「填 0 = 跟随上面的输出长度」，与实际行为（无上限）直接冲突。

### S10-06 [dim d][Major] 0-塌陷族第三例：这次塌在最后一层
- LOCATION：anchor `llm_adapter.py:263-266`
  ```python
  tpl_kwargs: dict = {"enable_thinking": True}
  if reasoning_max_tokens is not None and reasoning_max_tokens > 0:
      tpl_kwargs["thinking_budget"] = reasoning_max_tokens
  ```
- CLAIM：`reasoning_tokens=0` 是合法值（anchor `routes/settings.py:37` `Field(ge=0)`，说明写「0 表示使用 `max_output_tokens`」）。链路：交互式 anchor `routes/agent.py:826` → `stirrups.py:190,204` → `llm_client.py:84`；定时任务 anchor `schedule_engine.py:386` `safe_reasoning = min(int(rt), safe_out)`（0 保住）→ 同样透传到 `:264` 被 `> 0` 当「未设」丢掉 ⇒ 请求不带 `thinking_budget` ⇒ vLLM 按**无上限**出思考。而 anchor `safe_tokens.py:51-53` 对同一个 0 的处理是「预算 0，不从输出空间扣」——**两处对 0 的解释正好相反**。
- 关键佐证（这条为什么是「没修完」而不是「没修过」）：anchor `schedule_engine.py:382-385` 的注释已经写下本仓对这个坑的认知——「contracts 里 None=不限制思考预算 → 配 0 反而拿到无上限，与意图相反」，那次把 `if rt else None` 改成 `if rt is not None`，**0 在下一层又被 `> 0` 吞掉，坑只填了一半**。

### S10-07 [dim d][Major] 面板默认形态建的模型，被 reins 层判成「没配模型」
- LOCATION：anchor `auto_approve.py:104-107` `if not ctx.llm_base_url or not ctx.llm_api_key: return False`（`_judge_once`）；anchor `command_explain.py:391` 同形早退。写入侧：anchor `routes/settings.py:25` `api_key: str = ""`、anchor `database.py:1655` 还 `strip()`；面板侧 anchor `ModelsPanel.vue:182` `v-if="!isLocalProvider"`（本地 provider **连输入框都不渲染**）、`:335` `PROVIDERS[0] = { id:'vllm', label:'自定义', …, isLocal: true }`、`:372 provider: 'vllm'`（form 初值）、`:511 if (!isLocalProvider.value && trimmedKey)` ⇒ **面板新建的「自定义」模型库里存的就是空串**。
- CLAIM：anchor `llm_adapter.py:82,90` 明确写 `api_key or "EMPTY"`，证明 `""` 是本切片承认的合法态（本地 vLLM 默认免鉴权）。同一件事在 reins 层被 `not ctx.llm_api_key` 判成未配置 ⇒ 用户在 AgentPanel 打开自动审批开关也**永不生效**，命令解释同样永久缺席。失败方向是回退弹卡（不是安全放大），故 Major。

### S10-08 [dim d][Major] 每次启动都会重新判定「你是不是把两个字段填反了」
- LOCATION：anchor `database.py:519-520` —— 该调用位于 `if cursor.fetchone():`（表存在）分支内，**不在任何 once 闸门下**，即每次 `init_db` 都执行；实现在 anchor `:811-828`：
  ```python
  looks_like_id = lambda s: bool(s) and ("/" in s or s.startswith("Qwen/") or s.startswith("qwen/"))
  if looks_like_id(n) and not looks_like_id(mid):
      conn.execute("UPDATE models_config SET name = ?, model_id = ? …", (mid, n, …))
  ```
- CLAIM：判据是「名字里含 `/`」。`name="本地/vLLM"`、`model_id="qwen3-32b"` 是最自然的写法之一，直接命中 ⇒ 两值互换 ⇒ 之后 LLM 请求拿含 `/` 的中文显示名当模型名 ⇒ 上游 400 / anchor `routes/settings.py:219` 那句「模型不在服务端可用列表中」，用户以为是端点问题。用户在 UI 里改回来，**下次启动再被翻一次** ⇒ 该写法永久不可用。docstring（`:815`）自称「避免重启后强制覆盖用户在前端的重命名」，靠的是这个启发式，而它恰恰会覆盖。
- 覆盖度证据：`git grep _fix_model_name_and_id` 在 anchor 只有定义（`:811`）与调用（`:520`）两处，**全仓无测试钉这段行为**。seed 行两侧同值（anchor `:939`）故不受影响，幂等对已翻的行成立 ⇒ 破坏是一次性且不可由用户撤销。

### S10-09 / S10-10 [dim d][Major] 自动种子的两条 spec 违约
- **S10-09 预算被替换**：anchor `database.py:843` `with netconfig.new_sync_client(default_timeout=timeout) as _c`，而 anchor `netconfig.http_timeout:173-184` 的语义是「用户设了就用它**替换**调用点默认」且 `read=max(60.0, float(t))` —— 3.0s 的探测预算在 `t>0` 时被整个丢弃。anchor `main.py:71-76` 在 lifespan 里 `await seed_models_async(...)`，app 在 lifespan 让出前不接请求（`:52-53` 的注释自己承认「必须早于任何上游请求——下面的 seed_models_async 就会打 /models」）。设 `http.timeout_seconds=600` ⇒ 单个探测 read=600s、两个 1200s；设 5 ⇒ read 仍被抬到 60s。spec `:54` 写的是「探测：3s/端点超时，**总预算 ≤6s**」，spec `:28` 写「**静默降级不阻塞启动**」⇒ anchor 两条都不成立。放大因子：spec `:35-36` 明确「`_seed_models` 每次 `init_db` 都执行、仅表非空时提前返回」，所以只要 `models_config` 仍为空（**正是探测失败的场景**），每次启动都重复这个停顿。connect 侧 `min(10.0, t)` 使纯拒连仍在 10s 内失败 ⇒ 最坏路径需要「TCP 可连但不响应」（中间设备 drop / vLLM 卡死）。
- **S10-10 探测不带认证**：anchor `database.py:831` `def _probe_first_model(base_url: str, timeout: float = 3.0)` 签名里没有 api_key，`:844` `_c.get(url)` 不带任何 header；而 anchor `:876` `seed_models_async` 明明从 config 读出 `api_key = settings.vllm_api_key or "EMPTY"`，却只用于**写库**（`:888,903` 调用探测时不传）。对启用了 `--api-key` 的服务端，`/models` 回 401 → `resp.raise_for_status()`（`:845`）抛 → `:850-855` 记 warning 返回 None → **不种子**。用户现象：「`VLLM_BASE_URL` 和 `VLLM_API_KEY` 都设了，模型列表还是空的，日志里一句 401」。对照 anchor `routes/settings.py:191` 的 `test_model_connection` 就带了 header ⇒ 同一仓库里两种做法。
- 测试盲区（两条的共同解释）：anchor `backend/tests/test_seed_models.py:141,164,226` 的替身是 `fake_get(url, timeout=None)`，**既不断言 Authorization header，也不经真实 `netconfig.new_sync_client`** ⇒ 超时替换与 401 两个缺陷都落在同一个盲区里。

### S10-11 … S10-24（简述，均已按 LOCATION 打开 anchor 原文核对）
- **S10-11** anchor `database.py:1700-1701` 的 `allowed` 集合含 `is_default`、`:1735-1738` 的同档清理循环也含它，而 anchor `routes/settings.py:42-57 UpdateModelRequest` 只有 `is_default_ssh`；全后端 `git grep "extra=\|ConfigDict"` 零命中 ⇒ pydantic v2 默认 ignore ⇒ `PATCH {"is_default":true}` 得 **200 且什么都没改**。与 anchor 提交信息「allowed is_default in update_model_config」构成口径落差（DB 层确实允许，HTTP 层没接）。唯一 `is_default` 写入口是 anchor `routes/settings.py:70-81` 的 `PATCH /default`。
- **S10-12** anchor `database.py:1590 ORDER BY sort_order ASC, created_at ASC` + `:1662-1663` 的 `MAX+1` + anchor `routes/settings.py:56` 可写，vs anchor `client.ts:280-295` 无该键、`ModelsPanel.vue` 无排序控件、`useModelSelector.ts`/`models.ts` 也不排序。不是行为缺陷，是能力缺口；但 anchor `routes/settings.py:90-92` 的 `list[0]` 兜底正是按这个用户不可控的顺序挑模型 ⇒ 与 S10-02 连带。
- **S10-13** anchor `routes/settings.py:242 detail = f"HTTP {code}: {e.response.text[:200]}"`、`:246 log.warning` 同样记 body、`:251` 兜底记 `str(e)`；该 detail 经 anchor `stores/settings.ts:485` 保留进 anchor `ModelsPanel.vue:608` 直接显示。同一端点 anchor `:211` 的注释为「避免泄露同 tenant 其他模型 id」刻意不回传 `available` 全量 ⇒ 同一顾虑只堵了一半。OpenAI 风格 401 body 会带部分回显的密钥。
- **S10-14** anchor `database.py:1707-1708` 对 `k=="api_key" and v==""` 直接 `continue`；anchor `ModelsPanel.vue:509-515` 用 `if (… && trimmedKey)` 决定发不发，`:391 canSave` 此时判定通过 ⇒ 「有 key → 无 key」（换成免鉴权本地 vLLM）在 UI 上做不成，保存还回成功 toast，下次打开又把脱敏前缀反显回来。缓解：anchor `:192` placeholder 明写「留空保留原 Key，输入新值则替换」——**行为有文档**，缺的是显式清除语义；proxy 侧就有（`""` = 清除并直连，anchor `routes/settings.py:486`）。
- **S10-15** anchor `database.py:1712 if k == "api_key" and "****" in str(v): continue` 用「值里含掩码串」判定这是回传的脱敏值 ⇒ 字面含 `****` 的真实密钥被静默丢弃。与 `_mask_api_key`（`:1540-1544`，`key[:4]+"****"`）无结构关联；且 anchor 面板 `:512` 已有第二道等价守卫（比对 `maskedApiKey` 原值），DB 侧这道是**冗余的字符串嗅探**。
- **S10-16** anchor `get_default_model:1812 col = "is_default" if mode is None else "is_default_ssh"` vs 写侧的 anchor `_DEFAULT_COLUMNS = {None:"is_default", "global":"is_default", "ssh":"is_default_ssh"}`（`:1768`，注释还专门写「显式收窄成这张表，别让『任何非 ssh 的值』都落进 SSH 列」——**读侧正是那个被禁止的写法**）。`git grep _DEFAULT_COLUMNS` 只有 `:1768/:1783/:1788`，即只有 `set_default_model` 用表。同名串 `"global"` 在写侧=全局、读侧=SSH，含义相反。当前无调用方传 `"global"` ⇒ 潜伏 Minor，但它是下一个直接用 `DefaultMode` 调读口的人静默拿错档的位置。
- **S10-17** anchor `:1814 WHERE {col} = 1 LIMIT 1` 无 `ORDER BY`；对照 anchor `_resolve_model_pk:1576` 与 `get_model_config:1619` 都写了 `ORDER BY rowid LIMIT 1`，此处漏。anchor 写路径都保证唯一（`set_default_model:1791-1795` 单事务清 others + 置一，create/update 也清），残留入口是 item 25 已登记的 **0 默认**（不是 2 默认）⇒ 只有手改库/并发交错可达。
- **S10-18** anchor `post_processor.py:923 db.get_default_model()`（不传 mode = 只读 `is_default`，无 SSH 档回落）；`:925/:927/:929` 三条分支都以字面量 `"Qwen3-VL"` 收尾 —— 那是 spec `:11-13` 明确删除的旧硬编码种子名。生产不可达（anchor `routes/agent.py:1031` 传 `model_id=model_cfg.get("id")` 恒非 None），登记为「任何新调用方 submit 不传 model_id 即触发」。
- **S10-19** anchor `routes/settings.py:104-125 except Exception as e: raise HTTPException(400, str(e))`：`db.create_model_config` 除 IntegrityError 外还会抛 `sqlite3.OperationalError`（busy_timeout 5s 锁竞争，本仓常态）。渲染端 anchor `client.ts:197-199` 把 detail 当用户可读消息，anchor `ModelsPanel.vue:533-537` 原样 toast ⇒ 「database is locked」显示成输入错误。同文件其余写路由（`:294-297`、`:562-564`）都是 catch → 500 + 中文说明，只这一处口径不同。
- **S10-20** anchor `netconfig.py:34 TIMEOUT_MIN, TIMEOUT_MAX = 5, 600` 在**后端**零消费者：`set_policy:152` 用 `_clamp_int(timeout_seconds, 0, 0, TIMEOUT_MAX)`（low 传 0），`load_policy_from_db:112` 的 `_env_int(..., 0, TIMEOUT_MAX)`、anchor `database.get_http_policy:2142-2145` 也是 low=0，anchor `HttpSettingsRequest:488` 亦 `ge=0` ⇒ 那个 5s 下限从未生效。新证据是**跨上下文同名异值**：前端 anchor `useOpsSettings.ts:22 export const TIMEOUT_MIN = 0`（注释「0 是『不覆盖』的哨兵值，不是最小值」）才是真驱动 `OpsPanel.vue:53,71` 提示与校验的那一个。不是行为缺陷（0=不覆盖是刻意语义，`:487`、`:106-108` 有说明），是定义后即死的常量 + 会让人误以为存在 5s 保护。**工作树已在修（未提交）**：在写版把 `TIMEOUT_MIN` 改为 0 并让两处夹紧引用它（见 `00-INDEX.md` 告警块）。
- **S10-21** anchor `routes/settings.py:228-237` 的 `except httpx.TimeoutException` 里 `limit = netconfig.timeout_seconds(10.0)`，回 `f"连接超时 ({int(limit)}s)"` 并附 `timeout_seconds: int(limit)`；但请求是用 `netconfig.new_client(default_timeout=10.0)` 发的 ⇒ read 被抬到 `max(60.0, t)`。该分支 anchor `:226-227` 的注释自称「报实际值，否则用户按『10s 都连不上』去查网络」——正是要避免的误导换了形式还在：读数 5s、实际 read 60s。`int()` 截断另使亚秒读数显示为 0（本路由列型是 int，不触发）。
- **S10-22** anchor `:1558-1581` 与 `:1613-1621` 各自实现「先 `WHERE id=?`，落空再 `WHERE model_id=? ORDER BY rowid LIMIT 1`」。schema anchor `:213-233` 对 `model_id` 无唯一约束、`create_model_config` 也不查重名 ⇒ 两条模型填同一 LLM 模型名时，凡按名字进来的调用**确定性地但不可见地**命中 rowid 最小那条（UI 改的是另一条）。任务点名的 PK/model_id 互换歧义**不可达**：UI 建行 PK 是 anchor `:950-951` 的 uuid4 hex，seed 行 `id == model_id`（anchor `:939`）两键自洽，歧义要求某个 `model_id` 恰等于另一行的 32 位 hex。`test_model_default_round22.py` 只测「name 解析到目标行」「未知 key 抛错」，无重名用例。
- **S10-23** spec `:44-47` 的组件图与数据流写 `_seed_models(conn)` / `_insert_model`，代码是 `seed_models_async`（anchor `:858`）/ `_insert_seed_model`（anchor `:920`）—— spec 未随 P2-E9「探测移出 init_db」更新。反向核对**通过**的 spec 条目一并登记，见下方「核对为干净」。
- **S10-24 [Tradeoff]** anchor `routes/agent.py:34 from routes.terminal import _get_model_config`：terminal 侧 anchor `:29-31` 只是 `db.get_model_config(..., mask_key=False)` 的一行私有包装，而 `agent.py` 自己已 `import database as db` ⇒ 间接层零收益。两 router 的解析结果与 database 一致（都是 id→name 两级 + `mask_key=False`），**没有行为缺陷**。风险面：谁改那个包装（例如日后在里面加 SSH 档解析或缓存）就顺手改了 Agent 主链，而两处都没有 `_get_model_config` 的测试。值得点出的内部矛盾：本仓**已经**为消除同类跨 router 依赖专门建了 `backend/safe_tokens.py`，其 docstring 第 2 行原文「解耦 routes/chat 与 routes/agent 的跨路由依赖」——解法有先例却没用在这一处。

## 数值设置 × 0 值处理（任务书点名要的那张表，全部 anchor 行号）

| 设置 | 读点 | 0 值处理 | 判定 |
|---|---|---|---|
| `reasoning_tokens` | anchor `llm_adapter.py:264`（经 `routes/agent.py:826`、`stirrups.py:190/204`、`llm_client.py:84`） | `is not None and > 0` ⇒ 0 被当未设，`thinking_budget` 不下发 | **错**（S10-06） |
| `reasoning_tokens` | anchor `schedule_engine.py:386` | `min(int(rt), safe_out) if rt is not None` ⇒ 0 保住 | 写入侧正确，被下游吞掉 |
| `reasoning_tokens` | anchor `safe_tokens.py:53` | `if (rt and > 0) else 0` ⇒ 0 = 不扣预算 | 与 0 的原语义一致，但与 `llm_adapter` **两处两义** |
| `temperature` | anchor `schedule_engine.py:390` + `_num_param:39-46` | `default if value is None else float(value)` | ✓（该 bug 类的**已修样本**） |
| `temperature` | anchor `routes/agent.py:326`、`routes/terminal.py:128` | `.get(key, default)`；`_MC_COLS` 保证键必在 ⇒ 0.0 原样返回 | ✓（非 `or` 形） |
| `top_p` | anchor `routes/terminal.py:129` | `.get("top_p", 0.9)` ⇒ 0.0 保住 | 0 处理正确；但该值只有这一个读者（S10-04） |
| `max_tokens` | anchor `schedule_engine.py:296` | `int(get("max_tokens") or 16384)` ⇒ 0 塌成 16384 | `or` 塌陷残留；写侧 `Field(ge=1)` + 建表默认使 0 不可达 ⇒ 已并入 S7-10 |
| `max_output_tokens` | anchor `safe_tokens.py:45` | `if (m and > 0) else None` | ✓（`ge=1` 故 0 不可达，行为与 None 同） |
| `api_key == ""` | anchor `auto_approve.py:106`、`command_explain.py:391` | `not ctx.llm_api_key` 判成未配置 | **合法空值被吞**（S10-07） |
| `api_key == ""` 写侧 | anchor `database.py:1708` | `continue` | 刻意「不发即不改」；缺清除通道（S10-14） |
| `http.timeout_seconds` | anchor `netconfig.py:169-184` | `t <= 0 → 调用点默认`，显式 0 有独立语义 | ✓ |
| `http.retry_count` | anchor `netconfig.py:164` + `_clamp_int:91-96`（low=RETRY_MIN=0） | 0 保留 = 不重试，`:106-108` 明文说明 | ✓ |
| `http.*` 是否「用户写过」 | anchor `database.py:2174-2194 is_int_app_setting_set` | 脏行/空白/越界 → False；合法 0 → `(0, True)` | ✓（round 22 新增即为此） |
| `permission.approval_fallback_s` | anchor `main.py`（用 `get_app_setting(...) is not None`） | 脏行也算「写过」并推给运行时 | 在写版改用 `is_int_app_setting_set(...)[1]` ⇒ **另一会话正在修**，非 Models 区不重复计 |
| `conv_retention.keep_count` | anchor `database.py:2205 _int_app_setting(key, 1000)` 无 low/high | 0 原样进 `DELETE … rn > ?` | 在写版补 `low=0, high=100000` ⇒ 同上 |
| `keep_count` 显示 | anchor `ChatPanel.vue:150 data.keep_count ?? 1000` | 注释明确弃用 `\|\|` | ✓（同类已修样本） |
| `stores/settings.ts asNumber` | anchor `:150-154` | 显式区间判定，`maxCostUsd` 默认 0/lo 0 | ✓（S13 已记为该类的收口样本） |
| 诊断数值放行 | anchor `routes/settings.py:602-633` | 白名单 ∧ 可 float **双条件** | ✓（`:628-633` 注释解释了为何不能用前缀放行） |

## 核对为干净 / 已裁决不重议

- **anchor 的脱敏主体是对的**（记为 S10-24 同级的正面结论，避免后续重复怀疑）：`list_models_config:1594` 逐行掩 key；`get_model_config` 默认掩、`mask_key=False` 只出现在 `test_model_connection:173`（不回传 key）与内部 LLM/embedding 调用方（`git grep mask_key=False a8e588c -- backend/` 命中集合全为内部调用点）；`_mask_api_key` 只露前 4 字符；PATCH 响应走 `get_model_config(pk)` 默认脱敏；诊断对 `api_key/token/secret/password/proxy` 全打码，proxy 分支先于一切。
- **`supports_thinking/supports_image/supports_tool_calling` 三列零运行时消费者**：`git grep … a8e588c -- 'backend/**/*.py' | grep -v tests` 命中仅 `routes/settings.py:28-30/48-50/113-115` 的请求模型声明与 create 透传，读侧回传只用于 anchor `ModelsPanel.vue:49-51` 徽标 ⇒ 与本仓 **91-DO-NOT-RELITIGATE item 23** 的裁决一致（本轮只复核 anchor 状态与裁决相同，不重议；在写版删勾选、保留列与回传）。
- **定时任务不读 `max_output_tokens`** = item 29 已裁决 ⇒ 不重议；S7-10 只补「`:379` 两个分支同值 ⇒ `or` 是死式，读起来像列名写错」这一新事实。
- **后处理 `_call_llm_with_timeout` 不传 `top_p`** = item 24 已裁决 ⇒ 不重议，S10-04 只覆盖三条交互式/定时/子 Agent 主链。
- `models.ts` 与 `settings.modelConfigs` 的双轨列表原样存在，未触碰（已登记项）。`src/stores/models.ts`(31) 与 `useModelSelector.ts`(56) 都不读 `is_default*`（anchor `:23-29` 只按 `m.id === selectedId` 找显示名）⇒ 与默认档解析无关，**no finding**。
- `OpsPanel.vue` / `AgentPanel.vue`：`git grep "ModelConfig|modelConfigs|setDefaultModel|/settings/models"` 两文件均零命中 ⇒ 该区不消费模型配置，**no finding**。
- 正面成果核实无回退：anchor `ModelsPanel.vue:396,469-475` 的 `saving` 双保险防连点、`:563-564,592` 的忙态 Set 带档位键（`key(id, mode)`）、anchor `client.ts:206-209` 的重试只放 GET/DELETE（避免 POST 建模型双发）。
- spec 反向核对**通过**的项：spec `:26`「种子同时置 `is_default=1` 与 `is_default_ssh=1`」与 anchor `database.py:940` 把同一个 `int(is_default)` 写进两列一致 —— **这是刻意设计，不是「只写一档」的 bug**；spec `:62`「探测成功但 `data` 为空数组按失败处理」→ anchor `:846-849` 兑现；spec `:29`「已有非空 `models_config` 从不改动」→ anchor `:870-871` 兑现；spec `:61`「探测失败一律降级、不抛不弹 UI」→ anchor `:850-855` 兑现（但未说明降级可能是因为 read 被抬到 60s，见 S10-09）；spec `:52` SideChatPanel 空态 → anchor `SideChatPanel.vue:437-442` 兑现；spec `:68` 模型名入库无注入面 → 全用绑定参数兑现。
- **`test_ci_matrix_round22.py` 与跨层契约无关**（纠正任务书里把它当契约基准的用法）：它抽 `.github/workflows/ci.yml` 断言 runner/job 形状，文件头 `:1-13` 自述「这里不加行为测试，而是把 workflow 的形状钉住」。真正钉模型 payload 契约的是 `test_settings_key_contract_round22.py:111-141` + `test_model_default_round22.py` + `test_model_default_readback.py`；其未覆盖面见上方缝表末行。

## 闸门

- 全部为 static-read-only + `git grep` 引用计数；**未执行 pytest**（并行会话持 SQLite 写锁，见 `00-INDEX.md` 警示块）。可安全执行的动态证据仅一条：`git grep` 的 top_p / reasoning_tokens / `_DEFAULT_COLUMNS` / `_fix_model_name_and_id` 消费者集合。
- 与在写改动的交点，逐一标了「工作树已在修（未提交）」：S10-01（SSH-first 回落）、S10-04（top_p 一路打通 + 面板滑杆）、S10-05（面板补 `reasoning_tokens` 与 `DEFAULT_REASONING_TOKENS=8192`）、S10-20（`TIMEOUT_MIN→0`）。**这些都不算本片的新发现被修掉了**，除非你确认在写版落库。
