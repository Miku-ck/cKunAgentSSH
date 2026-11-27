#!/usr/bin/env python3
"""智能体死循环三层治理参考实现（仅标准库）。

事前预防：最大步数硬上限 / 每步状态推进 / 工具参数校验
事中检测：a 步数检测 b 重复检测 c 语义相似度检测 d 工作记忆停滞检测
事后干预：一级注入反思提示 -> 二级禁用工具并引导换路 -> 三级终止并转人工

依赖同目录的 agent_trace_demo.py（Tracer / LLMClient / ToolRegistry / parse_tool_call）。
运行：python loop_guard_demo.py
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agent_trace_demo import LLMClient, ToolRegistry, Tracer, parse_tool_call


# ============ 事前：策略与状态 ============

@dataclass
class LoopPolicy:
    max_steps: int = 8                   # 事前边界：单任务最大步数，硬熔断
    repeat_tolerance: int = 2            # 事中 b：连续 N 轮动作完全一致 -> 循环
    similarity_threshold: float = 0.90   # 事中 c：语义相似度阈值
    similarity_window: int = 3           #         窗口内两两超阈 -> 无进展
    stale_steps: int = 3                 # 事中 d：工作记忆连续 N 步无更新 -> 卡住
    reflect_prompt: str = (
        "检测到你在重复同样的操作且没有新进展。请停下来复盘：目标是否已满足？"
        "是否需要换一种方法？若信息不足，请直接说明并结束。"
    )


def _hash_state(state: dict[str, Any]) -> str:
    """只对「目标 + 工作记忆」做进度快照哈希。

    错误计数等元信息不计入：否则模型只要反复触发工具异常就能改变哈希、
    重置停滞计时，靠刷错误把死循环伪装成「有进展」，治理就被绕过了。
    """
    payload = {"goal": state.get("goal"), "memory": state.get("memory", [])}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


# ============ 事中 c：无语义依赖的相似度（字符 bigram Jaccard）============

def _normalize(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text.lower())


def _bigrams(text: str) -> set[str]:
    t = _normalize(text)
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) > 1 else {t}


def semantic_similarity(a: str, b: str) -> float:
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


# ============ 事中：检测器 + 事后：分级干预 ============

@dataclass
class StepRecord:
    index: int
    thought: str                 # 本轮模型输出（思考 + 决策）
    tool: str | None             # 本轮发起的工具调用
    args: dict[str, Any]
    state_hash: str              # 本轮后的工作记忆快照


class LoopGuard:
    """四种检测顺序执行；命中后按 strikes 递进干预：warn -> escalate -> terminate。"""

    def __init__(self, policy: LoopPolicy, tracer: Tracer):
        self.policy = policy
        self.tracer = tracer
        self.history: list[StepRecord] = []
        self.strikes = 0
        self.terminated: str | None = None
        self.events: list[str] = []

    def observe(self, thought: str, tool: str | None, args: dict[str, Any],
                state: dict[str, Any]) -> str:
        """每步结束后调用，返回本轮干预动作：none / warn / escalate / terminate。"""
        rec = StepRecord(len(self.history), thought, tool, args, _hash_state(state))
        reason = self._detect(rec)
        self.history.append(rec)
        if reason is None:
            return "none"
        self.strikes += 1
        action = ("warn" if self.strikes == 1
                  else "escalate" if self.strikes == 2
                  else "terminate")
        self.events.append(f"{action}:{reason}")
        if action == "terminate":
            self.terminated = reason
        return action

    def _detect(self, rec: StepRecord) -> str | None:
        p = self.policy
        # b. 重复检测：连续 N 轮「思考 + 工具 + 参数」完全一致
        if len(self.history) >= p.repeat_tolerance:
            tail = self.history[-p.repeat_tolerance:]
            if all(_normalize(t.thought) == _normalize(rec.thought)
                   and t.tool == rec.tool and t.args == rec.args for t in tail):
                return "repeat_action"
        # c. 语义检测：窗口内任意两轮输出相似度均超阈值 -> 无进展
        window = self.history[-p.similarity_window:] + [rec]
        if len(window) >= 2:
            sims = [semantic_similarity(a.thought, b.thought)
                    for i, a in enumerate(window) for b in window[i + 1:]]
            if sims and min(sims) > p.similarity_threshold:
                return "no_progress_semantic"
        # d. 状态检测：最近 N 步工作记忆哈希不变 -> 卡住
        if len(self.history) >= p.stale_steps:
            tail = self.history[-p.stale_steps:] + [rec]
            if len({t.state_hash for t in tail}) == 1:
                return "stale_state"
        return None


# ============ 受治理的 Agent 运行器 ============

def run_guarded_agent(question: str, llm: LLMClient, tools: ToolRegistry,
                      tracer: Tracer, policy: LoopPolicy,
                      tool_names: set[str]) -> dict[str, Any]:
    guard = LoopGuard(policy, tracer)
    state: dict[str, Any] = {"goal": question, "memory": [], "errors": []}
    injections: list[str] = []
    last_result: str | None = None
    answer: str | None = None
    used_tool: str | None = None

    with tracer.start_span("agent.run", {
        "agent.question": question,
        "loop.max_steps": policy.max_steps,
    }) as run_span:
        for step in range(policy.max_steps + 1):
            if step >= policy.max_steps:                       # a. 步数硬熔断
                guard.terminated = "step_limit"
                guard.events.append("terminate:step_limit")
                break

            with tracer.start_span("agent.step", {
                "agent.step.index": step, "loop.strikes": guard.strikes,
            }) as step_span:
                messages = [{"role": "system", "content": "你是助手"},
                            {"role": "user", "content": question}]
                if last_result is not None:
                    messages.append({"role": "user", "content": f"工具结果: {last_result}"})
                messages += [{"role": "user", "content": t} for t in injections]

                thought = llm.chat(messages)["text"]
                calls = parse_tool_call(thought)

                if not calls:                                   # 无工具调用 -> 最终答复
                    answer = thought
                    step_span.attrs["agent.final"] = True
                    break

                call = calls[0]
                used_tool = call["name"]
                try:
                    if call["name"] not in tool_names:
                        raise KeyError(f"工具已被治理禁用: {call['name']}")
                    result = tools.call(call["name"], call["args"])
                    if str(result) not in state["memory"]:         # 幂等记忆：仅新信息入 state
                        state["memory"].append(str(result))
                    last_result = str(result)
                except Exception as exc:                        # 参数/工具异常 -> 记入工作记忆
                    state["errors"].append(type(exc).__name__)

                action = guard.observe(thought, call["name"], call["args"], state)
                step_span.attrs["loop.action"] = action
                step_span.attrs["loop.detector"] = (
                    guard.events[-1].split(":", 1)[1] if action != "none" else None)

                if action == "terminate":
                    break
                if action == "warn":
                    injections.append(policy.reflect_prompt)
                elif action == "escalate":
                    tool_names.discard(used_tool)               # 二级：禁用重复工具
                    injections.append(
                        f"系统提示：工具 {used_tool} 已被禁用，请换用其他方法，"
                        f"或基于已有信息直接给出结论。")

        run_span.attrs["agent.loop_detected"] = guard.terminated is not None
        run_span.attrs["agent.termination_reason"] = guard.terminated
        run_span.attrs["agent.needs_human"] = guard.terminated is not None

    return {
        "answer": answer,
        "terminated_reason": guard.terminated,
        "needs_human": guard.terminated is not None,
        "steps": len(guard.history),
        "events": guard.events,
    }


# ============ 场景 ============

def looping_transport(messages: list[dict[str, str]]) -> str:
    """恶意模型：无视一切干预，永远重复同一决策。"""
    time.sleep(0.03)
    return "我需要先查一下：CALL tool.search(query='天气')"


def normal_transport(messages: list[dict[str, str]]) -> str:
    """正常模型：先查工具，拿到结果后作答。"""
    time.sleep(0.03)
    last = messages[-1]["content"]
    if last.startswith("工具结果"):
        return "根据已有信息，最终答案是：今天晴，25℃。"
    return "我需要先查一下：CALL tool.search(query='天气')"


def main() -> int:
    out_dir = Path(__file__).resolve().parent
    tracer = Tracer("loop-guard-demo", sink_path=out_dir / "loop_spans.jsonl")

    def fresh_tools(tracer: Tracer) -> ToolRegistry:
        tools = ToolRegistry(tracer)

        def search(query: str) -> str:          # 工具自身即参数校验：缺参/空值直接抛错
            if not query:
                raise ValueError("query 不能为空")
            return f"搜索结果[{query}]: 今天晴，25℃，东风3级。"

        tools.register("search", search)
        return tools

    print("== Case A：恶意循环模型，验证三级干预递进 ==")
    a = run_guarded_agent("今天天气怎么样？",
                          LLMClient(model="qwen3.8-27b", tracer=tracer,
                                    transport=looping_transport),
                          fresh_tools(tracer), tracer, LoopPolicy(), {"search"})
    print(f"  结果: {json.dumps(a, ensure_ascii=False)}")
    assert a["terminated_reason"] == "repeat_action"
    assert a["needs_human"] is True
    assert [e.split(":", 1)[0] for e in a["events"]] == ["warn", "escalate", "terminate"]
    assert a["steps"] < 8, "必须在步数上限内被干预终止"
    print("  [OK] warn -> escalate -> terminate 递进全部触发\n")
    tracer.print_tree()
    tracer.summarize()

    print("\n== Case B：正常模型，验证不误伤 ==")
    tracer.spans.clear()
    b = run_guarded_agent("今天天气怎么样？",
                          LLMClient(model="qwen3.8-27b", tracer=tracer,
                                    transport=normal_transport),
                          fresh_tools(tracer), tracer, LoopPolicy(), {"search"})
    print(f"  结果: {json.dumps(b, ensure_ascii=False)}")
    assert b["terminated_reason"] is None and b["answer"] and not b["events"]
    print("  [OK] 正常链路零误报\n")

    print("== Case C：步数硬熔断独立生效（max_steps=2）==")
    tracer.spans.clear()
    c = run_guarded_agent("今天天气怎么样？",
                          LLMClient(model="qwen3.8-27b", tracer=tracer,
                                    transport=looping_transport),
                          fresh_tools(tracer), tracer, LoopPolicy(max_steps=2), {"search"})
    print(f"  结果: {json.dumps(c, ensure_ascii=False)}")
    assert c["terminated_reason"] == "step_limit"
    print("  [OK] 硬边界优先于干预链生效\n")

    print("== Case D：仅启用状态停滞检测（屏蔽 b/c），验证 d 独立生效 ==")
    tracer.spans.clear()
    d = run_guarded_agent(
        "今天天气怎么样？",
        LLMClient(model="qwen3.8-27b", tracer=tracer, transport=looping_transport),
        fresh_tools(tracer), tracer,
        LoopPolicy(max_steps=8, repeat_tolerance=99, similarity_threshold=1.1,
                   stale_steps=2),
        {"search"})
    print(f"  结果: {json.dumps(d, ensure_ascii=False)}")
    assert d["terminated_reason"] == "stale_state"
    assert [e.split(":", 1)[0] for e in d["events"]] == ["warn", "escalate", "terminate"]
    print("  [OK] stale_state 检测器独立命中并走完三级干预\n")

    print(f"== 产物 ==\n  {out_dir / 'loop_spans.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
