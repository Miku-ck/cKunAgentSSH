"""定时任务系统提示词的 S7-02 守卫段不许被上下文段的失败一起吞掉。

原先整个块包在 `try: … except Exception: pass` 里，而块内既有「可选上下文」
（prompts 导入 + 当前时间 + SSH_MODE_PROMPT），也有**守卫本身**
（没有 target_host 就写明「本次没有可用的 SSH 会话，别调远程工具」）。
于是时钟或导入一抛，连那句守卫都消失，模型恢复成逐个试远程工具、全数撞上
run_headless 的 fail-closed，最后交回一份看不出为什么失败的报告。

修形：守卫只读 sched，搬到 try 之外；上下文段失败可以降级，但必须留日志。
"""
import logging

import pytest

import agent.schedule_engine as se
import prompts


def test_guard_survives_context_section_failure(monkeypatch, caplog):
    def _boom():
        raise RuntimeError("clock unavailable (simulated)")

    monkeypatch.setattr(prompts, "_current_time_str", _boom)
    with caplog.at_level(logging.WARNING, logger="agent.schedule_engine"):
        out = se._build_schedule_system_prompt({"target_host": "", "name": "夜间巡检"})
    assert "未指定目标主机" in out, "守卫段不许跟着上下文段一起被吞"
    assert "不要调用任何远程工具" in out
    assert any("上下文段构建失败" in r.getMessage() for r in caplog.records), \
        "降级可以，但不能无痕"
    # 尾部那段通用指令与守卫都不该依赖 prompts，仍要在
    assert "定时任务" in out


def test_guard_absent_when_target_host_present(monkeypatch):
    monkeypatch.setattr(prompts, "_current_time_str", lambda: "2026-10-09 03:00 UTC")
    out = se._build_schedule_system_prompt({"target_host": "conn-7", "name": "x"})
    assert "未指定目标主机" not in out
    assert prompts.SSH_MODE_PROMPT in out
    assert "[系统时间] 当前时间：2026-10-09 03:00 UTC" in out


def test_happy_path_keeps_every_section(monkeypatch):
    monkeypatch.setattr(prompts, "_current_time_str", lambda: "2026-10-09 03:00 UTC")
    out = se._build_schedule_system_prompt({"target_host": "", "name": "x"})
    assert "[执行环境]" in out and "[系统时间]" in out
    assert prompts.SSH_MODE_PROMPT in out
    assert out.index("[执行环境]") < out.index("[系统时间]"), \
        "守卫段在最前：它不依赖任何东西，也不该被后面的降级影响"


@pytest.mark.parametrize("target_host", [None, "", "   "])
def test_blank_target_host_all_get_the_guard(target_host):
    out = se._build_schedule_system_prompt({"target_host": target_host, "name": "x"})
    assert "未指定目标主机" in out
