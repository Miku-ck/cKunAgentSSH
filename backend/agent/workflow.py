"""
Workflow 三阶段引导 — Spec / Plan / Goal

复刻主流 agent 工具（Cursor / Claude Code 的 mode 概念）的阶段式引导：
Spec、Plan、Goal 不是 runbook 步骤自动机，而是 agent 的三个工作阶段，
阶段切换只改变 agent 的行为姿态与注入到 system prompt 的内容侧重点，
产物一律是 markdown 文档（spec.md / plan.md / goal.md），跨 agent_run /
跨会话延续靠文件落盘，不依赖数据库。

阶段语义（与主流理解对齐）：
  spec  → 只读理解需求，产出规格文档，等用户确认再做后续
  plan  → 把规格拆成有序步骤清单，用户批准后才动手（步骤可配合 todo_write）
  goal  → 把"这次任务达成什么 / 怎么算成功"写成执行锚，贯穿 ReAct 循环

实现方式：纯文件，无 DB。每个会话一个工件目录
  <base_dir>/{conversation_id}/spec.md|plan.md|goal.md
「当前阶段」由已存在的工件推导（spec→plan→goal 渐进，取最高阶）。
写入与读取都是宽松 markdown：文件即内容，不解析内部结构，不做验收机。

边界：
- 读不到会话（conversation_id 为空）或目录/文件缺失一律返回空/None，绝不抛。
- 不引入第三方依赖，用标准库 Path 读写（幂等、无副作用）。
- hydrate_for_session 返回注入 system prompt 的文本；无任何工件时返回空串，
  调用方据此跳过注入。
"""
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 缺省 base_dir：与 ~/.ckunagentic 用户目录惯例对齐（跨 cwd 稳定）
_DEFAULT_BASE_DIR = Path.home() / ".ckunagentic" / "workflow"

# 三个工件文件名，按阶段推进顺序排列
_SPEC_FILE = "spec.md"
_PLAN_FILE = "plan.md"
_GOAL_FILE = "goal.md"

# 阶段 → 注入段标题（调用方与测试据此断言注入是否发生）
_INJECT_HEADS = {
    "spec": "[规格]",
    "plan": "[计划]",
    "goal": "[目标]",
}


def _checked_path(base: str | Path, conversation_id: str, name: str) -> Optional[Path]:
    """审查R2修复：读写侧与删除入口（_checked_artifact_path）同一 containment 判据。

    原来只有删除做 resolve+is_relative_to，读/写侧直接拼接：conversation_id 里
    带 ".." 时读能越目录看到他人会话工件、写能把文件落到 base 之外。
    非法入参返回 None，调用方按"工件不存在"处理（保持本模块"绝不抛"契约）。
    """
    if not conversation_id:
        return None
    try:
        base_root = Path(base).resolve()
        path = (base_root / conversation_id / name).resolve()
    except (OSError, ValueError):
        # 非法路径字符（如 Windows 保留名、嵌入 \0）：视作非法入参，不抛
        return None
    if not path.is_relative_to(base_root):
        return None
    return path


def _read_artifact(path: Path) -> str:
    """读取工件文件；缺失/异常返回空串（不抛）。"""
    try:
        text = path.read_text(encoding="utf-8")
        return text.strip()
    except OSError:
        return ""


def _write_artifact(path: Path, content: str) -> str:
    """写工件文件；父目录自动建。返回写入内容（去首尾空白）或空串失败。

    原子写（tmp+os.replace，CONTEXT.md「半写损坏」口径）：撕裂的 spec/plan/goal
    会被 get_phase 当成有效阶段文件读回，阶段状态就静默漂移了。tmp 名带 pid，
    与 builtin_skills._persist_managed_slugs 同一写法。
    """
    content = (content or "").strip()
    if not content:
        return ""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("[workflow] 写入失败 %s: %s", path, e)
        return ""
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    return content


# ── 阶段推导 ────────────────────────────────────────────────────────────────


def get_phase(conversation_id: str, base_dir: str | Path | None = None) -> str:
    """当前会话的阶段：''(无) / spec / plan / goal。

    取已存在工作的最高阶（goal 存在则视为 goal 阶段，往里回退）。
    """
    if not conversation_id:
        return ""
    base = base_dir if base_dir is not None else _DEFAULT_BASE_DIR
    # 审查R2修复：阶段推导也走 containment 校验，与删除/读写入口同判据
    goal = _checked_path(base, conversation_id, _GOAL_FILE)
    if goal is not None and goal.is_file():
        return "goal"
    plan = _checked_path(base, conversation_id, _PLAN_FILE)
    if plan is not None and plan.is_file():
        return "plan"
    spec = _checked_path(base, conversation_id, _SPEC_FILE)
    if spec is not None and spec.is_file():
        return "spec"
    # 目录存在但没有任何已知工件不判别阶段，返回空
    return ""


# ── 写工件 ──────────────────────────────────────────────────────────────────


def _write_checked(base: str | Path, conversation_id: str, name: str, content: str) -> str:
    """审查R2修复：写侧统一入口，先做 containment 校验再落盘（非法入参返回空，不抛）。"""
    path = _checked_path(base, conversation_id, name)
    if path is None:
        logger.warning("[workflow] 拒绝非法写入 cid=%r name=%r", conversation_id, name)
        return ""
    return _write_artifact(path, content)


def write_spec(conversation_id: str, content: str,
               base_dir: str | Path | None = None) -> str:
    """写规格文档。返回写入内容；会话为空/内容空/入参非法返回空串。"""
    if not conversation_id:
        return ""
    base = base_dir if base_dir is not None else _DEFAULT_BASE_DIR
    return _write_checked(base, conversation_id, _SPEC_FILE, content)


def write_plan(conversation_id: str, content: str,
               base_dir: str | Path | None = None) -> str:
    """写计划文档。返回写入内容；会话为空/内容空/入参非法返回空串。"""
    if not conversation_id:
        return ""
    base = base_dir if base_dir is not None else _DEFAULT_BASE_DIR
    return _write_checked(base, conversation_id, _PLAN_FILE, content)


def write_goal(conversation_id: str, content: str,
               base_dir: str | Path | None = None) -> str:
    """写目标（成功标准）文档。返回写入内容；会话为空/内容空/入参非法返回空串。"""
    if not conversation_id:
        return ""
    base = base_dir if base_dir is not None else _DEFAULT_BASE_DIR
    return _write_checked(base, conversation_id, _GOAL_FILE, content)


# ── 删工件（仅用户显式动作） ────────────────────────────────────────────────

_KIND_FILES = {
    "spec": _SPEC_FILE,
    "plan": _PLAN_FILE,
    "goal": _GOAL_FILE,
}


def _checked_artifact_path(base: str | Path, conversation_id: str, kind: str) -> Optional[Path]:
    """入参白名单化后的工件路径；任一非法返回 None。

    这是本模块唯一的删除入口，误放的代价（删掉他人会话工件、或 base_dir 之外的
    任意文件）远大于误拦，所以不信任拼接：解析符号链接后要求路径仍落在 base 内。
    用「是否落在 base 内」而非「id 是否像 32 位 hex」做判据 —— 后者会在 id 生成
    规则变化时静默把删除变成空操作（跨层约定漂移）。
    审查R2修复：判据抽到 _checked_path，读/写侧与本删除侧共用同一实现。
    """
    if kind not in _KIND_FILES:
        return None
    return _checked_path(base, conversation_id, _KIND_FILES[kind])


def delete_artifact(conversation_id: str, kind: str,
                    base_dir: str | Path | None = None) -> bool:
    """删除某个工件。返回是否真的删掉了一个文件（不存在/入参非法返回 False，不抛）。

    只由用户显式动作调用（前端「目标」/「计划」chip 的 ×）。刻意不提供对应的模型工具：
    否则「计划批准门」有了逃逸口 —— 模型 plan_write 一个空内容就能把门关掉。
    """
    base = base_dir if base_dir is not None else _DEFAULT_BASE_DIR
    path = _checked_artifact_path(base, conversation_id, kind)
    if path is None:
        logger.warning("[workflow] 拒绝删除非法工件 kind=%r cid=%r", kind, conversation_id)
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.warning("[workflow] 删除工件失败 %s: %s", path, e)
        return False
    return True


# ── 读取 / 注入 ─────────────────────────────────────────────────────────────


def read_artifacts(conversation_id: str,
                   base_dir: str | Path | None = None) -> dict[str, str]:
    """读取会话三个工件原文。缺失字段返回空串；入参非法（越界会话 id）也返回空串。"""
    base = base_dir if base_dir is not None else _DEFAULT_BASE_DIR

    def _one(name: str) -> str:
        # 审查R2修复：读侧走 containment 校验，带 ".." 的会话 id 不再能越目录读他人工件
        path = _checked_path(base, conversation_id, name)
        return _read_artifact(path) if path is not None else ""

    return {"spec": _one(_SPEC_FILE), "plan": _one(_PLAN_FILE), "goal": _one(_GOAL_FILE)}


def hydrate_for_session(conversation_id: str,
                        base_dir: str | Path | None = None) -> str:
    """渲染注入 system prompt 的段落，供 routes/agent.py 按会话注入。

    只注入当前阶段及其前更高阶的内容，让模型知道"现在处于哪个阶段、
    已经确定/等待确认什么"。无任何工件返回空串（调用方据此跳过注入）。

    不做验收判定 —— 阶段推进与成功判定交由模型 + 用户决定，本模块不越权。
    """
    if not conversation_id:
        return ""
    base = base_dir if base_dir is not None else _DEFAULT_BASE_DIR
    arts = read_artifacts(conversation_id, base)
    phase = get_phase(conversation_id, base)

    lines: list[str] = []
    # 目标（最高阶）永远注入 —— 它是执行锚
    if arts.get("goal"):
        lines.append(_INJECT_HEADS["goal"])
        lines.append("本次任务的成功标准（执行中以此为准）：")
        lines.append(arts["goal"])
        lines.append("")
    # 计划（次高阶）
    if phase in ("plan", "goal") and arts.get("plan"):
        lines.append(_INJECT_HEADS["plan"])
        lines.append("已确定的执行步骤（按此推进，配合任务列表进度）：")
        lines.append(arts["plan"])
        lines.append("")
    # 规格（最低阶）
    if arts.get("spec"):
        lines.append(_INJECT_HEADS["spec"])
        lines.append("需求规格：")
        lines.append(arts["spec"])
        lines.append("")

    if not lines:
        return ""
    lines.append("[工作流]")
    lines.append(
        "当前处于「%s」阶段。尚未产出明确成功标准前，不要擅自宣布任务完成。"
        % (phase if phase else "需求理解")
    )
    return "\n".join(lines)


def goal_self_check_prompt(goal_text: str) -> str:
    """构造「目标验收自检」提示，供 driver 在收尾判定前交给 agent。

    goal 是执行锚：agent 声称任务完成（不再调工具）时，driver 不直接收尾，
    而是先让它对照成功标准自检一轮 —— 明确给出「已达成 / 未达成」结论；
    未达成则 agent 应继续（重新规划或补调工具），达成才能收尾。

    无 goal 内容时返回空串（调用方据此跳过自检，保持原有收尾行为）。
    """
    goal_text = (goal_text or "").strip()
    if not goal_text:
        return ""
    return (
        "[目标验收] 你已经停止调用工具，准备宣布任务完成。宣布前请先对照本次任务的成功标准自检：\n"
        f"{goal_text}\n\n"
        "请明确回答：是否已**全部达成**上述成功标准？\n"
        "- 若**已全部达成**：简述哪几条标准已满足，然后正常给出最终总结。\n"
        "- 若**仍有未达成**：指明确哪些标准未满足，并在下一步继续（重新规划或补调工具），"
        "不要在此轮直接宣布完成；若受限于迭代次数确实无法全部达成，请如实说明差距。"
    )