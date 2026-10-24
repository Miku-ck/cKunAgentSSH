"""
长任务结构化（task-structuring）— 发送前的**可选旁路**，纯推理护栏层

spec 2026-10-24-task-structuring · 接缝 S1（后端护栏）

产品定位：只在用户敲了一段较长的模糊运维需求时，帮其拆成明确步骤并补全
目标主机 / 影响范围 / 回滚方案等字段。**绝不静默替换原始指令**：结构化结果
回到前端后由用户显式采用，原文留作回退基准与审计证据。

为什么护栏是纯函数（本模块不含 LLM / DB / 网络）：
- 审批 / allowlist / prod 判定作用在「模型生成的实际命令 + 主机推出的环境」上，
  非 task 文本 —— 结构化碰不到闸门本身，只碰「意图边界」与「目标主机」两点。
- 唯一能翻转 test/prod 审批结论的路径是**目标主机被臆测**，故主机字段必须
  硬锁到当前 active target，LLM 返回的任何主机值一律丢弃。这是 fail-closed
  安全底座，必须脱离真实模型、可被守卫测试逐条钉死。

provenance 四态（前端 ProvenanceTag 与后端穷举同一份）：
  verbatim  原文   —— 直接来自用户措辞
  inferred  推断   —— 只读 / 观测类步骤，风险低
  completed 补全   —— 用户漏写、本该补的约束（回滚 / 影响范围等）
  added     新增·需确认 —— 用户没明确提过的写动作，采用前须逐条勾选
"""
from __future__ import annotations

import re
from typing import Any, Literal, Optional

Provenance = Literal["verbatim", "inferred", "completed", "added"]
OpKind = Literal["read", "write"]

# provenance / op_kind 的合法域 —— 与前端 task-structure-core.ts 同值，改一处须同步。
_VALID_PROVENANCE = ("verbatim", "inferred", "completed", "added")
_VALID_OP_KIND = ("read", "write")

# 结构化端点单轮输出上限：拆解一段需求不需要大输出，收紧防被灌长文拖慢首屏。
_MAX_STRUCTURE_TOKENS = 2048

# 主机字面量探测：ssh user@host 形式 与 IPv4。命中的"主机部分"若不在允许集合，
# 即视为模型臆测/引入的外机 —— 宁可标需确认也不静默放行。
_SSH_TARGET_RE = re.compile(r"\b([\w.-]+)@([\w.-]+)\b")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

_STEP_OP_HINTS = {
    "read": ("查", "看", "诊断", "检查", "观测", "采集", "列出", "定位", "grep", "status"),
    "write": ("重启", "删除", "清理", "修改", "部署", "扩容", "回滚", "停", "start",
              "restart", "reload", "kill", "rm", "systemctl", "apply", "变更", "迁移"),
}


def _norm_host(token: str) -> str:
    return (token or "").strip().lower()


def _allowed_host_set(active_host: str, extra_hosts: Optional[list[str]]) -> set[str]:
    allowed = {_norm_host(active_host)} if active_host else set()
    for h in extra_hosts or []:
        n = _norm_host(h)
        if n:
            allowed.add(n)
    return allowed


def _find_foreign_host_tokens(text: str, allowed: set[str]) -> list[str]:
    """返回 text 里出现的、不在允许集合内的主机字面量（用于把范围蔓延显式化）。

    只认 user@host 与裸 IPv4；host 部分比对允许集合。允许集合为空时，任何
    探测到的主机字面量都算外机（宁缺毋滥）。
    """
    if not text:
        return []
    foreign: list[str] = []
    for _user, host in _SSH_TARGET_RE.findall(text):
        if _norm_host(host) not in allowed:
            foreign.append(host)
    for ip in _IPV4_RE.findall(text):
        if _norm_host(ip) not in allowed:
            foreign.append(ip)
    return foreign


def _norm_provenance(value: Any, default: Provenance) -> Provenance:
    v = (value or "").strip().lower() if isinstance(value, str) else ""
    return v if v in _VALID_PROVENANCE else default  # type: ignore[return-value]


def _infer_op_kind(text: str, declared: Any) -> OpKind:
    """LLM 没给 / 给了非法 op_kind 时，按动词线索兜底判定；仍偏保守。"""
    v = (declared or "").strip().lower() if isinstance(declared, str) else ""
    if v in _VALID_OP_KIND:
        return v  # type: ignore[return-value]
    low = (text or "").lower()
    if any(k in low for k in _STEP_OP_HINTS["write"]):
        return "write"
    if any(k in (text or "") for k in _STEP_OP_HINTS["read"]):
        return "read"
    return "write"


def coerce_structure_payload(
    llm_payload: dict,
    *,
    active_host: str,
    environment: str,
    posture: Optional[str],
    allowed_hosts: Optional[list[str]] = None,
) -> dict:
    """把 LLM 原始返回收敛成受护栏约束的结构化结构（纯函数，可脱离模型单测）。

    护栏（对应 spec §1.3）：
    1. 主机字段无条件锁死 active_host，丢弃模型返回的任何主机值 → 翻转 prod 的路径被封。
    2. write + (inferred|completed) 的步骤一律收敛为 added + requires_confirm → 范围蔓延显式化。
    3. posture=='plan' 时剔除所有 write 步骤（对齐 resolveRunModes 的 plan=只读）。
    4. 步骤 / 字段文本里出现允许集合外的主机字面量 → 该步骤标 added + 需确认（不静默放行外机）。
    """
    allowed = _allowed_host_set(active_host, allowed_hosts)
    raw_steps = llm_payload.get("steps") or [] if isinstance(llm_payload, dict) else []

    steps: list[dict] = []
    for idx, s in enumerate(raw_steps):
        if not isinstance(s, dict):
            continue
        text = str(s.get("text") or "").strip()
        if not text:
            continue
        op = _infer_op_kind(text, s.get("op_kind"))
        # 只读步骤默认 inferred，写步骤默认 added（保守起步）
        prov_default: Provenance = "inferred" if op == "read" else "added"
        prov = _norm_provenance(s.get("provenance"), prov_default)

        # 护栏 2：写 + 非 verbatim/added → 收敛为 added
        if op == "write" and prov in ("inferred", "completed"):
            prov = "added"

        # 护栏 4：出现外机字面量 → 无论声明如何都升级为需确认（仅写步骤；
        # 只读步骤指向别处只是观测，不该被当成动作逼确认）
        if op == "write" and _find_foreign_host_tokens(text, allowed):
            prov = "added"

        requires_confirm = op == "write" and prov == "added"

        steps.append({
            "id": str(s.get("id") or f"step-{idx + 1}"),
            "text": text,
            "op_kind": op,
            "provenance": prov,
            "requires_confirm": requires_confirm,
        })

    # 护栏 3：plan 姿态只留只读步骤
    if posture == "plan":
        steps = [s for s in steps if s["op_kind"] == "read"]

    has_added_write_steps = any(s["requires_confirm"] for s in steps)

    fields = _coerce_fields(
        (llm_payload.get("fields") or {}) if isinstance(llm_payload, dict) else {},
        active_host=active_host,
        environment=environment,
        allowed=allowed,
    )

    ambiguities = _coerce_ambiguities(
        (llm_payload.get("ambiguities") or []) if isinstance(llm_payload, dict) else [],
    )

    structured_text = assemble_structured_text(
        steps=steps, fields=fields, environment=environment, ambiguities=ambiguities,
    )

    return {
        "steps": steps,
        "fields": fields,
        "ambiguities": ambiguities,
        "structured_text": structured_text,
        "has_added_write_steps": has_added_write_steps,
    }


def _coerce_fields(raw_fields: dict, *, active_host: str, environment: str,
                   allowed: set[str]) -> dict:
    """运维字段：host 硬锁 verbatim；其余按 {value,provenance,editable} 归一。"""
    if not isinstance(raw_fields, dict):
        raw_fields = {}

    # 护栏 1：主机字段永远 = active_host，模型给什么都丢弃
    host_slot = {"value": active_host or "", "provenance": "verbatim", "editable": False}

    def _slot(key: str, default_prov: Provenance = "completed") -> dict:
        node = raw_fields.get(key)
        value = ""
        prov = default_prov
        if isinstance(node, dict):
            value = str(node.get("value") or "").strip()
            prov = _norm_provenance(node.get("provenance"), default_prov)
        elif isinstance(node, str):
            value = node.strip()
        # 字段文本里出现外机 → 补全字段升级为需确认的 added，逼用户核对作用对象
        if _find_foreign_host_tokens(value, allowed):
            prov = "added"
        return {"value": value, "provenance": prov, "editable": True}

    return {
        "host": host_slot,
        "environment": environment,
        "impact_scope": _slot("impact_scope"),
        "rollback": _slot("rollback"),
        "pre_checks": _slot("pre_checks"),
        "change_window": _slot("change_window"),
    }


def _coerce_ambiguities(raw_amb: list) -> list:
    out: list[dict] = []
    if not isinstance(raw_amb, list):
        return out
    for i, a in enumerate(raw_amb):
        if not isinstance(a, dict):
            continue
        question = str(a.get("question") or "").strip()
        if not question:
            continue
        options = a.get("options") or []
        if not isinstance(options, list):
            options = []
        out.append({
            "id": str(a.get("id") or f"amb-{i + 1}"),
            "question": question,
            "why": str(a.get("why") or "").strip(),
            "options": [str(o) for o in options],
            "field_ref": a.get("field_ref") if isinstance(a.get("field_ref"), str) else None,
        })
    return out


def assemble_structured_text(*, steps: list[dict], fields: dict, environment: str,
                             ambiguities: list[dict]) -> str:
    """把收敛后的结构装配成写回输入框的纯文本（发送时即 AgentRunRequest.task）。"""
    lines: list[str] = []
    host = (fields.get("host") or {}).get("value", "") if isinstance(fields.get("host"), dict) else ""

    lines.append("## 目标")
    if host:
        lines.append(f"- 主机：{host}")
    lines.append(f"- 环境：{environment}")

    if steps:
        lines.append("")
        lines.append("## 步骤")
        for i, s in enumerate(steps, 1):
            tag = "写·需确认" if s["requires_confirm"] else ("读" if s["op_kind"] == "read" else "写")
            lines.append(f"{i}. [{tag}] {s['text']}")

    def _emit(label: str, key: str) -> None:
        node = fields.get(key)
        val = node.get("value", "") if isinstance(node, dict) else ""
        if val:
            lines.append("")
            lines.append(f"## {label}")
            lines.append(val)

    _emit("影响范围", "impact_scope")
    _emit("回滚方案", "rollback")
    _emit("前置检查", "pre_checks")
    _emit("变更窗口", "change_window")

    if ambiguities:
        lines.append("")
        lines.append("## 待澄清")
        for a in ambiguities:
            lines.append(f"- {a['question']}")

    return "\n".join(lines)


def build_structure_system_prompt(active_host: str, environment: str) -> str:
    """结构化提示词：约束模型只产出可核验的 JSON，且**绝不得自行指定目标主机**。

    与护栏互为两道：提示词是第一道软约束，coerce_structure_payload 是第二道硬约束
    （模型不听话也照样锁死）。
    """
    return (
        "你是运维任务的「结构化助手」。用户在任务台敲了一段较长的模糊需求，你的唯一职责"
        "是把它拆解为清晰、可执行的运维结构，并**只做推理、不执行任何操作、不臆测主机**。\n\n"
        f"当前已选定的目标主机是：{active_host or '（未选择远程目标）'}，环境：{environment}。\n"
        "【铁律】目标主机已固定，你**绝不得**在输出里指定、更换或新增任何主机地址；"
        "所有步骤都只针对上述这台主机。若用户文本里提到别的主机，把它写进 ambiguities 让用户确认，"
        "不要自行纳入。\n\n"
        "只输出一个 JSON 对象，不要任何解释文字、不要 markdown 代码围栏，形如：\n"
        "{\n"
        '  "steps": [\n'
        '    {"id": "step-1", "text": "看磁盘占用与 nginx 状态", "op_kind": "read", "provenance": "inferred"}\n'
        "  ],\n"
        '  "fields": {\n'
        '    "impact_scope": {"value": "仅 web-01 上的 nginx 服务", "provenance": "completed"},\n'
        '    "rollback": {"value": "改动前 cp 配置文件，异常则覆盖回滚并 reload", "provenance": "completed"},\n'
        '    "pre_checks": {"value": "确认备份盘空间充足", "provenance": "completed"},\n'
        '    "change_window": {"value": "低峰期", "provenance": "completed"}\n'
        "  },\n"
        '  "ambiguities": [\n'
        '    {"id": "amb-1", "question": "要顺带清理日志还是只查不改？", "why": "决定是否产生写操作", "options": ["只诊断", "诊断+清理"]}\n'
        "  ]\n"
        "}\n\n"
        "字段约定：\n"
        "- op_kind: 'read'（只读观测/诊断）或 'write'（改变系统状态的写动作）。拿不准填 'write'。\n"
        "- provenance: 'verbatim' 直接来自用户原话 / 'inferred' 你据原文推断的只读步骤 / "
        "'completed' 用户漏写但本该补的约束字段 / 'added' 用户没明确提过、需其确认的新增动作。\n"
        "- 任何写动作只要不是用户明确要求的，provenance 必须填 'added'（界面会高亮逼用户勾选）。\n"
        "- 简洁：步骤与字段都用短句，别堆解释。"
    )
