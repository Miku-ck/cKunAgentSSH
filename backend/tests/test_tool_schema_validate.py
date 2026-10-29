"""工具调用运行时 schema 校验 + few-shot 错误回返（CatPaw 优化项）。

`registry.execute` 在调用 `tool.execute` 前先用 `parameters` 对 args 做一次
JSON Schema 子集校验，失败时返回带有「正确用法示例」的 ToolResult——模型看到就
知道怎么改，且校验结果不进上下文（省 token）。
"""
import asyncio

from agent.tools import ToolContext, create_registry, validate_args


def _run(coro):
    return asyncio.run(coro)


# ── validate_args 单元层 ───────────────────────────────────────────────────


class TestValidateArgsUnit:
    def test_empty_parameters_is_permissive(self):
        """parameters 为空/None → 不做校验（老工具向后兼容）。"""
        assert validate_args(None, {"x": 1}) == []
        assert validate_args({}, {"x": 1}) == []

    def test_missing_required(self):
        errs = validate_args(
            {"type": "object", "properties": {"cmd": {"type": "string"}},
             "required": ["cmd"]},
            {},
        )
        assert errs and "缺少必填字段" in errs[0]

    def test_extra_field_rejected_only_when_additional_properties_false(self):
        params = {
            "type": "object",
            "properties": {"cmd": {"type": "string"}},
            "required": ["cmd"],
            "additionalProperties": False,
        }
        errs = validate_args(params, {"cmd": "ls", "foo": 1}, "shell_exec")
        assert any("非法字段" in e and "foo" in e for e in errs)

    def test_extra_field_absent_key_is_permissive(self):
        """未声明 additionalProperties → 兜底按 true 处理，放行额外字段。"""
        params = {
            "type": "object",
            "properties": {"cmd": {"type": "string"}},
            "required": ["cmd"],
        }
        assert validate_args(params, {"cmd": "ls", "foo": 1}) == []

    def test_type_mismatch(self):
        errs = validate_args(
            {"type": "object", "properties": {"port": {"type": "integer"}},
             "required": ["port"]},
            {"port": "80"},
        )
        assert errs and any(("整数" in e or "integer" in e) for e in errs), errs

    def test_enum_violation(self):
        errs = validate_args(
            {"type": "object",
             "properties": {"action": {"type": "string", "enum": ["a", "b"]}},
             "required": ["action"]},
            {"action": "c"},
        )
        assert errs and any("不在允许范围" in e for e in errs), errs

    def test_number_clamp_minmax_reported(self):
        errs = validate_args(
            {"type": "object",
             "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 5}},
             "required": ["n"]},
            {"n": 99},
        )
        assert errs and any("≤ 5" in e for e in errs), errs

    def test_valid_call_is_clean(self):
        assert validate_args(
            {"type": "object",
             "properties": {"cmd": {"type": "string"}, "n": {"type": "integer"}},
             "required": ["cmd"],
             "additionalProperties": False},
            {"cmd": "ls", "n": 3},
        ) == []


# ── registry.execute 集成层 ─────────────────────────────────────────────────


class TestRegistryExecuteValidation:
    def test_rejects_missing_required_with_example(self):
        reg = create_registry()
        res = _run(reg.execute("shell_exec", {}, ToolContext()))
        assert res.success is False
        assert "shell_exec" in res.error
        assert "示例" in res.error and '"command"' in res.error

    def test_rejects_extra_field(self):
        reg = create_registry()
        res = _run(reg.execute(
            "web_fetch", {"url": "https://x", "bogus": 1}, ToolContext()))
        assert res.success is False and "非法字段" in res.error

    def test_rejects_enum_violation(self):
        reg = create_registry()
        res = _run(reg.execute(
            "todo_write", {"action": "bogus"}, ToolContext()))
        assert res.success is False and "不在允许范围" in res.error

    def test_valid_call_passes_validation_to_execute(self):
        """合法调用校验层放过，落到 tool.execute 的真实语义层。"""
        reg = create_registry()
        # file_read 会尝试读真实文件 → 返回失败但原因是文件不存在，而非校验拒绝
        res = _run(reg.execute(
            "file_read", {"path": "/no/such/path/xyz"}, ToolContext()))
        assert "参数校验失败" not in res.error

    def test_shell_exec_decoy_shape_still_rejected_via_schema(self):
        """decoy 走 registry.execute 路径时由 schema 校验拦截（安全冗余）。"""
        reg = create_registry()
        res = _run(reg.execute(
            "shell_exec", {"command": "echo DECOY", "cmd": "rm -rf /"},
            ToolContext()))
        assert res.success is False
