# Agent 观察契约与结果校验设计 — P0 第一刀

- 日期：2026-11-24
- 状态：设计已逐节批准，待规格评审
- 范围：仅“观察契约 + 结果校验”。高风险动作门禁、不可信工具输出隔离、时效冲突裁决和 UI/SSE 展示不在本刀。
- 基线：当前工作树有大量未提交改动；本设计已在当前树只读核实，实施时保留全部在途工作，不 stash、不重置、不覆盖。
- 引用口径：文中 `文件:行号` 为 2026-11-24 实施前已核实锚点；实现后行号会漂移，以符号名为准。

## 1. 背景与目标

当前工具结果有两个 `ToolResult` 定义：`backend/agent/tools/__init__.py:20` 与 `backend/agent/harness/contracts.py:197`。二者已共享错误分类等字段，但仍缺少统一的来源、观察时间、数据生效时间、作用域、完整性和结果契约版本。`ToolRegistry.execute` 目前只做参数 schema 校验、硬超时和异常收口（`backend/agent/tools/__init__.py:396`，已核实）；`Saddle.execute` 将工具结果映射到 harness 结果（`backend/agent/harness/saddle.py:381`，已核实）；`build_tool_result_text` 直接把成功输出交给模型（`backend/agent/harness/stirrups.py:115`，已核实）。

目标是让工具返回首先成为可描述的“观察”，再进入模型上下文：

1. 每次真实工具执行生成统一观察信封。
2. 严格试点工具在结果进入模型前经过确定性校验。
3. 旧工具继续运行，但明确标记证据不完整。
4. 校验失败时不得把原始错误正文当作成功事实回传。
5. 不改变现有 UI、SSE payload、审计结论枚举和跨端契约。

## 2. 已批准决策

| 决策 | 结论 | 理由 |
| --- | --- | --- |
| 第一刀范围 | 观察契约 + 结果校验 | 后续证据绑定、时效裁决和拒答的基础；不与权限语义、提示隔离混在一个大改动 |
| 在途改动 | 在当前树继续 | 用户已确认；保留全部未提交工作，只做外科手术 |
| 兼容策略 | 渐进式双轨 | 旧工具缺字段不直接失败；严格试点逐步建立可信观察 |
| 实现路径 | 独立 `ToolObservation` 值对象 | 避免继续把字段散加到两个 `ToolResult`；比改 Driver 三条路径侵入更小 |
| 严格试点 | `file_read`、`file_glob`、`file_grep` | 只读、幂等，且已涉及空结果、截断和覆盖度问题 |
| 完整性口径 | 本次请求范围 | 不把“读取 200 行”误说成“整个文件已读完” |
| 校验失败 | `INVALID_RESULT`，不自动重试，不回传原始正文 | 结果契约错误不是瞬时网络错误，重试同一路径通常无新证据 |

## 3. 非目标

- 不合并两个 `ToolResult` 类型；本刀只让它们引用同一观察类型。
- 不实现工具结果 schema 生成器或结构化 JSON 输出协议；本刀是文本结果契约。
- 不实现独立事实验证器、引用检索、时效冲突裁决或拒答策略。
- 不改变高风险动作权限、审批卡、visible-terminal、Undo 或审计链结构。
- 不实现不可信工具输出隔离或提示注入防御；该能力留到下一刀。
- 不新增 UI、SSE 字段、数据库表或配置开关。
- 不修改 Goal/Plan/Spec、SSH 入口和 cKunBot。

## 4. 模块边界

新增 `backend/agent/observation.py`，只依赖标准库和契约类型，负责：

- 构建观察信封；
- 规范化参数并计算 SHA-256；
- 执行确定性校验；
- 计算 `valid/partial/degraded/invalid` 状态；
- 序列化观察头；
- 将 invalid 结果转换为统一的 `INVALID_RESULT` 失败结果。

修改边界：

- `backend/agent/harness/contracts.py`：新增 `ToolObservation`、`ObservationStatus`，扩展 `ErrorType.INVALID_RESULT`；两个 `ToolResult` 各新增可选 `observation`。
- `backend/agent/tools/__init__.py`：`BaseTool` 增加可选观察钩子；`ToolRegistry.execute` 在每次工具尝试返回后构建并校验观察。

观察钩子使用 `observation.py` 中的 `ObservationHint(TypedDict, total=False)`，只允许 `source_as_of: str | None`、`complete: bool | None`、`warnings: tuple[str, ...]` 三个键。`BaseTool.observation_hint(context, args, result) -> ObservationHint` 默认返回 `{}`；Registry 负责补齐其余字段并拒绝非法键或类型。
- `backend/agent/harness/saddle.py`：映射时透传 `observation`，其他逻辑不变。
- `backend/agent/harness/stirrups.py`：`build_tool_result_text` 在正文前序列化观察头；`build_error_feedback` 增加 `INVALID_RESULT` 分支。
- `backend/agent/tools/builtin/ops_file.py`：三只试点工具通过观察钩子补充请求范围完整性，不逐返回分支手写完整信封。

`Driver` 不新增观察逻辑，仍只消费 `build_tool_result_text` 的结果。

## 5. 数据流

```text
LLM tool_call
  -> PRE_TOOL_CALL 权限闸（现有）
  -> Saddle（现有沙箱 / Undo / 幂等重试）
  -> ToolRegistry.execute
       -> 参数 schema 校验（现有）
       -> 工具执行（现有硬超时）
       -> 构建 ToolObservation
       -> strict 校验或 legacy degraded
  -> Saddle 映射 ToolResult.observation
  -> Driver
       -> build_tool_result_text
       -> 观察头 + 正文 / 统一失败原因
  -> 模型上下文
```

校验位于 Registry 内，因此每次重试尝试都会重新观察。参数校验失败、未知工具和沙箱拦截发生在执行前，不伪造观察信封。

## 6. 观察信封

| 字段 | 类型 | 严格试点 | legacy | 说明 |
| --- | --- | --- | --- | --- |
| `observation_id` | `str` | 自动 | 自动 | 单次观察 UUID |
| `tool_name` | `str` | 自动 | 自动 | 实际注册工具名 |
| `args_sha256` | `str` | 自动 | 自动 | 规范化 JSON 的 SHA-256 |
| `source` | `str` | 必填 | `unknown` | `local` 或 `ssh:<session_id>`，不写真实主机地址 |
| `observed_at` | `str` | 自动 | 自动 | UTC ISO 时间 |
| `source_as_of` | `str \| None` | 必填 | `None` | 数据生效时间；文件读取可与观察时间相同 |
| `scope` | `dict[str, str]` | 必填 | `{}` | path、pattern、offset、limit、max_lines 等请求范围 |
| `complete` | `bool \| None` | 键必须存在 | `None` | 本次请求范围是否完整 |
| `schema_version` | `str` | 必填 | `legacy` | 试点固定为 `file-read@1`、`file-glob@1`、`file-grep@1` |
| `output_sha256` | `str` | 自动 | 自动 | 回传正文 SHA-256 |
| `warnings` | `tuple[str, ...]` | 可选 | 自动补证据不完整 | 截断、覆盖不足等确定性提示 |
| `status` | `ObservationStatus` | 计算 | 计算 | `valid/partial/degraded/invalid` |
| `issues` | `tuple[str, ...]` | 计算 | 计算 | 校验问题，供日志和测试断言 |

规范化参数规则：UTF-8、`sort_keys=True`、紧凑分隔符、保留中文；不可序列化参数由 Registry 的既有参数校验优先拦截。

`source` 只使用会话 ID 等 opaque 标识，不把内网地址、主机名或凭据写入公开文档、UI 或日志。

## 7. 状态与校验规则

### 7.1 valid

strict 工具成功，信封必填字段齐全，`complete=true`，无确定性问题。

### 7.2 partial

strict 工具成功，信封必填字段齐全，但 `complete=false`，例如明确截断或覆盖不足。正文可回传，观察头必须说明不完整。

### 7.3 degraded

以下任一情况：

- legacy 工具缺来源、生效时间或作用域；
- strict 工具 `complete=null`，无法证明请求范围完整；
- 工具本身失败，结果只作为诊断，不作为事实。

degraded 不改变原工具成功/失败行为，只增加确定性提示。

### 7.4 invalid

strict 工具出现以下确定性冲突：

- 缺必填信封字段；
- `success=true` 但 `output` 为空；
- `output_sha256` 与正文不一致；
- `complete=true` 但正文包含已知截断标记；
- 只读试点返回 `pending_delivery`；
- 观察钩子抛出异常或返回非法类型。

invalid 转换为：

```text
success=false
output=""
error_kind=INVALID_RESULT
retryable=false
observation.status=invalid
observation.issues=<具体问题>
```

原始正文不进入模型消息。观察只保留哈希和问题列表，不复制原始敏感输出。

### 7.5 工具失败

工具自身 `success=false` 时保留现有 `error_kind/retryable`、错误正文和部分输出诊断；观察状态为 degraded。参数校验失败、未知工具、沙箱拦截不生成观察。

## 8. 试点工具语义

### `file_read`

- `schema_version="file-read@1"`。
- `scope` 包含 `path/offset/limit`。
- 成功返回指定片段时，`complete` 表示“本次片段读取完成”，不表示整个文件已读完。
- 空文件或 offset 超范围的既有明确消息视为完整请求结果。
- 远程通道失败、缺退出码、stderr 异常沿用工具失败路径。

### `file_glob`

- `schema_version="file-glob@1"`。
- `scope` 包含 `path/pattern`。
- 明确带 `ops_common.truncate_output` 截断标记时 `complete=false`。
- 无匹配消息是完整请求结果。
- 无法证明未截断时 `complete=null`，状态 degraded。

### `file_grep`

- `schema_version="file-grep@1"`。
- `scope` 包含 `path/pattern/max_lines`。
- 达到 `max_lines` 或存在不可读文件覆盖提示时 `complete=false`。
- 无匹配消息是完整请求结果。
- 远程腿看不到逐个文件读取失败；不能据“无匹配”推断目录全覆盖，除非观察钩子能证明。

## 9. 错误、重试与回滚

- 新增 `ErrorType.INVALID_RESULT = "invalid_result"`。
- `ErrorClassifier` 优先采信执行边界写入的 `error_kind`，因此 invalid 不会被文案关键字误判为瞬时错误。
- `retry_with_backoff` 见到 `retryable=false` 立即停止；即使试点工具幂等，也不自动重跑同一 invalid 调用。
- `note_tool_outcome` 不把 `INVALID_RESULT` 计入连续参数错误。
- `build_error_feedback` 对 invalid 返回明确纠偏：结果不可作为事实依据，可更换工具、缩小范围或说明无法确认。
- strict 试点均为只读工具；invalid 触发自动回滚时只涉及 `NoOpUndo`。
- legacy 写工具保持原行为，不因 observation 失败被阻断。

## 10. 序列化

观察头放在工具正文最前，采用确定性纯文本，不引入新的提示词体系：

```text
[工具观察] status=partial source=ssh:<session_id> observed_at=<UTC> source_as_of=<UTC> scope=<compact-json> complete=false schema_version=<version>
提示：本次结果可能不完整，只能作为工具报告，不能据此做全量或当前事实断言。
---
<tool output>
```

legacy 使用 `status=degraded`、`source=unknown`、`complete=null`，并提示“来源、时效或作用域不完整”。invalid 不序列化正文，只走统一错误反馈。

观察头位于头部，现有 `Bridle.truncate_tool_result` 的头尾保留策略会优先保留它；不修改结果优先级和压缩逻辑。

## 11. 兼容与基线保护

- 两个 `ToolResult` 的既有构造参数保持兼容，新字段有默认值。
- 现有工具不实现观察钩子时走 legacy degraded。
- 现有 `TraceEvent.tool_call_result` payload 不变；invalid 只通过既有 `success=false` 和结果正文体现。
- 现有审计结论枚举、审批键、visible-terminal 回传和前端类型不变。
- 当前未提交的 `test_tool_error_typing_budget.py`、`test_tool_timeout_cleanup.py` 等测试必须保持通过。
- 实施时逐文件读取最新内容，不使用本设计行号作为自动编辑锚点。

## 12. 测试矩阵

### 单元测试

- 观察构建：自动字段、UTC、来源映射、规范化哈希。
- 四态判定：valid、partial、degraded、invalid。
- 故障注入：缺字段、成功空输出、哈希不一致、截断冲突、非法观察钩子。
- 序列化：元数据齐全；degraded 有提示；invalid 无原始正文。
- 错误语义：`INVALID_RESULT` 分类、不重试、不计参数错熔断。

### 集成测试

- Registry：strict valid/partial/invalid；legacy degraded；原输出不变。
- Saddle：observation 透传；瞬时重试与幂等保护不失效。
- Driver：串行和并行只读消息配对不变；invalid 的 `tool_call_result.success=false`。
- 试点工具：空结果、正常结果、截断、无匹配、远程失败、不可读覆盖。
- 回归：schema、超时、错误类型、并行只读、审批生命周期、visible-terminal。

### 契约与门禁

- `backend/tests/test_ai_loop_core_regression.py` 保持通过。
- `npm run typecheck`、`npm test`、`npm run test:backend`、`npm run lint:backend`。
- 提交前按仓库要求运行 `bash scripts/ci-check.sh`；本刀未经用户要求不提交。

## 13. 实施顺序

1. 新增观察类型、纯校验/序列化模块和单测，不改变运行时行为。
2. 接入 Registry、Saddle、Stirrups，仅启用 legacy degraded。
3. 为三只试点工具补观察钩子，启用 strict，补集成与故障注入测试。
4. 跑 targeted pytest 与完整门禁；门禁期间不再改树。

## 14. 验收标准

- 所有既有测试通过，旧工具构造方式不变。
- 新守卫被故障注入证伪至少一次：strict 工具缺信封或校验冲突时确实拒绝回传。
- invalid 原始正文不进入模型消息。
- legacy 工具仅增加 degraded 观察头，不改变成功/失败和重试行为。
- UI、SSE payload、审计结论枚举和跨端契约无变化。
- 不新增 `any`、`@ts-ignore`、`@ts-expect-error`；新增生产文件不超过 800 行。

## 15. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 当前工作树已有重叠改动 | 实施前逐文件读最新内容；只做最小 diff；不 stash/reset |
| 观察头增加上下文长度 | 头部短文本；利用现有头尾截断；不复制原始输出 |
| `complete` 语义被误解 | 文档和测试明确“本次请求范围”；模型可见头部提示 |
| legacy 全部 degraded 导致提示噪音 | 第一刀只加短标记；不新增 UI 或指标；后续按工具逐步 strict |
| invalid 影响可恢复性 | 明确错误类型和下一步；不自动重试；允许换工具或降级说明 |
| 文档行号漂移 | 所有锚点标注实施前已核实；实现以符号名为准 |
