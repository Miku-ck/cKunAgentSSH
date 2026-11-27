# Agent 观察契约与结果校验 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让每次真实工具执行先成为带来源、时间、作用域、完整性和契约版本的观察，严格试点结果在进入模型前经过确定性校验，非法正文不得作为事实回传。

**Architecture:** 新增独立 `ToolObservation` 值对象与纯函数模块；`ToolRegistry` 在真实执行返回后统一构建、校验并必要时把结果转换为 `INVALID_RESULT`；`Saddle` 只透传观察；`Stirrups` 在模型可见正文前加确定性观察头。旧工具走 legacy degraded，`file_read` / `file_glob` / `file_grep` 走 strict 双轨。

**Tech Stack:** Python 3 / asyncio / dataclasses / TypedDict / hashlib / pytest；现有 FastAPI Agent harness。

**Spec:** `docs/superpowers/specs/2026-11-24-agent-observation-contract-design.md`（已逐节批准）。执行者必须先读 spec 与本计划。

## Global Constraints

- 用户已批准在当前脏树继续：保留全部在途改动，不 stash、不 reset、不 checkout、不覆盖；每个目标文件编辑前读取最新内容。
- 未经用户明确要求不提交、不推送。计划中的验证不等于提交授权。
- 最小外科手术：不改 `Driver`、权限闸、审批、visible-terminal、Undo、UI、SSE payload、数据库和审计结论枚举。
- 不合并两个 `ToolResult`；两者只新增 `observation: ToolObservation | None = None`，既有构造参数保持兼容。
- 不新增 `any`、`@ts-ignore`、`@ts-expect-error`；异构结构用具体类型或 `unknown` + 类型守卫。
- 不写裸空 `catch {}` / `except Exception: pass`；Python 忽略异常必须有日志或理由注释。
- 不硬编码密钥、令牌、内网地址、端口；`source` 只写 `local` 或 `ssh:<opaque-session-id>`。
- 新增生产文件不超过 800 行；存量超限文件只减不增。
- 每个新守卫必须故障注入证伪至少一次：先写测试并跑红，再最小实现跑绿；随后按任务给出的方式主动破坏被测代码，确认测试再次变红，立即恢复并跑绿。
- 开发期在 `backend/` 下运行靶向 pytest；Windows Git Bash 环境若 httpx 受代理影响，按仓库记忆清理代理环境变量，不修改项目配置。
- 最终唯一门禁是 `bash scripts/ci-check.sh`；门禁运行期间独占机器且不改工作树。

## Plan-Level Clarifications

1. `complete` 的语义固定为“本次请求范围是否完整”。`file_read(offset=50, limit=20)` 完整返回这 20 行时可以是 `complete=true`，不表示整个文件已读完。
2. strict 工具的 `source_as_of` 缺省取同一次观察的 `observed_at`，表达“本次观察所见的数据时点”，不冒充文件 mtime；钩子以后可以提供更准确的来源时间。
3. `scope` 由 Registry 从规范化后的请求参数派生并转为字符串。`file_read` 缺省补 `offset=1, limit=200`；`file_grep` 缺省补 `max_lines=200`。
4. strict 成功结果若钩子未提供 `complete`，状态为 `degraded`，不是 `valid`；若直接构造的观察缺少 `scope` / `source_as_of` 等结构字段，则为 `invalid`。
5. 工具自身 `success=false` 一律 `degraded`，保留既有错误与部分输出；只有 strict 成功结果发生确定性契约冲突才转 `INVALID_RESULT`。
6. 远程 `file_grep` 没有逐文件失败可见性：命中未截断或“无匹配”均不能证明目录全覆盖，`complete=null`；明确达到 `max_lines` 时 `complete=false`。
7. invalid 的错误文案固定为“工具结果未通过观察契约校验，不能作为事实依据；可更换工具、缩小范围或说明无法确认”。具体问题放在 `error` 末尾，原始 `output` 清空。

## File Structure

| 文件 | 责任 | 动作 |
| --- | --- | --- |
| `backend/agent/observation.py` | 观察类型提示、参数哈希、构建、四态校验、invalid 转换、观察头序列化 | 新建 |
| `backend/agent/harness/contracts.py` | `ObservationStatus` / `ToolObservation` / `ErrorType.INVALID_RESULT` / harness 结果引用 | 改 |
| `backend/agent/tools/__init__.py` | 工具层结果引用、观察钩子、strict 常量、Registry 执行后观察 | 改 |
| `backend/agent/harness/saddle.py` | 工具层观察到 harness 结果透传 | 改 |
| `backend/agent/harness/stirrups.py` | 观察头、invalid 纠偏反馈、错误分类 | 改 |
| `backend/agent/tools/builtin/ops_file.py` | 三只试点工具的完整性提示 | 改 |
| `backend/tests/test_tool_observation_contract.py` | 纯函数、四态、哈希、序列化、invalid 正文隔离 | 新建 |
| `backend/tests/test_tool_observation_registry.py` | Registry strict / legacy / 故障注入 / 预执行错误 | 新建 |
| `backend/tests/test_tool_observation_pilot_tools.py` | 三只试点工具语义 | 新建 |
| `backend/tests/test_tool_error_typing_budget.py` | invalid 分类、不重试、不计参数熔断 | 最小扩充 |

---

### Task 1: 观察值对象与纯校验模块

**Files:**
- Create: `backend/agent/observation.py`
- Modify: `backend/agent/harness/contracts.py`
- Test: `backend/tests/test_tool_observation_contract.py`

**Interfaces:**
- Produces:
  - `ObservationStatus.VALID / PARTIAL / DEGRADED / INVALID`
  - frozen dataclass `ToolObservation`，字段与 spec §6 完全一致。
  - `ObservationHint(TypedDict, total=False)`，只允许 `source_as_of`、`complete`、`warnings`。
  - `canonical_args_sha256(args: dict) -> str`
  - `build_observation(tool_name: str, args: dict, context: object, result: object, *, strict: bool, schema_version: str, hint: ObservationHint, observation_id: str | None = None, observed_at: str | None = None) -> ToolObservation`
  - `validate_observation(observation: ToolObservation, result: object, *, strict: bool) -> ToolObservation`
  - `make_invalid_result(tool_name: str, observation: ToolObservation) -> "ToolResult"`
  - `serialize_observation_header(observation: ToolObservation) -> str`

- [ ] **Step 1: 写契约测试并确认红**

创建测试，覆盖以下固定行为；先导入尚不存在的模块，预期 collection error。

```python
from dataclasses import replace
from types import SimpleNamespace

from agent.harness.contracts import ObservationStatus, ToolObservation
from agent.observation import (
    build_observation,
    canonical_args_sha256,
    serialize_observation_header,
    validate_observation,
)


def _result(success=True, output="ok"):
    return SimpleNamespace(success=success, output=output, pending_delivery=None)


def test_strict_complete_result_is_valid():
    obs = build_observation(
        "file_read", {"path": "/tmp/a", "limit": 20},
        SimpleNamespace(ssh_session_id="opaque-1"), _result(),
        strict=True, schema_version="file-read@1",
        hint={"complete": True}, observation_id="obs-1",
        observed_at="2026-11-24T00:00:00+00:00",
    )
    checked = validate_observation(obs, _result(), strict=True)
    assert checked.status is ObservationStatus.VALID
    assert checked.source == "ssh:opaque-1"
    assert checked.source_as_of == "2026-11-24T00:00:00+00:00"
    assert checked.scope == {"path": "/tmp/a", "offset": "1", "limit": "20"}
    assert checked.output_sha256 == canonical_args_sha256({"output": "ok"})


def test_contract_conflicts_are_invalid_and_never_keep_raw_output():
    result = _result(output="secret raw body")
    obs = build_observation(
        "file_read", {"path": "/tmp/a"}, SimpleNamespace(ssh_session_id=""), result,
        strict=True, schema_version="file-read@1", hint={"complete": True},
    )
    broken = replace(obs, output_sha256="0" * 64)
    checked = validate_observation(broken, result, strict=True)
    assert checked.status is ObservationStatus.INVALID
    assert checked.issues == ("output_sha256 与正文不一致",)
    assert "secret raw body" not in repr(checked)


def test_serializer_puts_metadata_before_body_and_marks_degraded():
    legacy = build_observation(
        "shell_exec", {"command": "true"}, SimpleNamespace(ssh_session_id=""), _result(),
        strict=False, schema_version="legacy", hint={},
    )
    assert legacy.status is ObservationStatus.DEGRADED
    text = serialize_observation_header(legacy)
    assert text.startswith("[工具观察] status=degraded")
    assert "complete=null" in text
    assert "来源、时效或作用域不完整" in text
    assert text.endswith("---\n")
```

同时加入参数化测试：`complete=False → partial`；`complete=None → degraded`；strict 成功空输出、已知截断标记与 `complete=True`、`pending_delivery` 非空、缺 `scope`、缺 `source_as_of` 均 `invalid`；legacy 缺字段仍 `degraded`；`{"中文": "值", "a": 1}` 与交换键顺序的参数哈希相同，JSON 保留中文。

- [ ] **Step 2: 跑测试确认红**

Run: `cd backend && python -m pytest tests/test_tool_observation_contract.py -q`
Expected: FAIL，原因是 `ObservationStatus` / `agent.observation` 尚不存在。

- [ ] **Step 3: 实现值对象与纯函数**

在 `contracts.py` 增加：

```python
class ObservationStatus(str, Enum):
    VALID = "valid"
    PARTIAL = "partial"
    DEGRADED = "degraded"
    INVALID = "invalid"


@dataclass(frozen=True)
class ToolObservation:
    observation_id: str
    tool_name: str
    args_sha256: str
    source: str
    observed_at: str
    source_as_of: str | None
    scope: dict[str, str]
    complete: bool | None
    schema_version: str
    output_sha256: str
    warnings: tuple[str, ...] = ()
    status: ObservationStatus = ObservationStatus.DEGRADED
    issues: tuple[str, ...] = ()
```

在 `observation.py` 使用 `TYPE_CHECKING` 导入 `ToolResult` 供返回值注解，使用 `hashlib.sha256`、`json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")`、`uuid.uuid4().hex`、`datetime.now(timezone.utc).isoformat()`。`build_observation` 必须检查 hint 的值类型：`source_as_of` 为 `str | None`，`complete` 为 `bool | None`，`warnings` 为字符串 tuple；非法值抛 `ValueError`，由 Registry 转 invalid。`output_sha256` 对 `result.output` 的 UTF-8 字节计算，不对观察头计算。

`build_observation` 只负责自动字段与初始 degraded；`validate_observation` 按以下顺序返回 `dataclasses.replace` 后的副本：

1. 非 strict：degraded，warnings 至少含“来源、时效或作用域不完整”。
2. strict 且工具失败：degraded，保留工具诊断语义。
3. strict 成功：检查必填结构、空正文、哈希、pending delivery、complete 与截断冲突。
4. 无非法问题且 `complete=True`：valid；`False`：partial；`None`：degraded。

截断标记单一来源定义为 `TRUNCATION_MARKERS = ("... [输出已截断，省略中间",)`。`serialize_observation_header` 对 scope 用紧凑 JSON，固定输出：

```text
[工具观察] status=<status> source=<source> observed_at=<time> source_as_of=<time-or-null> scope=<compact-json> complete=<true-false-null> schema_version=<version>
提示：<valid/partial/degraded 对应提示>
---
```

valid 提示“以上为本次工具观察，只在所述来源、时间与范围内使用”；partial 提示“本次结果可能不完整，只能作为工具报告，不能据此做全量或当前事实断言”；degraded 提示“来源、时效或作用域不完整，只能作为工具报告，不能据此做当前事实断言”。invalid 不调用正文序列化。

`make_invalid_result` 在函数体内导入 `agent.tools.ToolResult`，返回 `success=False, output="", error=<固定纠偏文案 + issues>, error_kind="invalid_result", retryable=False, observation=observation`，避免模块顶层循环导入。

- [ ] **Step 4: 跑纯函数测试确认绿**

Run: `cd backend && python -m pytest tests/test_tool_observation_contract.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: 证伪哈希与 invalid 守卫**

临时把 `validate_observation` 的哈希比较改成恒真，跑 `test_contract_conflicts_are_invalid_and_never_keep_raw_output`，Expected: FAIL；立即恢复并重跑全文件，Expected: PASS。记录红/绿命令结果，不提交。

---

### Task 2: 错误语义与两个结果类型的观察引用

**Files:**
- Modify: `backend/agent/harness/contracts.py`
- Modify: `backend/agent/tools/__init__.py`
- Modify: `backend/agent/harness/stirrups.py`
- Test: `backend/tests/test_tool_error_typing_budget.py`

**Interfaces:**
- Consumes: Task 1 的 `ToolObservation` / `ObservationStatus`。
- Produces: `ErrorType.INVALID_RESULT = "invalid_result"`；两个 `ToolResult.observation`；invalid 的模型可见纠偏文本。

- [ ] **Step 1: 写错误语义测试并确认红**

在现有错误分类测试文件中追加三个测试，不重写既有夹具：

```python
import asyncio

from agent.harness.contracts import ErrorType
from agent.harness.stirrups import ErrorClassifier, build_tool_result_text, retry_with_backoff


def test_invalid_result_error_kind_wins_over_text_keywords():
    kind = ErrorClassifier().classify_tool_error(
        "timeout 临时失败", "file_read", ErrorType.INVALID_RESULT.value
    )
    assert kind is ErrorType.INVALID_RESULT


def test_invalid_result_is_not_retried_and_does_not_trip_arg_circuit():
    calls = 0

    async def execute_once():
        nonlocal calls
        calls += 1
        return _invalid_tool_result()

    result, error = asyncio.run(retry_with_backoff(execute_once, max_retries=3, base_delay=0))
    ctx = _run_context()
    text = build_tool_result_text(result, "file_read", ctx)
    assert calls == 1
    assert error
    assert "不能作为事实依据" in text
    assert "secret raw body" not in text
    assert ctx.metadata.get("consecutive_invalid_args", 0) == 0
    assert not ctx.metadata.get("arg_error_circuit_tripped")
```

`_invalid_tool_result()` 使用现有测试夹具的 harness `ToolResult`，填 `success=False, output="", error_kind="invalid_result", retryable=False`。`_run_context()` 复用文件现有上下文工厂；若没有工厂，用 `SimpleNamespace(run_id="run-1", metadata={})`，因为该入口只依赖这两个字段。

- [ ] **Step 2: 跑新增测试确认红**

Run: `cd backend && python -m pytest tests/test_tool_error_typing_budget.py -q`
Expected: FAIL，`ErrorType.INVALID_RESULT` 尚不存在。

- [ ] **Step 3: 实现类型与反馈**

在两个 `ToolResult` 末尾增加 `observation: ToolObservation | None = None`；harness contracts 用 `TYPE_CHECKING` 或同文件已定义类型直接注解，工具层从 `agent.harness.contracts` 导入 `ToolObservation`，不导入 harness 本体。

在 `ErrorType` 增加 `INVALID_RESULT`。确认 `ErrorClassifier` 对已知 `error_kind` 的优先分支自动覆盖它，不添加文本关键字猜测。

在 `build_error_feedback` 增加 invalid 分支：成功/失败都不改变原有错误构造，只有 invalid 使用 Task 1 的固定纠偏文案。`note_tool_outcome` 当前只计数 `INVALID_ARGS`，新增测试证明 `INVALID_RESULT` 不会增加计数，不改其逻辑。

- [ ] **Step 4: 跑错误测试确认绿**

Run: `cd backend && python -m pytest tests/test_tool_error_typing_budget.py tests/test_tool_observation_contract.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: 证伪不重试守卫**

临时让 `retry_with_backoff` 在 `explicit=False` 时仍返回 `True`，运行新增 invalid 测试，Expected: FAIL；立即恢复并重跑，Expected: PASS。

---

### Task 3: Registry 观察装配与 legacy degraded

**Files:**
- Modify: `backend/agent/tools/__init__.py`
- Modify: `backend/agent/observation.py`（仅在测试揭示签名缺口时补充）
- Test: `backend/tests/test_tool_observation_registry.py`

**Interfaces:**
- Produces:
  - `STRICT_OBSERVATION_TOOLS = frozenset({"file_read", "file_glob", "file_grep"})`
  - `STRICT_OBSERVATION_SCHEMAS = {"file_read": "file-read@1", "file_glob": "file-glob@1", "file_grep": "file-grep@1"}`
  - `BaseTool.observation_hint(self, context, args: dict, result: ToolResult) -> ObservationHint`
  - Registry 对真实执行返回统一附加 observation。

- [ ] **Step 1: 写 Registry 测试并确认红**

测试使用现有 `ToolRegistry` / `ToolContext` 模式注册最小假工具，不启动 SSH：

```python
class EchoTool(BaseTool):
    name = "echo_legacy"
    description = "echo"
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, context):
        return ToolResult(True, "legacy body")


class StrictEchoTool(BaseTool):
    name = "file_read"
    description = "strict echo"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}

    def observation_hint(self, context, args, result):
        return {"complete": True}

    async def execute(self, context, path):
        return ToolResult(True, "strict body")
```

断言：

1. legacy 成功：`success=True`、`output=="legacy body"`、observation degraded、source unknown、schema legacy。
2. strict 成功：observation valid、schema `file-read@1`、scope path 存在、output 未被改写。
3. strict 截断冲突：假工具返回带截断标记正文且 hint complete true，结果 `success=False`、`output==""`、`error_kind=="invalid_result"`、`retryable=False`、observation invalid。
4. 未知工具、参数 schema 失败：observation is None。
5. 工具抛异常：Registry 既有 FATAL 结果附加 degraded observation，不伪装成“未执行”。
6. 非 strict 观察构建失败：记录 warning，原结果原样返回，不阻断 legacy。
7. strict 钩子抛异常 / 返回非法键 / 返回非法类型：转 invalid，不泄露正文。

- [ ] **Step 2: 跑测试确认红**

Run: `cd backend && python -m pytest tests/test_tool_observation_registry.py -q`
Expected: FAIL，strict 结果尚无 observation。

- [ ] **Step 3: 接入 Registry**

在 `BaseTool` 增加默认 `observation_hint`，返回 `{}`。在 Registry 常量中固定 strict 集合与版本表。

把 `_execute_with_deadline(...)` 的返回值和异常收口都改为先得到 `ToolResult`，再调用一个私有方法 `_observe_result(tool, args, context, result)`：

```python
def _observe_result(self, tool, args, context, result):
    strict = tool.name in STRICT_OBSERVATION_TOOLS
    try:
        hint = tool.observation_hint(context, args, result)
        if not isinstance(hint, dict):
            raise ValueError("observation_hint 必须返回 dict")
        illegal = set(hint) - {"source_as_of", "complete", "warnings"}
        if illegal:
            raise ValueError(f"observation_hint 含非法键: {sorted(illegal)}")
        obs = build_observation(
            tool.name, args, context, result,
            strict=strict,
            schema_version=STRICT_OBSERVATION_SCHEMAS.get(tool.name, "legacy"),
            hint=hint,
        )
        obs = validate_observation(obs, result, strict=strict)
    except Exception as exc:
        logger.warning("工具 %s 观察校验失败: %s", tool.name, exc)
        if not strict:
            return result
        obs = build_observation(
            tool.name, args, context, result,
            strict=True, schema_version=STRICT_OBSERVATION_SCHEMAS[tool.name],
            hint={},
        )
        obs = replace(
            obs, status=ObservationStatus.INVALID, issues=(str(exc),)
        )
        return make_invalid_result(tool.name, obs)
    if obs.status is ObservationStatus.INVALID:
        return make_invalid_result(tool.name, obs)
    return replace(result, observation=obs)
```

`build_observation` 对 strict 自动补 `source_as_of=observed_at`；legacy 保持 None。未知工具和 schema 失败仍直接返回，不进入此方法。参数哈希失败不能绕过既有 schema 校验；若仍发生，按 strict invalid / legacy 原结果降级处理。

- [ ] **Step 4: 跑 Registry 与既有执行测试确认绿**

Run: `cd backend && python -m pytest tests/test_tool_observation_registry.py tests/test_tool_timeout_cleanup.py tests/test_tool_error_typing_budget.py -q`
Expected: 全部 PASS，超时清理与取消传播不变。

- [ ] **Step 5: 证伪 strict 拒绝**

临时把 strict 分支的 `make_invalid_result(...)` 改为 `return result`，运行截断冲突测试，Expected: FAIL；立即恢复并重跑全部 Task 3 测试，Expected: PASS。

---

### Task 4: Saddle 透传与模型可见观察头

**Files:**
- Modify: `backend/agent/harness/saddle.py`
- Modify: `backend/agent/harness/stirrups.py`
- Test: `backend/tests/test_tool_observation_registry.py`

**Interfaces:**
- Consumes: Registry 结果上的 `observation`。
- Produces: harness `ToolResult.observation`；`build_tool_result_text` 输出“观察头 + 原正文”。

- [ ] **Step 1: 写透传与序列化测试并确认红**

```python
def test_saddle_passes_observation_without_changing_output():
    registry = _registry_returning(_strict_result_with_observation())
    saddle = Saddle(registry=registry)
    result, _undo = asyncio.run(saddle.execute(
        ToolCall(name="file_read", args={"path": "/tmp/a"}), _harness_context()
    ))
    assert result.success
    assert result.output == "strict body"
    assert result.observation.status is ObservationStatus.VALID
    text = build_tool_result_text(result, "file_read", _harness_context())
    assert text.startswith("[工具观察] status=valid")
    assert text.endswith("strict body")


def test_invalid_text_never_contains_original_output():
    result = _invalid_tool_result()
    text = build_tool_result_text(result, "file_read", _harness_context())
    assert "不能作为事实依据" in text
    assert "secret raw body" not in text
    assert "[工具观察]" not in text
```

复用 Registry 测试中的假 registry；`_harness_context()` 使用现有 `RunContext` 夹具，确保 `push_undo` / metadata 可用。

- [ ] **Step 2: 跑测试确认红**

Run: `cd backend && python -m pytest tests/test_tool_observation_registry.py -q`
Expected: FAIL，Saddle 尚未透传 observation。

- [ ] **Step 3: 最小实现**

Saddle 映射增加：

```python
observation=getattr(raw_result, "observation", None),
```

Stirrups 成功分支改为：

```python
observation = getattr(result, "observation", None)
if observation is not None:
    return serialize_observation_header(observation) + result.output
return result.output
```

失败分支保持现有部分输出诊断；invalid 的 output 已由 Registry 清空，因此不会泄露。失败 degraded 结果可在错误反馈前加观察头，但不改变 error 文本与优先级。并行与串行 Driver 都继续只调用 `build_tool_result_text`，不修改 Driver。

- [ ] **Step 4: 跑映射与错误测试确认绿**

Run: `cd backend && python -m pytest tests/test_tool_observation_registry.py tests/test_tool_error_typing_budget.py tests/test_ai_loop_core_regression.py -q`
Expected: 全部 PASS；跨端审计枚举与消息配对不变。

- [ ] **Step 5: 证伪正文隔离**

临时在 invalid 测试夹具中填入原始 output 并绕过 Registry，直接调用 `build_tool_result_text`，确认失败分支会泄露，Expected: FAIL。随后不改变生产失败诊断语义，而在 Registry invalid 转换测试中继续断言原始 output 清空；恢复夹具并重跑。这个证伪证明隔离依赖 Registry 边界，不误删 legacy 失败诊断。

---

### Task 5: 三只试点工具提示与 strict 激活

**Files:**
- Modify: `backend/agent/tools/builtin/ops_file.py`
- Test: `backend/tests/test_tool_observation_pilot_tools.py`

**Interfaces:**
- Consumes: `BaseTool.observation_hint`、Registry strict 常量。
- Produces: 三只工具请求范围完整性，不改变工具正文和既有返回分支。

- [ ] **Step 1: 写试点语义测试并确认红**

每只工具至少覆盖 normal / empty-or-no-match / truncated-or-capped / failed 四条路径：

```python
def test_file_read_truncated_result_is_partial():
    tool = FileReadTool()
    result = ToolResult(True, "1: a\n... [输出已截断，省略中间 10 字符]\n9: z")
    hint = tool.observation_hint(SimpleNamespace(ssh_session_id="s"), {"path": "/tmp/a"}, result)
    assert hint["complete"] is False
    assert hint["warnings"]


def test_file_read_empty_result_is_complete_for_request():
    tool = FileReadTool()
    result = ToolResult(True, "无内容（offset 99 超出文件末尾或文件为空）")
    assert tool.observation_hint(SimpleNamespace(ssh_session_id="s"), {"path": "/tmp/a"}, result)["complete"] is True


def test_remote_grep_without_provable_coverage_is_degraded():
    tool = FileGrepTool()
    result = ToolResult(True, "未匹配到内容：error")
    hint = tool.observation_hint(SimpleNamespace(ssh_session_id="s"), {"path": "/srv", "pattern": "error"}, result)
    assert hint["complete"] is None


def test_grep_cap_and_unreadable_coverage_are_partial():
    tool = FileGrepTool()
    result = ToolResult(True, "a:1:x\n（已达 max_lines=1 上限，可能仍有匹配未列出）")
    assert tool.observation_hint(SimpleNamespace(ssh_session_id=""), {"path": "/tmp"}, result)["complete"] is False
```

加入 `file_glob` 截断与无匹配测试，以及经 Registry 的 strict valid / partial / degraded / invalid 集成断言。工具失败测试确认状态 degraded 且原 error 保留。

- [ ] **Step 2: 跑测试确认红**

Run: `cd backend && python -m pytest tests/test_tool_observation_pilot_tools.py -q`
Expected: FAIL，试点工具尚未覆盖 `observation_hint`，返回默认 `{}`。

- [ ] **Step 3: 实现三个小钩子**

在 `ops_file.py` 顶部复用 `TRUNCATION_MARKERS`，不复制匹配逻辑到三个工具。每只工具增加一个方法，先处理失败：

```python
def observation_hint(self, context, args, result):
    if not result.success:
        return {}
```

成功结果按以下表返回 `complete` 与 warnings：

| 工具 | complete=true | complete=false | complete=null |
| --- | --- | --- | --- |
| `file_read` | 正常片段、空文件、offset 超范围 | 正文含已知截断标记 | 不用于成功路径 |
| `file_glob` | 正常列表、无匹配 | 正文含已知截断标记 | 不用于成功路径 |
| `file_grep` | 本地无匹配且无覆盖不足；本地有匹配且无截断/不可读提示 | 含 `max_lines` 上限或“文件读不了”覆盖提示，或已知截断 | 远程成功但无法证明逐文件覆盖 |

warnings 使用固定短句：`"结果已截断"`、`"达到 max_lines 上限，可能仍有匹配"`、`"存在不可读文件，搜索覆盖不完整"`、`"远程搜索无法证明逐文件覆盖"`。不要复制原始匹配行、路径列表或错误正文到 warnings。

- [ ] **Step 4: 跑试点与 Registry 测试确认绿**

Run: `cd backend && python -m pytest tests/test_tool_observation_pilot_tools.py tests/test_tool_observation_registry.py tests/test_ops_file_job_img.py -q`
Expected: 全部 PASS，工具既有正文不变。

- [ ] **Step 5: 证伪完整性冲突**

临时让 `FileGlobTool.observation_hint` 对截断结果返回 `complete=True`，跑截断测试，Expected: FAIL；立即恢复并重跑全部 Task 5 测试，Expected: PASS。

---

### Task 6: 故障注入、兼容回归与完整门禁

**Files:**
- Test: 上述三个新测试文件与现有相关回归测试。
- Modify: 仅允许修复前五个任务暴露的具体问题；不新增功能。

**Interfaces:**
- Consumes: 完整观察链。
- Produces: 可复核的红/绿证据与全量门禁结果。

- [ ] **Step 1: 做一次端到端非法结果故障注入**

选择 Registry 测试中的 strict 假工具，临时返回 `ToolResult(True, "raw-secret", pending_delivery={"x": 1})`。运行对应 invalid 测试，Expected: FAIL 于“应拒绝”的断言；记录原始断言确实捕获了缺陷。恢复假工具，重跑：

Run: `cd backend && python -m pytest tests/test_tool_observation_contract.py tests/test_tool_observation_registry.py tests/test_tool_observation_pilot_tools.py tests/test_tool_error_typing_budget.py tests/test_tool_timeout_cleanup.py tests/test_ai_loop_core_regression.py -q`
Expected: 全部 PASS。

- [ ] **Step 2: 检查静态约束**

Run: `node scripts/check-file-size.mjs && node scripts/check-any.mjs`
Expected: 退出码 0；新增 `observation.py` 不超过 800 行，全仓库 any 不增加。

Run: `cd backend && python -m ruff check agent tests --select F,E9`
Expected: 0 错误。

- [ ] **Step 3: 跑后端完整测试与 lint**

Run: `npm run test:backend`
Expected: 全部 PASS。

Run: `npm run lint:backend`
Expected: 全部 PASS。

- [ ] **Step 4: 跑前端类型与测试，证明跨端无回归**

Run: `npm run typecheck`
Expected: 全部 PASS。

Run: `npm test`
Expected: 全部 PASS。

- [ ] **Step 5: 检查变更边界**

Run: `git status --short && git diff --stat && git diff -- backend/agent/harness/contracts.py backend/agent/tools/__init__.py backend/agent/harness/saddle.py backend/agent/harness/stirrups.py backend/agent/tools/builtin/ops_file.py backend/agent/observation.py backend/tests/test_tool_observation_contract.py backend/tests/test_tool_observation_registry.py backend/tests/test_tool_observation_pilot_tools.py backend/tests/test_tool_error_typing_budget.py`
Expected: 只包含本计划目标；发现用户 WIP 被误改时停止并报告，不用 checkout 掩盖。

- [ ] **Step 6: 运行唯一完整门禁**

Run: `bash scripts/ci-check.sh`
Expected: 全绿。运行期间不编辑文件、不启动其他重型套件、不改树。

- [ ] **Step 7: 按验收标准逐项核对**

- [ ] strict 缺信封 / 哈希冲突 / 截断冲突 / pending delivery 均拒绝原始正文。
- [ ] invalid 为 `INVALID_RESULT`、`retryable=false`，不重试、不计参数错误熔断。
- [ ] legacy 成功与失败语义不变，只增加 degraded 观察头。
- [ ] 串行与并行工具消息配对、审批、visible-terminal、Undo、超时清理回归绿。
- [ ] UI、SSE payload、审计结论枚举与数据库无变更。
- [ ] 未提交、未推送；所有用户 WIP 保持原样。

## Self-Review Evidence

计划编写时已对照 spec §4–§14：类型与纯函数对应 Task 1；错误语义对应 Task 2；Registry 双轨对应 Task 3；Saddle / Stirrups 对应 Task 4；三工具语义对应 Task 5；故障注入与门禁对应 Task 6。接口签名在任务间保持一致；没有以占位步骤替代实现；当前脏树通过“读最新内容 + 显式文件 diff + 不提交”保护。
