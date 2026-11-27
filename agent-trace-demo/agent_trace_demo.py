#!/usr/bin/env python3
"""Agent 执行过程调试 / 全链路追踪 最小参考实现（仅标准库，无第三方依赖）。

对应两个问题各给一段可运行代码：

1) 怎么调试执行过程
   - Span 结构化埋点：每次调用落 JSONL，出错带异常栈，事后可逐层复盘。
   - Replay（回放）：真实调用时录制输入输出，测试环境用录制结果替代模型，
     消除非确定性，失败可稳定复现、可单步重放。
   - 分层验证：LLM 调用 / 工具调用各自成 span，错误能定位到具体层。

2) 怎么做全链路追踪与可观测性
   - Trace/Span 树：trace_id 贯穿一次请求，span 通过 parent_span_id 串父子。
   - 跨进程不断链：W3C traceparent 写进 MQ 消息属性，消费端还原后续接。
   - 指标汇总：按 span 名聚合次数/耗时/token，作为监控告警的数据源。

运行：python agent_trace_demo.py
产物：spans.jsonl（全量事件）、llm_recordings.jsonl（录制，供回放）
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator


# ============ 1. Trace / Span 数据模型 ============

def _hex(n: int) -> str:
    return uuid.uuid4().hex[:n]


@dataclass
class SpanContext:
    trace_id: str                 # 全局唯一，贯穿一次请求（含跨进程）
    span_id: str                  # 当前 span
    parent_span_id: str | None = None    # 父 span，构成树


@dataclass
class Span:
    name: str
    ctx: SpanContext
    attrs: dict[str, Any] = field(default_factory=dict)
    status: str = "OK"            # OK / ERROR
    error: dict[str, str] | None = None
    start_ts: float = field(default_factory=time.time)
    end_ts: float | None = None

    @property
    def duration_ms(self) -> float:
        end = self.end_ts or time.time()
        return round((end - self.start_ts) * 1000, 2)


_current_ctx: contextvars.ContextVar[SpanContext | None] = contextvars.ContextVar(
    "span_ctx", default=None
)


class Tracer:
    """极简 tracer：结构化事件写 JSONL，同时在本进程内维护 span 列表用于打印树。"""

    def __init__(self, service_name: str, sink_path: Path | None = None):
        self.service_name = service_name
        self.sink_path = sink_path
        self.spans: list[Span] = []

    @contextmanager
    def start_span(self, name: str, attrs: dict[str, Any] | None = None) -> Iterator[Span]:
        parent = _current_ctx.get()
        if parent is None:
            ctx = SpanContext(trace_id=_hex(32), span_id=_hex(16))       # 根 span，开新 trace
        else:
            ctx = SpanContext(trace_id=parent.trace_id, span_id=_hex(16),
                              parent_span_id=parent.span_id)            # 子 span，继承 trace
        span = Span(name=name, ctx=ctx, attrs=dict(attrs or {}))
        token = _current_ctx.set(ctx)
        self._emit({"event": "span_start", "service": self.service_name,
                    **asdict(ctx), "name": name})
        try:
            yield span
        except Exception as exc:
            span.status = "ERROR"
            span.error = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            span.end_ts = time.time()
            self.spans.append(span)
            self._emit({
                "event": "span_end",
                "service": self.service_name,
                **asdict(ctx),
                "name": name,
                "attrs": span.attrs,
                "status": span.status,
                "error": span.error,
                "duration_ms": span.duration_ms,
            })
            _current_ctx.reset(token)

    def _emit(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        if self.sink_path:
            with self.sink_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def print_tree(self) -> None:
        children: dict[str | None, list[Span]] = {}
        for s in self.spans:
            children.setdefault(s.ctx.parent_span_id, []).append(s)

        def walk(parent_id: str | None, depth: int) -> None:
            for s in children.get(parent_id, []):
                flag = "OK  " if s.status == "OK" else "FAIL"
                err = f"  <- {s.error['type']}: {s.error['message']}" if s.error else ""
                print(f"{'  ' * depth}[{flag}] {s.name:<20} {s.duration_ms:>7.1f}ms "
                      f"span={s.ctx.span_id[:8]} trace={s.ctx.trace_id[:8]}{err}")
                walk(s.ctx.span_id, depth + 1)

        walk(None, 0)

    def summarize(self) -> None:
        """按 span 名聚合，等价于监控系统里的 RED + token 指标。"""
        agg: dict[str, dict[str, Any]] = {}
        for s in self.spans:
            row = agg.setdefault(s.name, {"count": 0, "ms": 0.0, "in": 0, "out": 0, "err": 0})
            row["count"] += 1
            row["ms"] += s.duration_ms
            row["in"] += int(s.attrs.get("gen_ai.usage.input_tokens") or 0)
            row["out"] += int(s.attrs.get("gen_ai.usage.output_tokens") or 0)
            row["err"] += 1 if s.status == "ERROR" else 0
        print("\n== 指标汇总（按 span 名聚合）==")
        for name, r in agg.items():
            print(f"  {name:<18} 次数={r['count']}  耗时={r['ms']:.1f}ms  "
                  f"in_tokens={r['in']}  out_tokens={r['out']}  错误={r['err']}")


# ============ 2. LLM 调用 Wrapper（录制 / 回放）============

class LLMRecorder:
    """把每次 LLM 调用的响应落盘，Replay 模式下按请求哈希取回，实现确定性复现。"""

    def __init__(self, path: Path):
        self.path = path
        self.records: dict[str, dict[str, Any]] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self.records[rec["key"]] = rec

    @staticmethod
    def make_key(messages: list[dict[str, str]], model: str) -> str:
        raw = json.dumps({"model": model, "messages": messages},
                         ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def save(self, key: str, payload: dict[str, Any]) -> None:
        self.records[key] = payload
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"key": key, **payload}, ensure_ascii=False) + "\n")


class LLMClient:
    """统一入口：真实调用走 transport，录制/回放由 recorder 控制。"""

    def __init__(self, model: str, tracer: Tracer, recorder: LLMRecorder | None = None,
                 replay: bool = False, transport: Callable[[list[dict[str, str]]], str] | None = None):
        self.model = model
        self.tracer = tracer
        self.recorder = recorder
        self.replay = replay
        self.transport = transport or self._default_transport

    def _default_transport(self, messages: list[dict[str, str]]) -> str:
        """模拟真实模型：有耗时、有非确定性。接入时换成真实 HTTP/gRPC 调用即可。"""
        time.sleep(0.05)
        last = messages[-1]["content"]
        if last.startswith("工具结果"):
            return "根据已有信息，最终答案是：今天晴，25℃。"
        return "我需要先查一下：CALL tool.search(query='天气')"

    def chat(self, messages: list[dict[str, str]], tools: list[str] | None = None) -> dict[str, Any]:
        key = LLMRecorder.make_key(messages, self.model)
        with self.tracer.start_span("llm.chat", {
            "gen_ai.model.name": self.model,
            "gen_ai.usage.input_tokens": sum(len(m["content"]) for m in messages),
            "gen_ai.request.tools": tools or [],
        }) as span:
            if self.replay:
                if self.recorder is None or key not in self.recorder.records:
                    raise RuntimeError(f"[replay] 缺少录制记录 key={key}")
                text = self.recorder.records[key]["response"]
                span.attrs["gen_ai.replay"] = True
            else:
                text = self.transport(messages)   # 真实调用抛异常会被 span 记为 ERROR 并上抛
                if self.recorder:
                    self.recorder.save(key, {"model": self.model, "response": text})
            span.attrs["gen_ai.usage.output_tokens"] = len(text)
            return {"text": text}


# ============ 3. 工具调用 Wrapper ============

class ToolRegistry:
    def __init__(self, tracer: Tracer):
        self.tracer = tracer
        self._tools: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, fn: Callable[..., Any]) -> None:
        self._tools[name] = fn

    def call(self, name: str, args: dict[str, Any]) -> Any:
        fn = self._tools.get(name)
        with self.tracer.start_span("tool.call", {
            "tool.name": name,
            "tool.args": args,
            "tool.registered": fn is not None,
        }) as span:
            if fn is None:
                raise KeyError(f"未注册的工具: {name}")
            result = fn(**args)
            span.attrs["tool.result_chars"] = len(str(result))
            return result


_TOOL_RE = re.compile(r"CALL\s+(tool\.\w+)\(([^)]*)\)")


def parse_tool_call(text: str) -> list[dict[str, Any]]:
    """示例解析：实际项目建议用 function calling 的结构化参数，别正则。"""
    calls = []
    for full_name, argstr in _TOOL_RE.findall(text):
        args = dict(re.findall(r"(\w+)='([^']*)'", argstr))
        calls.append({"name": full_name.split(".", 1)[1], "args": args})
    return calls


# ============ 4. 跨进程 / MQ 上下文传递（W3C traceparent）============

def inject_traceparent(ctx: SpanContext) -> str:
    """放入 MQ 消息属性（RabbitMQ properties.headers）或 HTTP header。"""
    return f"00-{ctx.trace_id}-{ctx.span_id}-01"


def extract_traceparent(traceparent: str) -> SpanContext:
    _version, trace_id, span_id, _flags = traceparent.split("-")
    return SpanContext(trace_id=trace_id, span_id=span_id)


# ============ 5. 一个两步骤 Agent 的完整示例 ============

def run_agent(question: str, llm: LLMClient, tools: ToolRegistry, tracer: Tracer) -> str:
    with tracer.start_span("agent.run", {"agent.question": question}):
        plan = llm.chat([{"role": "user", "content": question}])
        evidence = ""
        for tc in parse_tool_call(plan["text"]):
            evidence = tools.call(tc["name"], tc["args"])
        final = llm.chat([
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": question},
            {"role": "user", "content": f"工具结果: {evidence}"},
        ])
        return final["text"]


def demo_mq_propagation(tracer: Tracer) -> None:
    """生产者写消息属性 -> 消费者还原上下文，验证跨进程 trace_id 不断链。"""
    tracer.spans.clear()
    headers: dict[str, str] = {}

    with tracer.start_span("mq.producer.enqueue", {"mq.queue": "qa.result"}):
        headers["traceparent"] = inject_traceparent(_current_ctx.get())
        print(f"  生产者写入消息属性 traceparent={headers['traceparent']}")

    parent = extract_traceparent(headers["traceparent"])
    token = _current_ctx.set(parent)
    try:
        with tracer.start_span("mq.consumer.dequeue", {"mq.queue": "qa.result"}):
            print(f"  消费者恢复上下文 trace_id={parent.trace_id[:8]}...")
    finally:
        _current_ctx.reset(token)

    assert len({s.ctx.trace_id for s in tracer.spans}) == 1, "跨进程后 trace_id 必须一致"
    print("  [OK] 跨进程 trace_id 贯通校验通过\n")


def main() -> int:
    out_dir = Path(__file__).resolve().parent
    jsonl = out_dir / "spans.jsonl"
    record_path = out_dir / "llm_recordings.jsonl"
    jsonl.unlink(missing_ok=True)
    record_path.unlink(missing_ok=True)

    tracer = Tracer("agent-demo", sink_path=jsonl)
    recorder = LLMRecorder(record_path)

    tools = ToolRegistry(tracer)
    tools.register("search", lambda query: f"搜索结果[{query}]: 今天晴，25℃，东风3级。")

    llm = LLMClient(model="qwen3.8-27b", tracer=tracer, recorder=recorder)

    print("== 第一次运行（真实调用，顺带录制）==")
    answer = run_agent("今天天气怎么样？", llm, tools, tracer)
    print(f"最终答案: {answer}")
    assert len({s.ctx.trace_id for s in tracer.spans}) == 1, "同一请求的所有 span 必须共享 trace_id"
    print("[OK] 单 trace 贯通校验通过\n")
    tracer.print_tree()

    print("\n== 第二次运行（Replay 模式，验证可确定性复现）==")
    tracer.spans.clear()
    replay_llm = LLMClient(model="qwen3.8-27b", tracer=tracer, recorder=recorder, replay=True)
    replay_answer = run_agent("今天天气怎么样？", replay_llm, tools, tracer)
    print(f"回放答案: {replay_answer}")
    assert replay_answer == answer, "回放结果必须与原结果一致"
    assert len({s.ctx.trace_id for s in tracer.spans}) == 1
    print("[OK] 回放一致性校验通过\n")
    tracer.print_tree()
    tracer.summarize()

    print("\n== MQ 跨进程传递演示 ==")
    demo_mq_propagation(tracer)

    print("== 生成的文件 ==")
    for f in (jsonl, record_path):
        print(f"  {f}  ({f.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
