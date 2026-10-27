"""S4-07 的跨端字段上限对账：渲染层裁剪值必须等于后端 AuditAppendRequest 的 Field 上限。

`src/utils/remote-audit.ts` 的头注释写着「与后端 AuditAppendRequest 的字段上限
逐字相等」，而这条相等到本轮之前**没有任何东西钉着** —— 后端把 command_raw 收紧到
比如 4096 而前端仍裁到 8192 时，超长的批量命令会被 pydantic 折成 422，
`writeRemoteAudit` 把它吞成一条 console.warn（它刻意不阻塞已下发的远端动作），
于是表现是「命令照常执行、审计页少一条记录」，而且只有翻审计页的人才看得见。

这正是战役 00-INDEX 登记的「同族防护漏一处」：审计结论清单有四份同源守卫，字段上限
这一族却只有文案承诺。本守卫按仓内既有做法从**模型元数据**取上限（不是再抄一份数字），
前端侧用正则切常量，两侧都断言非空 —— 空集会让断言因看不见而通过。
"""
import re
from pathlib import Path

import pytest
from pydantic.types import MaxLen

from routes.audit import AuditAppendRequest

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "src" / "utils" / "remote-audit.ts"

# 前端常量名 → 后端模型字段名
_PAIRS = {
    "AUDIT_COMMAND_MAX": "command_raw",
    "AUDIT_RESULT_MAX": "result",
    "AUDIT_TARGET_MAX": "target_id",
}


def _backend_max_length(field: str) -> int:
    """从 FieldInfo.metadata 取 MaxLen。

    pydantic 2.13 的 FieldInfo 没有 .max_length 属性，约束只活在 metadata 里；
    取不到就是上限被删了 —— 宁可炸掉，不要悄悄比出一个假绿灯。
    """
    for meta in AuditAppendRequest.model_fields[field].metadata:
        if isinstance(meta, MaxLen):
            return meta.max_length
    raise AssertionError(
        f"AuditAppendRequest.{field} 已无 MaxLen 约束：本守卫的比对前提失效，"
        "要么上限被删（前端裁剪随之失去意义），要么换成了别的校验形态，请一并改这里"
    )


def _frontend_constants() -> dict[str, int]:
    src = FRONTEND.read_text(encoding="utf-8")
    found = {
        name: int(m.group(1))
        for name in _PAIRS
        if (m := re.search(rf"export const {name} = (\d+)", src))
    }
    missing = set(_PAIRS) - set(found)
    if missing:
        raise AssertionError(
            f"{FRONTEND.relative_to(ROOT)} 里找不到 {sorted(missing)} —— "
            "常量被改名或删除时，本守卫必须炸而不是拿空集比空集"
        )
    return found


def test_frontend_clip_matches_backend_field_limit():
    frontend = _frontend_constants()
    for const_name, model_field in _PAIRS.items():
        limit = _backend_max_length(model_field)
        assert frontend[const_name] == limit, (
            f"{const_name}={frontend[const_name]} 与 "
            f"AuditAppendRequest.{model_field} 上限 {limit} 不一致：超限内容会被 "
            "折成 422，而渲染层只 warn，审计记录静默丢失"
        )


def test_both_sides_are_non_empty_so_the_guard_is_not_vacuous():
    # 战役 round 22 的教训：切不出东西的守卫会永远绿。这里显式钉住两侧都非空。
    assert len(_frontend_constants()) == len(_PAIRS)
    limits = [_backend_max_length(f) for f in _PAIRS.values()]
    assert len(limits) == len(_PAIRS) and all(limits)


def test_action_literals_fit_the_backend_action_limit():
    """渲染层只会发这两个 action；超了 128 或全是空白当场 422。"""
    src = FRONTEND.read_text(encoding="utf-8")
    actions = re.findall(r"export const AUDIT_ACTION_\w+ = '([^']*)'", src)
    assert actions, f"{FRONTEND.relative_to(ROOT)} 里没切到任何 AUDIT_ACTION_* 常量"
    field = AuditAppendRequest.model_fields["action"]
    upper = next(m.max_length for m in field.metadata if isinstance(m, MaxLen))
    for action in actions:
        assert 0 < len(action) <= upper, f"action {action!r} 不在 (0, {upper}] 内"


def test_target_type_literal_within_limit():
    """buildRemoteAuditRecord 硬编码 target_type='ssh'，同样受后端上限管。"""
    src = FRONTEND.read_text(encoding="utf-8")
    literals = set(re.findall(r"target_type: '([^']*)'", src))
    assert literals == {"ssh"}, f"预期的 target_type 单一来源不见了：{literals}"
    upper = next(
        m.max_length for m in AuditAppendRequest.model_fields["target_type"].metadata
        if isinstance(m, MaxLen)
    )
    assert all(0 <= len(v) <= upper for v in literals)


@pytest.mark.parametrize("field", sorted(_PAIRS.values()))
def test_no_clip_helper_reintroduces_a_magic_number(field: str):
    """裁剪只走 clipAuditText(value, AUDIT_*_MAX)；调用点抄数字就绕开了本守卫。

    守卫认得常量，认不出某个新写的 `slice(0, 4096)`。按仓内既有做法把这种漂移
    挡在源码层（见 test_settings_key_contract 的同类读法）。
    """
    const_name = next(k for k, v in _PAIRS.items() if v == field)
    src = FRONTEND.read_text(encoding="utf-8")
    body = src[src.index("export function buildRemoteAuditRecord"):]
    calls = re.findall(r"clipAuditText\([^,]+,\s*([^)]+?)\)", body)
    assert calls, f"buildRemoteAuditRecord 里没了 clipAuditText 调用，{const_name} 失去消费者"
    for arg in calls:
        assert arg.strip() == const_name or "AUDIT_" in arg, (
            f"clipAuditText 的第二个参数写成了 {arg!r}：内联数字不受跨端上限对账管辖"
        )
