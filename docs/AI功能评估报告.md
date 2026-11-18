# AI 功能评估报告

> 评估范围：cKunAgentSSH 当前主干已实现的全部 AI 功能。
> 评估维度：核心用途、主要模块与调用链路、模型/服务依赖、实现完整度、局限与风险、优化建议。
> 评估方式：基于对 `backend/agent/**`、`backend/bot/**`、`backend/routes/**`、`src/stores/**`、`src/api/**` 等代码的实读核实，非文档转述。
>
> **修订说明（2026-11-18）**：本报告原基线为 2026-11-14。以下四条结论已随代码演进失效，本次就地修正并标注「（已修复）」：
> ① 多模态视觉输入（原 R8）已实现；② 密钥明文落库（原 R1/P0-1）已改 DPAPI+keyring 信封；
> ③ 无故障回退链（原 R7/P1-6）已实现备用模型切换；④ `supports_tool_calling`（原 R9）已被 driver 真实消费。
> 风险表（§5）保留原始条目并追加现状栏，以留存评估演进轨迹，勿据旧结论判断当前代码。

---

## 1. 总览

项目的 AI 能力由三部分构成，彼此共享模型配置与记忆底座，但职责边界清晰：

| 子系统 | 一句话定位 | 代码规模 | 完整度 |
| --- | --- | --- | --- |
| 运维 Agent（任务台） | 受审批与审计约束的 ReAct 运维执行引擎，AI 的唯一对话面 | 后端 agent/ 包约 2.9 万行 | 核心链路完整，无占位 |
| cKunBot（陪聊机器人） | 独立人格的陪伴聊天，多渠道接入，不执行任何运维操作 | backend/bot/ 22 文件约 5850 行 | 完整，含设计性局限 |
| 前端集成与轻量端点 | 双 store 执行态管理 + 4 个无状态轻量推理端点 + 跨页 AI 入口 | ssh-ai.ts 965 行 + agent.ts 1067 行 | 完整，仅 1 处显式占位 |

**整体结论（前置）**：核心 AI 链路工程化程度高——拦截器链纪律一致、fail-closed 原则贯穿审批/审计/桥接/记忆各层、测试密度大（backend/tests/ 238 个文件，权限安全类 20+）。达到可发布水平。主要风险不在功能缺失，而集中在两处：**对第三方 SDK/平台协议的运行期 hack（稳定性）**与**远程命令中止语义不完整（安全边界）**；原列的「密钥明文落库」已于 2026-11-18 前修复（见修订说明）。

---

## 2. 运维 Agent 核心（后端）

### 2.1 核心用途

将一次自然语言运维请求转化为受控的多步执行：意图判定 → 上下文装配 → ReAct 循环（LLM 决策 + 工具调用）→ 风险拦截与人工审批 → 审计落盘 → 事后反思与策略演化。所有对话统一进入 `POST /agent/run`（SSE），没有独立的「聊天模式」。

### 2.2 主要模块与调用链路

**主执行链**：

```
POST /agent/run (routes/agent.py:698, SSE)
  → Harness.assemble() / Harness.run()   [agent/harness/harness.py]
      组装 LLMClient + ToolRegistry + Reins + Saddle + Bridle + TelemetryBus + CostTracker
  → Driver.drive()                        [harness/driver.py, 1828 行]
      THINK → ACT → OBSERVE → ... → COMPLETED / FAILED / MAX_REACHED
      每轮：动态意图重分类、末轮软预警、强制收敛轮（物理禁工具）、思考预算钳制
  → chunk 队列合并 LLM 流式 token 与 Driver 事件 → SSE 输出（15s 心跳）
```

**拦截器链（Reins，`harness/reins/defaults.py::create_default_reins()`，17 个）**：

| 拦截器 | 位置 | 挂载点 / 职责 |
| --- | --- | --- |
| SkillRein | reins/lifecycle.py | AGENT_START 技能注入 |
| FilesystemGuard | reins/guards.py | PRE_TOOL_CALL(p10) 文件内容级危险判定 |
| LoopDetector | reins/flow.py | POST_ITERATION 死循环检测 |
| PermissionGate | reins/gate.py（1208 行） | PRE_TOOL_CALL(p8) 风险分级 + 审批弹卡 |
| AuditLogger | reins/guards.py | 多点审计链写入 |
| DynamicBudgetRein | reins/flow.py | 动态步数预算（按任务复杂度分配） |
| CostBudgetRein / WallClockRein | reins/flow.py | 成本/token 硬熔断、墙钟 deadline |
| ConfidenceMonitor | reins/flow.py | 置信度监控 |
| ReflectionRein ×2 / PostmortemRein ×2 | reins/lifecycle.py | 完成/失败触发反思与复盘 |
| ClarificationInterceptor | reins/clarification.py（709 行） | 澄清向导（交互/prod/hardline/safe 四道闸） |
| PlanModeGuard / PlanDisciplineRein | reins/plan_mode.py | 计划姿态只读强制 |

**工具系统**：37 个内置工具，单一事实源 `tools/builtin/__init__.py::_ALL_TOOL_CLASSES`（Shell、联网、待办、技能、子 Agent、主机/进程/网络观测、多会话巡检、服务管理、定时任务、文件五件套、后台 Job×4、记忆×4、Runbook×2、工作流×3）。风险三级标注在 `ToolMeta.risk_level`（单一数据源，注册表实时读取）。MCP 插件经 `agent/plugins/mcp_tool.py`。

**远程执行链**（Agent 操作落到目标主机的路径）：

```
工具(shell_exec / ops_*)
  → ops_common.exec_remote()        [tools/builtin/ops_common.py:100]
  → electron_bridge.py（客户端：错误分类 / fail-closed / 在途请求绝不重发）
  → bridge_relay.py（服务端：hello token HMAC 恒时比对、req-res 复用、断线立即失败）
  → /ws/bridge（main.py:484）
  → Electron 主进程反向拨号 → ssh2 执行
```

**记忆系统**（`agent/memory/`）：

| 模块 | 职责 |
| --- | --- |
| mstore.py（886 行） | memories 主表唯一事实源 + memory_jobs 任务队列 + 审计留痕 |
| retriever.py | FTS BM25 + 向量 KNN 并行召回 → RRF(k=60) 融合 → kind 加权 → 余弦下限 → 断层截尾 |
| reconcile.py（527 行） | Reconciler：LLM 批量裁决 ADD/UPDATE/DELETE/NOOP，失败重试 3 次，无 LLM 时纯阈值去重 |
| vec.py / embedding.py | sqlite-vec 加速 + Python 余弦兜底；embedding 仅真实 API，失败宁缺毋滥 |
| blocks.py / budget.py | 画像块（agent 仅限 replace）；统一注入预算（blocks 15% / injection 20% / summary 25% / reserve 12%） |
| consciousness_tick.py | 后台心跳三任务：reconcile_jobs / trim+decay / reflect_failures |

**审批与审计**：三模式 `normal | auto | plan`（auto 为 LLM 代审一次，裁不过照旧弹卡；plan 只读强制）。授权三范围 once（绑 run_id）/ session（绑会话）/ permanent（allowlist 文件 + TTL + `@host` 主机限域；损坏文件隔离 `.bak` 留证）。命令内容级判定在 `harness/safety.py`（1855 行，含 dotenv/凭据/xargs/路径逃逸等家族规则），只读证明在 `harness/readonly_policy.py`（1219 行）。审计为哈希链（`agent/audit.py` 630 行），写失败 fail-closed。

**后处理**（`post_processor.py`，1208 行）：标题生成（仅首轮 + fallback）、记忆提取（投 memory_jobs）、技能沉淀（质量门 + slug 去重 + 全局上限 5）。触发点在 `routes/agent.py:1447`（done 事件、assistant 落库后）。反思与策略演化由 `agent/reflection.py` + `agent/evolution.py`（无 LLM 轻量演化，写入 agent_strategies 表）承担，后台线程执行。

### 2.3 模型与服务依赖

| 依赖 | 说明 |
| --- | --- |
| LLM 协议 | OpenAI 兼容（openai SDK `AsyncOpenAI`），对接本地 vLLM 或 DeepSeek/OpenAI/Qwen/Kimi/GLM/Xiaomi/Anthropic 兼容端点 |
| 客户端管理 | `llm_adapter.py`：LRU 池（max 8）+ in-flight 计数防淘汰掐断在途流 |
| thinking | vLLM `chat_template_kwargs.enable_thinking + thinking_budget`（Qwen3 族） |
| 降级策略 | BadRequestError 三路重试：去 tools（纯文本）→ 去 extra_body → 抛出 |
| embedding | 同一模型配置派生的 `/v1/embeddings` 客户端；不可用时静默降级 FTS-only |
| 向量存储 | sqlite-vec 按连接加速，不可用回退 Python 余弦全量扫描 |
| 视觉 | **已支持多模态视觉输入**（已修复 R8）：`harness/llm_client.py::_attach_images_to_messages` 在 LLM 请求边界把 image_url 并入首条 user 消息，不污染持久态；不支持视觉的端点有降级提示 |

### 2.4 实现完整度

**评级：核心链路完整，工程纪律一致。**

- 全库检索 TODO/FIXME/NotImplemented/占位 后确认：核心 AI 链路无未完成占位，命中的均为设计说明或 SQL 占位符绑定。
- 真实缺口（代码内明示）：
  1. ~~`db/_secrets.py:8` —— 非 Windows 平台密钥明文落库，唯一真 TODO（接系统凭据库）。~~ **（已修复）** 现为 DPAPI（Windows）/ keyring（POSIX）凭据信封，写入侧 fail-closed，见 `backend/db/_secrets.py`。
  2. `tools/builtin/ops_common.py:132` —— bridge 缺 kill 通道，取消运行时远程命令可能继续执行（注释明示待协议补齐）。
  3. `agent/plugins/manager.py:9` —— MCP 插件进程树兜底（Windows Job Object 式）未实现。
  4. `tools/builtin/ops_schedule.py` —— cron month 字段仅支持 `*`，按月指定显式拒绝（fail-fast 设计，非静默）。
- 测试：backend/tests/ 238 个文件覆盖执行循环、意图/预算、工具、权限/审批/审计（密度最高，20+ 文件）、桥接、记忆、反思演化、后处理、LLM 客户端，无裸奔模块。

---

## 3. cKunBot 陪聊子系统（backend/bot/）

### 3.1 核心用途

独立人格的陪伴聊天：人格/表达 DNA/关系/七轴状态/记忆/主动性/多渠道。与运维 Agent **零打通**（不调工具、不执行命令），仅共用模型配置与 memories 表（不同 scope）。三条出话路径（stream_reply / complete_reply / ambient_reply）共用同一份 prompt 装配（`chat_service._system_prompt()`），分节顺序固定，SAFETY_CHARTER 压尾。

### 3.2 主要模块与调用链路

**人格与表达**：

| 模块 | 职责 |
| --- | --- |
| character.py（301 行） | 安全宪章（7 条，压 prompt 尾部）+ 8 情境反应库 + 人格编辑器取值字典 + 保存侧注入拒绝 + 输出侧敏感句剔除 |
| voice.py（316 行） | 表达 DNA 档位字典（句长/语气词/标点/emoji/颜文字/表情包）、去 AI 模板腔（过度清洗自动回滚）、分条与群聊投递形态收口 |
| relation.py（120 行） | 关系分：score 只增 + 读时衰减，四阶段（陌生/熟悉/亲近/默契），UI 进度条与 prompt 同源 |
| state.py（396 行） | 七轴状态（trust/intimacy/tension/novelty/valence/energy/patience）：事件→增量确定性推导、读时惰性衰减回基线、软饱和+事件冷却+限幅；**0 次额外模型调用**，只喂档位词；pacing_hint 驱动前端动画 |
| memory.py（866 行） | 11 类正则抽事实 + 噪声过滤；三重门控召回（硬命中/情境门/旧事钩子）；定向遗忘；45 天半衰 + 200 条容量淘汰 |
| plan.py / proactive.py（846 行）/ pace.py | 计划钩子（时间词→窗口）、主动性调度（四重闸 + 冷场阶梯 + 探口）、分段投递间隔（总预算 6s） |

**多渠道接入**（provider 5 成员协议：start / outbound_parts / fallback / name / reply_window_s）：

```
入站：平台事件 → ChannelProvider.start(cfg, on_inbound)
  → ChannelRuntime._on_inbound()（每条消息实时解析 persona_id，在线换人格不重连）
  → InboundPipeline.handle()（按会话分锁幂等 / ambient 窄路 / 意愿寄存捎带 / 超时 fallback）
  → chat_service.stream_reply()（LLM 流式 + 清洗）
  → _deliver()：provider.outbound_parts() 分段 + pace 逐段延迟发送
```

- **QQ**：botpy 1.2.1 长连接，阶梯重连（2/5/15/30s），401/403 不重试；主动消息绕过 SDK 直调 v2 接口（支持互动召回）。
- **微信**：回调推送模型（无长连接），`/bot/wechat/{channel_id}` 回调（64KB 限流），半小时客服接管续期。
- **本地**：非 provider，`/bot/chat` SSE 直连。

**寄存机制**：平台不让主动发（permission/频控/offline）时，「心里话」写 `bot_proactive_log`（TTL 48h），对方来消息时以「对了，…」捎带。

**表情包**：模型独占一条输出 `[表情:开心]` 标记 → 校验键 + 收口为一轮最多一张 → 本地内置 PNG / QQ base64 直传富媒体 / 微信公网 URL（无则回落颜文字）。

### 3.3 模型与服务依赖

- LLM：与运维 Agent 共用 `llm_adapter.chat_stream`（OpenAI 兼容 SSE）与同一套模型配置表；按人格 model_id → SSH 默认 → 全局默认逐级回落。
- embedding：由同一模型配置派生，不可用时静默降级 FTS-only。
- 平台依赖：QQ botpy SDK（含运行期 parser 补丁）；微信公众号回调加解密（wechat_crypto.py）。

### 3.4 实现完整度

**评级：完整，无 TODO 残留；存在设计性局限与上游耦合。**

- 测试：bot 相关 19 个文件约 7460 行（主动性 1467 行最重），管线/人格/状态/记忆/计划/路由/存储全维度覆盖。
- 设计性局限（代码内明示或结构决定）：
  1. botpy 运行期 hack：`_install_group_message_parser()` 往 SDK parsers dict 补全量群消息；主动消息绕 SDK 直调 `Route`（SDK `payload = locals()` 会把 null 塞进 JSON）——对上游版本升级脆弱。
  2. 主动消息失败分类靠异常中文文案匹配（业务码只在异常文案里），上游文案一变分类即失准。
  3. 微信发图依赖公网 `webhook_base_url`，否则回落颜文字；小程序渠道 media_id 无法直接发图。
  4. ambient（群环境消息）全程进程内不落库，重启丢失；@ 窗口 5 分钟受平台被动回复窗口硬约束。
  5. 七轴状态是人格级而非会话级，群聊 fatigue_care 选题直接跳过。
  6. QQ 主动消息超 800 字直接截断不分段。

---

## 4. 前端 AI 集成与轻量端点

### 4.1 核心用途

渲染层不承担任何推理，负责三件事：① 会话与消息的持久化与回放；② 单次 run 的执行态（SSE 事件、审批坞、澄清卡、交付卡）；③ 四个无状态轻量推理端点的调用与结果呈现。

### 4.2 主要模块与调用链路

**双 store 分工**（项目术语表钉住的边界）：

| Store | 职责 |
| --- | --- |
| `src/stores/ssh-ai.ts`（965 行） | 会话与消息唯一对话面：发送装配唯一通路 `sendAgentMessage`、三态终端快照、防串主机复查、结构化双态（baseline/enhanced）、跨页预填 `pendingPrefill`（消费一次即清空）、终态固化 watch |
| `src/stores/agent.ts`（1067 行） | 单次 run 执行态：`runAgent`（并发保护 + 旧流 abort）、SSE 全事件 switch 处理、审批三档（once/session/always）、澄清提交、交付确认、`snapshot()` 固化轨迹进消息、四条终态路径对称 + 迟到事件守卫 |

**SSE 可靠性**（`src/api/sse.ts`）：后端 15s 心跳 + 前端 90s 停滞看门狗（5s 巡检，任何字节刷新计时，到点 cancel 并报「连接停滞」）；连接超时 28s 独立；无 `[DONE]` 收尾按「响应中断」走 onError，不把半截响应当完成。

**轻量端点清单**：

| 功能 | 后端路由 | 前端调用 | 说明 |
| --- | --- | --- | --- |
| 长指令结构化 | `/agent/structure`（agent.py:2175） | TaskStructureDialog + useTaskStructuring | 不用工具，拆解补全运维字段，含写步骤时 auto 审批回落 normal |
| Git 提交信息 | `/agent/commit-message`（agent.py:2289） | GitRepoDetailPanel | 无状态单次推理 |
| 命令安全分类 | `/agent/safety/classify`（agent.py:2119） | BulkCommandModal（批量下发唯一判定源） | 判定失败按危险处理，fail-closed |
| 终端命令预测 | `/terminal/predict`（terminal.py:80） | useCompletion 补全弹窗 | 无状态 |
| 批量结果分析 | 无独立端点 | BulkCommandModal → askAi → 主 run | 复用任务台链路 |
| 告警诊断 run | 无独立端点 | useAlertTrigger → 建专用会话 + runAgent | 审批/审计/成本熔断照常生效，四层防风暴闸 |
| 终端选区预填 | 无后端（纯前端拼装） | useTerminalContext → pendingPrefill | 带工作目录与最近命令 |

**跨页 AI 入口统一模式**：全部收敛到 `pendingPrefill`（切回首页任务台 + 填输入框），没有第二个 AI 界面。

**模型管理**：`ModelsPanel.vue`（provider 预设 vllm/openai 兼容、连通性测试、上下文/输出/思考预算、ssh/global 双默认档）→ `routes/settings.py` → `db/models_config.py`。发送时仅透传 model_id，配置本体不出库。

**Electron 主进程**：零 AI 逻辑。只负责后端进程托管（spawn + 健康检查 + token 注入）、`/ws/bridge` 反向客户端、pty/ssh 执行落点。模型配置是渲染层直连 FastAPI。

### 4.3 模型与服务依赖

- 渲染层经 preload 取 token，直连本地 FastAPI；SSE 与 WS 两条通道。
- 无第三方 AI SDK，所有推理经后端。

### 4.4 实现完整度

**评级：完整，1 处显式占位。**

- ＋菜单云端占位项（`HomeComposer.vue:1794`）渲染为禁用态并标注「未接入」，有配套测试——前端唯一显式未接线功能。
- 模型能力标记的消费者现状（修订 2026-11-18）：`supports_tool_calling` 已被 `harness/driver.py:93,338` 真实消费（无该能力时物理禁工具），原「三列均无消费者」结论失效；`supports_thinking` 经 thinking 预算链消费；`provider` 列作展示/回传字段保留于 ModelsPanel。
- 全库 `src/**` 无散落 TODO/FIXME。

---

## 5. 横向分析

### 5.1 共享底座

| 底座 | 运维 Agent | cKunBot |
| --- | --- | --- |
| 模型配置表（db/models_config.py） | 共用；SSH 默认 → 全局默认回退链 | 共用；人格 model_id → SSH 默认 → 全局默认 |
| llm_adapter.chat_stream | 共用（StreamLLMClient 包装） | 共用（chat_service 直接调） |
| memories 表 | 只读 global / host:* scope | 写读 persona:{id} scope |
| embedding 客户端 | run 前检索 / memory_search / Reconciler | 抽事实批量 embed / 召回 |
| bridge 通道 | 工具远程执行唯一通路 | 不使用 |

### 5.2 隔离边界（正确性关键）

- **scope 隔离**：两套 AI 共表不同 scope。衰减/淘汰/遗忘 SQL 必须限定 scope+kind——碰宽了会删到对方的记忆（项目已有专门测试守卫）。
- **人格隔离**：cKunBot 不持有工具执行能力，运维话术不会喂进陪聊人格；任务台是运维 Agent 的唯一对话面。
- **群记忆隔离**：群聊召回只认 `source_conv_id` 是本群会话的记忆，防止当着多人出口私聊事实。

### 5.3 工程纪律观察

- fail-closed 一致性高：审计写失败阻断、allowlist 损坏隔离留证、embedding 失败宁缺毋滥、安全分类失败按危险处理、bridge 断线在途请求绝不重发。
- 单一事实源纪律：工具清单、风险等级、人格选项、审计结论枚举、中英 README 均有同源守卫测试。

---

## 6. 局限与潜在风险（按严重度）

### 高（安全边界）

| # | 风险 | 位置 | 影响 |
| --- | --- | --- | --- |
| ~~R1~~ | ~~非 Windows 平台 API 密钥明文落库~~ **（已修复 2026-11-18）** | `backend/db/_secrets.py`（DPAPI + keyring 信封，写入 fail-closed） | 原结论已失效：非 Windows 走系统凭据库，库文件不出现密钥明文 |
| R2 | bridge 缺 kill/取消通道 | `ops_common.py:132` 注释明示 | 用户中止 run 后，已下发的远程命令可能继续执行——对「危险操作审批后执行」的场景，中止语义不完整 |
| R3 | auto 审批模式的 LLM 代判误判面 | `reins/auto_approve.py` | 代审仅一次，误判放行中等风险不可逆操作的可能性存在；已用「裁不过照旧弹卡」兜底，但误放行方向无二次校验 |

### 中（稳定性 / 上游耦合）

| # | 风险 | 位置 | 影响 |
| --- | --- | --- | --- |
| R4 | botpy 运行期 parser 补丁 + 绕 SDK 直调 | `qq_manager.py` | SDK 升级（改 parsers 结构 / payload 行为）会静默破坏群消息与主动消息 |
| R5 | 主动消息失败分类靠异常文案匹配 | `qq_channel.classify_proactive_error()` | 平台改异常文案，寄存/重试分类失准，可能错发或漏寄存 |
| R6 | ambient 群环境消息不落库 | `group_ambient.py` | 重启丢失上下文，群内连续性中断 |
| ~~R7~~ | ~~单模型依赖，无故障回退链~~ **（已修复 2026-11-18）** | `harness/harness.py::_make_fallback_switch` + `stirrups.py::call_llm` | 原结论已失效：TRANSIENT 错误耗尽重试后自动切备用模型（每 run 一次），切前迁移 token/chunk 回调并告警；FATAL 不切 |

### 低（能力边界 / 遗留）

| # | 风险 | 位置 | 影响 |
| --- | --- | --- | --- |
| ~~R8~~ | ~~无视觉多模态消费~~ **（已修复 2026-11-18）** | `harness/llm_client.py::_attach_images_to_messages`（`harness.assemble` 注入 image_url） | 原结论已失效：图片在 LLM 请求边界并入首条 user 消息，不污染持久态；不支持的端点有中文降级提示 |
| ~~R9~~ | 能力标记无消费者 **（部分修复 2026-11-18）** | `db 列 + ModelsPanel 徽标` + `harness/driver.py:93,338` | `supports_tool_calling` 已被 driver 真实消费（无该能力时物理禁工具），原结论对此项已失效；`provider` 列仍仅作展示/回传字段，`supports_image` 消费面待核 |
| R10 | 七轴状态人格级而非会话级 | `bot/state.py` | 群聊场景 fatigue_care 直接跳过，状态对多会话场景表达力不足 |
| R11 | QQ 主动消息 800 字截断 / 微信发图依赖公网 URL | bot 渠道层 | 长内容与富媒体体验受限（均有回落路径，非阻断） |
| R12 | cron month 仅支持 `*` | `ops_schedule.py` | 按月计划任务无法表达（显式拒绝，无静默错误） |

---

## 7. 优化建议（按优先级）

### P0（安全正确性，建议尽快）

1. ~~**密钥接系统凭据库**（修 R1）~~ **（已完成 2026-11-18）**：Windows DPAPI + POSIX keyring 凭据信封已落地，写入侧 fail-closed。
2. **bridge 协议补 kill/取消通道**（修 R2）：在 relay 协议中增加 cancel 帧，Electron 侧对在途 ssh 命令执行 `channel.close()`/进程终止；至少对 dangerous 命令保证中止语义完整。这是审批体系可信度的组成部分。

### P1（稳定性与体验，下一迭代）

3. **主动消息错误分类改结构化**（修 R5）：QQ 返回体含业务码时解析业务码，文案匹配降级为兜底；为 R4 的 SDK 升级脆弱性补一组「升级前冒烟测试」。
4. **ambient 落库持久化**（修 R6）：群环境消息按现有幂等键写入 bot_messages 或独立表，重启可续。
5. **模型能力标记收敛**（修 R9 剩余部分）：`supports_tool_calling` 已消费（本条对该项已完成）；`provider` 列明确为展示字段可保留，`supports_image` 的消费面需核实后二选一——接入或删列，消除半退役状态。
6. ~~**可选备用端点回退**（修 R7，需产品决策）~~ **（已完成 2026-11-18）**：`harness.py::_make_fallback_switch` 已实现按 run 配置的备用模型切换，仅 TRANSIENT 触发、每 run 一次，保留「缺默认模型明确拒绝」语义。

### P2（能力扩展，择机）

7. ~~**视觉多模态接入**（修 R8）~~ **（已完成 2026-11-18）**：`harness/llm_client.py` 在请求边界构造 image_url 内容块，无视觉能力的模型给出降级提示。
8. **＋菜单云端项接线决策**：接入真实功能或移除占位，避免长期禁用态积灰。
9. **cron month 支持**（修 R12）：放宽 `ops_schedule.py` 校验并补 schedule_engine 对应解析。
10. **七轴状态按会话维度细化**（修 R10）：将 last_event_kind 等短期轴下移到会话级，解除群聊 fatigue_care 的跳过逻辑。

---

## 8. 结论

- **成熟度**：三套 AI 子系统均已完整落地且无占位，核心链路（ReAct 循环 + 17 拦截器 + 审批/审计/记忆）工程化程度显著高于同类项目平均水平；238 个测试文件、fail-closed 纪律与单一事实源守卫是可信度的主要来源。
- **最值得肯定的设计**：审批与交付分离、授权三范围 + 主机限域、记忆统一预算分账、bot 状态轴零模型调用的确定性推导。
- **当前最大的两个风险**：远程命令中止语义不完整（R2）与 auto 代判误判面（R3）——均属安全边界，建议按 P0 处理。原列的「密钥明文落库」（R1）已修复。
- **主要的外部脆弱点**：QQ SDK 运行期补丁与异常文案匹配（R4/R5），升级前需跑渠道冒烟测试。

---

*评估基线：2026-11-14 主干代码，2026-11-18 就地修订四条已失效结论（R1/R7/R8/R9，见文首修订说明）。行号引用随代码演进可能漂移，函数名与文件为准。*
