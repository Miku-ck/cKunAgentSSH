"""跨层键契约（round 22）—— 前端 payload 键集合 ⇄ 后端 pydantic 请求模型字段。

本仓反复出现的缺陷族是「跨层键漂移」：两端各写一份字段清单，改名/增删时只动一边。
pydantic 默认**静默丢弃**未声明字段，于是漂移不报错、不 422，只是那一项永远写不进
去 —— 前面几轮的 reasoning_tokens、is_default_ssh 都是这么丢的。

两侧都不该靠人肉对齐，所以这里直接读源码比对：
- 前端：src/api/client.ts 里各 update/create 方法的 payload 类型（内联或具名接口）；
- 后端：routes/settings.py 里对应的请求模型 model_fields。

方向是**不对称**的两条规则，而不是一句「两边相等」：
1) 前端发的键必须都在后端模型里 —— 否则该键被静默丢弃（真事故）；
2) 后端的必填键必须都在前端里 —— 否则请求恒 422（真事故）。
后端多出来的可选键不算错：那是「前端这一版没暴露」，不是失配。

运行: python -m pytest backend/tests/test_settings_key_contract_round22.py -v
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLIENT_TS = ROOT / "src" / "api" / "client.ts"


# ── TS 侧抽取 ───────────────────────────────────────────────────────────────

def _balanced(text: str, start: int) -> str:
    """从 text[start]（必须是 '{'）取到配对'}'，支持嵌套。"""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("未闭合的 {}")


def _strip_nested(block: str) -> str:
    """把嵌套的 {...} 换成 {}，只留顶层成员分隔符 —— 否则内联类型里的嵌套键会被当成顶层键。"""
    out, i = [], 0
    while i < len(block):
        if block[i] == '{':
            depth, j = 0, i
            while j < len(block):
                if block[j] == '{':
                    depth += 1
                elif block[j] == '}':
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            out.append('{}')
            i = j + 1
        else:
            out.append(block[i])
            i += 1
    return ''.join(out)


def _keys_of_object(block: str) -> set[str]:
    """对象/接口体里的顶层成员名，覆盖两种排版：
    多行 `  name?: T`（换行分隔）与内联 `{ a: T; b: T }`（; 或 , 分隔）。
    只对**内层**做嵌套剥离：整块剥离会把最外层 {} 也折成 {}，内联类型就一个键都读不到。"""
    inner = block[block.index("{") + 1:block.rindex("}")]
    pattern = r"(?:^|[{;,]|\n)\s*(?:readonly\s+)?([A-Za-z_][\w]*)\??\s*:"
    return set(re.findall(pattern, _strip_nested(inner)))


def _resolve_interface(name: str, src: str) -> set[str]:
    m = re.search(rf"export interface {name}\s*\{{", src)
    if not m:
        raise AssertionError(f"client.ts 里找不到接口 {name}")
    return _keys_of_object(_balanced(src, m.end() - 1))


def _payload_keys(method: str, src: str) -> set[str]:
    """取 `method: (…) => …` 里 payload 参数的键集合。

    两种写法都支持：内联对象字面量类型 `payload: { a: T; b: T }`，
    以及具名类型 `data: MemorySettings` / `payload: ModelConfigPayload`。
    """
    m = re.search(rf"\n\s*{method}\s*[:(]", src)
    if not m:
        raise AssertionError(f"client.ts 里找不到方法 {method}")
    head = src[m.start():m.start() + 1200]
    pm = re.search(r"\b(?:payload|data|req)\s*:\s*", head)
    if not pm:
        raise AssertionError(f"{method} 的签名里找不到 payload 参数")
    rest = head[pm.end():]
    # 泛型包装要先剥掉：`Partial<{ a: T; b: T }>` 是「部分更新 PUT」的写法
    # （不传的键 = 不改），不剥的话下面那个可选组会把 "Partial" 本身当成接口名去
    # client.ts 里找，报出「找不到接口 Partial」——契约本身没坏，是读法跟不上写法。
    generic = re.match(r"\s*(?:Partial|Omit|Pick|Readonly)\s*<\s*", rest)
    if generic:
        rest = rest[generic.end():]
    if rest.lstrip().startswith("{"):
        return _keys_of_object(_balanced(rest, rest.index("{")))
    tm = re.match(r"\s*(?:Partial<)?([A-Za-z_][\w]*)>?", rest)
    if not tm:
        raise AssertionError(f"{method} 的 payload 类型无法解析: {rest[:60]!r}")
    return _resolve_interface(tm.group(1), src)


# ── 对照表：前端方法 → 后端请求模型 ─────────────────────────────────────────

CONTRACTS = [
    ("updateMemorySettings", "MemorySettingsRequest"),
    ("updatePostProcessSettings", "PostProcessSettingsRequest"),
    ("updateConvRetention", "ConversationRetentionRequest"),
    ("updateHttpSettings", "HttpSettingsRequest"),
    ("updateWebToolsSettings", "WebToolsSettingsRequest"),
    ("updatePermissionPolicy", "PermissionPolicyRequest"),
    ("createModelConfig", "CreateModelRequest"),
    ("updateModelConfig", "UpdateModelRequest"),
]


@pytest.fixture(scope="module")
def client_src():
    return CLIENT_TS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def models():
    import sys
    sys.path.insert(0, str(ROOT / "backend"))
    from routes import settings
    return settings


@pytest.mark.parametrize("method,model", CONTRACTS)
def test_frontend_keys_are_all_accepted_by_backend(method, model, client_src, models):
    sent = _payload_keys(method, client_src)
    accepted = set(models.__getattribute__(model).model_fields)
    drifted = sent - accepted
    assert not drifted, (
        f"{method} 发的键后端不认（pydantic 静默丢弃，该项永远写不进去）: "
        f"{sorted(drifted)}；后端字段={sorted(accepted)}"
    )


@pytest.mark.parametrize("method,model", CONTRACTS)
def test_backend_required_keys_are_all_sent(method, model, client_src, models):
    fields = models.__getattribute__(model).model_fields
    required = {k for k, f in fields.items() if f.is_required()}
    sent = _payload_keys(method, client_src)
    missing = required - sent
    assert not missing, f"{method} 少发后端必填键，请求恒 422: {sorted(missing)}"


# ── 默认档位枚举（同一词在两语言各写一份，最容易只改一边）───────────────────

def test_model_default_mode_enum_matches_backend(client_src, models):
    # 只吃一行：`[^;]+` 会跨行吞到下一个分号，把无关的字面量都算进枚举
    m = re.search(r"export type ModelDefaultMode\s*=\s*([^\n]+)", client_src)
    assert m, "client.ts 里找不到 ModelDefaultMode"
    ts = set(re.findall(r"'(\w+)'", m.group(1)))
    py = set(models.DefaultMode.__args__)
    assert ts == py, f"档位枚举漂移：前端 {sorted(ts)} ⇄ 后端 {sorted(py)}"


def test_memory_settings_interface_matches_request_shape(client_src, models):
    """读侧同样对齐：GET /settings/memory 的回读类型与写入模型字段同名同集合，
    否则面板上的开关与后端库里的键会各说各话。"""
    iface = _resolve_interface("MemorySettings", client_src)
    req = set(models.MemorySettingsRequest.model_fields)
    assert iface == req, f"MemorySettings ⇄ MemorySettingsRequest 集合不等: {iface ^ req}"


# ── 自检：证明这套比对真的能抓到改名 ─────────────────────────────────────────

@pytest.mark.parametrize("old,new", [
    ("max_items", "limitItems"),
    ("inject_budget_tokens", "injectBudget"),
])
def test_guard_actually_catches_renamed_key(old, new, models):
    """自检：把前端接口里的一个键改名，比对必须判为漂移。

    没有这条，抽取逻辑若写错（读不到键、或键集恒等于后端），上面两条规则会
    一路"通过"，成为摆设 —— 跨层契约测试最常见的失效方式。
    """
    src = CLIENT_TS.read_text(encoding="utf-8")
    iface = _balanced(src, src.index("{", re.search(r"export interface MemorySettings\b", src).end()))
    mutated_keys = _keys_of_object(iface.replace(f"{old}:", f"{new}:"))
    accepted = set(models.MemorySettingsRequest.model_fields)
    assert old not in mutated_keys, f"改名没命中接口体：{old}"
    assert mutated_keys - accepted == {new}, (
        f"改名后的键 {new!r} 没被判为漂移 —— 比对逻辑抓不到跨层改名，这套契约测试是摆设")
